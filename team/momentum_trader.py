"""
Midway Traders — momentum trader with risk controls.

Strategy: a moving-average crossover. When the average of the last 5 mid
prices is more than 0.3% above the average of the last 20, the symbol is
treated as trending up and bought; when it is more than 0.3% below, shares
held are sold. The buffer keeps the bot out of calm, noisy markets, where a
short-horizon trend signal does not cover its trading costs, and lets it
trade sharp moves such as a scheduled earnings shock.

Every tick, in order:
  1. Nothing is sent during the pre-open auction. Until the exchange has
     sent the seat's portfolio (it does so after the first fill), only the
     starter order may trade.
  2. Exits first: a stop-loss sells shares bought below their average entry.
  3. A starter order (5 shares) is placed until one fill is confirmed, so the
     session always has at least one fill. It is re-sent if not filled.
  4. The crossover signal, with every buy checked against team.risk limits.

The venue is long-only this week, so sells never exceed the shares held, and
at most one order per symbol is outstanding at a time. Orders are priced at
the touch (ask for buys, bid for sells) because a trader cannot cancel a
resting order.

Run one seat:
    TEAM_ID=midway_traders_trader_1 python -m team.momentum_trader
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import trader.config as _trader_config
from arena import Signal, Trader
from team.risk import RiskLimits

logger = logging.getLogger(__name__)

# The engine logs order acknowledgements at DEBUG; enable that level for its
# trader logger only so each order_ack appears in the session log.
logging.getLogger("trader.trader").setLevel(logging.DEBUG)

SHORT_WINDOW = 5
LONG_WINDOW = 20
BUFFER = 0.003            # 0.3% band around the long average
QUANTITY = 5              # shares per signal order
STARTER_QUANTITY = 5
STARTER_RETRY_SEC = 30.0  # re-send the starter if it has not filled by then
STARTER_MAX_ATTEMPTS = 3
PENDING_TIMEOUT_SEC = 20.0


class MomentumTrader(Trader):
    """Moving-average crossover trader with a starter order and risk limits."""

    def __init__(self, limits: RiskLimits | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.limits = limits or RiskLimits()
        self._clock = clock
        self._preopen = False
        self._start_net_worth: float | None = None
        self._halt_logged = False
        self._starter_filled = False
        self._starter_attempts = 0
        self._starter_sent_at: float | None = None
        # symbol -> (side, quantity still open, limit price, time sent)
        self._pending: dict[str, tuple[str, int, float, float]] = {}
        # shares this bot bought and still holds, and what they cost
        self._bought: dict[str, int] = {}
        self._cost: dict[str, float] = {}
        self._fills = 0
        self._last_note: dict[str, str] = {}

    # ── required hook ───────────────────────────────────────────────────

    def on_tick(self, market, portfolio) -> Signal | None:
        """Return at most one Signal per tick, or None."""
        if self._preopen:
            return None
        now = self._clock()
        self._expire_pending(now)

        net_worth = portfolio.net_worth(market)
        if self._start_net_worth is None:
            # The exchange sends this seat its portfolio only after its first
            # fill; until then the local Portfolio reports a placeholder that
            # is lower than the real net worth. Only the starter may trade on
            # it (sized against the placeholder, so the limits are stricter);
            # its fill brings the real numbers, and the loss limit is anchored
            # to those.
            if not self._has_server_state(portfolio, net_worth):
                if self._starter_filled:
                    return None
                return self._starter(market, portfolio, net_worth, now, False)
            self._start_net_worth = net_worth
            logger.info("RISK   session reference net worth %.2f", net_worth)
        halted = self.limits.halted(net_worth, self._start_net_worth)
        if halted and not self._halt_logged:
            self._halt_logged = True
            logger.warning("RISK   session loss limit reached (net worth %.2f vs "
                           "%.2f at the open): no new positions, exits only",
                           net_worth, self._start_net_worth)

        signal = self._stop_loss(market, portfolio, now)
        if signal is None and not self._starter_filled:
            signal = self._starter(market, portfolio, net_worth, now, halted)
        if signal is None:
            signal = self._crossover(market, portfolio, net_worth, now, halted)
        return signal

    # ── optional hooks ──────────────────────────────────────────────────

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        """Record an execution against the order and the bought position."""
        self._fills += 1
        self._starter_filled = True

        limit = None
        pending = self._pending.get(symbol)
        if pending and pending[0] == side:
            limit = pending[2]
            remaining = pending[1] - quantity
            if remaining > 0:
                self._pending[symbol] = (side, remaining, pending[2], pending[3])
            else:
                del self._pending[symbol]

        if side == "buy":
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
        if limit is not None:
            delta = price - limit
            worse = delta > 0 if side == "buy" else delta < 0
            tag = f"  vs limit {delta:+.4f}"
        held = self._bought.get(symbol, 0)
        avg_entry = self._cost[symbol] / held if held else 0.0
        (logger.warning if worse else logger.info)(
            "FILL   #%d %-4s %3d %-6s @ %8.2f  notional %9.2f%s  "
            "| bought and held %d, avg entry %.4f",
            self._fills, side, quantity, symbol, price, quantity * price, tag,
            held, avg_entry)

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Track the opening auction; log everything else."""
        if event == "SESSION_PREOPEN":
            self._preopen = True
        elif event == "AUCTION_RESULT":
            self._preopen = False
        if event != "AUCTION_INDICATIVE":       # sent every pre-open tick
            logger.info("EVENT  %-14s %s", event, message)

    # ── the three order sources ─────────────────────────────────────────

    def _stop_loss(self, market, portfolio, now: float) -> Signal | None:
        """Sell shares bought whose bid has fallen through the stop."""
        for symbol in sorted(self._bought):
            if symbol in self._pending:
                continue
            bid = market.best_bid(symbol)
            avg = self._cost[symbol] / self._bought[symbol]
            if not self.limits.stop_hit(bid, avg):
                continue
            qty = min(self._bought[symbol], portfolio.positions.get(symbol, 0))
            if qty < 1:
                continue
            logger.warning("RISK   stop-loss %s: bid %.2f is %.2f%% below average "
                           "entry %.4f", symbol, bid, (1 - bid / avg) * 100, avg)
            return self._order(symbol, "sell", qty, bid, now, "stop-loss")
        return None

    def _starter(self, market, portfolio, net_worth: float, now: float,
                 halted: bool) -> Signal | None:
        """Small order that guarantees the session has a fill.

        A 5-share buy if the risk limits allow one; otherwise a sell of up to
        5 shares already held, which reduces exposure and still fills.
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
                if symbol in self._pending or not ask:
                    continue
                qty, _ = self.limits.buy_quantity(
                    STARTER_QUANTITY, ask,
                    self._symbol_value(symbol, market, portfolio), gross, net_worth)
                if qty >= 1 and portfolio.can_buy(symbol, qty, ask):
                    return self._send_starter(symbol, "buy", qty, ask, now, label)

        held = sorted(((self._symbol_value(sym, market, portfolio), sym)
                       for sym, qty in portfolio.positions.items() if qty > 0),
                      reverse=True)
        for _, symbol in held:
            bid = market.best_bid(symbol)
            if symbol in self._pending or not bid:
                continue
            qty = min(STARTER_QUANTITY, portfolio.positions[symbol])
            return self._send_starter(symbol, "sell", qty, bid, now,
                                      label + ", buys blocked by risk limits")
        return None

    def _send_starter(self, symbol: str, side: str, qty: int, price: float,
                      now: float, label: str) -> Signal:
        self._starter_attempts += 1
        self._starter_sent_at = now
        return self._order(symbol, side, qty, price, now, label)

    def _crossover(self, market, portfolio, net_worth: float, now: float,
                   halted: bool) -> Signal | None:
        """5/20 moving-average crossover with a 0.3% buffer."""
        gross = self._gross_value(market, portfolio)
        for symbol in sorted(market.symbols()):
            if symbol in self._pending:
                continue
            hist = market.prices(symbol)
            if len(hist) < LONG_WINDOW:
                continue
            short_ma = sum(hist[-SHORT_WINDOW:]) / SHORT_WINDOW
            long_ma = sum(hist[-LONG_WINDOW:]) / LONG_WINDOW
            trend = f"short {short_ma:.2f} / long {long_ma:.2f}"

            if short_ma > long_ma * (1 + BUFFER) and not halted:
                ask = market.best_ask(symbol)
                if not ask:
                    continue
                qty, why = self.limits.buy_quantity(
                    QUANTITY, ask, self._symbol_value(symbol, market, portfolio),
                    gross, net_worth)
                if why:
                    self._note(symbol, f"buy cut to {qty} by {why} limit")
                if qty < 1 or not portfolio.can_buy(symbol, qty, ask):
                    continue
                return self._order(symbol, "buy", qty, ask, now, trend)

            if short_ma < long_ma * (1 - BUFFER):
                bid = market.best_bid(symbol)
                qty = min(QUANTITY, portfolio.positions.get(symbol, 0))
                if bid and qty >= 1:
                    return self._order(symbol, "sell", qty, bid, now, trend)
        return None

    # ── helpers ─────────────────────────────────────────────────────────

    def _order(self, symbol: str, side: str, qty: int, price: float,
               now: float, reason: str) -> Signal:
        self._pending[symbol] = (side, qty, price, now)
        logger.info("ORDER  %-4s %d %s @ %.2f limit (notional %.2f) — %s",
                    side, qty, symbol, price, qty * price, reason)
        return Signal(symbol=symbol, side=side, quantity=qty, price=price)

    def _expire_pending(self, now: float) -> None:
        """Forget orders that have not filled in time; they may still rest."""
        for symbol, (side, qty, price, sent) in list(self._pending.items()):
            if now - sent >= PENDING_TIMEOUT_SEC:
                del self._pending[symbol]
                logger.warning("ORDER  %s %d %s @ %.2f not filled after %.0fs",
                               side, qty, symbol, price, PENDING_TIMEOUT_SEC)

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


def _configure_logging(seat: str) -> Path:
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


if __name__ == "__main__":
    log_path = _configure_logging(os.environ.get("TEAM_ID", "trader"))
    logger.info("Logging to %s", log_path)
    MomentumTrader().run()
