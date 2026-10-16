#!/usr/bin/env python
"""
scripts/event_backtest.py — replay a recorded block through a strategy seat.

Unlike scripts/backtest.py, this replays everything a live seat sees: book
snapshots, trades, AND the session events (CALENDAR warnings,
CALENDAR_EVENT, SHOCK), and it answers the seat the way the venue would —
acks, fills through on_fill, cancels, and rejects through on_reject — so
the team traders (team.base_trader) run unchanged.

Usage:
    python scripts/event_backtest.py sessions/<file>.jsonl.gz --bot momentum
    python scripts/event_backtest.py <file> --bot momentum --no-events
    python scripts/event_backtest.py <file> --compare          # the table

MODEL — read before trusting a number
    Clock      the tape's timestamps. The seat's on_tick runs every --tick
               seconds of tape time; each order and cancel reaches the venue
               --latency seconds after it was sent.
    Taker      a marketable order walks the recorded book up to its limit
               (--fill sweep) or takes the top level only (--fill touch),
               pays --fee, and shares already taken from a snapshot are not
               available again until the next snapshot of that symbol.
    Maker      the remainder rests. A resting bid fills when a later tape
               trade prints at or below it with a seller as aggressor (asks
               mirror this), for --maker-haircut of the printed quantity: no
               queue position is modelled, so 1.0 is optimistic. Maker fills
               earn --rebate.
    Venue      orders are refused as live: SHORTS_LOCKED (sell > shares
               held), POSITION_LIMIT (±400 counting resting buys),
               INSUFFICIENT_CASH, PRICE_BAND (±10% of the mid), RATE_LIMITED
               (more than 20 messages in one second), STP_CANCEL (an order
               crossing the seat's own resting order cancels that order),
               ORDER_NOT_FOUND (cancel of an order no longer resting).
    Score      the venue's season score (exchange.scoring.score_equity):
               net worth marked at the reference price every 60 s of tape
               (one snapshot per 60 ticks), idle cash earning the venue's
               CASH_INTEREST_PER_TICK, so a seat that does nothing scores ~0.
    Limits     nobody reacts to our orders, and every share we "buy" was
               bought by someone else on the day. In-sample: treat the
               numbers as an upper bound on what the logic can earn.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import math
import pathlib
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "engine"), str(ROOT)]

import arena  # noqa: E402,F401  (puts the engine on sys.path first)
from arena import Signal, Trader  # noqa: E402
from exchange import config as venue_config, scoring  # noqa: E402
from trader.trader import MarketData  # noqa: E402

POSITION_LIMIT = 400
PRICE_BAND = 0.10
QUOTA_PER_SEC = 20
MARK_EVERY_SEC = 1.0
SNAPSHOT_SEC = 60.0          # the venue's season snapshot: every 60 ticks of 1 s


# ── the tape ────────────────────────────────────────────────────────────────

@dataclass
class Tape:
    records: list[tuple[float, str, dict]]
    symbols: list[str]


def load_tape(path: pathlib.Path) -> Tape:
    opener = gzip.open if path.suffix == ".gz" else open
    records, symbols = [], {}
    with opener(path, "rt") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("aux"):
                continue
            msg = rec.get("msg") or {}
            kind = msg.get("type")
            if kind not in ("book_snapshot", "trade_execution", "session_event"):
                continue
            if kind == "book_snapshot":
                symbols.setdefault(msg["symbol"], None)
            records.append((float(rec["ts"]), kind, msg))
    records.sort(key=lambda r: r[0])
    return Tape(records, list(symbols))


# ── the seat's account ──────────────────────────────────────────────────────

class SimPortfolio:
    """The read surface on_tick uses: cash, positions, net_worth, can_*."""

    def __init__(self, cash: float, positions: dict[str, int]) -> None:
        self.cash = cash
        self.positions = dict(positions)
        self.fees = 0.0
        self.rebates = 0.0

    def net_worth(self, market) -> float:
        return self.cash + sum(q * (market.mid_price(s) or 0.0)
                               for s, q in self.positions.items())

    def scored_equity(self, ref: dict[str, float]) -> float:
        """Net worth the way the venue scores it: inventory at the reference."""
        return self.cash + sum(q * ref.get(s, 0.0) for s, q in self.positions.items())

    def can_buy(self, symbol: str, qty: int, price: float) -> bool:
        return self.cash >= price * qty * 1.001

    def can_sell(self, symbol: str, qty: int) -> bool:
        return self.positions.get(symbol, 0) >= qty


@dataclass
class Resting:
    order_id: str
    symbol: str
    side: str
    price: float
    remaining: int
    credit: float = 0.0       # fractional maker fills accumulated


@dataclass
class Result:
    bot: str
    pnl: float = 0.0
    ride: float = 0.0
    fade: float = 0.0
    max_dd: float = 0.0
    equity: list = field(default_factory=list)     # one point per SNAPSHOT_SEC
    taker_fills: int = 0
    maker_fills: int = 0
    fees: float = 0.0
    rebates: float = 0.0
    rejects: dict[str, int] = field(default_factory=dict)


# ── the venue ───────────────────────────────────────────────────────────────

class Venue:
    def __init__(self, bot: Trader, portfolio: SimPortfolio, args) -> None:
        self.bot = bot
        self.pf = portfolio
        self.args = args
        self.market = MarketData()
        self.now = 0.0
        self.books: dict[str, dict] = {}
        self.ref: dict[str, float] = {}
        self.taken: dict[tuple[str, str, float], int] = {}
        self.resting: list[Resting] = []
        self.inbound: list[tuple[float, str, object]] = []   # (arrive, kind, item)
        self.quota: dict[int, int] = {}
        self.ids = itertools.count(1)
        self.res = Result(bot=args.label)

    # ── messages to the bot ─────────────────────────────────────────────

    def _reject(self, code: str, message: str) -> None:
        self.res.rejects[code] = self.res.rejects.get(code, 0) + 1
        hook = getattr(self.bot, "on_reject", None)
        if hook:
            hook(code, message)

    def _fill(self, side: str, symbol: str, qty: int, price: float,
              maker: bool) -> None:
        notional = qty * price
        if maker:
            rebate = notional * self.args.rebate
            self.pf.rebates += rebate
            self.pf.cash += rebate
            self.res.maker_fills += 1
        else:
            fee = notional * self.args.fee
            self.pf.fees += fee
            self.pf.cash -= fee
            self.res.taker_fills += 1
        if side == "buy":
            self.pf.cash -= notional
            self.pf.positions[symbol] = self.pf.positions.get(symbol, 0) + qty
        else:
            self.pf.cash += notional
            self.pf.positions[symbol] = self.pf.positions.get(symbol, 0) - qty
        self.bot.on_fill(side, symbol, qty, price)

    # ── order handling ──────────────────────────────────────────────────

    def submit(self, signal: Signal) -> None:
        self.inbound.append((self.now + self.args.latency, "order", signal))

    def cancel(self, order_id: str, symbol: str) -> None:
        self.inbound.append((self.now + self.args.latency, "cancel", (order_id, symbol)))

    def _take_quota(self) -> bool:
        second = int(self.now)
        used = self.quota.get(second, 0)
        if used >= QUOTA_PER_SEC:
            return False
        self.quota[second] = used + 1
        return True

    def _arrive(self, kind: str, item) -> None:
        if not self._take_quota():
            self._reject("RATE_LIMITED", "Message quota exceeded")
            return
        if kind == "cancel":
            order_id, symbol = item
            for r in self.resting:
                if r.order_id == order_id:
                    self.resting.remove(r)
                    return
            self._reject("ORDER_NOT_FOUND", f"Order '{order_id}' not found")
            return
        self._order(item)

    def _order(self, s: Signal) -> None:
        sym, side, qty, px = s.symbol, s.side, int(s.quantity), float(s.price)
        held = self.pf.positions.get(sym, 0)
        if side == "sell" and qty > held:
            self._reject("SHORTS_LOCKED", f"You hold {held} {sym} — sell at most that many.")
            return
        open_buys = sum(r.remaining for r in self.resting
                        if r.symbol == sym and r.side == "buy")
        if side == "buy" and held + open_buys + qty > POSITION_LIMIT:
            self._reject("POSITION_LIMIT", f"{sym} exposure over ±{POSITION_LIMIT}")
            return
        if side == "buy" and self.pf.cash < qty * px * (1 + self.args.fee):
            self._reject("INSUFFICIENT_CASH", f"{sym} buy needs more cash")
            return
        mid = self.market.mid_price(sym)
        if mid and not mid * (1 - PRICE_BAND) <= px <= mid * (1 + PRICE_BAND):
            self._reject("PRICE_BAND", f"{sym} {side} at {px} is outside the LULD band")
            return

        # Self-trade prevention: our own resting order on the other side
        # that this one would cross is cancelled, and the owner told.
        for r in list(self.resting):
            if r.symbol == sym and r.side != side and (
                    px >= r.price if side == "buy" else px <= r.price):
                self.resting.remove(r)
                self._reject("STP_CANCEL",
                             f"Your resting {r.side} {r.remaining} {sym} @ {r.price} "
                             "was cancelled by self-trade prevention")

        remaining = qty
        book = self.books.get(sym) or {"bids": [], "asks": []}
        levels = book["asks"] if side == "buy" else book["bids"]
        if self.args.fill == "touch":
            levels = levels[:1]
        for lvl_px, lvl_qty in levels:
            if remaining <= 0:
                break
            if (side == "buy" and lvl_px > px) or (side == "sell" and lvl_px < px):
                break
            key = (sym, side, lvl_px)
            avail = int(lvl_qty) - self.taken.get(key, 0)
            if avail <= 0:
                continue
            q = min(avail, remaining)
            self.taken[key] = self.taken.get(key, 0) + q
            remaining -= q
            self._fill(side, sym, q, lvl_px, maker=False)

        order_id = f"bt-{next(self.ids)}"
        ack = getattr(self.bot, "on_ack", None)
        if ack:
            ack(order_id, sym, side, px)
        if remaining > 0:
            self.resting.append(Resting(order_id, sym, side, px, remaining))

    # ── tape events ─────────────────────────────────────────────────────

    def on_snapshot(self, msg: dict) -> None:
        snap = SimpleNamespace(symbol=msg["symbol"], bids=msg.get("bids") or [],
                               asks=msg.get("asks") or [],
                               mid_price=float(msg.get("mid_price") or 0.0),
                               spread=float(msg.get("spread") or 0.0))
        self.market.update_book(snap)
        ref = float(msg.get("ref_price") or 0.0) or snap.mid_price
        if ref > 0:
            self.ref[snap.symbol] = ref
        self.books[snap.symbol] = {"bids": snap.bids, "asks": snap.asks}
        for key in [k for k in self.taken if k[0] == snap.symbol]:
            del self.taken[key]
        # A bid at or above our resting ask (or an ask at or below our bid)
        # arrived after our order, so it would have traded with us.
        for r in list(self.resting):
            if r.symbol != snap.symbol:
                continue
            levels = snap.bids if r.side == "sell" else snap.asks
            crossing = sum(int(q) for p, q in levels
                           if (p >= r.price if r.side == "sell" else p <= r.price))
            if crossing:
                self._maker_fill(r, crossing)

    def on_trade(self, msg: dict) -> None:
        sym, px, qty = msg["symbol"], float(msg["price"]), int(msg["quantity"])
        hits = "buy" if msg.get("aggressor") == "sell" else "sell"
        for r in list(self.resting):
            if r.symbol != sym or r.side != hits:
                continue
            if (hits == "buy" and px > r.price) or (hits == "sell" and px < r.price):
                continue
            self._maker_fill(r, qty)

    def _maker_fill(self, r: Resting, printed: int) -> None:
        """Fill a resting order for the haircut share of `printed` shares."""
        r.credit += printed * self.args.maker_haircut
        q = min(r.remaining, int(r.credit))
        if q <= 0:
            return
        r.credit -= q
        r.remaining -= q
        if r.remaining <= 0:
            self.resting.remove(r)
        self._fill(r.side, r.symbol, q, r.price, maker=True)

    def deliver(self, until: float) -> None:
        due = sorted((x for x in self.inbound if x[0] <= until), key=lambda x: x[0])
        self.inbound = [x for x in self.inbound if x[0] > until]
        for arrive, kind, item in due:
            self.now = arrive
            self._arrive(kind, item)

    def tick(self) -> None:
        signal = self.bot.on_tick(self.market, self.pf)
        if signal is not None:
            self.submit(signal)
        take = getattr(self.bot, "take_cancels", None)
        if take:
            for order_id, symbol in take():
                self.cancel(order_id, symbol)


# ── baselines ───────────────────────────────────────────────────────────────

class DoNothing(Trader):
    def on_tick(self, market, portfolio):
        return None


class BuyAndHold(Trader):
    """Put 90% of the cash into equal slices at the first chance, then hold."""

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.done: set[str] = set()
        self.budget: float | None = None

    def on_tick(self, market, portfolio):
        if self.budget is None:
            self.budget = 0.9 * portfolio.cash / max(1, len(self.symbols))
        for sym in self.symbols:
            ask = market.best_ask(sym)
            if sym in self.done or not ask:
                continue
            self.done.add(sym)
            qty = min(int(self.budget // (ask * 1.01)), 60)
            if qty > 0:
                return Signal(symbol=sym, side="buy", quantity=qty,
                              price=round(ask * 1.005, 2))
        if len(self.done) == len(self.symbols):
            self.done.clear()          # keep topping up to the budget
            self.budget = 0.0 if self._invested(market, portfolio) else self.budget
        return None

    def _invested(self, market, portfolio) -> bool:
        value = sum(q * (market.mid_price(s) or 0) for s, q in portfolio.positions.items())
        return value >= 0.85 * portfolio.net_worth(market)


def make_bot(name: str, clock, symbols: list[str]):
    if name == "momentum":
        from team.momentum_trader import MomentumTrader
        return MomentumTrader(clock=clock)
    if name == "meanrev":
        from team.mean_reversion_trader import MeanReversionTrader
        return MeanReversionTrader(clock=clock)
    if name == "donothing":
        return DoNothing()
    if name == "buyhold":
        return BuyAndHold([s for s in symbols if s != "ARENA10"])
    raise SystemExit(f"unknown bot {name!r}")


# ── the run ─────────────────────────────────────────────────────────────────

def run(tape: Tape, args) -> Result:
    venue: Venue | None = None
    clock = lambda: venue.now  # noqa: E731
    bot = make_bot(args.bot, clock, tape.symbols)
    seeds = {s: args.seed_shares for s in tape.symbols if s != "ARENA10"}
    venue = Venue(bot, SimPortfolio(args.cash, seeds), args)
    quiet = logging_off()

    bot.on_event("FEE_SCHEDULE", "backtest fees",
                 {"taker": args.fee, "rebate": args.rebate})
    start_nw: float | None = None
    peak, next_tick, next_mark, next_snap = -math.inf, None, None, None
    interest = (1.0 + venue_config.CASH_INTEREST_PER_TICK) ** args.tick - 1.0
    for ts, kind, msg in tape.records:
        if next_tick is None:
            next_tick, next_mark = ts, ts
        while next_tick <= ts:
            venue.deliver(next_tick)
            venue.now = next_tick
            if len(venue.market.symbols()) >= len(seeds):
                if start_nw is None:
                    start_nw = venue.pf.net_worth(venue.market)
                    next_snap = next_tick
                if venue.pf.cash > 0:       # idle cash earns the venue's rate
                    venue.pf.cash *= 1.0 + interest
                if next_tick >= next_snap:
                    venue.res.equity.append(venue.pf.scored_equity(venue.ref))
                    next_snap += SNAPSHOT_SEC
                venue.tick()
            next_tick += args.tick
        venue.deliver(ts)
        venue.now = ts
        if kind == "book_snapshot":
            venue.on_snapshot(msg)
        elif kind == "trade_execution":
            venue.on_trade(msg)
        elif kind == "session_event":
            event = msg.get("event", "")
            if args.no_events and event in ("CALENDAR", "CALENDAR_EVENT", "SHOCK"):
                continue
            if event not in ("SESSION_OPEN", "SESSION_CLOSED"):
                bot.on_event(event, msg.get("message", ""), msg.get("data") or {})
        if start_nw is not None and ts >= next_mark:
            nw = venue.pf.net_worth(venue.market)
            peak = max(peak, nw)
            venue.res.max_dd = max(venue.res.max_dd, peak - nw)
            next_mark = ts + MARK_EVERY_SEC

    res = venue.res
    res.equity.append(venue.pf.scored_equity(venue.ref))   # the close
    res.pnl = venue.pf.net_worth(venue.market) - (start_nw or args.cash)
    res.fees, res.rebates = venue.pf.fees, venue.pf.rebates
    plans = list(getattr(bot, "finished_plans", [])) + list(getattr(bot, "_plans", {}).values())
    res.ride = sum(p.gross_pnl() for p in plans if p.mode == "ride")
    res.fade = sum(p.gross_pnl() for p in plans if p.mode == "fade")
    quiet()
    return res


def logging_off():
    import logging
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    return lambda: logging.disable(previous)


def print_table(results: list[Result]) -> None:
    head = (f"{'bot':<26}{'P&L':>10}{'ride':>9}{'fade':>9}{'max DD':>9}"
            f"{'taker':>7}{'maker':>7}{'fees':>8}{'rebates':>9}"
            f"{'score':>8}{'ret':>8}{'vol':>8}{'mdd':>7}  rejects")
    print(head)
    print("-" * len(head))
    for r in results:
        rej = ", ".join(f"{k} {v}" for k, v in sorted(r.rejects.items())) or "0"
        sc = scoring.score_equity(r.equity)
        print(f"{r.bot:<26}{r.pnl:>+10,.0f}{r.ride:>+9,.0f}{r.fade:>+9,.0f}"
              f"{r.max_dd:>9,.0f}{r.taker_fills:>7}{r.maker_fills:>7}"
              f"{r.fees:>8,.0f}{r.rebates:>9,.0f}"
              f"{sc['risk_adjusted']:>+8.3f}{sc['cumulative_return']:>+8.2%}"
              f"{sc['vol']:>8.4%}{sc['max_drawdown']:>7.2%}  {rej}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("file", help="sessions/<file>.jsonl[.gz]")
    ap.add_argument("--bot", default="momentum",
                    help="momentum | meanrev | donothing | buyhold")
    ap.add_argument("--no-events", action="store_true",
                    help="hide CALENDAR / CALENDAR_EVENT / SHOCK from the bot")
    ap.add_argument("--compare", action="store_true",
                    help="run the standard set of bots and print one table")
    ap.add_argument("--tick", type=float, default=0.1)
    ap.add_argument("--latency", type=float, default=0.05)
    ap.add_argument("--fill", choices=("sweep", "touch"), default="sweep")
    ap.add_argument("--fee", type=float, default=0.003)
    ap.add_argument("--rebate", type=float, default=0.001)
    ap.add_argument("--maker-haircut", type=float, default=0.5)
    ap.add_argument("--cash", type=float, default=250_000.0)
    ap.add_argument("--seed-shares", type=int, default=20)
    args = ap.parse_args(argv)

    tape = load_tape(pathlib.Path(args.file))
    runs = ([("momentum", False), ("momentum", True), ("meanrev", False),
             ("meanrev", True), ("donothing", False), ("buyhold", False)]
            if args.compare else [(args.bot, args.no_events)])
    results = []
    for bot, no_events in runs:
        one = argparse.Namespace(**vars(args))
        one.bot, one.no_events = bot, no_events
        one.label = bot + (" (no events)" if no_events else "")
        results.append(run(tape, one))
    print(f"{args.file}: tick {args.tick}s, latency {args.latency}s, fill "
          f"{args.fill}, fee {args.fee:.2%}, rebate {args.rebate:.2%}, maker "
          f"haircut {args.maker_haircut}, ${args.cash:,.0f} + "
          f"{args.seed_shares} shares/name")
    print_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
