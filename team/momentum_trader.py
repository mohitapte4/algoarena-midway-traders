"""
Midway Traders — momentum trader (seat: midway_traders_trader_1).

Strategy: a moving-average crossover. When the average of the last 5 mid
prices is more than 0.3% above the average of the last 20, the symbol is
treated as trending up and bought; when it is more than 0.3% below, shares
held are sold. The buffer keeps the bot out of calm, noisy markets, where a
short-horizon trend signal does not cover its trading costs, and lets it
trade sharp moves such as a scheduled earnings shock.

The starter order, stop-loss and risk limits come from team.base_trader.

Run:
    make momentum
    TEAM_ID=midway_traders_trader_1 python -m team.momentum_trader
"""

from __future__ import annotations

import logging
import os

from team.base_trader import BaseTrader, Signal, configure_logging

logger = logging.getLogger(__name__)

SHORT_WINDOW = 5
LONG_WINDOW = 20
BUFFER = 0.003            # 0.3% band around the long average
QUANTITY = 5              # shares per signal order


class MomentumTrader(BaseTrader):
    """Moving-average crossover trader with a starter order and risk limits."""

    def _signal(self, market, portfolio, net_worth: float, now: float,
                halted: bool) -> Signal | None:
        """5/20 moving-average crossover with a 0.3% buffer."""
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
                signal = self._limited_buy(symbol, QUANTITY, market, portfolio,
                                           net_worth, now, trend)
                if signal:
                    return signal
                continue

            if short_ma < long_ma * (1 - BUFFER):
                bid = market.best_bid(symbol)
                qty = min(QUANTITY, portfolio.positions.get(symbol, 0))
                if bid and qty >= 1:
                    return self._order(symbol, "sell", qty, bid, now, trend)
        return None


if __name__ == "__main__":
    log_path = configure_logging(os.environ.get("TEAM_ID", "trader"))
    logger.info("Logging to %s", log_path)
    MomentumTrader().run()
