"""
Midway Traders — risk limits for the trading bots.

Every rule is a pure function of numbers the bot already has (net worth,
the value it holds, the current price), so each one can be tested on its
own. All limits are fractions of the seat's live net worth, which keeps them
meaningful whatever the seat's starting capital.

    Session loss limit   stop opening positions once net worth is this far
                         below its value at the open (exits still allowed)
    Order size           one order's notional
    Concentration        notional held in a single symbol
    Gross exposure       notional held across all symbols
    Stop-loss            sell a position whose bid has fallen this far
                         below its average entry price (for a short: buy
                         back once the ask has risen this far above it)

Event trades (team.events) are short-lived and sized separately, by
EventLimits: a bigger slice of net worth in one name for well under a
minute, with a share cap that keeps the team inside the venue's limit
(±800 shares per symbol from week 4).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    """Moderate limits by default; every field is a fraction."""

    max_session_loss: float = 0.01
    max_order: float = 0.02
    max_symbol: float = 0.05
    max_gross: float = 0.20
    stop_loss: float = 0.015

    def halted(self, net_worth: float, start_net_worth: float | None) -> bool:
        """True once the session loss limit has been breached."""
        if start_net_worth is None or start_net_worth <= 0:
            return False
        return net_worth < start_net_worth * (1 - self.max_session_loss)

    def buy_quantity(self, wanted: int, price: float, symbol_value: float,
                     gross_value: float, net_worth: float) -> tuple[int, str]:
        """Largest quantity up to `wanted` that respects every buy limit.

        `symbol_value` and `gross_value` are what is already held, at market.
        Returns the quantity and, when it is below `wanted`, the name of the
        limit that bound; an empty string means nothing was cut.
        """
        if wanted < 1 or price <= 0 or net_worth <= 0:
            return 0, "no price or net worth"
        room = {
            "order size": self.max_order * net_worth,
            "concentration": self.max_symbol * net_worth - symbol_value,
            "gross exposure": self.max_gross * net_worth - gross_value,
        }
        binding = min(room, key=room.get)
        qty = min(wanted, int(max(room[binding], 0.0) // price))
        return qty, ("" if qty == wanted else binding)

    def stop_hit(self, bid: float | None, avg_entry: float | None) -> bool:
        """True when the bid is at or below the stop under the average entry."""
        if not bid or not avg_entry:
            return False
        return bid <= avg_entry * (1 - self.stop_loss)

    def short_stop_hit(self, ask: float | None, avg_entry: float | None) -> bool:
        """True when the ask is at or above the stop over a short's average entry."""
        if not ask or not avg_entry:
            return False
        return ask >= avg_entry * (1 + self.stop_loss)


@dataclass(frozen=True)
class EventLimits:
    """Size of one event plan; shares count held + resting + unacked orders."""

    max_symbol: float = 0.12      # of net worth, in the event's symbol
    max_order_shares: int = 60    # per order
    max_shares: int = 200         # per symbol, per seat

    def ride_shares(self, price: float, net_worth: float, held: int,
                    fraction: float) -> int:
        """Shares a ride may buy at `price`, scaled by the edge fraction."""
        if price <= 0 or net_worth <= 0 or fraction <= 0:
            return 0
        by_value = int(self.max_symbol * net_worth // price)
        room = min(self.max_shares, by_value) - max(held, 0)
        return max(0, int(room * min(fraction, 1.0)))


HALT_GRACE_SEC = 3.0    # past the venue's stated halt end before trying again

_RESUME_IN = re.compile(r"Resume in (\d+(?:\.\d+)?)s")


class TradingState:
    """Exchange halts end on the venue's resume event, or once the duration
    the venue announced has passed: a seat that dropped during the halt never
    sees the resume. A halt that states no duration is never lifted by a guess.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.symbols: dict[str, float] = {}     # symbol -> when the halt ends
        self.market_until: float | None = None
        self.seat_limit: int | None = None

    def _until(self, data: dict) -> float:
        duration = data.get("duration")
        if isinstance(duration, (int, float)):
            return self._clock() + duration + HALT_GRACE_SEC
        return math.inf

    def event(self, event: str, data: dict) -> None:
        symbol = data.get("symbol")
        if event == "TRADING_HALT" and symbol:
            self.symbols[symbol] = self._until(data)
        elif event == "TRADING_RESUMED" and symbol:
            self.symbols.pop(symbol, None)
        elif event == "MARKET_HALT":
            self.market_until = self._until(data)
        elif event == "MARKET_RESUMED":
            # The teacher's lift resumes every halted symbol in this one event.
            self.market_until = None
            for s in data.get("symbols") or ():
                self.symbols.pop(s, None)
        if event in ("SESSION_PREOPEN", "SESSION_OPEN"):
            scenario = data.get("scenario") or {}
            limit = scenario.get("position_limit")
            if isinstance(limit, (int, float)) and limit > 0:
                # Three seats share the team's limit. Never increase our
                # existing caps merely because a scenario allows more.
                self.seat_limit = max(0, int(limit) // 3)

    def halted_reject(self, symbol: str, message: str) -> None:
        """A SYMBOL_HALTED reject: the halt began while we were not listening."""
        found = _RESUME_IN.search(message)
        self.symbols[symbol] = (self._clock() + float(found.group(1)) + HALT_GRACE_SEC
                                if found else math.inf)

    def blocked(self, symbol: str) -> bool:
        now = self._clock()
        return ((self.market_until is not None and now < self.market_until)
                or now < self.symbols.get(symbol, -math.inf))

    def cap(self, default: int) -> int:
        return default if self.seat_limit is None else min(default, self.seat_limit)
