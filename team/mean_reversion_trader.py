"""
Midway Traders — mean-reversion trader (seat: midway_traders_trader_2).

Strategy: buy dips and sell the rebound. Over the last 20 mid prices, the
latest mid's z-score is its distance from the average in standard
deviations. A symbol is bought when it sits at least 2 standard deviations
below its average AND at least 0.5% below it in price. The second condition
matters in calm markets, where the deviation is tiny: a round trip costs
about 0.3% in taker fees plus the spread, so a smaller dip cannot pay for
itself. Shares this bot bought are sold once the mid is back at its
average.

This is the opposite bet to team.momentum_trader: momentum buys strength
and sells weakness, this buys weakness and sells the recovery. Running both
seats on the same market shows which one the market rewards. A dip that
keeps falling is the risk here; the stop-loss, starter order and risk
limits come from team.base_trader.

Run:
    make meanrev
    TEAM_ID=midway_traders_trader_2 python -m team.mean_reversion_trader
"""

from __future__ import annotations

import logging
import os
import statistics

from team.base_trader import BaseTrader, Signal, configure_logging

logger = logging.getLogger(__name__)

WINDOW = 20
ENTRY_Z = 2.0             # buy at this many standard deviations below the mean
MIN_DEVIATION = 0.005     # ...and at least 0.5% below it, to clear costs
QUANTITY = 5              # shares per buy


class MeanReversionTrader(BaseTrader):
    """Buys dips below the 20-tick average and sells when they recover."""

    def _signal(self, market, portfolio, net_worth: float, now: float,
                halted: bool) -> Signal | None:
        """Z-score entry, exit at the mean."""
        for symbol in sorted(market.symbols()):
            if symbol in self._pending:
                continue
            hist = market.prices(symbol)
            if len(hist) < WINDOW:
                continue
            window = hist[-WINDOW:]
            mean = statistics.fmean(window)
            std = statistics.pstdev(window, mean)
            last = window[-1]
            if mean <= 0 or std <= 0:
                continue
            z = (last - mean) / std
            reason = f"mid {last:.2f} / mean {mean:.2f}, z {z:+.2f}"

            # Exit: only shares this bot bought, never other holdings.
            if last >= mean and self._bought.get(symbol):
                bid = market.best_bid(symbol)
                qty = min(self._bought[symbol], portfolio.positions.get(symbol, 0))
                if bid and qty >= 1:
                    return self._order(symbol, "sell", qty, bid, now,
                                       reason + ", back at the mean")
                continue

            if (z <= -ENTRY_Z and (mean - last) / mean >= MIN_DEVIATION
                    and not halted):
                signal = self._limited_buy(symbol, QUANTITY, market, portfolio,
                                           net_worth, now, reason)
                if signal:
                    return signal
        return None


if __name__ == "__main__":
    log_path = configure_logging(os.environ.get("TEAM_ID", "trader"))
    logger.info("Logging to %s", log_path)
    MeanReversionTrader().run()
