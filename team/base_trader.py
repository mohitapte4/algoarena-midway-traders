"""
Midway Traders — shared plumbing for the strategy traders.

A strategy subclasses BaseTrader and implements `_signal()`. Everything else
is common to every seat, so each strategy gets the same protections.

Every tick, in order:
  1. Nothing is sent during the pre-open auction, after the reject budget is
     spent, or before the exchange has sent the seat's portfolio (it does so
     after the first fill) — except the starter order and event reactions.
  2. Event plans (team.events): when a CALENDAR_EVENT or SHOCK fires, ride
     good news / fade bad news. These outrank everything else.
     Every event also gets a reaction, because the session checks that each
     seat sends an order or cancel within 60 s of every event: the seat's
     other orders in the name are cancelled and, unless a plan or the
     strategy trades the name first, a 5-share bid rests at the settle level
     once the ramp is over.
  3. Exits: a stop-loss sells shares bought below their average entry.
  4. A starter order (5 shares) is placed until one fill is confirmed, so the
     session always has at least one fill. It is re-sent if not filled.
  5. The strategy's `_signal()`, with every buy checked against team.risk
     limits. Symbols with a scheduled event coming up are left alone.

How orders are priced. The seat is a maker by default: strategy entries and
exits rest at the touch (bid for buys, ask for sells) and earn the maker
rebate instead of paying the taker fee. Only the races take liquidity: the
event ride entry and fade sale, the stop-loss and the starter.

Every order passes one gate (`_place`), built so the venue never has reason
to refuse it — the seat must stay under 10 rejected orders a session:
  * sells never exceed the shares held minus the shares already offered
    (long-only: an uncovered sell is rejected);
  * buys stay inside this seat's share cap and its cash, fee included;
  * at most one order per symbol and side; an order that would cross the
    seat's own resting order waits for that order to be cancelled first
    (self-trade prevention cancels it and counts a reject);
  * prices stay inside the venue's LULD band;
  * orders and cancels are paced well inside the team's message quota.
Rejects that still happen pause the symbol, and at REJECT_BUDGET the seat
stops sending orders for the rest of the session.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from arena import Signal, Trader      # first: puts the engine on sys.path
import trader.config as _trader_config  # noqa: E402  (engine module)
from team.events import (
    DEFAULT_OVERSHOOT, DEFAULT_RAMP_TICKS, EXCHANGE_TICK_SEC, FADE, RIDE,
    EventPlan, designed_edge, size_fraction,
)
from team.risk import EventLimits, RiskLimits
from team.venue import Order, OwnOrders, TokenBucket

logger = logging.getLogger(__name__)

# The engine logs order acknowledgements at DEBUG; enable that level for its
# trader logger only so each order_ack appears in the session log.
logging.getLogger("trader.trader").setLevel(logging.DEBUG)

STARTER_QUANTITY = 5
STARTER_RETRY_SEC = 30.0  # re-send the starter if it has not filled by then
STARTER_MAX_ATTEMPTS = 3

DEFAULT_TAKER = 0.003     # hosted venue, until FEE_SCHEDULE says otherwise
DEFAULT_REBATE = 0.0
EDGE_MARGIN = 1.25        # a signal must clear its cost by this factor

SEAT_MAX_SHARES = 200     # per symbol: held + on order. The team limit (600
                          # in week 3) counts both seats' orders and the
                          # broker's inventory and bids (team.broker
                          # MAX_INVENTORY = 150), so 2 × 200 + 150 stays under.
PRICE_BAND = 0.09         # venue rejects limits beyond ±10% of the mark
ORDERS_PER_SEC = 4.0      # orders + cancels; the team shares 20 per tick
QUOTE_DRIFT = 0.001       # re-quote once the touch is 10 bp away
TICK_SIZE = 0.01

REJECT_BUDGET = 8         # stop sending at this many rejects (limit is 10)
REJECT_COOLDOWN_SEC = 10.0
RATE_LIMIT_PAUSE_SEC = 1.0
LEDGER_SETTLE_SEC = 1.0   # trust the venue's positions this long after a fill
WINDOW_MARGIN_SEC = 10.0  # event window extends this far past the deadline
REACTION_EXPIRY_SEC = 45.0  # inside the session check's 60-tick window


@dataclass(frozen=True)
class Reaction:
    """The order one fired event is owed if nothing else trades the name."""

    symbol: str
    t0: float                 # when the event arrived (bot clock)
    start: float              # bid once the ramp is over
    settle: float             # pre × (1 + pct): the bid pays no more
    expires: float


class BaseTrader(Trader):
    """Maker quotes, event plans, starter order, stop-loss and risk limits."""

    # Which event legs this seat trades (team.events.RIDE / FADE).
    EVENT_MODES: frozenset[str] = frozenset()

    def __init__(self, limits: RiskLimits | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 event_limits: EventLimits | None = None) -> None:
        super().__init__()
        self.limits = limits or RiskLimits()
        self.event_limits = event_limits or EventLimits()
        self._clock = clock
        self._preopen = False
        self._start_net_worth: float | None = None
        self._halt_logged = False
        self._starter_filled = False
        self._starter_attempts = 0
        self._starter_sent_at: float | None = None
        # shares this bot bought and still holds, and what they cost
        self._bought: dict[str, int] = {}
        self._cost: dict[str, float] = {}
        self._fills = 0
        self._last_note: dict[str, str] = {}

        # orders on the book and the venue's view of them
        self.orders = OwnOrders()
        self._bucket = TokenBucket(ORDERS_PER_SEC, ORDERS_PER_SEC)
        self._cancels: list[Order] = []
        self.rejects: Counter[str] = Counter()
        self._stopped = False
        self._paused_until = 0.0
        self._cooldown: dict[str, float] = {}
        # positions from our own fills, to cover the lag before the venue's
        # portfolio update arrives
        self._ledger: dict[str, int] = {}
        self._seen_positions: dict[str, int] | None = None
        self._last_fill_at = -math.inf

        # fees, from FEE_SCHEDULE
        self.taker = DEFAULT_TAKER
        self.rebate = DEFAULT_REBATE

        # events
        self._last_mid: dict[str, float] = {}
        self._session_tick: int | None = None   # exchange tick at the open
        self._windows: dict[str, tuple[float, float]] = {}   # "*" = all
        self._new_events: list[dict] = []
        self._plans: dict[str, EventPlan] = {}
        self._reactions: dict[str, Reaction] = {}
        self._last_order_at: dict[str, float] = {}
        self._last_ramp = (DEFAULT_RAMP_TICKS, DEFAULT_OVERSHOOT)
        self.finished_plans: list[EventPlan] = []

        # this tick's market and portfolio, so helpers need not pass them
        self._market = None
        self._portfolio = None

    # ── required hook ───────────────────────────────────────────────────

    def on_tick(self, market, portfolio) -> Signal | None:
        """Return at most one Signal per tick, or None."""
        if self._preopen or self._stopped:
            return None
        now = self._clock()
        self._market, self._portfolio = market, portfolio
        self.orders.expire(now)
        self._sync_ledger(portfolio, now)

        net_worth = portfolio.net_worth(market)
        if self._start_net_worth is None:
            # The exchange sends this seat its portfolio only after its first
            # fill; until then the local Portfolio reports a placeholder that
            # is lower than the real net worth. Only the starter and event
            # reactions may trade on it (sized against the placeholder, so the
            # limits are stricter); the first fill brings the real numbers,
            # and the loss limit is anchored to those.
            if not self._has_server_state(portfolio, net_worth):
                self._record_mids(market)
                signal = None
                if not self._starter_filled:
                    signal = self._starter(market, portfolio, net_worth, now, False)
                return signal or self._reaction_signal(market, now, False)
            self._start_net_worth = net_worth
            logger.info("RISK   session reference net worth %.2f", net_worth)
        halted = self.limits.halted(net_worth, self._start_net_worth)
        if halted and not self._halt_logged:
            self._halt_logged = True
            logger.warning("RISK   session loss limit reached (net worth %.2f vs "
                           "%.2f at the open): no new positions, exits only",
                           net_worth, self._start_net_worth)

        self._open_plans(net_worth, halted)
        self._record_mids(market)
        self._pull_quotes_in_windows(now)

        signal = self._event_signal(market, now, halted)
        if signal is None:
            signal = self._reaction_signal(market, now, halted)
        if signal is None:
            signal = self._stop_loss(market, portfolio, now)
        if signal is None and not self._starter_filled:
            signal = self._starter(market, portfolio, net_worth, now, halted)
        if signal is None:
            signal = self._signal(market, portfolio, net_worth, now, halted)
        return signal

    # ── optional hooks ──────────────────────────────────────────────────

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        """Record an execution against its order, the ledger and any plan."""
        now = self._clock()
        self._fills += 1
        self._starter_filled = True
        self._last_fill_at = now
        self._ledger[symbol] = self._ledger.get(symbol, 0) + (
            quantity if side == "buy" else -quantity)
        order = self.orders.on_fill(symbol, side, quantity, price, now)
        plan = self._plans.get(symbol)
        if plan is not None and order is not None and order.purpose.startswith(plan.mode):
            plan.record(side, quantity, price)

        if side == "buy" and order is not None and order.purpose.startswith("fade"):
            pass    # restoring inventory sold ahead of the drop, not a new position
        elif side == "buy":
            self._bought[symbol] = self._bought.get(symbol, 0) + quantity
            self._cost[symbol] = self._cost.get(symbol, 0.0) + quantity * price
        elif self._bought.get(symbol):
            sold = min(quantity, self._bought[symbol])
            avg = self._cost[symbol] / self._bought[symbol]
            self._bought[symbol] -= sold
            self._cost[symbol] -= sold * avg
            if self._bought[symbol] == 0:
                del self._bought[symbol], self._cost[symbol]

        # A limit order cannot execute at a worse price than its limit, so a
        # difference on the wrong side indicates an exchange-side error.
        tag, worse = "", False
        if order is not None:
            delta = price - order.price
            worse = delta > 0 if side == "buy" else delta < 0
            tag = f"  vs limit {delta:+.4f} ({order.purpose}, " \
                  f"{'taker' if order.taker else 'maker'})"
        held = self._bought.get(symbol, 0)
        avg_entry = self._cost[symbol] / held if held else 0.0
        (logger.warning if worse else logger.info)(
            "FILL   #%d %-4s %3d %-6s @ %8.2f  notional %9.2f%s  "
            "| bought and held %d, avg entry %.4f",
            self._fills, side, quantity, symbol, price, quantity * price, tag,
            held, avg_entry)

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Auction state, fees, the event calendar and shocks."""
        if event != "AUCTION_INDICATIVE":       # sent every pre-open tick
            logger.info("EVENT  %-14s %s", event, message)
        if event == "SESSION_PREOPEN":
            self._preopen = True
        elif event == "AUCTION_RESULT":
            self._preopen = False
        elif event == "FEE_SCHEDULE":
            self.taker = float(data.get("taker", self.taker))
            self.rebate = float(data.get("rebate", self.rebate))
        elif event == "CALENDAR":
            self._on_calendar(data)
        elif event == "CALENDAR_EVENT":
            ramp = int(data.get("ramp_ticks") or DEFAULT_RAMP_TICKS)
            overshoot = float(data.get("overshoot") or DEFAULT_OVERSHOOT)
            self._last_ramp = (ramp, overshoot)
            for symbol in data.get("affected") or (
                    [data["symbol"]] if data.get("symbol") else []):
                self._on_move(symbol, ramp, overshoot, message,
                              pct=float(data.get("pct") or 0.0))
        elif event == "SHOCK":
            # A teacher-injected move carries target prices, not a pct; the
            # venue ramps it like a calendar event (server.py, inject shock).
            ramp, overshoot = self._last_ramp
            prices = data.get("prices") or {}
            for symbol in data.get("affected") or list(prices):
                if symbol in prices:
                    self._on_move(symbol, ramp, overshoot, message,
                                  target=float(prices[symbol]))

    # ── venue feedback (called by team.venue.TeamBot) ───────────────────

    def on_connect(self) -> None:
        """A (re)connection: whatever we had on the book is unknown now."""
        self.orders.clear()
        self._cancels.clear()

    def on_ack(self, order_id: str, symbol: str, side: str, price: float) -> None:
        self.orders.on_ack(order_id, symbol, side, price)

    def on_reject(self, code: str, message: str) -> None:
        """Count a venue error, reconcile the order, and back off."""
        now = self._clock()
        self.rejects[code] += 1
        total = sum(self.rejects.values())
        order = self.orders.on_reject(code, message)
        symbol = order.symbol if order else None
        if symbol:
            self._cooldown[symbol] = now + REJECT_COOLDOWN_SEC
        if code == "RATE_LIMITED":
            self._paused_until = now + RATE_LIMIT_PAUSE_SEC
        if code == "POSITION_LIMIT" and symbol in self._plans:
            self._plans[symbol].stop_buys = True
        logger.warning("REJECT #%d %s %s: %s", total, code, symbol or "-", message)
        if total >= REJECT_BUDGET and not self._stopped:
            self._stopped = True
            logger.error("REJECT budget of %d reached: no more orders this "
                         "session", REJECT_BUDGET)

    def take_cancels(self) -> list[tuple[str, str]]:
        """Cancels to send now, as (order_id, symbol), within the rate limit."""
        now = self._clock()
        out = []
        while self._cancels and self._bucket.available(now):
            order = self._cancels.pop(0)
            if order not in list(self.orders) or order.order_id is None:
                continue
            self._bucket.take(now)
            order.cancel_sent_at = now
            out.append((order.order_id, order.symbol))
            logger.info("CANCEL %s %d %s @ %.2f (%s)", order.side, order.quantity,
                        order.symbol, order.price, order.purpose)
        return out

    def run(self) -> None:
        """Connect to the exchange and trade until interrupted."""
        from team.venue import TeamBot
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
        asyncio.run(TeamBot(self).run())

    # ── the strategy hook ───────────────────────────────────────────────

    def _signal(self, market, portfolio, net_worth: float, now: float,
                halted: bool) -> Signal | None:
        """The strategy. Return an order via `_quote()`, or None."""
        raise NotImplementedError

    # ── events ──────────────────────────────────────────────────────────

    def _on_calendar(self, data: dict) -> None:
        """The schedule (at the open) and the ~2-minute warnings."""
        tick = data.get("tick")
        if self._session_tick is None and tick is not None and not data.get("imminent"):
            self._session_tick = int(tick)     # the open-time announcement
        lead = float(data.get("announce_lead") or 120) * (2 if data.get("early") else 1)
        now = self._clock()
        ramp_sec = self._last_ramp[0] * EXCHANGE_TICK_SEC
        for ev in data.get("imminent") or []:
            fire_in = lead * EXCHANGE_TICK_SEC
            if self._session_tick is not None and tick is not None and "tick" in ev:
                counted = (self._session_tick + int(ev["tick"]) - int(tick)) * EXCHANGE_TICK_SEC
                # Joined mid-session, the open we saw was not the real one.
                if 0 <= counted <= 2 * lead * EXCHANGE_TICK_SEC:
                    fire_in = counted
            end = now + max(fire_in, 0.0) + 3 * ramp_sec + WINDOW_MARGIN_SEC
            key = "*" if ev.get("market_wide") or not ev.get("symbol") else ev["symbol"]
            start, old_end = self._windows.get(key, (now, end))
            self._windows[key] = (min(start, now), max(old_end, end))
            logger.info("EVENT  window %s: %s expected in %.0fs — quotes pulled, "
                        "no new strategy orders until %.0fs from now",
                        key, ev.get("kind", "event"), fire_in, end - now)

    def _on_move(self, symbol: str, ramp: int, overshoot: float, label: str, *,
                 pct: float | None = None, target: float | None = None) -> None:
        """Queue a fired event for `_open_plans` and owe it a reaction."""
        now = self._clock()
        pre = self._last_mid.get(symbol) or (
            self._market.mid_price(symbol) if self._market is not None else None)
        if not pre:
            logger.warning("EVENT  %s reaction: no price seen yet, nothing sent", symbol)
            return
        if pct is None:
            pct = target / pre - 1.0
        self._new_events.append({
            "symbol": symbol, "pct": pct, "pre": pre, "ramp": ramp,
            "overshoot": overshoot, "t0": now, "label": label})
        # A ramp longer than the expiry must still leave time to bid.
        wait = min(ramp * EXCHANGE_TICK_SEC, REACTION_EXPIRY_SEC / 2)
        reaction = Reaction(symbol=symbol, t0=now, start=now + wait,
                            settle=pre * (1 + pct), expires=now + REACTION_EXPIRY_SEC)
        self._reactions[symbol] = reaction
        self._cancel_others(symbol, now)
        logger.info("EVENT  %s reaction: cancel other orders; unless a plan "
                    "trades first, bid %d at no more than %.2f in %.0fs",
                    symbol, STARTER_QUANTITY, reaction.settle, wait)

    def _cancel_others(self, symbol: str, now: float) -> None:
        """Cancel this seat's resting orders in the name that no plan owns."""
        for order in self.orders:
            if order.symbol == symbol and not order.purpose.startswith((RIDE, FADE)):
                self._cancel(order, now)

    def _reaction_signal(self, market, now: float, halted: bool) -> Signal | None:
        """The fallback bid for events nothing else has traded since."""
        for symbol, r in list(self._reactions.items()):
            acted = self._last_order_at.get(symbol, -math.inf) >= r.t0
            if acted or now >= r.expires:
                del self._reactions[symbol]
                if not acted:
                    logger.warning("EVENT  %s reaction: expired after %.0fs, no "
                                   "order sent", symbol, now - r.t0)
                continue
            self._cancel_others(symbol, now)
            if now < r.start:
                continue
            if halted:
                del self._reactions[symbol]
                logger.info("EVENT  %s reaction: session loss limit, no new "
                            "position — the cancels are the reaction", symbol)
                continue
            bid = market.best_bid(symbol)
            if not bid:
                continue
            signal = self._place(symbol, "buy", STARTER_QUANTITY, min(bid, r.settle),
                                 now, "event reaction", taker=False, purpose="reaction")
            if signal is not None:
                logger.info("EVENT  %s reaction: bid %d @ %.2f (settle %.2f)",
                            symbol, signal.quantity, signal.price, r.settle)
                return signal
        return None

    def _in_window(self, symbol: str, now: float) -> bool:
        if symbol in self._plans:
            return True
        for key in (symbol, "*"):
            span = self._windows.get(key)
            if span and span[0] <= now <= span[1]:
                return True
        return False

    def _pull_quotes_in_windows(self, now: float) -> None:
        """Cancel resting strategy quotes in names with an event coming."""
        for order in self.orders:
            if order.purpose == "quote" and self._in_window(order.symbol, now):
                self._cancel(order, now)

    def _open_plans(self, net_worth: float, halted: bool) -> None:
        """Turn the events that arrived since the last tick into plans."""
        events, self._new_events = self._new_events, []
        round_trip = 2 * self.taker
        for ev in events:
            symbol, pre, pct = ev["symbol"], ev["pre"], ev["pct"]
            mode = RIDE if pct > 0 else FADE
            edge = designed_edge(mode, pct, ev["overshoot"])
            fraction = size_fraction(edge, round_trip)
            why = (f"{symbol} {pct:+.2%}: {mode} edge {edge:.2%} vs round trip "
                   f"{round_trip:.2%}")
            if mode not in self.EVENT_MODES:
                logger.info("EVENT  %s — not this seat's leg", why)
                continue
            if fraction <= 0:
                logger.info("EVENT  %s — too small to pay the fee, no trade", why)
                continue
            if mode == RIDE:
                if halted:
                    logger.info("EVENT  %s — session loss limit, no new ride", why)
                    continue
                held = (max(self._position(symbol), 0)
                        + self.orders.open_quantity(symbol, "buy"))
                shares = self.event_limits.ride_shares(
                    pre * (1 + pct / 2), net_worth, held, fraction)
            else:
                shares = int(self.sellable(symbol) * fraction)
                extra = (ev["overshoot"] - 1) * abs(pct)
                logger.info("EVENT  %s fade-extra leg edge %.2f%% — not traded",
                            symbol, extra * 100)
            if shares < 1:
                logger.info("EVENT  %s — no shares to trade", why)
                continue
            old = self._plans.pop(symbol, None)
            if old is not None:
                self.finished_plans.append(old)
            plan = EventPlan(symbol=symbol, mode=mode, pct=pct, pre=pre,
                             t0=ev["t0"], ramp_sec=ev["ramp"] * EXCHANGE_TICK_SEC,
                             overshoot=ev["overshoot"], target_shares=shares,
                             label=ev["label"])
            self._plans[symbol] = plan
            if mode == RIDE:
                logger.info("EVENT  %s — RIDE %d shares: buy up to %.2f, sell at "
                            "%.2f, stop half the move below entry", why, shares,
                            plan.buy_cap, plan.target)
            else:
                logger.info("EVENT  %s — FADE %d shares: sell down to %.2f, buy "
                            "back after %.0fs at no more than %.2f", why, shares,
                            plan.sell_floor, plan.ramp_sec, plan.rebuy_cap)

    def _event_signal(self, market, now: float, halted: bool) -> Signal | None:
        """Work the active plans, rides first, biggest positions first."""
        def priority(plan: EventPlan):
            return (plan.mode != RIDE, -self._position(plan.symbol))
        for plan in sorted(self._plans.values(), key=priority):
            step = self._ride_step if plan.mode == RIDE else self._fade_step
            signal = step(plan, market, now, halted)
            if plan.done:
                self._finish(plan)
            if signal is not None:
                return signal
        return None

    def _finish(self, plan: EventPlan) -> None:
        self._plans.pop(plan.symbol, None)
        self.finished_plans.append(plan)
        logger.info("EVENT  %s %s done: bought %d, sold %d, gross P&L %+.2f",
                    plan.mode.upper(), plan.symbol, plan.bought, plan.sold,
                    plan.gross_pnl())

    def _ride_step(self, plan: EventPlan, market, now: float,
                   halted: bool) -> Signal | None:
        sym = plan.symbol
        bid, ask = market.best_bid(sym), market.best_ask(sym)
        live_buy = self.orders.live(sym, "buy")
        live_sell = self.orders.live(sym, "sell")
        held = plan.held

        # Risk exits: out of time, or the move is failing.
        if held > 0 and (now >= plan.deadline or (bid and bid <= plan.stop_price)):
            if live_buy:
                self._cancel(live_buy, now)
            if live_sell:
                if not live_sell.taker:
                    self._cancel(live_sell, now)
                return None
            why = "time" if now >= plan.deadline else "stop"
            return self._place(sym, "sell", held, bid, now,
                               f"ride exit ({why})", taker=True)

        # Peak already reached: take it.
        if held > 0 and bid and bid >= plan.target and not live_sell:
            return self._place(sym, "sell", held, bid, now,
                               "ride exit (target)", taker=True)

        if now < plan.entry_end:
            if halted or plan.stop_buys or live_buy:
                return None
            want = (plan.target_shares - plan.bought
                    - self.orders.open_quantity(sym, "buy"))
            if want > 0 and ask and ask <= plan.buy_cap:
                qty = min(want, self.event_limits.max_order_shares)
                return self._place(sym, "buy", qty, plan.buy_cap, now,
                                   "ride entry", taker=True)
            return None

        # Entry window over: drop any remainder, then offer at the target.
        if live_buy:
            self._cancel(live_buy, now)
            return None
        if held <= 0:
            if not live_sell:
                plan.done = True
            return None
        if live_sell is None:
            price = max(plan.target, ask or plan.target)
            return self._place(sym, "sell", held, price, now,
                               "ride exit (resting at target)", taker=False)
        return None

    def _fade_step(self, plan: EventPlan, market, now: float,
                   halted: bool) -> Signal | None:
        sym = plan.symbol
        bid, ask = market.best_bid(sym), market.best_ask(sym)
        live_buy = self.orders.live(sym, "buy")
        live_sell = self.orders.live(sym, "sell")

        if now < plan.rebuy_start:
            # Get out of the way of the drop, no lower than the floor.
            want = (plan.target_shares - plan.sold
                    - self.orders.open_quantity(sym, "sell"))
            if want > 0 and not live_sell and bid and bid >= plan.sell_floor:
                qty = min(want, self.event_limits.max_order_shares)
                return self._place(sym, "sell", qty, plan.sell_floor, now,
                                   "fade sell", taker=True)
            return None

        if live_sell:
            self._cancel(live_sell, now)
            return None
        owed = plan.owed - self.orders.open_quantity(sym, "buy")
        if plan.owed <= 0:
            if not live_buy:
                plan.done = True
            return None

        if now >= plan.deadline:
            # The settle level never came: buy back at the touch anyway — the
            # sale was to step aside from the drop, not a view on the price.
            if live_buy and not live_buy.taker:
                self._cancel(live_buy, now)
                return None
            if live_buy or not ask or owed <= 0:
                return None
            return self._place(sym, "buy", min(owed, self.event_limits.max_order_shares),
                               ask, now, "fade rebuy (time)", taker=True)

        # Rest a bid at the touch, never above the settle level.
        if not bid:
            return None
        price = min(bid, plan.rebuy_cap)
        if live_buy:
            if not live_buy.taker and price > live_buy.price * (1 + QUOTE_DRIFT):
                self._cancel(live_buy, now)
            return None
        if owed <= 0:
            return None
        return self._place(sym, "buy", owed, price, now,
                           "fade rebuy (resting)", taker=False)

    # ── the other order sources ─────────────────────────────────────────

    def _stop_loss(self, market, portfolio, now: float) -> Signal | None:
        """Sell shares bought whose bid has fallen through the stop."""
        for symbol in sorted(self._bought):
            if symbol in self._plans:
                continue
            bid = market.best_bid(symbol)
            avg = self._cost[symbol] / self._bought[symbol]
            if not self.limits.stop_hit(bid, avg):
                continue
            resting = self.orders.live(symbol, "sell")
            if resting is not None:
                if not resting.taker:
                    self._cancel(resting, now)   # free the shares, then sell
                continue
            qty = min(self._bought[symbol], self.sellable(symbol))
            if qty < 1:
                continue
            logger.warning("RISK   stop-loss %s: bid %.2f is %.2f%% below average "
                           "entry %.4f", symbol, bid, (1 - bid / avg) * 100, avg)
            return self._place(symbol, "sell", qty, bid, now, "stop-loss",
                               taker=True)
        return None

    def _starter(self, market, portfolio, net_worth: float, now: float,
                 halted: bool) -> Signal | None:
        """Small order that guarantees the session has a fill.

        A 5-share buy at the ask if the risk limits allow one; otherwise a
        sell of up to 5 shares already held, which reduces exposure and
        still fills.
        """
        if self._starter_attempts >= STARTER_MAX_ATTEMPTS:
            return None
        if (self._starter_sent_at is not None
                and now - self._starter_sent_at < STARTER_RETRY_SEC):
            return None
        label = f"starter {self._starter_attempts + 1}/{STARTER_MAX_ATTEMPTS}"

        if not halted:
            gross = self._gross_value(market, portfolio)
            for symbol in sorted(market.symbols()):
                ask = market.best_ask(symbol)
                if self._busy(symbol) or not ask:
                    continue
                qty, _ = self.limits.buy_quantity(
                    STARTER_QUANTITY, ask,
                    self._symbol_value(symbol, market, portfolio), gross, net_worth)
                if qty >= 1 and portfolio.can_buy(symbol, qty, ask):
                    signal = self._place(symbol, "buy", qty, ask, now, label,
                                         taker=True)
                    if signal:
                        return self._sent_starter(now, signal)

        held = sorted(((self._symbol_value(sym, market, portfolio), sym)
                       for sym, qty in portfolio.positions.items() if qty > 0),
                      reverse=True)
        for _, symbol in held:
            bid = market.best_bid(symbol)
            if self._busy(symbol) or not bid:
                continue
            signal = self._place(symbol, "sell", STARTER_QUANTITY, bid, now,
                                 label + ", buys blocked by risk limits",
                                 taker=True)
            if signal:
                return self._sent_starter(now, signal)
        return None

    def _sent_starter(self, now: float, signal: Signal) -> Signal:
        self._starter_attempts += 1
        self._starter_sent_at = now
        return signal

    # ── strategy order helpers ──────────────────────────────────────────

    def _quote(self, symbol: str, side: str, qty: int, now: float,
               reason: str, net_worth: float | None = None) -> Signal | None:
        """Rest a passive order at the touch, or keep the one already there.

        Buys are cut to the risk limits first. A resting quote whose touch
        has moved more than QUOTE_DRIFT away is cancelled and re-placed on a
        later tick.
        """
        market = self._market
        touch = market.best_bid(symbol) if side == "buy" else market.best_ask(symbol)
        if not touch:
            return None
        live = self.orders.live(symbol, side)
        if live is not None:
            if live.taker:
                return None
            away = (touch - live.price if side == "buy" else live.price - touch)
            if away > live.price * QUOTE_DRIFT:
                self._cancel(live, now)
            return None
        if side == "buy":
            qty, why = self.limits.buy_quantity(
                qty, touch, self._symbol_value(symbol, market, self._portfolio),
                self._gross_value(market, self._portfolio),
                net_worth if net_worth is not None
                else self._portfolio.net_worth(market))
            if why:
                self._note(symbol, f"buy cut to {qty} by {why} limit")
        return self._place(symbol, side, qty, touch, now, reason, taker=False,
                           purpose="quote")

    def _withdraw(self, symbol: str, side: str, now: float) -> None:
        """Cancel this side's resting strategy quote, if any."""
        live = self.orders.live(symbol, side)
        if live is not None and live.purpose == "quote":
            self._cancel(live, now)

    def min_edge(self) -> float:
        """Smallest expected move a strategy entry must clear.

        Entries and exits rest as maker orders (paid the rebate); the worst
        case is one leg paying the taker fee, e.g. a stop-loss exit.
        """
        return max(0.0, self.taker - self.rebate) * EDGE_MARGIN

    # ── the gate every order passes ─────────────────────────────────────

    def _place(self, symbol: str, side: str, qty: int, price: float | None,
               now: float, reason: str, *, taker: bool,
               purpose: str | None = None) -> Signal | None:
        """Build an order the venue will accept, or return None."""
        if self._stopped or not price or qty < 1:
            return None
        if now < self._paused_until or now < self._cooldown.get(symbol, 0.0):
            return None
        if self.orders.live(symbol, side) is not None:
            return None
        if not self._bucket.available(now):
            return None

        # Never cross our own resting order (self-trade prevention).
        other = self.orders.live(symbol, "sell" if side == "buy" else "buy")
        if other is not None and (price >= other.price if side == "buy"
                                  else price <= other.price):
            self._cancel(other, now)
            return None

        price = self._in_band(symbol, side, price)
        if side == "sell":
            qty = min(qty, self.sellable(symbol))
        else:
            held = max(self._position(symbol), self._ledger.get(symbol, 0), 0)
            qty = min(qty, SEAT_MAX_SHARES - held
                      - self.orders.open_quantity(symbol, "buy"))
            cash = self._portfolio.cash if self._portfolio is not None else 0.0
            room = cash / (1 + self.taker) - self.orders.open_buy_notional()
            qty = min(qty, int(room // price) if room > 0 else 0)
        if qty < 1:
            return None

        self._bucket.take(now)
        self._last_order_at[symbol] = now
        self.orders.add(Order(symbol=symbol, side=side, price=price,
                              quantity=qty, sent_at=now,
                              purpose=purpose or reason, taker=taker))
        logger.info("ORDER  %-4s %d %s @ %.2f %s (notional %.2f) — %s",
                    side, qty, symbol, price, "take" if taker else "rest",
                    qty * price, reason)
        return Signal(symbol=symbol, side=side, quantity=qty, price=price)

    def _cancel(self, order: Order, now: float) -> None:
        """Queue a cancel, unless it could race a fill or is already sent."""
        if (order.order_id is None or order.cancel_sent_at is not None
                or order in self._cancels
                or self.orders.recently_filled(order.symbol, order.side, now)):
            return
        self._cancels.append(order)

    def sellable(self, symbol: str) -> int:
        """Shares that may be offered now without risking a short sale."""
        venue = self._position(symbol)
        own = self._ledger.get(symbol, venue)
        return max(0, min(venue, own) - self.orders.open_quantity(symbol, "sell"))

    def _in_band(self, symbol: str, side: str, price: float) -> float:
        mid = self._market.mid_price(symbol) if self._market is not None else None
        if mid:
            price = min(max(price, mid * (1 - PRICE_BAND)), mid * (1 + PRICE_BAND))
        # buys round down and sells up, so rounding never makes a price worse
        cents = price / TICK_SIZE
        cents = math.floor(cents + 1e-6) if side == "buy" else math.ceil(cents - 1e-6)
        return round(cents * TICK_SIZE, 2)

    # ── helpers ─────────────────────────────────────────────────────────

    def _busy(self, symbol: str) -> bool:
        """A symbol with any live order of ours, or an event coming."""
        return (self.orders.live(symbol, "buy") is not None
                or self.orders.live(symbol, "sell") is not None
                or self._in_window(symbol, self._clock()))

    def _position(self, symbol: str) -> int:
        if self._portfolio is None:
            return self._ledger.get(symbol, 0)
        return int(self._portfolio.positions.get(symbol, 0))

    def _sync_ledger(self, portfolio, now: float) -> None:
        """Adopt the venue's positions whenever they change, unless a fill
        arrived so recently that the update may not include it yet."""
        positions = dict(portfolio.positions)
        if positions != self._seen_positions and now - self._last_fill_at >= LEDGER_SETTLE_SEC:
            self._seen_positions = positions
            self._ledger = {s: int(q) for s, q in positions.items()}

    def _record_mids(self, market) -> None:
        for symbol in market.symbols():
            mid = market.mid_price(symbol)
            if mid:
                self._last_mid[symbol] = mid

    @staticmethod
    def _has_server_state(portfolio, net_worth: float) -> bool:
        placeholder = _trader_config.STARTING_CASH
        return bool(portfolio.positions) or abs(net_worth - placeholder) > 1e-6

    @staticmethod
    def _symbol_value(symbol: str, market, portfolio) -> float:
        return abs(portfolio.positions.get(symbol, 0)) * (market.mid_price(symbol) or 0.0)

    @staticmethod
    def _gross_value(market, portfolio) -> float:
        return sum(abs(qty) * (market.mid_price(sym) or 0.0)
                   for sym, qty in portfolio.positions.items())

    def _note(self, symbol: str, text: str) -> None:
        """Log a risk decision once, until it changes."""
        if self._last_note.get(symbol) != text:
            self._last_note[symbol] = text
            logger.info("RISK   %s: %s", symbol, text)


def configure_logging(seat: str) -> Path:
    """Log to the console and to logs/<seat>_<timestamp>.log."""
    logs = Path(__file__).resolve().parent.parent / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / f"{seat}_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(), logging.FileHandler(path, encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    return path
