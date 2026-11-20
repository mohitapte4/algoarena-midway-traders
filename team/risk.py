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
(±800 shares per symbol from week 4). Every buy, events included, still
passes HARD_GROSS in the order gate.

Phase 3 adds:
    KillSwitch           one switch per seat: no new orders, resting orders
                         cancelled. Tripped by a human
                         (a KILL file), the reject budget, a hard loss, or
                         repeated disconnects
    TradingState         halts lift RESUME_SETTLE_SEC after the venue resumes
    position_stop,       the broker's per-position stop and its toxicity
    ToxicityMeter        widening (one-sided flow against its quotes)

The limit table and the reasoning behind each number are in RISK.md.
"""

from __future__ import annotations

import logging
import math
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hard ceiling on what a trader seat has traded, at market, checked in the
# order gate for every buy (strategy, events, flashes, starter). Strategy
# buys keep RiskLimits.max_gross (20%) at their source; events get 12% of net
# worth per name (EventLimits), so 35% lets two or three event names run at
# once and stops a market-wide print from riding all twelve.
HARD_GROSS = 0.35


@dataclass(frozen=True)
class RiskLimits:
    """Moderate limits by default; every field is a fraction."""

    max_session_loss: float = 0.01
    max_order: float = 0.02
    max_symbol: float = 0.05
    max_gross: float = 0.20
    stop_loss: float = 0.015
    kill_loss: float = 0.02       # hard: trip the kill switch

    def kill_hit(self, net_worth: float, start_net_worth: float | None) -> bool:
        """True once net worth is `kill_loss` below its value at the open."""
        if start_net_worth is None or start_net_worth <= 0:
            return False
        return net_worth < start_net_worth * (1 - self.kill_loss)

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


# A resumed symbol stays blocked this long. The venue's velocity check looks
# back 60 s and still holds the move that halted it: on Nov 6 TSLA resumed at
# 15:45:40, trader_1 requoted it every second from 15:45:47 and it halted
# again at 15:46:19. Ten 1 s ticks let the book refill and the mark settle.
RESUME_SETTLE_SEC = 10.0
BAND_MARGIN = 0.01      # inside the week's LULD band: the mark moves every tick

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
        self.luld_band: float | None = None

    def _until(self, data: dict) -> float:
        duration = data.get("duration")
        if isinstance(duration, (int, float)):
            return self._clock() + duration + RESUME_SETTLE_SEC
        return math.inf

    def event(self, event: str, data: dict) -> None:
        symbol = data.get("symbol")
        if event == "TRADING_HALT" and symbol:
            self.symbols[symbol] = self._until(data)
        elif event == "TRADING_RESUMED" and symbol:
            self.symbols[symbol] = self._clock() + RESUME_SETTLE_SEC
        elif event == "MARKET_HALT":
            self.market_until = self._until(data)
        elif event == "MARKET_RESUMED":
            # The teacher's lift resumes every halted symbol in this one event.
            settle = self._clock() + RESUME_SETTLE_SEC
            self.market_until = settle
            for s in data.get("symbols") or ():
                self.symbols[s] = settle
        if event in ("SESSION_PREOPEN", "SESSION_OPEN"):
            scenario = data.get("scenario") or {}
            limit = scenario.get("position_limit")
            if isinstance(limit, (int, float)) and limit > 0:
                # Three seats share the team's limit. Never increase our
                # existing caps merely because a scenario allows more.
                self.seat_limit = max(0, int(limit) // 3)
            band = (scenario.get("config") or {}).get("LULD_BAND_PCT")
            if isinstance(band, (int, float)) and band > 0:
                self.luld_band = float(band)

    def halted_reject(self, symbol: str, message: str) -> None:
        """A SYMBOL_HALTED reject: the halt began while we were not listening."""
        found = _RESUME_IN.search(message)
        self.symbols[symbol] = (self._clock() + float(found.group(1)) + RESUME_SETTLE_SEC
                                if found else math.inf)

    def blocked(self, symbol: str) -> bool:
        now = self._clock()
        return ((self.market_until is not None and now < self.market_until)
                or now < self.symbols.get(symbol, -math.inf))

    def cap(self, default: int) -> int:
        return default if self.seat_limit is None else min(default, self.seat_limit)

    def band(self, default: float) -> float:
        """How far from the venue's mark a limit price may go this week."""
        if self.luld_band is None:
            return default
        return min(default, self.luld_band - BAND_MARGIN)


# ── kill switch ─────────────────────────────────────────────────────────────

KILL_FILE = "KILL"              # every seat; KILL_<seat id> for one seat
KILL_POLL_SEC = 1.0
MAX_DROPS = 6                   # dropped connections on one link ...
DROP_WINDOW_SEC = 300.0         # ... within this window. Nov 6 (the hosted
                                # feed's worst day) peaked at 4 in 5 min on a
                                # link, and every seat still traded the block.


@dataclass
class KillSwitch:
    """One switch per seat: once tripped, no new orders for the session.

    The seat's order gate asks `tripped` before anything else; cancels still
    go out, and the seat cancels what it has resting.

    Trips:
      * human: a file named KILL (all seats) or KILL_<seat> in the repo root.
        Works on Windows, where a signal would not. Delete the file and
        restart the seat to clear it.
      * the reject budget (the seat calls trip)
      * a hard loss (the seat calls trip)
      * MAX_DROPS dropped connections on one link within DROP_WINDOW_SEC
        (on_disconnect; the broker has one link per venue)
    """

    seat: str
    clock: Callable[[], float]
    kill_dir: Path = REPO_ROOT
    max_drops: int = MAX_DROPS
    drop_window_sec: float = DROP_WINDOW_SEC
    reason: str | None = None
    _drops: dict = field(default_factory=dict)      # link -> drop times
    _next_poll: float = -math.inf

    @property
    def tripped(self) -> bool:
        return self.reason is not None

    def trip(self, reason: str) -> bool:
        """Trip once; True if this call tripped it."""
        if self.tripped:
            return False
        self.reason = reason
        logger.error("KILL   %s tripped: %s — no new orders this session",
                     self.seat, reason)
        return True

    def poll(self) -> None:
        """The human trip: look for the kill file, at most once a second."""
        now = self.clock()
        if now < self._next_poll:
            return
        self._next_poll = now + KILL_POLL_SEC
        for name in (KILL_FILE, f"{KILL_FILE}_{self.seat}"):
            if (self.kill_dir / name).exists():
                self.trip(f"human ({name})")
                return

    def on_disconnect(self, link: str = "") -> None:
        """An established connection dropped. A link that keeps dropping
        cannot manage its orders, so MAX_DROPS within the window trips."""
        now = self.clock()
        drops = self._drops.setdefault(link, deque())
        drops.append(now)
        while drops and now - drops[0] > self.drop_window_sec:
            drops.popleft()
        if len(drops) >= self.max_drops:
            self.trip(f"{len(drops)} disconnects in {self.drop_window_sec:.0f}s"
                      + (f" on {link}" if link else ""))


def kill_switch(seat: str, clock: Callable[[], float]) -> KillSwitch:
    """The seat's switch, looking for the kill file in the repo root."""
    return KillSwitch(seat, clock)


# ── broker: per-position stop and toxicity ──────────────────────────────────

BROKER_SESSION_LOSS = 0.20   # equity this far below the open: reducing orders only
BROKER_KILL_LOSS = 0.25      # ... this far: kill switch, every quote pulled
BROKER_POSITION_STOP = 0.015  # traded inventory this far under water: quote
                              # only the side that reduces it (same 1.5% as
                              # the traders' stop-loss)
TOXIC_WINDOW_SEC = 30.0
TOXIC_QUOTES = 3             # one side filled for this many quote sizes ...
TOXIC_WIDEN = 2.0            # ... widens the quotes by this factor


class TradedCost:
    """Average cost of the inventory traded on top of the opening grant."""

    def __init__(self) -> None:
        self.qty = 0
        self.avg = 0.0

    def fill(self, side: str, qty: int, price: float) -> None:
        signed = qty if side == "buy" else -qty
        new = self.qty + signed
        if self.qty == 0 or (self.qty > 0) == (signed > 0):
            self.avg = (self.avg * abs(self.qty) + price * qty) / abs(new) if new else 0.0
        elif new == 0:
            self.avg = 0.0
        elif (new > 0) != (self.qty > 0):
            self.avg = price            # flipped: the rest opened at this price
        self.qty = new


def position_stop(traded_qty: int, avg_cost: float, mark: float | None,
                  pct: float = BROKER_POSITION_STOP) -> bool:
    """True when the traded inventory is `pct` or more under water at `mark`."""
    if not traded_qty or not avg_cost or not mark:
        return False
    if traded_qty > 0:
        return mark <= avg_cost * (1 - pct)
    return mark >= avg_cost * (1 + pct)


class ToxicityMeter:
    """How one-sided the flow against our quotes has been, per symbol.

    Informed flow hits one side repeatedly (Session 3: 110 NVDA sold into our
    bids in 3 s). When the fills on one side over TOXIC_WINDOW_SEC reach
    TOXIC_QUOTES quote sizes, the quotes widen by TOXIC_WIDEN.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._fills: dict[str, deque] = {}

    def fill(self, symbol: str, side: str, qty: int) -> None:
        self._fills.setdefault(symbol, deque()).append((self._clock(), side, qty))

    def factor(self, symbol: str, quote_size: int) -> float:
        fills = self._fills.get(symbol)
        if not fills:
            return 1.0
        now = self._clock()
        while fills and now - fills[0][0] > TOXIC_WINDOW_SEC:
            fills.popleft()
        buys = sum(q for _, s, q in fills if s == "buy")
        sells = sum(q for _, s, q in fills if s == "sell")
        return TOXIC_WIDEN if max(buys, sells) >= TOXIC_QUOTES * quote_size else 1.0
