"""
Midway Traders — the week-8 lead-lag links.

The venue links three pairs of stocks: `lag` ticks after a leader's mark
moves by r, the follower's mark moves by beta × r for one tick, then walks
back on the next. The pairs below were found in the week-8 history
(scripts/find_links.py, 1,000 sessions, scheduled events and halts removed):

    leader  follower  lag  beta    corr at the lag
    AMD     GOOGL      6   +0.36   +0.62
    NFLX    AMZN       8   -0.30   -0.33
    MSFT    TSLA       4   -0.35   -0.17

The next candidates are the same three pairs read backwards (AMZN -> NFLX
-0.11, GOOGL -> AMD +0.06, TSLA -> MSFT -0.06): each leader's own returns
alternate from tick to tick, and the echo carries that pattern. The venue
never makes a stock both a leader and a follower, so they are not links.
Every other pair is under 0.01.

The echo is real but small. Outside the scheduled events the largest leader
moves are 14-18 bp (AMD) and 3-6 bp (NFLX, MSFT), so the echo is at most
about 7 bp for one tick, against a 16 bp spread in every follower. Taking
both ways lost 16 bp a round trip in the history before any fee. So the seat
rests a small maker order in the follower, in the predicted direction, and
cancels it once the echo tick has passed: it costs a cancel fee if nobody
trades with it, and it is on the right side if somebody does.

A move counts once it reaches the link's threshold, about the leader's 99th
percentile one-tick move, which is 20-40 cues a session per link plus every
tick of an earnings or econ ramp. While a move is still inside its lag, a
smaller move the other way does not flip the follower's direction.

The one place the echo pays is a leader's earnings release. The leader then
moves the same way for a whole ramp (15 ticks), each tick's echo lands before
the last has fully walked back, and the follower drifts to about
0.35 × |beta| × the announced move by the tick the last echo arrives
(ramp + lag − 1), then gives it back within eight ticks. For NFLX -> AMZN
that is 0.105 × the move, 0.68% on the average 6.5% release. Taking AMZN
against NFLX's direction before the first echo and taking the other way on
that tick made +0.39% a share net of a 16 bp spread and 16 bp of fees, in 89%
of the 1,000 history sessions; leaving eight ticks later lost 0.16%.
EchoPlan is that trade. It is skipped when the expected drift does not clear
the spread and fees on the venue by EDGE_MARGIN.

Everything here is pure: marks and times in, cues out.
"""

from __future__ import annotations

from dataclasses import dataclass

TICK_SEC = 1.0            # the venue marks each name once a second
STALE_TICKS = 2.5         # a mark older than this is a gap, not a one-tick move
SEND_MARGIN_SEC = 1.0     # stop sending this long before the echo is due
ECHO_DRIFT = 0.35         # follower drift at the last echo, per |beta| × move
ECHO_EXIT_LEAD_SEC = 0.3  # leave this early: the drift peaks for one tick
ECHO_GIVE_UP_SEC = 8.0    # past the exit time the drift is gone: stop chasing


@dataclass(frozen=True)
class Link:
    leader: str
    follower: str
    lag: int                  # ticks from the leader's move to the echo
    beta: float               # signed: the follower moves beta × the leader's move
    threshold: float          # smallest leader move (a fraction) worth a cue


LINKS: tuple[Link, ...] = (
    Link("AMD", "GOOGL", lag=6, beta=+0.36, threshold=0.0010),
    Link("NFLX", "AMZN", lag=8, beta=-0.30, threshold=0.0004),
    Link("MSFT", "TSLA", lag=4, beta=-0.35, threshold=0.0003),
)


@dataclass
class Cue:
    """One leader move and the follower order it calls for."""

    link: Link
    move: float               # the leader's one-tick return
    at: float                 # when the move was seen
    sent: bool = False

    @property
    def side(self) -> str:
        return "buy" if self.move * self.link.beta > 0 else "sell"

    @property
    def echo_at(self) -> float:
        return self.at + self.link.lag * TICK_SEC

    @property
    def expected(self) -> float:
        """The follower's expected one-tick move, signed."""
        return self.move * self.link.beta


class LinkTracker:
    """Turns leader marks into follower cues. O(1) per mark."""

    def __init__(self, links: tuple[Link, ...] = LINKS) -> None:
        self._by_leader = {link.leader: link for link in links}
        self._last: dict[str, tuple[float, float]] = {}    # leader -> (mark, time)
        self._cues: dict[str, Cue] = {}                     # follower -> latest cue

    def on_mark(self, symbol: str, mark: float, now: float) -> Cue | None:
        """Take a venue mark; return the cue it raises, if any."""
        link = self._by_leader.get(symbol)
        if link is None or not mark or mark <= 0:
            return None
        last = self._last.get(symbol)
        if last is not None and mark == last[0]:
            return None                   # the same tick, snapshotted again
        self._last[symbol] = (mark, now)
        if last is None or now - last[1] > STALE_TICKS * TICK_SEC:
            return None
        move = mark / last[0] - 1.0
        if abs(move) < link.threshold:
            return None
        current = self._cues.get(link.follower)
        if (current is not None and now < current.echo_at
                and move * current.move < 0 and abs(move) < abs(current.move)):
            return None
        cue = self._cues[link.follower] = Cue(link, move, now)
        return cue

    def pending(self, now: float) -> list[Cue]:
        """Cues not yet acted on that can still be sent before their echo."""
        return [cue for cue in self._cues.values()
                if not cue.sent and now <= cue.echo_at - SEND_MARGIN_SEC]

    def reset(self) -> None:
        """Forget marks and cues: after a reconnect the last mark is old."""
        self._last.clear()
        self._cues.clear()


@dataclass
class EchoPlan:
    """A follower trade through its leader's earnings ramp."""

    link: Link
    pct: float                # the leader's announced move, signed
    t0: float                 # when the release arrived
    ramp_ticks: int
    filled: int = 0
    exited: int = 0
    entry_sent: bool = False
    done: bool = False

    @property
    def side(self) -> str:
        return "buy" if self.pct * self.link.beta > 0 else "sell"

    @property
    def exit_side(self) -> str:
        return "sell" if self.side == "buy" else "buy"

    @property
    def expected(self) -> float:
        """The follower's drift by the exit, as a fraction of its price."""
        return ECHO_DRIFT * abs(self.link.beta) * abs(self.pct)

    @property
    def enter_until(self) -> float:
        """The first echo lands `lag` ticks after the release."""
        return self.t0 + (self.link.lag - 1) * TICK_SEC

    @property
    def exit_at(self) -> float:
        """The tick the last ramp tick's echo arrives."""
        return (self.t0 + (self.ramp_ticks + self.link.lag - 1) * TICK_SEC
                - ECHO_EXIT_LEAD_SEC)

    @property
    def give_up(self) -> float:
        return self.exit_at + ECHO_GIVE_UP_SEC

    @property
    def open_shares(self) -> int:
        return self.filled - self.exited

    def worth_it(self, spread: float, taker: float, margin: float) -> bool:
        """Does the drift clear one spread and two taker fees by `margin`?"""
        return self.expected >= margin * (spread + 2 * taker)


def echo_plans(affected: list[str], pct: float, ramp_ticks: int, now: float,
               links: tuple[Link, ...] = LINKS) -> list[EchoPlan]:
    """Plans for a release that moves a leader but not its follower.

    A market-wide print moves the follower itself, far more than the echo.
    """
    return [EchoPlan(link, pct, now, ramp_ticks) for link in links
            if link.leader in affected and link.follower not in affected]
