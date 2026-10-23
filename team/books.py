"""
Midway Traders — each seat's own books, kept apart from the venue's.

The risk code trades on the venue's positions (BaseTrader._sync_ledger copies
them in), so it says nothing about whether the seat can keep its own books.
These books are built from the seat's own messages only:

    fills        TradeExecution: price, quantity, and our side's fee or
                 rebate (we are the taker iff the aggressor is our side)
    futures      BookSnapshot.asset_type "future": a fill on one moves no
                 notional, only the fee or rebate; FUTURES_SETTLEMENT: the
                 variation against each mark is paid in cash and realised
    IPO pricing  IPO_PRICED: the offer price. No message carries the
                 allocated quantity, so it is read off the venue's position.
    dividends    DIVIDEND: amount per share × our own position
    liquidation  LIQUIDATION of this seat: no fills are sent, so the
                 close-out is read off the venue's update that follows it
    grants       SESSION_PREOPEN / SESSION_OPEN: the starting shares, read
                 off the venue's position like an IPO allocation
    upgrades     UPGRADE_PURCHASED: this seat's share of the price
    carry        total_carry_paid: margin interest and borrow fees, taken
                 from the venue as a labelled item

Interest on idle cash is in no message, so it is what the cash comparison
leaves over: it must not be negative, and must be near rate × cash × ticks.

Books mirrors the venue's Portfolio (engine/exchange/server.py): cash moves by
price × quantity ± fee ∓ rebate, positions keep a signed average cost, and
the part of a fill that closes a position realises (price − entry) × closed.
VenueState is the venue's view, parsed once from a PortfolioUpdate or a
leaderboard row; nothing past the parse touches raw message fields.

Bookkeeper runs one seat's books off its message stream and logs a RECON
when the venue first answers a reconnect and at the close, against the
first leaderboard after SESSION_CLOSED. A reconnect that finds a BREAK
re-seeds the books from the venue and counts a rebase, which the close
reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from shared.messages import (BookSnapshot, Leaderboard, PortfolioUpdate,
                             SessionEvent, TradeExecution)

from team.config import CASH_INTEREST_PER_TICK

logger = logging.getLogger(__name__)

MONEY_TOL = 0.01         # leaderboard money is rounded to cents
INTEREST_FLOOR = 1.0     # the interest estimate may be off by $1...
INTEREST_SLACK = 0.5     # ...or by half of itself, whichever is more
MARK_ROUNDING = 0.00005  # FUTURES_SETTLEMENT marks are rounded to 4 dp


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


@dataclass(frozen=True)
class Liquidation:
    tick: int | None            # the last leaderboard tick before it
    cash_before: float
    proceeds: float


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
    # a PortfolioUpdate carries no avg_cost, nor does a starting grant. A
    # later leaderboard can supply it.
    unpriced: set[str] = field(default_factory=set)
    # False once a fill lands on an unpriced position: realized is then
    # measured from a guessed entry and cannot be compared.
    entry_known: bool = True
    # Futures whose entry is a settlement mark, which the venue rounds.
    marked: set[str] = field(default_factory=set)
    liquidation: Liquidation | None = None
    anchor_tick: int | None = None          # first leaderboard on or after the seed
    # (tick, venue net worth) P&L is measured from. A starting grant moves
    # net worth without being P&L, so it restarts the measure.
    pnl_anchor: tuple[int, float] | None = None
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
        """Measure elapsed ticks and P&L from this leaderboard unless already
        measuring: the close is a leaderboard too, so both ends mark
        positions the same way."""
        if self.anchor_tick is None:
            self.anchor_tick = venue.tick
        if self.pnl_anchor is None and venue.net_worth is not None:
            self.pnl_anchor = (venue.tick, venue.net_worth)

    def apply_fill(self, side: str, symbol: str, qty: int, price: float,
                   fee: float, rebate: float, *, future: bool = False) -> None:
        """A future posts margin, not the notional: only the fee or rebate
        moves cash."""
        signed = qty if side == "buy" else -qty
        self.cash -= (0.0 if future else signed * price) + fee - rebate
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

    def apply_grant(self, symbol: str, qty: int) -> None:
        """Starting shares cost nothing. The venue enters a new holding at its
        reference price, which no message gives, and keeps the entry of one
        already held."""
        if symbol not in self.avg_cost:
            self.unpriced.add(symbol)
        self._set_position(symbol, self.positions.get(symbol, 0) + qty)
        self.pnl_anchor = None

    def apply_dividend(self, symbol: str, amount_per_share: float) -> None:
        """Longs are paid, shorts pay."""
        self.cash += self.positions.get(symbol, 0) * amount_per_share

    def apply_charge(self, amount: float) -> None:
        self.cash -= amount

    def settle_future(self, symbol: str, mark: float) -> None:
        """Variation margin: the gain or loss since the entry is paid in cash
        and realised, and the entry resets to the mark."""
        qty = self.positions.get(symbol, 0)
        if not qty:
            return
        if symbol in self.unpriced:
            self.unpriced.discard(symbol)
            self.entry_known = False
        variation = (mark - self.avg_cost.get(symbol, mark)) * qty
        self.cash += variation
        self.realized += variation
        self.avg_cost[symbol] = mark
        self.marked.add(symbol)

    @property
    def mark_slack(self) -> float:
        """How far rounded marks can leave cash and realized from the
        venue's. The error sits in the entry of contracts still held since
        their mark; closing them realises it back out."""
        return MARK_ROUNDING * sum(abs(self.positions.get(s, 0))
                                   for s in self.marked)

    def apply_liquidation(self, realized: float, futures: set[str],
                          tick: int | None) -> float:
        """The venue closes every position at its own price and sends no
        fills. `realized` is what the close realised by the venue's figures;
        cash moves by that plus the entry cost of the shares sold (a future
        has none). Returns the proceeds."""
        if self.unpriced:
            self.entry_known = False
        proceeds = realized + sum(q * self.avg_cost.get(s, 0.0)
                                  for s, q in self.positions.items()
                                  if s not in futures)
        self.liquidation = Liquidation(tick, self.cash, proceeds)
        self.cash += proceeds
        self.realized += realized
        for s in list(self.positions):
            self._set_position(s, 0)
        return proceeds

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
            self.unpriced.discard(symbol)
            self.marked.discard(symbol)


# ── reconciliation ──────────────────────────────────────────────────────────

class Verdict(Enum):
    PASS = "PASS"
    BREAK = "BREAK"
    UNCHECKED = "UNCHECKED"     # the venue reported nothing to compare


@dataclass(frozen=True)
class Row:
    item: str
    own: float | None
    venue: float | None
    ok: bool | None             # None: shown, not judged
    note: str = ""

    @property
    def diff(self) -> float | None:
        if self.own is None or self.venue is None:
            return None
        return self.venue - self.own


@dataclass(frozen=True)
class Recon:
    title: str
    rows: tuple[Row, ...]

    @property
    def verdict(self) -> Verdict:
        judged = [r.ok for r in self.rows if r.ok is not None]
        if not judged:
            return Verdict.UNCHECKED
        return Verdict.PASS if all(judged) else Verdict.BREAK

    def row(self, item: str) -> Row:
        return next(r for r in self.rows if r.item == item)

    def format(self) -> list[str]:
        lines = [f"RECON  {self.title}: {self.verdict.value}",
                 f"RECON    {'item':<20}{'own':>14}{'venue':>14}{'diff':>12}"]
        for r in self.rows:
            mark = {True: "ok", False: "BREAK", None: ""}[r.ok]
            if r.ok is None and r.venue is None:
                mark = "n/a"
            tail = "  ".join(t for t in (mark, r.note) if t)
            lines.append(f"RECON    {r.item:<20}{_fmt(r.own):>14}"
                         f"{_fmt(r.venue):>14}{_fmt(r.diff):>12}  {tail}")
        return lines


def _fmt(v: float | None) -> str:
    if v is None:
        return "n/a"
    return str(v) if isinstance(v, int) else f"{round(v, 2) + 0.0:,.2f}"


def position_rows(books: Books, venue: VenueState) -> list[Row]:
    if venue.positions is None:
        return [Row("positions", len(books.positions), None, None,
                    "not reported")]
    rows = [Row("positions held", len(books.positions), len(venue.positions),
                len(books.positions) == len(venue.positions))]
    for s in sorted(books.positions.keys() | venue.positions.keys()):
        own, theirs = books.positions.get(s, 0), venue.positions.get(s, 0)
        rows.append(Row(f"pos {s}", own, theirs, own == theirs))
    return rows


def pnl_rows(books: Books, venue: VenueState) -> list[Row]:
    rows = [_money_row("fees", books.fees, venue.fees),
            _money_row("rebates", books.rebates, venue.rebates)]
    if books.entry_known:
        rows.append(_money_row("realized", books.realized, venue.realized,
                               books.mark_slack))
    else:
        rows.append(Row("realized", books.realized, venue.realized, None,
                        "n/a: a fill met a position of unknown entry"))
    return rows


def _money_row(item: str, own: float, venue: float | None,
               slack: float = 0.0) -> Row:
    if venue is None:
        return Row(item, own, None, None, "not reported")
    return Row(item, own, venue, abs(venue - own) <= MONEY_TOL + slack + 1e-9)


def cash_rows(books: Books, venue: VenueState,
              interest: float | None) -> list[Row]:
    """Own cash after the venue's carry, against the venue's cash. What is
    left over is interest on idle cash, which no message itemizes."""
    if venue.carry is None or venue.cash is None:
        return [Row("cash", books.cash, venue.cash, None, "not reported")]
    carry = venue.carry - books.carry_base
    own = books.cash - carry
    residual = venue.cash - own
    return [
        Row("carry (venue's)", None, carry, None,
            "margin interest + borrow fees, taken from the venue"),
        Row("cash", own, venue.cash,
            _cash_ok(residual, interest, carry, books.mark_slack),
            "diff is interest on idle cash"),
        Row("interest (est)", interest, residual, None,
            "rate x cash x ticks" if interest is not None else "no tick seen"),
    ]


def _cash_ok(residual: float, interest: float | None, carry: float,
             slack: float) -> bool | None:
    if residual < -(MONEY_TOL + slack):
        return False
    if interest is None:
        return None
    slack = max(INTEREST_FLOOR, INTEREST_SLACK * interest)
    if residual > interest + slack:
        return False
    # The venue pays no interest on a tick it charges carry, so a seat that
    # paid carry may earn well under the estimate.
    return carry > MONEY_TOL or residual >= interest - slack


def liquidation_rows(books: Books) -> list[Row]:
    liq = books.liquidation
    if liq is None:
        return []
    return [Row("liquidated", None, liq.proceeds, None,
                f"after t{liq.tick}: the venue closed every position, "
                f"proceeds from its update")]


def net_worth_rows(books: Books, venue: VenueState) -> list[Row]:
    """Shown, not judged: with positions equal, own net worth differs from
    the venue's by exactly the cash residual."""
    if None in (venue.cash, venue.carry, venue.net_worth):
        return [Row("net worth", None, venue.net_worth, None, "not reported")]
    held = venue.net_worth - venue.cash     # positions at the venue's marks
    own = books.cash - (venue.carry - books.carry_base) + held
    rows = [Row("net worth", own, venue.net_worth, None)]
    if books.pnl_anchor is not None:
        tick, start = books.pnl_anchor
        rows.append(Row(f"P&L since t{tick}", own - start,
                        venue.net_worth - start, None))
    return rows


# ── the seat's state machine ────────────────────────────────────────────────

class Phase(Enum):
    UNSEEDED = "unseeded"
    LIVE = "live"
    RECONNECTING = "reconnecting"   # the venue's next figures are a check
    CLOSING = "closing"             # SESSION_CLOSED seen, awaiting the leaderboard
    CLOSED = "closed"


# The venue sends the PortfolioUpdate these cause BEFORE the event itself.
FOLLOWS_ITS_UPDATE = frozenset({"IPO_PRICED", "DIVIDEND", "SESSION_PREOPEN",
                                "SESSION_OPEN", "UPGRADE_PURCHASED",
                                "FUTURES_SETTLEMENT"})


class Bookkeeper:
    """One seat's books, fed every message the seat receives.

    A PortfolioUpdate is compared only once the next message is in, so the
    event that caused it is booked first. The starting-share grant is booked
    at the open from the update that arrived since the last connect or close.
    """

    def __init__(self, seat: str,
                 interest_rate: float = CASH_INTEREST_PER_TICK) -> None:
        self.seat = seat
        self.interest_rate = interest_rate
        self.phase = Phase.UNSEEDED
        self.books: Books | None = None
        self.recons: list[Recon] = []
        self.tick: int | None = None
        self._update: VenueState | None = None      # the last PortfolioUpdate
        self._unsettled = False
        self._update_since_boundary = False
        self._futures: set[str] = set()
        self._liquidating = False
        self._handlers = {
            BookSnapshot: self._on_snapshot,
            TradeExecution: self._on_trade,
            PortfolioUpdate: self._on_update,
            Leaderboard: self._on_leaderboard,
            SessionEvent: self._on_event,
        }
        self._events = {
            "IPO_PRICED": self._on_ipo,
            "DIVIDEND": self._on_dividend,
            "SESSION_PREOPEN": self._on_open,
            "SESSION_OPEN": self._on_open,
            "UPGRADE_PURCHASED": self._on_upgrade,
            "FUTURES_SETTLEMENT": self._on_futures_settlement,
            "LIQUIDATION": self._on_liquidation,
            "SESSION_CLOSED": self._on_close,
        }

    def on_connect(self) -> None:
        self._settle()
        self._update_since_boundary = False
        if self.phase is Phase.LIVE:
            self.phase = Phase.RECONNECTING

    def on_message(self, msg: Any) -> None:
        follows = (isinstance(msg, SessionEvent)
                   and msg.event in FOLLOWS_ITS_UPDATE)
        if not follows:
            self._settle()
        handler = self._handlers.get(type(msg))
        if handler is not None:
            handler(msg)
        if follows:
            self._settle()

    # ── messages ────────────────────────────────────────────────────────

    def _on_snapshot(self, msg: BookSnapshot) -> None:
        # Only "future" settles as one: crypto, ETFs and the like are
        # not equities either, but trade as shares.
        if msg.asset_type == "future":
            self._futures.add(msg.symbol)

    def _on_trade(self, msg: TradeExecution) -> None:
        if self.books is None:
            return              # the update that follows seeds the books
        for side, who in (("buy", msg.buyer_id), ("sell", msg.seller_id)):
            if who != self.seat:
                continue
            taker = msg.aggressor == side
            self.books.apply_fill(side, msg.symbol, msg.quantity, msg.price,
                                  fee=msg.fee if taker else 0.0,
                                  rebate=0.0 if taker else msg.maker_rebate,
                                  future=msg.symbol in self._futures)

    def _on_update(self, msg: PortfolioUpdate) -> None:
        if msg.team_id != self.seat:
            return
        venue = venue_from_update(msg)
        if self._liquidating:
            self._book_liquidation(venue)
        self._update = venue
        self._unsettled = True
        self._update_since_boundary = True

    def _on_leaderboard(self, msg: Leaderboard) -> None:
        self.tick = msg.tick
        venue = venue_from_leaderboard(msg, self.seat)
        if venue is None:
            return
        if self.books is None:
            self._seed(venue)
            return
        self.books.anchor(venue)
        priced = self.books.learn_entries(venue)
        if priced:
            logger.info("BOOKS  entry prices from the %s: %s", venue.source,
                        ", ".join(f"{s} {self.books.avg_cost[s]:.4f}"
                                  for s in priced))
        if self.phase is Phase.RECONNECTING:
            self._check_reconnect(venue)
        elif self.phase is Phase.CLOSING:
            self._reconcile_close(venue)

    def _on_event(self, msg: SessionEvent) -> None:
        handler = self._events.get(msg.event)
        if handler is not None:
            handler(msg.data or {})

    def _on_ipo(self, data: dict) -> None:
        symbol, offer = data.get("symbol"), data.get("offer_price")
        if self.books is None or self._update is None or not offer:
            return
        qty = (self._update.positions.get(symbol, 0)
               - self.books.positions.get(symbol, 0))
        if qty > 0:
            self.books.apply_ipo(symbol, qty, float(offer))
            logger.info("BOOKS  IPO %s +%d @ %.2f (quantity from the venue's "
                        "position)", symbol, qty, float(offer))

    def _on_dividend(self, data: dict) -> None:
        if self.books is not None and data.get("symbol"):
            self.books.apply_dividend(data["symbol"],
                                      float(data.get("amount_per_share", 0.0)))

    def _on_open(self, data: dict) -> None:
        if (self.books is not None and self._update is not None
                and self._update_since_boundary):
            gaps = {s: q - self.books.positions.get(s, 0)
                    for s, q in sorted(self._update.positions.items())}
            granted = {s: gap for s, gap in gaps.items() if gap > 0}
            for s, gap in granted.items():
                self.books.apply_grant(s, gap)
            if granted:
                logger.info("BOOKS  starting grant %s (quantities from the "
                            "venue's positions)", _positions(granted))
        self._update_since_boundary = False
        if self.phase in (Phase.CLOSING, Phase.CLOSED):
            self.phase = Phase.LIVE

    def _on_upgrade(self, data: dict) -> None:
        share = (data.get("split") or {}).get(self.seat)
        if self.books is not None and share:
            self.books.apply_charge(float(share))
            logger.info("BOOKS  upgrade %s: %.2f charged to this seat",
                        data.get("upgrade", "?"), float(share))

    def _on_futures_settlement(self, data: dict) -> None:
        if self.books is None:
            return
        for symbol, mark in (data.get("marks") or {}).items():
            if mark:
                self.books.settle_future(symbol, float(mark))

    def _on_liquidation(self, data: dict) -> None:
        # The venue sends LIQUIDATION before the update it causes.
        if data.get("team_id") == self.seat and self.books is not None:
            self._liquidating = True

    def _on_close(self, data: dict) -> None:
        if isinstance(data.get("tick"), int):
            self.tick = data["tick"]
        self._update_since_boundary = False
        if self.books is None:
            return
        if self._update is not None:
            self._log(Recon(
                f"{self.seat} positions at SESSION_CLOSED vs the last "
                f"portfolio update", tuple(position_rows(self.books,
                                                         self._update))))
        self.phase = Phase.CLOSING

    # ── transitions ─────────────────────────────────────────────────────

    def _settle(self) -> None:
        if not self._unsettled:
            return
        self._unsettled = False
        if self.phase is Phase.UNSEEDED:
            self._seed(self._update)
        elif self.phase is Phase.RECONNECTING:
            self._check_reconnect(self._update)

    def _seed(self, venue: VenueState) -> None:
        if not venue.complete:
            return
        self.books = Books.seed(venue)
        self.phase = Phase.LIVE
        logger.info("BOOKS  seeded from venue (%s): cash %.2f, positions %s",
                    venue.source, venue.cash, _positions(venue.positions))

    def _book_liquidation(self, after: VenueState) -> None:
        self._liquidating = False
        before = self._update.realized if self._update else self.books.realized
        realized = after.realized - before
        proceeds = self.books.apply_liquidation(realized, self._futures,
                                                self.tick)
        logger.warning("BOOKS  liquidated: proceeds %.2f, realized %.2f (from "
                       "the venue's update; no fills are sent)",
                       proceeds, realized)

    def _check_reconnect(self, venue: VenueState) -> None:
        recon = Recon(f"{self.seat} reconnect check vs {venue.source}",
                      (*position_rows(self.books, venue),
                       *cash_rows(self.books, venue,
                                  self._interest(venue))))
        if recon.verdict is Verdict.UNCHECKED:
            return              # wait for figures that can be compared
        self.phase = Phase.LIVE
        if recon.verdict is Verdict.PASS:
            self.recons.append(recon)
            logger.info("BOOKS  reconnect check PASS: books survived the drop")
            return
        self._log(recon)
        if not venue.complete:
            logger.warning("BOOKS  cannot rebase: %s is incomplete",
                           venue.source)
            return
        old = self.books
        self.books = Books.seed(venue, fills=old.fills,
                                rebases=old.rebases + 1)
        self.books.liquidation = old.liquidation
        # The last update predates the rebase; comparing it would re-report
        # the break just repaired.
        self._update = None
        logger.warning("BOOKS  rebased on the venue (%s): cash %.2f, "
                       "positions %s, rebase %d", venue.source, venue.cash,
                       _positions(venue.positions), self.books.rebases)

    def _reconcile_close(self, venue: VenueState) -> None:
        books = self.books
        self._log(Recon(
            f"{self.seat} close vs {venue.source} ({books.fills} fills, "
            f"{books.rebases} rebases)",
            (*position_rows(books, venue), *pnl_rows(books, venue),
             *cash_rows(books, venue, self._interest(venue)),
             *liquidation_rows(books), *net_worth_rows(books, venue))))
        self.phase = Phase.CLOSED

    def _interest(self, venue: VenueState) -> float | None:
        """rate × the average of cash at the seed and now × ticks since.
        A liquidated seat earns nothing after the close-out."""
        books = self.books
        now = venue.tick if venue.tick is not None else self.tick
        cash_now = books.cash
        if books.liquidation is not None:
            now, cash_now = books.liquidation.tick, books.liquidation.cash_before
        if now is None or books.anchor_tick is None:
            return None
        cash = max(0.0, (books.seed_cash + cash_now) / 2)
        return self.interest_rate * cash * max(0, now - books.anchor_tick)

    def _log(self, recon: Recon) -> None:
        self.recons.append(recon)
        emit = logger.info if recon.verdict is not Verdict.BREAK else logger.warning
        for line in recon.format():
            emit(line)


def _positions(positions: dict[str, int]) -> str:
    return ", ".join(f"{s} {q}" for s, q in sorted(positions.items())) or "none"
