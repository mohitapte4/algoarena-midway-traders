"""
Midway Traders — momentum trader (seat: midway_traders_trader_1).

Strategy: a moving-average crossover. When the average of the last 5 mid
prices is more than 0.3% above the average of the last 20, the symbol is
treated as trending up and a bid rests at the touch; when it is more than
0.3% below, shares held are offered at the touch. The buffer keeps the bot
out of calm, noisy markets, where a short-horizon trend signal does not
cover its trading costs; it is raised automatically if the venue's fees make
0.3% too thin (BaseTrader.min_edge).

Scheduled events are traded from the message, not the chart: this seat
rides good news and fades bad news (team.events). Ahead of an event its
quotes in that name are pulled, so the crossover never trades the ramp.

The starter order, stop-loss, maker quoting and risk limits come from
team.base_trader.

Run (0.1 s loop — reaction speed decides the event race):
    make momentum
    TICK_INTERVAL=0.1 TEAM_ID=midway_traders_trader_1 python -m team.momentum_trader
"""

from __future__ import annotations

import logging
import os

from team.base_trader import BaseTrader, Signal, configure_logging
from team.events import FADE, RIDE

logger = logging.getLogger(__name__)

SHORT_WINDOW = 5
LONG_WINDOW = 20
BUFFER = 0.003            # 0.3% band around the long average
QUANTITY = 5              # shares per quote


class MomentumTrader(BaseTrader):
    """Moving-average crossover quoting, plus event rides and fades."""

    EVENT_MODES = frozenset({RIDE, FADE})

    def _signal(self, market, portfolio, net_worth: float, now: float,
                halted: bool) -> Signal | None:
        """5/20 moving-average crossover with a fee-aware buffer."""
        buffer = max(BUFFER, self.min_edge())
        for symbol in sorted(market.symbols()):
            if self._in_window(symbol, now):
                continue
            hist = market.prices(symbol)
            if len(hist) < LONG_WINDOW:
                continue
            short_ma = sum(hist[-SHORT_WINDOW:]) / SHORT_WINDOW
            long_ma = sum(hist[-LONG_WINDOW:]) / LONG_WINDOW
            trend = f"short {short_ma:.2f} / long {long_ma:.2f}"

            if short_ma > long_ma * (1 + buffer) and not halted:
                signal = self._quote(symbol, "buy", QUANTITY, now, trend, net_worth)
                if signal:
                    return signal
                continue

            self._withdraw(symbol, "buy", now)
            if short_ma < long_ma * (1 - buffer):
                qty = min(QUANTITY, self.sellable(symbol))
                if qty >= 1:
                    signal = self._quote(symbol, "sell", qty, now, trend)
                    if signal:
                        return signal
        return None


if __name__ == "__main__":
    log_path = configure_logging(os.environ.get("TEAM_ID", "trader"))
    logger.info("Logging to %s", log_path)
    MomentumTrader().run()
