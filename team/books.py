"""
Midway Traders — each seat's own books, kept apart from the venue's.

The risk code trades on the venue's positions (BaseTrader._sync_ledger copies
them in), so it says nothing about whether the seat can keep its own books.
These books are built from the seat's own messages only:

    fills        TradeExecution: price, quantity, and our side's fee or
                 rebate (we are the taker iff the aggressor is our side)
    IPO pricing  IPO_PRICED: the offer price. No message carries the
                 allocated quantity, so it is read off the venue's position.
    dividends    DIVIDEND: amount per share × our own position
    carry        total_carry_paid: margin interest and borrow fees, taken
                 from the venue as a labelled item

Books mirrors the venue's Portfolio (engine/exchange/server.py): cash moves by
price × quantity ± fee ∓ rebate, positions keep a signed average cost, and
the part of a fill that closes a position realises (price − entry) × closed.
VenueState is the venue's view, parsed once from a PortfolioUpdate or a
leaderboard row; nothing past the parse touches raw message fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from shared.messages import Leaderboard, PortfolioUpdate


@dataclass(frozen=True)
class VenueState:
    """The venue's figures for one seat. None: the source did not report it."""

    source: str
    cash: float | None
    positions: dict[str, int] | None
    realized: float | None
    fees: float | None
    rebates: float | None
    carry: float | None
    net_worth: float | None
    avg_cost: dict[str, float] | None = None    # leaderboard only
    tick: int | None = None                     # leaderboard only

    @property
    def complete(self) -> bool:
        """Reports everything books can be seeded from."""
        return None not in (self.cash, self.positions, self.realized,
                            self.fees, self.rebates, self.carry)


def venue_from_update(msg: PortfolioUpdate) -> VenueState:
    return VenueState(
        source="portfolio update",
        cash=msg.cash,
        positions={s: q for s, q in msg.positions.items() if q},
        realized=msg.realized_pnl,
        fees=msg.total_fees_paid,
        rebates=msg.total_rebates_earned,
        carry=msg.total_carry_paid,
        net_worth=msg.net_worth,
    )


def venue_from_leaderboard(msg: Leaderboard, seat: str) -> VenueState | None:
    """The seat's leaderboard row, or None if the seat is not on the board.

    Money on the leaderboard is rounded to cents, and a hosted venue may leave
    fields out; a missing or unreadable field is None.
    """
    row = next((r for r in (*msg.traders, *msg.brokers)
                if r.get("team_id") == seat), None)
    if row is None:
        return None
    return VenueState(
        source=f"leaderboard t{msg.tick}",
        cash=_number(row, "cash"),
        positions=_table(row, "positions", int),
        realized=_number(row, "realized_pnl"),
        fees=_number(row, "total_fees_paid"),
        rebates=_number(row, "total_rebates_earned"),
        carry=_number(row, "total_carry_paid"),
        net_worth=_number(row, "net_worth"),
        avg_cost=_table(row, "avg_cost", float),
        tick=msg.tick,
    )


def _number(row: dict[str, Any], key: str) -> float | None:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def _table(row: dict[str, Any], key: str, kind: Callable) -> dict | None:
    raw = row.get(key)
    if not isinstance(raw, dict):
        return None
    try:
        return {str(s): kind(v) for s, v in raw.items() if v}
    except (TypeError, ValueError):
        return None


@dataclass
class Books:
    cash: float
    positions: dict[str, int]
    avg_cost: dict[str, float]
    realized: float
    fees: float
    rebates: float
    carry_base: float           # the venue's total_carry_paid at the seed
    seed_cash: float
    # Held symbols whose entry price the books have not learned: a seed from
    # a PortfolioUpdate carries no avg_cost. A later leaderboard can supply it.
    unpriced: set[str] = field(default_factory=set)
    # False once a fill lands on an unpriced position: realized is then
    # measured from a guessed entry and cannot be compared.
    entry_known: bool = True
    anchor_tick: int | None = None          # first leaderboard on or after the seed
    anchor_net_worth: float | None = None
    fills: int = 0
    rebases: int = 0

    @classmethod
    def seed(cls, venue: VenueState, *, fills: int = 0,
             rebases: int = 0) -> Books:
        """Books that start where the venue is. `venue` must be complete."""
        positions = dict(venue.positions)
        avg_cost = {s: c for s, c in (venue.avg_cost or {}).items()
                    if s in positions}
        books = cls(cash=venue.cash, positions=positions, avg_cost=avg_cost,
                    realized=venue.realized, fees=venue.fees,
                    rebates=venue.rebates, carry_base=venue.carry,
                    seed_cash=venue.cash,
                    unpriced=set(positions) - set(avg_cost),
                    fills=fills, rebases=rebases)
        if venue.tick is not None:
            books.anchor(venue)
        return books

    def anchor(self, venue: VenueState) -> None:
        """Measure elapsed ticks and P&L from this leaderboard: the close is
        a leaderboard too, so both ends mark positions the same way."""
        self.anchor_tick = venue.tick
        self.anchor_net_worth = venue.net_worth

    def apply_fill(self, side: str, symbol: str, qty: int, price: float,
                   fee: float, rebate: float) -> None:
        signed = qty if side == "buy" else -qty
        self.cash -= signed * price + fee - rebate
        self.fees += fee
        self.rebates += rebate
        self.fills += 1

        if symbol in self.unpriced:
            self.unpriced.discard(symbol)
            self.entry_known = False
        old = self.positions.get(symbol, 0)
        entry = self.avg_cost.get(symbol, price)
        if old == 0 or (old > 0) == (signed > 0):
            self.avg_cost[symbol] = (abs(old) * entry + qty * price) / (abs(old) + qty)
        else:
            closed = min(abs(old), qty)
            self.realized += (price - entry) * closed * (1 if old > 0 else -1)
            if qty > abs(old):
                self.avg_cost[symbol] = price
        self._set_position(symbol, old + signed)

    def apply_ipo(self, symbol: str, qty: int, offer: float) -> None:
        """The venue sets the entry to the offer, whatever was held before."""
        self.cash -= qty * offer
        self._set_position(symbol, self.positions.get(symbol, 0) + qty)
        self.avg_cost[symbol] = offer
        self.unpriced.discard(symbol)

    def apply_dividend(self, symbol: str, amount_per_share: float) -> None:
        """Longs are paid, shorts pay."""
        self.cash += self.positions.get(symbol, 0) * amount_per_share

    def learn_entries(self, venue: VenueState) -> list[str]:
        """Take the venue's entry price for unpriced symbols whose position
        agrees with ours. Returns the symbols priced."""
        priced = [s for s in sorted(self.unpriced)
                  if s in (venue.avg_cost or {})
                  and (venue.positions or {}).get(s) == self.positions.get(s)]
        for s in priced:
            self.avg_cost[s] = venue.avg_cost[s]
            self.unpriced.discard(s)
        return priced

    def _set_position(self, symbol: str, qty: int) -> None:
        if qty:
            self.positions[symbol] = qty
        else:
            self.positions.pop(symbol, None)
            self.avg_cost.pop(symbol, None)
