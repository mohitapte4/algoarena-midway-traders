"""
Midway Traders — choosing a venue for an order (week 8).

The same stock trades on several venues, each with its own book and its own
fees. An order goes to the venue where it is worth most after fees:

    taker buy    lowest   ask × (1 + taker fee)
    taker sell   highest  bid × (1 − taker fee)
    maker        highest  rebate − gap, where gap is how far the venue's
                 touch sits behind the national best on that side

A resting order behind the national best waits behind every order at the
better price, so a venue's rebate must beat the best-priced venue's by more
than that gap to win the order. A venue that charges makers has a negative
rebate and loses to any venue at the same price that does not.

A venue whose mid is more than MAX_VENUE_GAP from the home venue's is left
out: in open play team venues have sat 5-50% away from the house, and that
far apart they are not one market to route across.

Pure: quotes and fees in, a choice out.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_VENUE_GAP = 0.02      # a venue this far from the home mid is not compared


@dataclass(frozen=True)
class VenueQuote:
    venue: str
    bid: float
    ask: float
    taker: float          # fee on notional, a fraction
    rebate: float         # paid to the maker; negative when makers are charged

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


def coherent(quotes: list[VenueQuote]) -> list[VenueQuote]:
    """The quotes within MAX_VENUE_GAP of the first (home) venue's mid."""
    if not quotes:
        return []
    home = quotes[0].mid
    return [q for q in quotes if abs(q.mid / home - 1.0) <= MAX_VENUE_GAP]


def score(side: str, taker: bool, quote: VenueQuote, best_bid: float,
          best_ask: float) -> float:
    """What sending the order to this venue is worth, as a fraction of price
    relative to the national best. Higher is better."""
    if taker:
        if side == "buy":
            return 1.0 - quote.ask * (1 + quote.taker) / best_ask
        return quote.bid * (1 - quote.taker) / best_bid - 1.0
    gap = ((best_bid - quote.bid) / best_bid if side == "buy"
           else (quote.ask - best_ask) / best_ask)
    return quote.rebate - gap


def choose(side: str, taker: bool, quotes: list[VenueQuote]) -> VenueQuote | None:
    """The venue to send to; `quotes[0]` is the home venue and wins ties."""
    usable = coherent(quotes)
    if not usable:
        return None
    best_bid = max(q.bid for q in usable)
    best_ask = min(q.ask for q in usable)
    return max(usable, key=lambda q: score(side, taker, q, best_bid, best_ask))


def explain(side: str, taker: bool, chosen: VenueQuote,
            quotes: list[VenueQuote]) -> str:
    """One log line: each venue's touch and fee, and which one won."""
    def part(q: VenueQuote) -> str:
        touch = q.bid if (side == "buy") != taker else q.ask
        fee = f"taker {q.taker * 1e4:.0f} bp" if taker else f"rebate {q.rebate * 1e4:+.0f} bp"
        return f"{q.venue} {touch:.2f} {fee}"
    left_out = len(quotes) - len(coherent(quotes))
    tail = f"; {left_out} venue(s) over {MAX_VENUE_GAP:.0%} from home left out" if left_out else ""
    return f"-> {chosen.venue} ({' | '.join(part(q) for q in quotes)}{tail})"


class Router:
    """Asks each of this seat's venue connections for a quote, then chooses.

    A seat is anything with `route_quote(symbol, side) -> VenueQuote | None`
    (None: that venue cannot take the order now). The first seat is home.
    """

    def __init__(self, seats: list) -> None:
        self.seats = seats

    def choose(self, symbol: str, side: str, taker: bool = False):
        """(seat, explanation) for the chosen venue, or (None, "").

        If the home venue cannot take the order, the first venue that can
        stands in as home for the coherence check.
        """
        quotes, seats = [], {}
        for seat in self.seats:
            quote = seat.route_quote(symbol, side)
            if quote is not None:
                quotes.append(quote)
                seats[quote.venue] = seat
        chosen = choose(side, taker, quotes)
        if chosen is None:
            return None, ""
        return seats[chosen.venue], explain(side, taker, chosen, quotes)
