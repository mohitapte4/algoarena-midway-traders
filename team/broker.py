"""
Midway Traders — broker (market maker).

Posts two-sided quotes and earns the spread plus maker rebates on every
passive fill. Three decisions, three methods — override any of them.
Inventory management (skew) is the key to staying solvent: the exchange
charges margin interest and liquidates teams below maintenance.

Underneath, TeamBrokerBot sends only orders and cancels the venue accepts
(long-only asks, no cancels for filled quotes, nothing after the close) and
stands aside while a scheduled event or a shock moves the price.

Run:
    TEAM_ID=midway_traders_broker python -m team.broker
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass

import broker.config as _config
from arena import Broker
from arena.broker import _AdaptedBrokerBot
from broker.broker import _print_startup
from shared.messages import (CancelOrder, ErrorMsg, OrderAck, PlaceOrder,
                             SessionEvent, TradeExecution, parse_message)
from team.events import DEFAULT_RAMP_TICKS, EXCHANGE_TICK_SEC
from team.reconnect import run_with_backoff
from team.venue import TokenBucket

logger = logging.getLogger(__name__)

# Volatility-adjusted width. `history` holds one venue mark per book update,
# about 1-2 a second per name, so 10 points is the last ~5-10 seconds.
LOOKBACK = 10
CALM_BPS = 10.0         # recent range below this: keep the engine default
WIDEN_FACTOR = 2.0      # quote width = 2 × the recent high-low range
MAX_WIDEN_BPS = 150.0   # never wider than 1.5% of the price
DEFAULT_WIDTH = 0.30    # engine BASE_SPREAD...
DEFAULT_CAP_BPS = 25.0  # ...capped at 25 bp of the price

REJECT_BUDGET = 8       # graded: under 10 rejects a session; stop quoting at 8
MAX_INVENTORY = 150     # per symbol, held + bid. The venue's limit (600 in week 3)
                        # is per team and counts resting orders: two trader
                        # seats at SEAT_MAX_SHARES (200) plus this stays under it.
MESSAGES_PER_SEC = 8.0  # orders + cancels: the team shares 20 per exchange
                        # tick (1 s) and each trader seat takes 4 (base_trader)
PAUSE_RAMPS = 3         # stay out of an event's symbol for three ramps
PRICE_EPS = 0.005       # half a cent: quotes sit on the penny grid
_NOT_FOUND = re.compile(r"Order '([^']+)' not found")
_STP = re.compile(r"resting (buy|sell) \d+ (\S+) @ ([\d.]+)")


def volatility_spread(price, history) -> float | None:
    """Dollar width for a choppy market, or None when it is calm.

    A scheduled event ramps the price several percent in ~15 s; a quote left
    at the default width is bought or sold by every fast trader on the way
    (week 2: one broker lost $2,964 on AAPL asks in a single minute).
    Widening with the recent range keeps quotes out of reach while the price
    is moving and costs nothing when it is not.
    """
    if not price or price <= 0 or len(history) < LOOKBACK:
        return None
    recent = history[-LOOKBACK:]
    move = max(recent) - min(recent)
    if move < price * CALM_BPS / 10_000:
        return None
    default = min(DEFAULT_WIDTH, price * DEFAULT_CAP_BPS / 10_000)
    return max(default, min(WIDEN_FACTOR * move, price * MAX_WIDEN_BPS / 10_000))


class MyBroker(Broker):
    """Your market-making logic. An empty subclass already quotes."""

    def spread(self, symbol, price, history):
        # Level 3: widen the quotes when the market gets choppy.
        return volatility_spread(price, history)

    def skew(self, symbol, inventory):
        # TODO Level 4: shift both quotes to flatten your inventory.
        # Long (inventory > 0) → quote lower to attract sellers.
        return None

    # Optional extra hooks:
    #   def toxic(self, trader_id): ...                 # Level 5
    #   def on_fill(self, side, symbol, quantity, price): ...

    async def _run_venues(self) -> None:
        """One TeamBrokerBot per venue; the first one's Yahoo feed serves all."""
        urls = [u.strip() for u in _config.EXCHANGE_URLS if u.strip()]
        bots = [TeamBrokerBot(self, url) for url in urls] or [TeamBrokerBot(self)]
        for b in bots[1:]:
            b.state.yahoo_prices = bots[0].state.yahoo_prices
            b._own_yahoo_feed = False
        await asyncio.gather(*(b.run() for b in bots))


@dataclass
class _Quote:
    """One acked quote and the shares of it still resting."""

    symbol: str
    side: str
    price: float
    open_qty: int


class _Tap:
    """Wraps the WebSocket: each raw message is awaited by `peek` first."""

    def __init__(self, ws, peek) -> None:
        self._ws = ws
        self._peek = peek

    def __getattr__(self, name):
        return getattr(self._ws, name)

    async def __aiter__(self):
        async for raw in self._ws:
            await self._peek(raw)
            yield raw


class TeamBrokerBot(_AdaptedBrokerBot):
    """The engine's broker with each of the venue's reject causes designed out.

    Asks never exceed the inventory while shorts are locked, cancels go only
    to quotes still resting, nothing is placed after the close, and a symbol
    with an event coming or firing goes quiet until its ramp has played out.
    Every error the venue still sends is counted; at REJECT_BUDGET the
    broker pulls its quotes and stops quoting.
    """

    def __init__(self, owner: Broker, exchange_url: str | None = None) -> None:
        super().__init__(owner, exchange_url)
        # Replaces the engine's buy_ids / sell_ids: only what is still resting.
        self._live: dict[str, _Quote] = {}
        # Shares an order took as a taker before its ack arrived.
        self._taken: dict[tuple[str, str], int] = {}
        self._shorts_allowed = False        # until the open says otherwise
        self._paused_until: dict[str, float] = {}   # symbol or "*" -> time
        self._errors = 0
        self._clock = time.monotonic
        self._sleep = asyncio.sleep
        self._bucket = TokenBucket(MESSAGES_PER_SEC, MESSAGES_PER_SEC)

    # ── connection ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Quote this venue, reconnecting with backoff whenever the link ends."""
        _print_startup()
        self.start_yahoo_feeds()
        await run_with_backoff(self._serve, f"broker {_config.TEAM_ID} "
                                            f"{self.exchange_url}")

    async def _serve(self) -> None:
        """One connection: quote until the venue closes it or it drops.

        The venue cancels a seat's resting orders when it disconnects
        (server.py, cancel-on-disconnect), so each connection starts with
        nothing tracked; cancelling over the dead socket, as the engine
        does, would only lose track of what was left.
        """
        self._live.clear()
        self._taken.clear()
        self.state.resting_orders.clear()
        self.state.last_quote_prices.clear()
        try:
            await self.connect_to_exchange()
            self._session_open = False
            listener = asyncio.create_task(self.listen_to_exchange())
            requoter = asyncio.create_task(self.requote_loop())
            try:
                await listener
            finally:
                requoter.cancel()
                await asyncio.wait({requoter})   # a cancel aimed at us still propagates
        finally:
            if self._ws:
                with contextlib.suppress(Exception):
                    await self._ws.close()

    async def _send(self, msg) -> None:
        """Send, pacing orders and cancels inside the team's message quota.

        A requote is a cancel and a replace of every level, and the venue
        refuses whatever goes past the quota (week-3 dry run: 69
        RATE_LIMITED in the first two seconds), so a send waits its turn.
        Orders still waiting when the session closes are dropped.
        """
        if isinstance(msg, (PlaceOrder, CancelOrder)):
            while not self._bucket.take(self._clock()):
                await self._sleep(1.0 / self._bucket.rate)
        # A requote waiting its turn can outlive the session (dry run: 3
        # SESSION_CLOSED); once it is closed only cancels are accepted.
        if isinstance(msg, PlaceOrder) and not self._session_open:
            return
        await super()._send(msg)

    # ── quoting ─────────────────────────────────────────────────────────

    def _ask_room(self, symbol: str) -> float:
        """Shares the venue lets us offer: any while shorts are allowed,
        else the inventory (it rejects a sell that would take it below 0,
        ignoring our resting sells)."""
        if self._shorts_allowed:
            return math.inf
        return max(self.state.positions.get(symbol, 0), 0)

    def _bid_room(self, symbol: str) -> int:
        """Shares we may still bid for without the team breaching its limit."""
        return max(MAX_INVENTORY - max(self.state.positions.get(symbol, 0), 0), 0)

    def _paused(self, symbol: str) -> bool:
        now = self._clock()
        return any(self._paused_until.get(k, 0.0) > now for k in (symbol, "*"))

    def needs_requote(self, symbol: str, centre: float) -> bool:
        """The engine's test, except a missing ask with nothing to sell is
        intended, not a side that was taken."""
        last = self.state.last_quote_prices.get(symbol)
        if last is None:
            return True
        resting = self.state.resting_orders.get(symbol, {})
        if not resting.get("buy_qty") and self._bid_room(symbol) > 0:
            return True
        if not resting.get("sell_qty") and self._ask_room(symbol) > 0:
            return True
        if abs(centre - last) >= self.requote_threshold(centre):
            return True
        age = time.time() - self.state.last_quote_time.get(symbol, 0.0)
        return age >= _config.QUOTE_MAX_AGE_SEC

    async def quote_symbol(self, symbol: str) -> None:
        """The engine's ladder, with each ask capped at the inventory not
        already offered on the levels inside it."""
        if self._errors >= REJECT_BUDGET or self._paused(symbol):
            return
        centre = self.quote_centre(symbol)
        if not centre:
            return
        if not self.needs_requote(symbol, centre):
            return

        spread = self.state.compute_spread(symbol)
        skew = self.state.compute_skew(symbol)
        half = spread / 2
        bid_price = math.floor((centre - half + skew) * 100) / 100
        ask_price = math.ceil((centre + half + skew) * 100) / 100
        if bid_price <= 0 or ask_price <= bid_price:
            return

        await self.cancel_quotes(symbol)
        if not self._session_open:
            return

        half_step = max(half, 0.01)
        unoffered = self._ask_room(symbol)
        unbid = self._bid_room(symbol)
        bid_total = ask_total = 0
        for lvl in range(_config.QUOTE_LEVELS):
            size = _config.QUOTE_SIZE * (lvl + 1)
            lvl_bid = math.floor((bid_price - lvl * half_step) * 100) / 100
            lvl_ask = math.ceil((ask_price + lvl * half_step) * 100) / 100
            if lvl_bid <= 0:
                continue
            bid_size = min(size, unbid)
            if bid_size > 0:
                await self._send(PlaceOrder(
                    team_id=_config.TEAM_ID, symbol=symbol, side="buy",
                    order_type="limit", price=lvl_bid, quantity=bid_size,
                ))
                bid_total += bid_size
                unbid -= bid_size
            ask_size = min(size, unoffered)
            if ask_size > 0:
                await self._send(PlaceOrder(
                    team_id=_config.TEAM_ID, symbol=symbol, side="sell",
                    order_type="limit", price=lvl_ask, quantity=ask_size,
                ))
                ask_total += ask_size
                unoffered -= ask_size

        resting = self.state.resting_orders.setdefault(symbol, {})
        resting["buy_qty"] = bid_total
        resting["sell_qty"] = ask_total
        self.state.last_quote_prices[symbol] = centre
        self.state.last_quote_time[symbol] = time.time()
        logger.info("Quoted %s bid=%.2f x%d ask=%.2f x%d centre=%.4f", symbol,
                    bid_price, bid_total, ask_price, ask_total, centre)

    async def cancel_quotes(self, symbol: str) -> None:
        """Cancel this symbol's quotes that are still resting, and no others:
        a cancel for a filled quote is an ORDER_NOT_FOUND."""
        mine = [i for i, q in self._live.items() if q.symbol == symbol]
        for order_id in mine:
            del self._live[order_id]
            await self._send(CancelOrder(
                team_id=_config.TEAM_ID, order_id=order_id, symbol=symbol))
        resting = self.state.resting_orders.setdefault(symbol, {})
        resting["buy_qty"] = resting["sell_qty"] = 0
        self.state.last_quote_prices.pop(symbol, None)

    async def flatten_all(self) -> None:
        """Pull the quotes and send no close-out orders.

        After SESSION_CLOSED the venue rejects every PlaceOrder, but it still
        accepts cancels and leaves the books in place, so cancelling is safe.
        """
        await self.cancel_all_quotes()
        logger.info("Session closed: quotes cancelled, no close-out orders "
                    "(the venue marks positions at the close)")

    # ── venue messages ──────────────────────────────────────────────────

    async def listen_to_exchange(self) -> None:
        if not isinstance(self._ws, _Tap):
            self._ws = _Tap(self._ws, self._peek)
        await super().listen_to_exchange()

    async def _peek(self, raw) -> None:
        """Count the venue's errors; the engine loop only logs them."""
        try:
            msg = parse_message(json.loads(raw))
        except (KeyError, ValueError):
            return                     # the engine loop logs these
        if not isinstance(msg, ErrorMsg):
            return
        try:
            await self._on_error(msg)
        except Exception:
            logger.exception("Could not handle venue error [%s]", msg.code)

    async def _on_error(self, msg: ErrorMsg) -> None:
        self._errors += 1
        if msg.code == "ORDER_NOT_FOUND":
            found = _NOT_FOUND.search(msg.message)
            if found:
                self._live.pop(found.group(1), None)
        elif msg.code == "STP_CANCEL":
            # The venue names the quote it pulled by side, symbol and price.
            found = _STP.search(msg.message)
            if found:
                side, symbol, price = found.groups()
                pulled = next((i for i, q in self._live.items()
                               if (q.symbol, q.side) == (symbol, side)
                               and abs(q.price - float(price)) < PRICE_EPS),
                              None)
                self._live.pop(pulled, None)
        if self._errors == REJECT_BUDGET:
            logger.error("%d venue errors this session (last [%s] %s): "
                         "cancelling every quote and quoting nothing more",
                         self._errors, msg.code, msg.message)
            await self.cancel_all_quotes()

    def _on_order_ack(self, msg: OrderAck) -> None:
        if msg.team_id != _config.TEAM_ID:
            return
        open_qty = msg.quantity - self._taken.pop((msg.symbol, msg.side), 0)
        if open_qty > 0:
            self._live[msg.order_id] = _Quote(msg.symbol, msg.side,
                                              msg.price, open_qty)

    def _on_trade(self, msg: TradeExecution) -> None:
        try:
            super()._on_trade(msg)
            if msg.buyer_id == _config.TEAM_ID:
                self._book_fill(msg.symbol, "buy", msg.price, msg.quantity,
                                taker=msg.aggressor == "buy")
            elif msg.seller_id == _config.TEAM_ID:
                self._book_fill(msg.symbol, "sell", msg.price, msg.quantity,
                                taker=msg.aggressor == "sell")
        except Exception:
            logger.exception("Could not book trade %s", msg.trade_id)

    def _book_fill(self, symbol: str, side: str, price: float, qty: int,
                   taker: bool) -> None:
        """Take a fill off the quotes it executed against.

        A resting quote fills at its own price, or better in the auction
        cross, and the venue fills our most aggressive quote first; so the
        quotes at or through the print, best first, are the ones that traded.
        A taker fill with no such quote belongs to an order not yet acked,
        and its ack subtracts it.
        """
        sign = 1 if side == "buy" else -1
        hit = sorted((i for i, q in self._live.items()
                      if (q.symbol, q.side) == (symbol, side)
                      and sign * (q.price - price) > -PRICE_EPS),
                     key=lambda i: -sign * self._live[i].price)
        for order_id in hit:
            quote = self._live[order_id]
            take = min(qty, quote.open_qty)
            quote.open_qty -= take
            qty -= take
            if quote.open_qty == 0:
                del self._live[order_id]
            if qty == 0:
                return
        if taker:
            key = (symbol, side)
            self._taken[key] = self._taken.get(key, 0) + qty

    async def _on_session_event(self, msg: SessionEvent) -> None:
        try:
            if msg.event in ("SESSION_PREOPEN", "SESSION_OPEN"):
                flags = (msg.data.get("scenario") or {}).get("flags") or {}
                self._shorts_allowed = flags.get("shorts_allowed") is True
            elif msg.event == "CALENDAR":
                await self._on_calendar(msg.data)
            elif msg.event in ("CALENDAR_EVENT", "SHOCK"):
                await self._on_fire(msg.event, msg.data)
            await super()._on_session_event(msg)
        except Exception:
            logger.exception("Could not handle session event %s", msg.event)

    # ── events ──────────────────────────────────────────────────────────

    async def _on_calendar(self, data: dict) -> None:
        """An imminent event: out until three default ramps after it fires.

        The venue warns `announce_lead` ticks ahead (twice that on the early
        feed), so the fire is at most that far away.
        """
        lead = float(data.get("announce_lead") or 120)
        if data.get("early"):
            lead *= 2
        for ev in data.get("imminent") or []:
            key = ("*" if ev.get("market_wide") or not ev.get("symbol")
                   else ev["symbol"])
            await self._pause(key, (lead + PAUSE_RAMPS * DEFAULT_RAMP_TICKS)
                              * EXCHANGE_TICK_SEC, ev.get("kind", "event"))

    async def _on_fire(self, event: str, data: dict) -> None:
        """A CALENDAR_EVENT or SHOCK: out until three ramps from now.

        This replaces a longer pause from the warning, which assumed the
        event would fire as late as possible: requoting three ramps after the
        fire is also the broker's order within 60 ticks of the event.
        """
        ramp = int(data.get("ramp_ticks") or DEFAULT_RAMP_TICKS)
        if data.get("market_wide"):
            keys = ["*"]
        else:
            keys = data.get("affected") or (
                [data["symbol"]] if data.get("symbol") else [])
        for key in keys:
            await self._pause(key, PAUSE_RAMPS * ramp * EXCHANGE_TICK_SEC, event,
                              replace=True)

    async def _pause(self, key: str, seconds: float, reason: str, *,
                     replace: bool = False) -> None:
        until = self._clock() + seconds
        if not replace:
            until = max(self._paused_until.get(key, 0.0), until)
        self._paused_until[key] = until
        for symbol in list(self._quote_symbols) if key == "*" else [key]:
            await self.cancel_quotes(symbol)
        logger.info("EVENT %s: %s quotes pulled for %.0fs", reason, key, seconds)


if __name__ == "__main__":
    from team import logs
    logs.setup()
    MyBroker().run()
