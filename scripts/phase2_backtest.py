#!/usr/bin/env python
"""
scripts/phase2_backtest.py — the Phase 2 backtest of the trader_1 trend signal.

Every number in BACKTEST.md comes from one run of this script on the
recorded Session 5 tape:

    python scripts/phase2_backtest.py \\
        sessions/session_week05_20261029_221018_market.jsonl.gz

It replays the tape through scripts/event_backtest.py's venue model (taker
fills walk the recorded book, maker fills come from later prints, latency,
the venue's rejects) at the fee schedule Session 5 actually charged, and runs:

  1. Whole session: the live bot (crossover signal + event trades), the
     signal alone (event trades off, quotes still pulled around events), and
     a do-nothing baseline.
  2. Dispersion: the same three bots on five independent 10-minute blocks,
     each started fresh.
  3. Look-ahead check: the signal fed the book mids one update older (a real
     edge should shrink a little) and one update ahead (look-ahead: a
     windfall a genuine backtest must not see). Measured on trades and on
     the signal's forward returns.
  4. Placebo: the same crossover rule in the house simulator across ten price
     seeds. Its prices are a random walk, so any edge must disappear.
  5. Live comparison: trader_1's live fills, read from the public tape,
     against the replayed live bot's fills on the same tape.

Outputs: a printed report, results/phase2/results.json and
docs/phase2/equity_week05.png. The run is deterministic.
"""

from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import math
import pathlib
import statistics as st
import sys
from collections import defaultdict
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "engine"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

_spec = importlib.util.spec_from_file_location("event_backtest",
                                               ROOT / "scripts" / "event_backtest.py")
eb = importlib.util.module_from_spec(_spec)
sys.modules["event_backtest"] = eb      # its dataclasses look themselves up here
_spec.loader.exec_module(eb)

from team.momentum_trader import (BUFFER, LONG_WINDOW, QUANTITY,  # noqa: E402
                                  SHORT_WINDOW, MomentumTrader)

LIVE_SEAT = "midway_traders_trader_1"
TAKER_FEE = 0.0030          # Session 5's schedule, read from the tape's fills
MAKER_REBATE = 0.0005
BLOCKS = 5
FORWARD_UPDATES = 20        # horizon for the signal's forward-return check
SIM_SEEDS = 10
SIM_TICKS = 3000


# ── tape helpers ────────────────────────────────────────────────────────────

class MidIndex:
    """Each symbol's book mids in time order, for marks and the look-ahead."""

    def __init__(self, tape) -> None:
        self.ts: dict[str, list[float]] = defaultdict(list)
        self.mid: dict[str, list[float]] = defaultdict(list)
        for ts, kind, msg in tape.records:
            if kind == "book_snapshot" and msg.get("mid_price"):
                self.ts[msg["symbol"]].append(ts)
                self.mid[msg["symbol"]].append(msg["mid_price"])

    def at(self, symbol: str, ts: float) -> float | None:
        i = bisect.bisect_right(self.ts[symbol], ts) - 1
        return self.mid[symbol][i] if i >= 0 else None

    def next_after(self, symbol: str, ts: float) -> float | None:
        i = bisect.bisect_right(self.ts[symbol], ts)
        return self.mid[symbol][i] if i < len(self.mid[symbol]) else None


def blocks_of(tape, n: int) -> list:
    """Split a tape into n equal-time blocks, each a tape of its own."""
    t0, t1 = tape.records[0][0], tape.records[-1][0]
    edges = [t0 + (t1 - t0) * k / n for k in range(n + 1)]
    out = []
    for a, b in zip(edges, edges[1:]):
        recs = [r for r in tape.records if a <= r[0] < b or (b == t1 and r[0] == t1)]
        out.append(eb.Tape(records=recs, symbols=tape.symbols))
    return out


# ── the bots ────────────────────────────────────────────────────────────────

class _ShiftedMarket:
    """The book as the signal sees it, with its mid history moved in time.

    shift=+1 drops the newest mid (the feature is one update old); shift=-1
    appends the next mid on the tape (look-ahead). Everything else, including
    the touch the order is priced at, is the real book.
    """

    def __init__(self, market, shift: int, index: MidIndex, now: float) -> None:
        self._m, self._shift, self._index, self._now = market, shift, index, now

    def __getattr__(self, name):
        return getattr(self._m, name)

    def prices(self, symbol: str) -> list[float]:
        hist = list(self._m.prices(symbol))
        if self._shift > 0:
            return hist[:-self._shift]
        nxt = self._index.next_after(symbol, self._now)
        return hist[1:] + [nxt] if (hist and nxt) else hist


def signal_only(shift: int, index: MidIndex):
    """The live MomentumTrader with its event trades switched off."""

    class SignalOnly(MomentumTrader):
        EVENT_MODES = frozenset()

        def _reaction_signal(self, market, now, halted):
            return None

        def _signal(self, market, portfolio, net_worth, now, halted):
            if shift:
                market = _ShiftedMarket(market, shift, index, now)
            return super()._signal(market, portfolio, net_worth, now, halted)

    return SignalOnly


BOTS = ("live bot", "signal only", "do nothing")
LOOKAHEAD = {"signal, 1 update old": 1, "signal, 1 update ahead (look-ahead)": -1}


def make_factory(name: str, index: MidIndex):
    def factory(_name, clock, symbols):
        if name == "live bot":
            return MomentumTrader(clock=clock)
        if name == "do nothing":
            return eb.DoNothing()
        shift = 0 if name == "signal only" else LOOKAHEAD[name]
        return signal_only(shift, index)(clock=clock)
    return factory


# ── running and measuring ───────────────────────────────────────────────────

@dataclass
class Run:
    name: str
    pnl: float
    max_dd: float
    fees: float
    rebates: float
    taker: int
    maker: int
    rejects: dict
    fills: list = field(default_factory=list)   # (ts, side, symbol, qty, px, fee_cash)


def run_bot(tape, name: str, index: MidIndex, cash: float, seed_shares: int) -> Run:
    ns = argparse.Namespace(bot=name, no_events=False, label=name, tick=0.1,
                            latency=0.05, fill="sweep", fee=TAKER_FEE,
                            rebate=MAKER_REBATE, maker_haircut=0.5, cash=cash,
                            seed_shares=seed_shares)
    fills: list = []
    original_fill, original_make = eb.Venue._fill, eb.make_bot

    def recording_fill(self, side, symbol, qty, price, maker):
        fee_cash = qty * price * (MAKER_REBATE if maker else -TAKER_FEE)
        fills.append((self.now, side, symbol, qty, price, fee_cash))
        return original_fill(self, side, symbol, qty, price, maker)

    eb.Venue._fill, eb.make_bot = recording_fill, make_factory(name, index)
    try:
        res = eb.run(tape, ns)
    finally:
        eb.Venue._fill, eb.make_bot = original_fill, original_make
    return Run(name, res.pnl, res.max_dd, res.fees, res.rebates,
               res.taker_fills, res.maker_fills, dict(res.rejects), fills)


def trading_pnl(fills: list, index: MidIndex, at: float) -> float:
    """Cash from fills, fees and rebates, plus the net position marked at mid."""
    cash, pos = 0.0, defaultdict(int)
    for ts, side, sym, qty, px, fee_cash in fills:
        if ts > at:
            break
        cash += (-qty * px if side == "buy" else qty * px) + fee_cash
        pos[sym] += qty if side == "buy" else -qty
    return cash + sum(q * (index.at(s, at) or 0.0) for s, q in pos.items())


def round_trips(fills: list) -> tuple[int, int]:
    """FIFO-matched closing trades: (closed, closed at a profit after costs)."""
    lots: dict[str, list[list[float]]] = defaultdict(list)   # [signed qty, px, cost/share]
    closed = wins = 0
    for _ts, side, sym, qty, px, fee_cash in fills:
        sign, per_share = (1 if side == "buy" else -1), fee_cash / qty
        left = qty
        while left and lots[sym] and lots[sym][0][0] * sign < 0:
            lot = lots[sym][0]
            q = min(left, abs(lot[0]))
            gain = (px - lot[1]) * q * (-sign) + (lot[2] + per_share) * q
            closed += 1
            wins += gain > 0
            lot[0] += q * sign
            left -= q
            if lot[0] == 0:
                lots[sym].pop(0)
        if left:
            lots[sym].append([left * sign, px, per_share])
    return closed, wins


def forward_edge(index: MidIndex, shift: int) -> tuple[int, float, float]:
    """Entries of the crossover and their mean forward move (bps, signed).

    The signal at update t uses mids up to t (shift 0), up to t-1 (shift 1)
    or up to t+1 (shift -1); the forward move is always mid[t+H] / mid[t].
    """
    moves = []
    for sym, mids in index.mid.items():
        if sym == "ARENA10":
            continue
        state = 0
        for t in range(LONG_WINDOW + 1, len(mids) - FORWARD_UPDATES - 1):
            h = mids[: t + 1 - shift]
            s_ma = sum(h[-SHORT_WINDOW:]) / SHORT_WINDOW
            l_ma = sum(h[-LONG_WINDOW:]) / LONG_WINDOW
            new = 1 if s_ma > l_ma * (1 + BUFFER) else -1 if s_ma < l_ma * (1 - BUFFER) else 0
            if new and new != state:
                moves.append(new * (mids[t + FORWARD_UPDATES] / mids[t] - 1) * 1e4)
            state = new
    if len(moves) < 2:
        return len(moves), float("nan"), float("nan")
    return len(moves), st.mean(moves), st.mean(moves) / (st.pstdev(moves) / math.sqrt(len(moves)))


def next_move_corr(index: MidIndex, shift: int) -> tuple[int, float]:
    """Correlation of the crossover feature with the very next mid move.

    The feature is short MA / long MA - 1 built from mids up to t - shift;
    the move is mid[t+1] / mid[t] - 1. A feature that has seen mid[t+1]
    (shift -1) is correlated with that move by construction: the textbook
    look-ahead signature. Pooled across symbols.
    """
    xs, ys = [], []
    for sym, mids in index.mid.items():
        if sym == "ARENA10":
            continue
        for t in range(LONG_WINDOW + 1, len(mids) - 2):
            h = mids[: t + 1 - shift]
            xs.append(sum(h[-SHORT_WINDOW:]) / SHORT_WINDOW / (sum(h[-LONG_WINDOW:]) / LONG_WINDOW) - 1)
            ys.append(mids[t + 1] / mids[t] - 1)
    return len(xs), st.correlation(xs, ys)


def by_symbol(fills: list, index: MidIndex, at: float) -> dict[str, float]:
    """Trading P&L per symbol, as trading_pnl but split by name."""
    out: dict[str, float] = {}
    for sym in sorted({f[2] for f in fills}):
        out[sym] = trading_pnl([f for f in fills if f[2] == sym], index, at)
    return out


def event_reactions(tape, fills: list, window: float = 90.0) -> list[dict]:
    """For each fired event: fills in its symbol in the next `window` seconds."""
    out = []
    for ts, kind, m in tape.records:
        if kind != "session_event" or m.get("event") != "CALENDAR_EVENT":
            continue
        text = m.get("message", "")
        sym = next((s for s in tape.symbols if s in text.split()), None)
        hits = [f for f in fills if ts <= f[0] <= ts + window and (sym is None or f[2] == sym)]
        out.append({"event": text, "fills": len(hits),
                    "first_after_s": round(hits[0][0] - ts, 2) if hits else None,
                    "shares_bought": sum(f[3] for f in hits if f[1] == "buy"),
                    "shares_sold": sum(f[3] for f in hits if f[1] == "sell")})
    return out


def live_fills(tape) -> list:
    """trader_1's own fills on the public tape, with the fee or rebate it got."""
    out = []
    for ts, kind, m in tape.records:
        if kind != "trade_execution" or LIVE_SEAT not in (m.get("buyer_id"), m.get("seller_id")):
            continue
        side = "buy" if m.get("buyer_id") == LIVE_SEAT else "sell"
        fee_cash = m.get("maker_rebate", 0.0) if m.get("maker_id") == LIVE_SEAT else -m.get("fee", 0.0)
        out.append((ts, side, m["symbol"], m["quantity"], m["price"], fee_cash))
    return out


def sim_placebo(seeds: int, ticks: int) -> list[dict]:
    """The crossover rule on the house simulator's random-walk prices.

    SimSession's `seed` alone does not change its prices (they are drawn from
    FUNDAMENTAL_SEED), so each seed also sets FUNDAMENTAL_SEED. The trader's
    half of the simulator's split fee equals the live taker fee.
    """
    import plugins.securities.defaults as defaults
    from shared.messages import Signal
    from sim.session import SimSession

    def crossover(symbol, prices, history, book, portfolio):
        if len(history) < LONG_WINDOW:
            return None
        s_ma = sum(history[-SHORT_WINDOW:]) / SHORT_WINDOW
        l_ma = sum(history[-LONG_WINDOW:]) / LONG_WINDOW
        held = portfolio.get("positions", {}).get(symbol, 0)
        px = prices.get(symbol)
        if s_ma > l_ma * (1 + BUFFER) and held < 100:
            return Signal(symbol=symbol, side="buy", quantity=QUANTITY, price=px)
        if s_ma < l_ma * (1 - BUFFER) and held > -100:
            return Signal(symbol=symbol, side="sell", quantity=QUANTITY, price=px)
        return None

    out = []
    original = defaults.FUNDAMENTAL_SEED
    try:
        for seed in range(seeds):
            defaults.FUNDAMENTAL_SEED = f"phase2-placebo-{seed}"
            res = SimSession().run(n_ticks=ticks, strategies=[("crossover", crossover)],
                                   symbols=["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"],
                                   fee_rate=2 * TAKER_FEE, seed=seed, verbose=False)
            curve = res.traders[0].pnl_curve
            peak, dd = -math.inf, 0.0
            for v in curve:
                peak = max(peak, v)
                dd = max(dd, peak - v)
            out.append({"seed": seed, "pnl": curve[-1] - curve[0], "max_dd": dd,
                        "fills": len(res.traders[0].fills)})
    finally:
        defaults.FUNDAMENTAL_SEED = original
    return out


# ── report ──────────────────────────────────────────────────────────────────

def summarise(values: list[float]) -> dict:
    return {"mean": st.mean(values), "sd": st.pstdev(values) if len(values) > 1 else 0.0,
            "positive": sum(v > 0 for v in values), "n": len(values)}


def plot(path: pathlib.Path, index: MidIndex, t0: float, t1: float,
         curves: dict[str, list], events: list[tuple[float, str]]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    grid = [t0 + k * 10.0 for k in range(int((t1 - t0) // 10) + 1)]
    fig, ax = plt.subplots(figsize=(9, 4.2))
    styles = {"live (trader_1, from the tape)": ("#111111", "-"),
              "replay: live bot": ("#1f77b4", "-"),
              "replay: signal only": ("#d62728", "--")}
    for label, fills in curves.items():
        colour, ls = styles[label]
        ax.plot([(g - t0) / 60 for g in grid], [trading_pnl(fills, index, g) for g in grid],
                color=colour, linestyle=ls, linewidth=1.6, label=label)
    for ts, text in events:
        ax.axvline((ts - t0) / 60, color="#999999", linewidth=0.8, linestyle=":")
        ax.text((ts - t0) / 60, ax.get_ylim()[1], " " + text, rotation=90, va="top",
                fontsize=7, color="#555555")
    ax.axhline(0, color="#cccccc", linewidth=0.8)
    ax.set_xlabel("minutes since the open (Session 5, Oct 29)")
    ax.set_ylabel("trading P&L, $ (fills, fees, rebates, marks)")
    ax.set_title("trader_1 trend signal: live vs. replay on the Session 5 tape")
    ax.legend(loc="lower left", fontsize=8, frameon=False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("tape", help="sessions/<file>.jsonl[.gz]")
    ap.add_argument("--cash", type=float, default=300_000.0)
    ap.add_argument("--seed-shares", type=int, default=20)
    ap.add_argument("--out", default=str(ROOT / "results" / "phase2" / "results.json"))
    ap.add_argument("--plot", default=str(ROOT / "docs" / "phase2" / "equity_week05.png"))
    a = ap.parse_args(argv)

    tape = eb.load_tape(pathlib.Path(a.tape))
    index = MidIndex(tape)
    t0, t1 = tape.records[0][0], tape.records[-1][0]
    report: dict = {"tape": a.tape, "fees": {"taker": TAKER_FEE, "maker_rebate": MAKER_REBATE},
                    "cash": a.cash, "seed_shares": a.seed_shares}

    # 1. whole session
    whole = {n: run_bot(tape, n, index, a.cash, a.seed_shares) for n in BOTS + tuple(LOOKAHEAD)}
    base = whole["do nothing"].pnl
    print(f"\n1. Whole session, fees taker {TAKER_FEE:.2%} / rebate {MAKER_REBATE:.2%}")
    print(f"   {'bot':38}{'P&L':>10}{'vs do-nothing':>15}{'max DD':>9}{'fills':>7}{'hit rate':>12}{'fees':>8}{'rebates':>9}")
    report["whole"] = {}
    for n, r in whole.items():
        closed, wins = round_trips(r.fills)
        hit = wins / closed if closed else float("nan")
        print(f"   {n:38}{r.pnl:>+10,.0f}{r.pnl - base:>+15,.0f}{r.max_dd:>9,.0f}"
              f"{r.taker + r.maker:>7}{f'{wins}/{closed} {hit:.0%}' if closed else '-':>12}"
              f"{r.fees:>8,.0f}{r.rebates:>9,.0f}")
        report["whole"][n] = {"pnl": r.pnl, "vs_do_nothing": r.pnl - base, "max_dd": r.max_dd,
                              "taker_fills": r.taker, "maker_fills": r.maker, "fees": r.fees,
                              "rebates": r.rebates, "round_trips": closed, "wins": wins,
                              "rejects": r.rejects}

    # 2. dispersion across independent blocks
    print(f"\n2. Dispersion: {BLOCKS} independent 10-minute blocks, P&L vs do-nothing")
    report["blocks"] = {}
    per_block: dict[str, list[float]] = defaultdict(list)
    for k, blk in enumerate(blocks_of(tape, BLOCKS)):
        runs = {n: run_bot(blk, n, MidIndex(blk), a.cash, a.seed_shares) for n in BOTS}
        for n in ("live bot", "signal only"):
            per_block[n].append(runs[n].pnl - runs["do nothing"].pnl)
        report["blocks"][k] = {n: {"pnl": r.pnl, "max_dd": r.max_dd} for n, r in runs.items()}
    for n, vals in per_block.items():
        s = summarise(vals)
        print(f"   {n:14} mean {s['mean']:+8,.0f}   sd {s['sd']:7,.0f}   blocks ahead of "
              f"do-nothing {s['positive']}/{s['n']}   per block: "
              + "  ".join(f"{v:+,.0f}" for v in vals))
        report.setdefault("block_summary", {})[n] = {**s, "values": vals}

    # 3. look-ahead check on the signal's forward returns
    print(f"\n3. Look-ahead check: crossover entries, mean move over the next "
          f"{FORWARD_UPDATES} updates (bps, in the signal's direction, before costs)")
    report["forward_edge"] = {}
    for label, shift in (("as live (mids up to t)", 0), ("1 update old (up to t-1)", 1),
                         ("1 update ahead (up to t+1)", -1),
                         ("planted bug: 20 updates ahead", -FORWARD_UPDATES)):
        n, mean, t = forward_edge(index, shift)
        print(f"   {label:30} entries {n:5}   mean {mean:+7.2f} bps   t-stat {t:+6.2f}")
        report["forward_edge"][label] = {"entries": n, "mean_bps": mean, "t_stat": t}
    print("   correlation of the feature with the very next mid move:")
    report["next_move_corr"] = {}
    for label, shift in (("as live", 0), ("1 update old", 1), ("1 update ahead", -1)):
        n, corr = next_move_corr(index, shift)
        print(f"   {label:30} pairs {n:6}   corr {corr:+.3f}")
        report["next_move_corr"][label] = {"pairs": n, "corr": corr}

    # 4. placebo in the simulator
    sims = sim_placebo(SIM_SEEDS, SIM_TICKS)
    s = summarise([x["pnl"] for x in sims])
    print(f"\n4. Placebo: same rule, house simulator, {SIM_SEEDS} price seeds x {SIM_TICKS} ticks, "
          f"taker {TAKER_FEE:.2%}")
    print(f"   P&L mean {s['mean']:+,.0f}   sd {s['sd']:,.0f}   seeds positive {s['positive']}/{s['n']}"
          f"   worst DD {max(x['max_dd'] for x in sims):,.0f}   per seed: "
          + "  ".join(f"{x['pnl']:+,.0f}" for x in sims))
    report["placebo"] = {"summary": s, "seeds": sims}

    # 5. live comparison
    live = live_fills(tape)
    replay = whole["live bot"].fills
    print("\n5. Live vs replay, trader_1 trading P&L on the same tape")
    report["live_vs_replay"] = {}
    for label, fills in (("live (from the tape)", live), ("replay: live bot", replay),
                         ("replay: signal only", whole["signal only"].fills)):
        closed, wins = round_trips(fills)
        buys = sum(1 for f in fills if f[1] == "buy")
        fees = -sum(f[5] for f in fills if f[5] < 0)
        reb = sum(f[5] for f in fills if f[5] > 0)
        pnl = trading_pnl(fills, index, t1)
        syms = sorted({f[2] for f in fills})
        print(f"   {label:22} fills {len(fills):4} ({buys} buys)   trading P&L {pnl:>+9,.0f}   "
              f"fees {fees:6,.0f}  rebates {reb:5,.0f}   symbols {', '.join(syms)}")
        report["live_vs_replay"][label] = {"fills": len(fills), "buys": buys, "trading_pnl": pnl,
                                           "fees": fees, "rebates": reb, "symbols": syms,
                                           "round_trips": closed, "wins": wins,
                                           "by_symbol": by_symbol(fills, index, t1),
                                           "events": event_reactions(tape, fills)}
    print("   trading P&L by symbol (live | replay):")
    lv, rp = (report["live_vs_replay"][k]["by_symbol"] for k in ("live (from the tape)", "replay: live bot"))
    for sym in sorted(set(lv) | set(rp)):
        print(f"     {sym:6} {lv.get(sym, 0.0):>+9,.0f} | {rp.get(sym, 0.0):>+9,.0f}")
    print("   fills in the event's symbol within 90 s of each event (live | replay):")
    for le, re_ in zip(*(report["live_vs_replay"][k]["events"] for k in ("live (from the tape)", "replay: live bot"))):
        print(f"     {le['event'][:34]:34} live {le['fills']:2} fills, first +{le['first_after_s']}s, "
              f"bought {le['shares_bought']}, sold {le['shares_sold']}  |  replay {re_['fills']:2} fills, "
              f"first +{re_['first_after_s']}s, bought {re_['shares_bought']}, sold {re_['shares_sold']}")

    def short_label(text: str) -> str:
        """'Earnings: NFLX moving +7.6%' -> 'NFLX +7.6%'; market-wide -> 'econ -3.0%'."""
        words = text.replace(":", "").split()
        move = words[-1] if words else ""
        name = next((w for w in words if w in tape.symbols), "econ")
        return f"{name} {move}"

    events = [(ts, short_label(m.get("message", ""))) for ts, kind, m in tape.records
              if kind == "session_event" and m.get("event") == "CALENDAR_EVENT"]
    plot(pathlib.Path(a.plot), index, t0, t1,
         {"live (trader_1, from the tape)": live, "replay: live bot": replay,
          "replay: signal only": whole["signal only"].fills}, events)
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {out} and {a.plot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
