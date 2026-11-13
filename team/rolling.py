"""
Midway Traders — rolling statistics for the hot path.

Week 7 sends a book snapshot every 0.2 s per symbol. A strategy that rebuilds
its averages from the whole price history on every tick does O(window) work
per symbol per tick; these classes update in O(1) per new price instead.

RollingWindow
    Mean and population standard deviation of the last `size` values.
    Running sums are kept relative to a reference value K (sum of x - K and
    of (x - K)^2), which keeps the numbers small when a $500 stock moves by
    cents. Adding and subtracting floats slowly accumulates rounding error,
    so every RESYNC pushes the sums are recomputed exactly from the window:
    O(size) once per RESYNC pushes, which is O(1) amortized.

RollingRange
    Max and min of the last `size` values with two monotonic deques. Each
    value enters and leaves each deque once, so a push is O(1) amortized.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable


class RollingWindow:
    """Mean and pstdev of the last `size` values, O(1) per push."""

    RESYNC = 1000

    def __init__(self, size: int, values: Iterable[float] = ()) -> None:
        if size < 1:
            raise ValueError("size must be at least 1")
        self.size = size
        self._q: deque[float] = deque()
        self._k = 0.0
        self._s = 0.0          # sum of (x - k)
        self._ss = 0.0         # sum of (x - k)^2
        self._since = 0
        for x in values:
            self.push(x)

    def push(self, x: float) -> None:
        if not self._q:
            self._k = x
        d = x - self._k
        self._q.append(x)
        self._s += d
        self._ss += d * d
        if len(self._q) > self.size:
            old = self._q.popleft() - self._k
            self._s -= old
            self._ss -= old * old
        self._since += 1
        if self._since >= self.RESYNC:
            self._resync()

    def _resync(self) -> None:
        self._since = 0
        self._k = self._q[-1]
        self._s = sum(x - self._k for x in self._q)
        self._ss = sum((x - self._k) ** 2 for x in self._q)

    def __len__(self) -> int:
        return len(self._q)

    @property
    def full(self) -> bool:
        return len(self._q) == self.size

    @property
    def last(self) -> float | None:
        return self._q[-1] if self._q else None

    @property
    def mean(self) -> float | None:
        n = len(self._q)
        return self._k + self._s / n if n else None

    @property
    def pstdev(self) -> float | None:
        n = len(self._q)
        if not n:
            return None
        m = self._s / n
        return math.sqrt(max(self._ss / n - m * m, 0.0))


class RollingRange:
    """Max and min of the last `size` values, O(1) amortized per push."""

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError("size must be at least 1")
        self.size = size
        self._i = 0                                   # index of the next push
        self._max: deque[tuple[int, float]] = deque()  # decreasing values
        self._min: deque[tuple[int, float]] = deque()  # increasing values

    def push(self, x: float) -> None:
        i = self._i
        self._i += 1
        while self._max and self._max[-1][1] <= x:
            self._max.pop()
        self._max.append((i, x))
        while self._min and self._min[-1][1] >= x:
            self._min.pop()
        self._min.append((i, x))
        oldest = i - self.size + 1
        if self._max[0][0] < oldest:
            self._max.popleft()
        if self._min[0][0] < oldest:
            self._min.popleft()

    def __len__(self) -> int:
        return min(self._i, self.size)

    @property
    def high(self) -> float | None:
        return self._max[0][1] if self._max else None

    @property
    def low(self) -> float | None:
        return self._min[0][1] if self._min else None


class Sampler:
    """One sample per `period` seconds per key, on average, from a faster feed.

    The strategy windows count samples, and their sizes were chosen on tapes
    with a book snapshot every 0.5 s. Week 7 sends one every 0.2 s, which
    would make the same window 2.5 times shorter in time (10/40 would trade
    like the 4/16 that lost on the Week 5 tape). Sampling keeps the horizon.
    """

    def __init__(self, period: float) -> None:
        self.period = period
        self._due: dict[str, float] = {}

    def due(self, key: str, now: float) -> bool:
        """True (and the slot is taken) if `key` may be sampled at `now`."""
        due = self._due.get(key)
        if due is not None and now < due:
            return False
        late = due is None or now >= due + self.period
        self._due[key] = now + self.period if late else due + self.period
        return True
