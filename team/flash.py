"""
Midway Traders — trading the week-7 flashes.

A flash is an unannounced 1.5-2.5% jump in one symbol on one venue that walks
back to where it was within `revert_ticks` (about 2 s). The venue announces
it only as it happens, with a FLASH session event: data = symbol, pct,
revert_ticks, tick. Week 7 grades an order on the flashed symbol, on that
venue, within 5 s of the event, for at least a quarter of the flashes.

The trade bets on the walk back. On a jump up the seat offers shares half way
into the jump (pre-flash mid × (1 + pct / 2)); on a drop it bids half way
down. Both rest as maker orders, so a fill earns the rebate and pays no taker
fee; the entry only takes liquidity when the book has already moved past that
level by enough to pay the taker fee and still clear MIN_TAKE_EDGE. A filled
entry is closed by a maker order at the pre-flash mid, where the price
returns to. The entry is cancelled once the flash should be over and the
exit after EXIT_SEC, after which the shares are managed like any others.

While a flash plays out (QUIET_SEC), the seat's strategy, starter and
stop-loss leave the name alone: a 2% jump would otherwise read as a trend to
the momentum seat, and the 1.5% stop-loss would sell the bottom of a drop
that is about to reverse.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from team.events import EXCHANGE_TICK_SEC

FLASH_SHARES = 10
ENTRY_FRACTION = 0.5        # rest the entry half way into the jump
MIN_TAKE_EDGE = 0.002       # take only if the move pays both fees with this left
REACT_DEADLINE_SEC = 5.0    # the graded window
DEFAULT_REVERT_TICKS = 2
QUIET_SEC = 10.0            # strategy, starter and stop-loss stay out this long
EXIT_SEC = 30.0             # an unfilled exit is cancelled after this
TICK = 0.01


def parse_flash(data: dict) -> tuple[str, float, int] | None:
    """(symbol, pct as a fraction, revert_ticks) from a FLASH event, or None.

    pct is read as a fraction (0.02 = 2%, the unit CALENDAR_EVENT uses); a
    value above 0.5 can only be percent units and is divided by 100.
    """
    symbol = data.get("symbol")
    try:
        pct = float(data.get("pct") or 0.0)
    except (TypeError, ValueError):
        return None
    if not symbol or not pct:
        return None
    if abs(pct) > 0.5:
        pct /= 100.0
    try:
        revert = int(data.get("revert_ticks") or DEFAULT_REVERT_TICKS)
    except (TypeError, ValueError):
        revert = DEFAULT_REVERT_TICKS
    return symbol, pct, max(revert, 1)


def start_price(data: dict) -> float | None:
    """The fair value before the flash, which it walks back to: the venue
    sends it as `start_price` (course engine, Nov 12). None if absent."""
    try:
        price = float(data.get("start_price") or 0.0)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _up(price: float) -> float:
    return round(math.ceil(price / TICK - 1e-6) * TICK, 2)


def _down(price: float) -> float:
    return round(math.floor(price / TICK + 1e-6) * TICK, 2)


def entry_order(pct: float, pre: float, bid: float | None, ask: float | None,
                taker: float, rebate: float) -> tuple[str, float, bool]:
    """(side, price, takes liquidity) for a flash entry.

    The level is pre × (1 ± |pct| × ENTRY_FRACTION). If the book already
    trades through it by enough to pay the taker fee on the way in (the exit
    rests at the pre-flash mid and earns the rebate), the entry takes the
    touch; otherwise it rests at the level, never crossing the book.
    """
    move = abs(pct) * ENTRY_FRACTION
    if pct > 0:                                   # jump up: sell into it
        level = _up(pre * (1 + move))
        if bid and bid >= level and bid / pre - 1 - taker + rebate >= MIN_TAKE_EDGE:
            return "sell", bid, True
        price = level
        if bid:
            price = max(price, _up(bid + TICK))
        if ask and bid and bid >= level:
            price = max(price, ask)
        return "sell", price, False
    level = _down(pre * (1 - move))                # drop: buy into it
    if ask and ask <= level and 1 - ask / pre - taker + rebate >= MIN_TAKE_EDGE:
        return "buy", ask, True
    price = level
    if ask:
        price = min(price, _down(ask - TICK))
    if bid and ask and ask <= level:
        price = min(price, bid)
    return "buy", price, False


def exit_price(side: str, pre: float, bid: float | None, ask: float | None) -> float:
    """A maker exit at the pre-flash mid, kept off the other side of the book."""
    if side == "buy":
        price = _down(pre)
        return min(price, _down(ask - TICK)) if ask else price
    price = _up(pre)
    return max(price, _up(bid + TICK)) if bid else price


@dataclass
class FlashPlan:
    """One flash this seat is trading."""

    symbol: str
    pct: float
    pre: float                  # mid before the jump: where it walks back to
    t0: float                   # when the FLASH event arrived (bot clock)
    revert_ticks: int
    entry_sent_at: float | None = None
    filled: int = 0             # entry shares executed
    exited: int = 0             # exit shares executed
    done: bool = False

    @property
    def side(self) -> str:
        return "sell" if self.pct > 0 else "buy"

    @property
    def exit_side(self) -> str:
        return "buy" if self.pct > 0 else "sell"

    @property
    def entry_until(self) -> float:
        """When the flash should be over: the entry stops being worth having."""
        return self.t0 + (self.revert_ticks + 1) * EXCHANGE_TICK_SEC

    @property
    def exit_until(self) -> float:
        return self.t0 + EXIT_SEC

    @property
    def open_shares(self) -> int:
        return self.filled - self.exited


class FlashLog:
    """Flashes seen and how fast this seat put an order on each."""

    def __init__(self) -> None:
        self.seen = 0
        self.reactions: list[float] = []    # seconds from FLASH to the order
        self.missed: list[str] = []

    def reacted(self, seconds: float) -> None:
        self.reactions.append(seconds)

    def report(self) -> str:
        n = len(self.reactions)
        in_time = sum(r <= REACT_DEADLINE_SEC for r in self.reactions)
        if not n:
            return f"{self.seen} flashes seen, no orders sent on them"
        s = sorted(self.reactions)
        median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        return (f"{self.seen} flashes seen, an order on {n} ({in_time} within "
                f"{REACT_DEADLINE_SEC:.0f} s, graded: at least a quarter); "
                f"reaction median {median * 1000:.0f} ms, slowest {s[-1] * 1000:.0f} ms")
