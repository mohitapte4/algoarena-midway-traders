"""
Midway Traders — measuring the hot path and the message budget.

LatencyMeter
    How long a piece of code takes, in a fixed-size ring of the most recent
    samples. Adding a sample is O(1); the percentiles are computed only when
    a report is logged (once a minute), off the hot path.

MessageMeter
    Orders and cancels this seat sent, the venue's RATE_LIMITED answers, and
    the cancellation-fee bill. Week 7 grades quota rejections (under 5% of
    messages) and asks each team to know its cancel bill: the venue charges
    CANCEL_FEE_PER_ORDER for every cancel that removes a resting order
    (engine/exchange/server.py, _charge_cancel_fee), so a cancel that loses
    the race with a fill (ORDER_NOT_FOUND) is not billed.
"""

from __future__ import annotations

from collections import deque

CANCEL_FEE_PER_ORDER = 0.05   # engine/exchange/config.py default; per share: 0
REPORT_EVERY_SEC = 60.0


class LatencyMeter:
    """Durations in seconds; reports mean, p50, p99 and max in microseconds."""

    def __init__(self, name: str, size: int = 4096) -> None:
        self.name = name
        self._recent: deque[float] = deque(maxlen=size)
        self.count = 0
        self.total = 0.0
        self.worst = 0.0

    def add(self, seconds: float) -> None:
        self._recent.append(seconds)
        self.count += 1
        self.total += seconds
        if seconds > self.worst:
            self.worst = seconds

    def percentile(self, q: float) -> float | None:
        if not self._recent:
            return None
        s = sorted(self._recent)
        return s[min(len(s) - 1, max(0, int(round(q / 100 * len(s))) - 1))]

    def report(self) -> str:
        if not self.count:
            return f"{self.name}: no samples"
        return (f"{self.name}: {self.count} calls, mean {self.total / self.count * 1e6:.0f} us, "
                f"p50 {self.percentile(50) * 1e6:.0f} us, p99 {self.percentile(99) * 1e6:.0f} us, "
                f"max {self.worst * 1e6:.0f} us")


class MessageMeter:
    """Order and cancel traffic for one seat on one venue."""

    def __init__(self, cancel_fee: float = CANCEL_FEE_PER_ORDER) -> None:
        self.cancel_fee = cancel_fee
        self.orders = 0
        self.cancels = 0
        self.rate_limited = 0
        self.cancel_not_found = 0

    def order(self) -> None:
        self.orders += 1

    def cancel(self) -> None:
        self.cancels += 1

    def limited(self) -> None:
        self.rate_limited += 1

    def not_found(self) -> None:
        self.cancel_not_found += 1

    @property
    def messages(self) -> int:
        return self.orders + self.cancels

    @property
    def limited_share(self) -> float:
        return self.rate_limited / self.messages if self.messages else 0.0

    @property
    def cancel_bill(self) -> float:
        """Every cancel sent is billed (Ed #102), found or not."""
        return self.cancels * self.cancel_fee

    def report(self) -> str:
        return (f"{self.orders} orders, {self.cancels} cancels, "
                f"{self.rate_limited} RATE_LIMITED ({self.limited_share:.1%} of messages, "
                f"graded limit 5%); cancel fees {self.cancels} cancels x "
                f"${self.cancel_fee:.2f} = ${self.cancel_bill:.2f} "
                f"({self.cancel_not_found} of them lost the race to a fill)")
