#!/usr/bin/env python3
"""backtest_parquet.py — an event-driven backtester that reads the Parquet tape
and runs on the LOGICAL clock only.

Week 4, Parts 2–3 (slides 18–24, lab steps 6–8). Run tape_to_parquet.py first.

The loop, once per tick t of the tape (the logical clock; `ts` is only used
to pace a replay at --speed):

    1. apply every book_snapshot of tick t, in recording order (seq), and
    2. FILL each order due at t against the FIRST book of its symbol in tick t
       (the order reaches the venue during the next tick: the latency model;
       a symbol with no update that tick fills against its standing book)
    3. deliver the session events of tick t   -> strategy.on_event(event, now=t)
    4. ask for decisions                      -> strategy.on_tick(now=t, market, portfolio)
       each Signal becomes an order due at t + --latency-ticks (default 1:
       "you fill at the next tick, not this one")
    5. mark to the mid, record equity

Costs: every fill is a TAKER fill and pays --fee (default 0.30 % = the hosted
venue's TAKER_FEE_RATE; the tape's trade_execution.fee/notional says the same).
--fill sweep walks the recorded depth up to the limit price, --fill touch takes
the best level only; the remainder is dropped (IOC). Long-only and a position
cap of 400 (week 2's rules) unless you say otherwise.

Planted bugs (Part 2):
    --peek 1        the strategy reads the book of tick t+1 while deciding at t.
                    A clock violation: the vectorised sig[t]*ret[t] bug, event-driven.
    --realclock     EventRide times its exit with time.time() instead of the tick.
                    One line. P&L now depends on --speed.

Usage:
    python backtest_parquet.py                                   # EventRide vs baselines
    python backtest_parquet.py --strategy momentum --tune        # IS/OOS lookback search
    python backtest_parquet.py --latency-table                   # 0, 1, 2, 5 ticks of latency
    python backtest_parquet.py --lookahead-table                 # the planted bug, measured
    python backtest_parquet.py --metrics                         # + Sharpe (ddof=1 vs ddof=0)
    python backtest_parquet.py --strategy event_ride --from-t 390 --to-t 450 --speed 5
    python backtest_parquet.py --strategy event_ride --from-t 390 --to-t 450 --speed 5 --realclock

Needs: pip install polars pyarrow pydantic   (tested: polars 2.0.0, pyarrow 26.0.0)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

import polars as pl

# Signal: the arena's own model when this runs inside a repo, else the same
# fields here. Either way the strategies below construct it with keywords.
for _root in pathlib.Path(__file__).resolve().parents:
    if (_root / "shared" / "messages.py").exists():
        sys.path.insert(0, str(_root))
        break
    if (_root / "engine" / "shared" / "messages.py").exists():
        sys.path.insert(0, str(_root / "engine"))
        break
try:
    from shared.messages import Signal          # type: ignore  # noqa: E402
except Exception:                                # pragma: no cover - standalone copy
    from typing import Literal

    from pydantic import BaseModel

    class Signal(BaseModel):                     # same fields as shared.messages.Signal
        type: Literal["signal"] = "signal"
        symbol: str
        side: Literal["buy", "sell"]
        quantity: int
        price: float
        confidence: float = 1.0

EQUITIES = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "NFLX", "AMD", "INTC"]


# ─────────────────────────────────────────────────────────────────────────────
# Market and portfolio views handed to the strategy
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Book:
    t: int
    mid: float
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None


class Market:
    """What the strategy may see at logical time `now`: books and mid history up to now."""

    def __init__(self) -> None:
        self.books: dict[str, Book] = {}
        self.history: dict[str, list[float]] = defaultdict(list)   # one mid per tick

    def symbols(self) -> list[str]:
        return sorted(self.books)

    def book(self, s: str) -> Book | None:
        return self.books.get(s)

    def best_bid(self, s: str) -> float | None:
        b = self.books.get(s)
        return b.best_bid if b else None

    def best_ask(self, s: str) -> float | None:
        b = self.books.get(s)
        return b.best_ask if b else None

    def prices(self, s: str) -> list[float]:
        return self.history[s]


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    fees: float = 0.0
    fills: list[tuple] = field(default_factory=list)
    inflight: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # sent, not yet at the venue

    def working(self, s: str) -> int:
        """Position plus orders still in flight: what you will hold if they all fill."""
        return self.positions[s] + self.inflight[s]


# ─────────────────────────────────────────────────────────────────────────────
# Strategies: on_event(event, now) and on_tick(now, market, portfolio) -> [Signal]
# ─────────────────────────────────────────────────────────────────────────────
class Strategy:
    name = "base"

    def on_event(self, ev: dict, now: int) -> None:
        pass

    def on_tick(self, now: int, market: Market, pf: Portfolio) -> list[Signal]:
        return []


class DoNothing(Strategy):
    """Baseline 1: keep the 20 granted shares per name and the cash."""
    name = "do_nothing"


class BuyAndHold(Strategy):
    """Baseline 2: put ~90 % of cash in the ten names, equal weight, then hold."""
    name = "buy_hold"

    def __init__(self) -> None:
        self.todo: dict[str, int] | None = None

    def on_tick(self, now, market, pf):
        if self.todo is None:
            syms = [s for s in EQUITIES if market.best_ask(s)]
            if len(syms) < len(EQUITIES):
                return []
            per = 0.9 * pf.cash / len(syms)
            self.todo = {s: min(380, int(per / market.best_ask(s))) for s in syms}
        out = []
        for s, q in self.todo.items():
            if q > 0 and market.best_ask(s):
                take = min(q, 60)
                self.todo[s] -= take
                out.append(Signal(symbol=s, side="buy", quantity=take,
                                  price=round(market.best_ask(s) * 1.003, 2)))
        return out


class TickMomentum(Strategy):
    """Baseline 3 (and the IS/OOS example): buy strength, dump weakness.

    LOOKBACK-tick mid return above +THRESH -> buy SIZE; below -THRESH -> sell all.
    """
    name = "momentum"

    def __init__(self, lookback: int = 20, thresh: float = 0.003, size: int = 40) -> None:
        self.lookback, self.thresh, self.size = lookback, thresh, size

    def on_tick(self, now, market, pf):
        out = []
        for s in EQUITIES:
            h = market.prices(s)
            bid, ask = market.best_bid(s), market.best_ask(s)
            if len(h) <= self.lookback or not (bid and ask):
                continue
            r = h[-1] / h[-1 - self.lookback] - 1
            pos = pf.positions[s]
            if r > self.thresh and pos < 200:
                out.append(Signal(symbol=s, side="buy", quantity=self.size,
                                  price=round(ask * 1.003, 2)))
            elif r < -self.thresh and pos > 0:
                out.append(Signal(symbol=s, side="sell", quantity=min(pos, 60),
                                  price=round(bid * 0.997, 2)))
        return out


class EventRide(Strategy):
    """The strategy: trade the CALENDAR_EVENT message, not the chart.

    Good news (pct > 0): buy SIZE of each affected name, paying at most
    pre-mid × (1 + pct/2), until HOLD ticks have passed; then sell back to the
    position you had. Bad news (pct < 0, long-only): sell what you hold,
    accepting no less than pre-mid × (1 + pct/2), and buy it back after HOLD
    ticks. Unfilled remainders are re-sent every tick (the tape drops them). The fee gate: skip a leg whose designed edge
    (overshoot - 1/2) × |pct| does not beat two taker fees.

    `realclock=True` is the planted bug: the exit is timed with time.time()
    (HOLD × 1.06 s, the tape's average tick) instead of the logical clock.
    """
    name = "event_ride"

    def __init__(self, hold: int = 15, size: int = 100, fee: float = 0.003,
                 realclock: bool = False) -> None:
        self.hold, self.size, self.fee, self.realclock = hold, size, fee, realclock
        self.plans: list[dict] = []
        self.new: list[dict] = []

    def on_event(self, ev, now):
        if ev.get("event") != "CALENDAR_EVENT" or ev.get("pct") is None:
            return
        d = json.loads(ev["data"])
        pct, over = float(ev["pct"]), float(d.get("overshoot") or 1.0)
        if (over - 0.5) * abs(pct) <= 2 * self.fee:            # the fee gate
            return
        for s in d.get("affected") or []:
            if s in EQUITIES:
                self.new.append({"sym": s, "pct": pct, "t0": now})

    def due(self, p: dict, now: int) -> bool:
        if self.realclock:
            return time.time() - p["wall"] >= self.hold * 1.06       # BUG: real clock
        return now - p["t_in"] >= self.hold                          # logical clock

    def on_tick(self, now, market, pf):
        for p in self.new:                                           # open a plan
            mid = market.book(p["sym"]).mid
            base = pf.working(p["sym"])
            p.update(t_in=now, wall=time.time(), base=base,
                     cap=round(mid * (1 + p["pct"] / 2), 2),
                     target=base + self.size if p["pct"] > 0 else 0)
            self.plans.append(p)
        self.new = []
        out, keep = [], []
        for p in self.plans:
            s, pos, b = p["sym"], pf.working(p["sym"]), market.book(p["sym"])
            if not self.due(p, now):                                 # work the entry
                if p["pct"] > 0 and pos < p["target"]:
                    out.append(Signal(symbol=s, side="buy", quantity=p["target"] - pos,
                                      price=p["cap"]))
                elif p["pct"] < 0 and pos > p["target"]:
                    out.append(Signal(symbol=s, side="sell", quantity=pos - p["target"],
                                      price=p["cap"]))
                keep.append(p)
            elif pf.inflight[s]:                                     # wait for the venue
                keep.append(p)
            elif pos > p["base"] and b.best_bid:                     # exit: back to base
                out.append(Signal(symbol=s, side="sell", quantity=pos - p["base"],
                                  price=round(b.best_bid * 0.997, 2)))
                keep.append(p)
            elif pos < p["base"] and b.best_ask:
                out.append(Signal(symbol=s, side="buy", quantity=p["base"] - pos,
                                  price=round(b.best_ask * 1.003, 2)))
                keep.append(p)
        self.plans = keep                                            # done when pos == base
        return out


STRATEGIES = {"do_nothing": DoNothing, "buy_hold": BuyAndHold, "momentum": TickMomentum,
              "event_ride": EventRide}


# ─────────────────────────────────────────────────────────────────────────────
# The tape, from Parquet
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Tape:
    ticks: list[int]
    books_at: dict[int, list[tuple]]      # t -> [(symbol, Book)] in seq order
    events_at: dict[int, list[dict]]
    ts_at: dict[int, float]               # t -> first wall-clock ts of the tick


def load_tape(W: str, week: int | None, t_from: int | None, t_to: int | None) -> Tape:
    def scan(kind):
        lf = pl.scan_parquet(f"{W}/{kind}/**/*.parquet", hive_partitioning=True)
        if week is not None:
            lf = lf.filter(pl.col("week") == week)
        if t_from is not None:
            lf = lf.filter(pl.col("t") >= t_from)
        if t_to is not None:
            lf = lf.filter(pl.col("t") <= t_to)
        return lf

    book = (scan("book_snapshot")
            .filter(pl.col("symbol").is_in(EQUITIES))
            .select("seq", "ts", "t", "symbol", "mid", "bid_px", "bid_qty", "ask_px", "ask_qty")
            .sort("seq").collect())
    if week is None:
        weeks = (pl.scan_parquet(f"{W}/book_snapshot/**/*.parquet", hive_partitioning=True)
                 .select(pl.col("week").unique()).collect()["week"].to_list())
        if len(weeks) > 1:
            raise SystemExit(f"weeks {sorted(weeks)} in the warehouse: pass --week N")
    ev = scan("session_event").sort("seq").collect()
    books_at: dict[int, list[tuple]] = defaultdict(list)
    ts_at: dict[int, float] = {}
    if t_from is not None:              # warm start: the book each symbol had before the slice
        lf = pl.scan_parquet(f"{W}/book_snapshot/**/*.parquet", hive_partitioning=True)
        if week is not None:
            lf = lf.filter(pl.col("week") == week)
        warm = (lf.filter(pl.col("symbol").is_in(EQUITIES) & (pl.col("t") < t_from))
                .sort("seq").group_by("symbol").last()
                .select("symbol", "mid", "bid_px", "bid_qty", "ask_px", "ask_qty")
                .sort("symbol").collect())
        for sym, mid, bp, bq, ap, aq in warm.iter_rows():
            books_at[t_from].append((sym, Book(t_from - 1, mid, list(zip(bp, bq)),
                                               list(zip(ap, aq)))))
    for seq, ts, t, sym, mid, bp, bq, ap, aq in book.iter_rows():
        books_at[t].append((sym, Book(t, mid, list(zip(bp, bq)), list(zip(ap, aq)))))
        ts_at.setdefault(t, ts.timestamp())
    events_at: dict[int, list[dict]] = defaultdict(list)
    for r in ev.iter_rows(named=True):
        events_at[r["t"]].append(r)
        ts_at.setdefault(r["t"], r["ts"].timestamp())
    ticks = list(range(min(books_at), max(books_at) + 1))     # every tick, even quiet ones
    return Tape(ticks, books_at, events_at, ts_at)


# ─────────────────────────────────────────────────────────────────────────────
# The engine
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Result:
    name: str
    pnl: float
    pnl_is: float
    pnl_oos: float
    max_dd: float
    fills: int
    fees: float
    digest: str
    equity: list[float]
    wall: float


def fill(order: Signal, book: Book | None, pf: Portfolio, fee: float, mode: str,
         long_only: bool, cap: int, now: int) -> None:
    if book is None:
        return
    s, want = order.symbol, order.quantity
    if order.side == "sell" and long_only:
        want = min(want, pf.positions[s])
    if order.side == "buy":
        want = min(want, cap - pf.positions[s])
    levels = book.asks if order.side == "buy" else book.bids
    if mode == "touch":
        levels = levels[:1]
    for px, qty in levels:
        if want <= 0:
            break
        if (order.side == "buy" and px > order.price) or (order.side == "sell" and px < order.price):
            break
        q = int(min(want, qty))
        if q <= 0:
            continue
        sgn = 1 if order.side == "buy" else -1
        f = px * q * fee
        pf.cash -= sgn * px * q + f
        pf.positions[s] += sgn * q
        pf.fees += f
        pf.fills.append((now, s, order.side, px, q))
        want -= q


def run(strategy: Strategy, tape: Tape, *, fee: float = 0.003, latency: int = 1,
        mode: str = "sweep", long_only: bool = True, cap: int = 400,
        cash: float = 250_000.0, seed_shares: int = 20, split: int = 1400,
        speed: float = math.inf, peek: int = 0) -> Result:
    market, pf = Market(), Portfolio(cash=cash)
    future = Market() if peek else None
    pending: dict[int, list[Signal]] = defaultdict(list)
    equity: list[float] = []
    start_equity = eq_split = None
    t0_wall, t0_ts = time.time(), tape.ts_at.get(tape.ticks[0])
    for i, t in enumerate(tape.ticks):
        if math.isfinite(speed) and t in tape.ts_at:                 # pacing only
            wait = (tape.ts_at[t] - t0_ts) / speed - (time.time() - t0_wall)
            if wait > 0:
                time.sleep(wait)
        arriving = defaultdict(list)                                 # orders that reach the
        for o in pending.pop(t, []):                                 # venue during tick t
            arriving[o.symbol].append(o)

        def execute(o):
            pf.inflight[o.symbol] -= o.quantity if o.side == "buy" else -o.quantity
            fill(o, market.books.get(o.symbol), pf, fee, mode, long_only, cap, t)

        for sym, b in tape.books_at.get(t, []):                      # 1. book updates, in seq
            market.books[sym] = b                                    #    order; an arriving
            for o in arriving.pop(sym, []):                          # 2. order fills against
                execute(o)                                           #    the FIRST book of
        for orders in arriving.values():                             #    its symbol in tick t
            for o in orders:                                         #    (no update: the
                execute(o)                                           #    standing book)
        for sym, b in market.books.items():
            market.history[sym].append(b.mid)
        if start_equity is None and len(market.books) == len(EQUITIES):
            for s in EQUITIES:                                       # granted shares
                pf.positions[s] += seed_shares
                pf.cash -= seed_shares * market.books[s].mid
            start_equity = pf.cash + sum(pf.positions[s] * market.books[s].mid for s in EQUITIES)
        if start_equity is None:
            continue
        view = market
        if peek:                                                     # BUG: look-ahead
            future.books = dict(market.books)
            for sym, b in tape.books_at.get(t + peek, []):
                future.books[sym] = b
            for sym in EQUITIES:
                future.history[sym] = market.history[sym][:-1] + [future.books[sym].mid]
            view = future
        for ev in tape.events_at.get(t, []):                         # 3. events
            strategy.on_event(ev, now=t)
        for sig in strategy.on_tick(now=t, market=view, pf=pf) or []:  # 4. decisions
            if latency == 0:
                fill(sig, market.books.get(sig.symbol), pf, fee, mode, long_only, cap, t)
            else:
                pending[t + latency].append(sig)
                pf.inflight[sig.symbol] += sig.quantity if sig.side == "buy" else -sig.quantity
        eq = pf.cash + sum(pf.positions[s] * market.books[s].mid for s in EQUITIES)
        equity.append(eq)                                            # 5. mark
        if t == split:
            eq_split = eq
    peak, mdd = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = max(mdd, peak - e)
    end = equity[-1]
    if eq_split is None:
        eq_split = end
    digest = hashlib.sha256(repr(pf.fills).encode()).hexdigest()[:12]
    return Result(strategy.name, end - start_equity, eq_split - start_equity, end - eq_split,
                  mdd, len(pf.fills), pf.fees, digest, equity, time.time() - t0_wall)


# ─────────────────────────────────────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────────────────────────────────────
def show(rows: list[Result], split: int) -> None:
    print(f"  {'strategy':<22} {'P&L':>10} {'IS t<' + str(split):>11} {'OOS':>10} "
          f"{'max DD':>9} {'fills':>6} {'fees':>9}  digest")
    for r in rows:
        print(f"  {r.name:<22} {r.pnl:>+10,.0f} {r.pnl_is:>+11,.0f} {r.pnl_oos:>+10,.0f} "
              f"{r.max_dd:>9,.0f} {r.fills:>6} {r.fees:>9,.0f}  {r.digest}")


def metrics(rows: list[Result]) -> None:
    """Sharpe on the per-tick equity changes: mean / sample std (ddof=1).

    Not annualised: one 50-minute block is not a year, and ticks are not
    independent days. ddof=0 (the population std) makes it a hair larger;
    on 2,800 ticks the difference is in the 4th digit — on 20 daily returns
    it is 2.6 % (sqrt(20/19))."""
    print(f"  {'strategy':<22} {'mean $/tick':>12} {'std ddof=1':>11} {'std ddof=0':>11} "
          f"{'Sharpe/tick':>12} {'x sqrt(N)':>10}")
    for r in rows:
        d = [b - a for a, b in zip(r.equity, r.equity[1:])]
        n = len(d)
        mu = sum(d) / n
        var1 = sum((x - mu) ** 2 for x in d) / (n - 1)
        var0 = var1 * (n - 1) / n
        sh = mu / math.sqrt(var1) if var1 > 0 else float("nan")
        print(f"  {r.name:<22} {mu:>+12.4f} {math.sqrt(var1):>11.3f} {math.sqrt(var0):>11.3f} "
              f"{sh:>+12.4f} {sh * math.sqrt(n):>+10.2f}")


def lookahead_table(W: str, week: int | None) -> None:
    """The old lab's bug, vectorised (Polars), then event-driven (--peek)."""
    m = (pl.scan_parquet(f"{W}/book_snapshot/**/*.parquet", hive_partitioning=True)
         .filter(pl.col("symbol").is_in(EQUITIES))
         .sort("t", "seq").group_by("symbol", "t").agg(pl.col("mid").last())
         .sort("symbol", "t")
         .with_columns(ret=pl.col("mid").pct_change().over("symbol"),
                       sig=(pl.col("mid") > pl.col("mid").rolling_mean(40).over("symbol"))
                       .cast(pl.Float64))
         .with_columns(bug=pl.col("sig") * pl.col("ret"),
                       fix=pl.col("sig").shift(1).over("symbol") * pl.col("ret"))
         .collect())
    print("  vectorised, long above the 40-tick mean, no costs, all 10 names pooled:")
    for col, label in (("bug", "BUG  sig[t]   * ret[t]"), ("fix", "FIX  sig[t-1] * ret[t]"),
                       ("ret", "hold every name")):
        r = m[col].drop_nulls()
        tot = float(r.sum()) / len(EQUITIES)
        print(f"    {label:<24} mean {1e4 * r.mean():+7.3f} bp/tick   sum/name {100 * tot:+7.2f} %"
              f"   t-stat {r.mean() / r.std() * math.sqrt(len(r)):+6.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--warehouse", default="/tmp/w4/tape.parquet")
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--strategy", default=None, choices=sorted(STRATEGIES))
    ap.add_argument("--fee", type=float, default=0.003)
    ap.add_argument("--latency-ticks", type=int, default=1)
    ap.add_argument("--fill", default="sweep", choices=["sweep", "touch"])
    ap.add_argument("--split", type=int, default=1400, help="IS = t < split, OOS = t >= split")
    ap.add_argument("--from-t", type=int, default=None)
    ap.add_argument("--to-t", type=int, default=None)
    ap.add_argument("--speed", type=float, default=math.inf,
                    help="replay pacing on the tape's ts: 1 = real time, 5 = 5x, inf = no sleep")
    ap.add_argument("--peek", type=int, default=0, help="PLANTED BUG: see the book N ticks ahead")
    ap.add_argument("--realclock", action="store_true", help="PLANTED BUG: time.time() exit")
    ap.add_argument("--hold", type=int, default=15)
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--tune", action="store_true", help="momentum: pick the lookback on IS only")
    ap.add_argument("--latency-table", action="store_true")
    ap.add_argument("--lookahead-table", action="store_true")
    ap.add_argument("--metrics", action="store_true", help="also print Sharpe per strategy")
    a = ap.parse_args()

    t_load = time.perf_counter()
    tape = load_tape(a.warehouse, a.week, a.from_t, a.to_t)
    t_load = time.perf_counter() - t_load
    nb = sum(len(v) for v in tape.books_at.values())
    print(f"tape: t {tape.ticks[0]}..{tape.ticks[-1]} ({len(tape.ticks):,} ticks, {nb:,} book "
          f"snapshots) loaded from Parquet in {t_load * 1e3:.0f} ms · fee {a.fee:.2%} · "
          f"latency {a.latency_ticks} tick · fill {a.fill}")
    kw = dict(fee=a.fee, latency=a.latency_ticks, mode=a.fill, split=a.split,
              speed=a.speed, peek=a.peek)

    def make(name, **over):
        if name == "event_ride":
            return EventRide(hold=over.get("hold", a.hold), fee=a.fee, realclock=a.realclock)
        if name == "momentum":
            return TickMomentum(lookback=over.get("lookback", a.lookback))
        return STRATEGIES[name]()

    if a.lookahead_table:
        lookahead_table(a.warehouse, a.week)
        print("  event-driven, TickMomentum, 0.30 % fee, next-tick fills:")
        rows = []
        for fee in (a.fee, 0.0):
            for peek in (0, 1, 2, 5):
                r = run(make("momentum"), tape, **dict(kw, peek=peek, fee=fee))
                r.name = f"momentum fee={fee:.2%} peek={peek}"
                rows.append(r)
        show(rows, a.split)
        return 0

    if a.latency_table:
        rows = []
        for lat in (0, 1, 2, 5):
            r = run(make("event_ride"), tape, **dict(kw, latency=lat))
            r.name = f"event_ride latency={lat}"
            rows.append(r)
        show(rows, a.split)
        return 0

    if a.tune:
        print(f"  tune TickMomentum's lookback on IS (t < {a.split}) only, then run OOS once:")
        best = None
        for lb in (5, 10, 20, 40, 80, 160):
            r = run(make("momentum", lookback=lb), tape, **kw)
            print(f"    lookback {lb:>3}: IS {r.pnl_is:>+9,.0f}   (OOS {r.pnl_oos:>+9,.0f}"
                  f" — not looked at when choosing)")
            if best is None or r.pnl_is > best[1]:
                best = (lb, r.pnl_is, r.pnl_oos)
        print(f"  chosen on IS: lookback {best[0]} -> IS {best[1]:+,.0f}, OOS {best[2]:+,.0f}")
        return 0

    names = [a.strategy] if a.strategy else ["event_ride", "do_nothing", "buy_hold", "momentum"]
    rows = [run(make(n), tape, **kw) for n in names]
    show(rows, a.split)
    if a.metrics:
        metrics(rows)
    if math.isfinite(a.speed) or a.realclock:
        for r in rows:
            print(f"  speed {a.speed:g}{' REALCLOCK' if a.realclock else ''}: {r.name} "
                  f"P&L {r.pnl:+,.2f} · {r.fills} fills · digest {r.digest} · "
                  f"wall {r.wall:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
