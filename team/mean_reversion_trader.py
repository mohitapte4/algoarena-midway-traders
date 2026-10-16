"""
Midway Traders — mean-reversion trader (seat: midway_traders_trader_2).

Strategy: buy dips and sell the rebound. Over the last 20 mid prices, the
latest mid's z-score is its distance from the average in standard
deviations. A symbol is bought when it sits at least 2 standard deviations
below its average AND at least 0.5% below it in price: a bid rests at the
touch. The second condition matters in calm markets, where the deviation is
tiny and cannot pay for itself; it is raised automatically if the venue's
fees make 0.5% too thin (BaseTrader.min_edge). Shares this bot bought are
offered at the touch once the mid is back at its average.

On scheduled events this seat only protects what it holds: on bad news it
sells at once and buys back after the drop (team.events FADE). It does not
ride good news, so it never races the momentum seat for the same shares.

This is the opposite bet to team.momentum_trader: momentum buys strength
and sells weakness, this buys weakness and sells the recovery. Running both
seats on the same market shows which one the market rewards. A dip that
keeps falling is the risk here; the stop-loss, starter order and risk
limits come from team.base_trader.

Run:
    make meanrev
    TICK_INTERVAL=0.1 TEAM_ID=midway_traders_trader_2 python -m team.mean_reversion_trader
"""

from __future__ import annotations

import logging
import os
import statistics

from team.base_trader import BaseTrader, Signal, configure_logging
from team.events import FADE

logger = logging.getLogger(__name__)

WINDOW = 20
ENTRY_Z = 2.0             # buy at this many standard deviations below the mean
MIN_DEVIATION = 0.005     # ...and at least 0.5% below it, to clear costs
QUANTITY = 5              # shares per quote


class MeanReversionTrader(BaseTrader):
    """Quotes dips below the 20-tick average and offers the recovery."""

    EVENT_MODES = frozenset({FADE})

    def _signal(self, market, portfolio, net_worth: float, now: float,
                halted: bool) -> Signal | None:
        """Z-score entry, exit at the mean."""
        min_deviation = max(MIN_DEVIATION, self.min_edge())
        for symbol in sorted(market.symbols()):
            if self._in_window(symbol, now):
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
                self._withdraw(symbol, "buy", now)
                qty = min(self._bought[symbol], self.sellable(symbol))
                if qty >= 1:
                    signal = self._quote(symbol, "sell", qty, now,
                                         reason + ", back at the mean")
                    if signal:
                        return signal
                continue

            if (z <= -ENTRY_Z and (mean - last) / mean >= min_deviation
                    and not halted):
                signal = self._quote(symbol, "buy", QUANTITY, now, reason,
                                     net_worth)
                if signal:
                    return signal
                continue
            self._withdraw(symbol, "buy", now)
        return None


if __name__ == "__main__":
    log_path = configure_logging(os.environ.get("TEAM_ID", "trader"))
    logger.info("Logging to %s", log_path)
    MeanReversionTrader().run()
