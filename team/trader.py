"""
Midway Traders — trader bot.

Level 1 implementation: place a single size-limited buy to exercise the
full path (signal → risk check → order → fill → PortfolioUpdate), then
return None on every subsequent tick. on_fill() is overridden to record
executions.

Run one seat:
    TEAM_ID=midway_traders_trader_1 python -m team.trader
"""

from __future__ import annotations

import logging

from arena import Signal, Trader

logger = logging.getLogger(__name__)

#: Bounds on the Level 1 order; the tighter of the two applies.
MAX_SHARES = 5
MAX_NOTIONAL = 5_000.0


class MyTrader(Trader):
    """Single-order trader with execution logging."""

    def __init__(self) -> None:
        super().__init__()
        self._opened = False
        self._limit: float | None = None
        self._fills = 0
        self._shares = 0
        self._spent = 0.0

    # ── required hook ───────────────────────────────────────────────────

    def on_tick(self, market, portfolio) -> Signal | None:
        """Return a Signal on the first tick that permits one, else None."""
        if self._opened:
            return None

        # Sorted for determinism: symbol iteration order would otherwise
        # depend on which book snapshot arrived first.
        for symbol in sorted(market.symbols()):
            # Price at the offer to make the limit marketable. Before the
            # ask side is populated, fall back to the mid.
            price = market.best_ask(symbol) or market.mid_price(symbol)
            if not price or price <= 0:
                continue

            qty = min(MAX_SHARES, int(MAX_NOTIONAL // price))
            if qty < 1:
                continue

            # Local affordability check. The exchange enforces this as
            # well; checking here avoids a round-trip and a rejection.
            if not portfolio.can_buy(symbol, qty, price):
                continue

            self._opened = True
            self._limit = price
            logger.info("ORDER  buy %d %s @ %.2f limit (notional %.2f)",
                        qty, symbol, price, qty * price)
            return Signal(symbol=symbol, side="buy", quantity=qty, price=price)

        return None

    # ── optional hook ───────────────────────────────────────────────────

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        """Record an execution.

        Accumulates across calls: an order that crosses several price
        levels is reported as one fill per level, so the volume-weighted
        average is only correct if each call is aggregated.

        A buy limit cannot execute above its limit price, so the reported
        difference is price improvement. A positive value indicates an
        exchange-side error and is logged at WARNING.
        """
        self._fills += 1
        self._shares += quantity
        self._spent += quantity * price

        delta = 0.0 if self._limit is None else price - self._limit
        tag = "" if self._limit is None else f"  vs limit {delta:+.4f}"
        emit = logger.warning if delta > 0 else logger.info
        emit("FILL   #%d %-4s %3d %-6s @ %8.2f  notional %9.2f%s",
             self._fills, side, quantity, symbol, price,
             quantity * price, tag)

        if self._shares:
            logger.info("       position %d shares, vwap %.4f, cost %.2f",
                        self._shares, self._spent / self._shares, self._spent)

    # Remaining hooks are unused at Level 1:
    #   on_event(event, message, data)
    #   on_ipo(symbol, lo, hi, shares, data)


if __name__ == "__main__":
    MyTrader().run()
