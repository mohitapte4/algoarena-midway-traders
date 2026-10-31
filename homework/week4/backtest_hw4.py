#!/usr/bin/env python3
"""backtest_hw4.py — our momentum seat on the Parquet tape, on the logical clock, with baselines.

    python backtest_hw4.py [--speed 30]     # every week in the warehouse

Uses the lab's engine (backtest_parquet.run): decisions at tick t see books up to t, orders
fill on the first book of t + latency or later, every fill pays the venue's taker fee. The
strategy is told the tick and never reads a clock.

Fixed in this file, not tuned on results:
    split        SPLIT = 1400 (the lab's), IS is through the end of t1400, OOS is the rest
    parameters   the live seat's (team/momentum_trader.py, team/risk.py; events.py is a copy
                 of team/events.py)
    fee          the week's taker rate measured from the tape, sum(fee) / sum(notional)
    rules        RULES below, copied from season/weekNN.json

The engine only models taker fills, while the live seat rests quotes. So the port crosses the
spread, its crossover buffer must clear a taker round trip, max(0.3 %, 2 x fee), and it trades
when the signal changes state rather than on every tick the signal holds. It does not model
the short-sale rule.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib

import duckdb

from backtest_parquet import (EQUITIES, BuyAndHold, DoNothing, Signal, Strategy, TickMomentum,
                              load_tape, run)

from events import DEADLINE_RAMPS, FADE, RIDE, EventPlan, designed_edge, size_fraction

HERE = pathlib.Path(__file__).resolve().parent

SPLIT = 1400
RULES = {2: {"position_limit": 400, "shorts": False},       # season/weekNN.json
         3: {"position_limit": 600, "shorts": False},
         4: {"position_limit": 800, "shorts": True}}
SHORT, LONG, BUFFER, QUANTITY = 5, 20, 0.003, 5     # team/momentum_trader.py
MAX_SHARES, MAX_ORDER, MAX_SHORT = 200, 60, 100     # team/risk.py, team/momentum_trader.py
WINDOW_MARGIN = 10                                  # team/base_trader.py, seconds -> ticks
RAMP_TICKS = 15                                     # team/events.py DEFAULT_RAMP_TICKS


class MidwayMomentum(Strategy):
    """Ride good news, fade bad news, and trade the 5/20 crossover the rest of the time."""
    name = "midway_momentum"

    def __init__(self, fee: float, long_only: bool = True) -> None:
        self.fee, self.long_only = fee, long_only
        self.buffer = max(BUFFER, 2 * fee)
        self.new: list[dict] = []
        self.plans: dict[str, EventPlan] = {}
        self.window_until: dict[str, int] = {}
        self.state: dict[str, int] = {}
        self.seen = 0                           # fills already booked to a plan

    def on_event(self, ev, now):
        d = json.loads(ev["data"]) if ev.get("data") else {}
        if ev.get("event") == "CALENDAR":
            lead = int(d.get("announce_lead") or 0)
            for e in d.get("imminent") or []:
                names = [e["symbol"]] if e.get("symbol") in EQUITIES else EQUITIES
                for s in names:
                    self.window_until[s] = (now + lead + DEADLINE_RAMPS * RAMP_TICKS
                                            + WINDOW_MARGIN)
        elif ev.get("event") == "CALENDAR_EVENT" and ev.get("pct") is not None:
            for s in d.get("affected") or []:
                if s in EQUITIES:
                    self.new.append({"sym": s, "pct": float(ev["pct"]),
                                     "over": float(d.get("overshoot") or 1.3),
                                     "ramp": int(d.get("ramp_ticks") or RAMP_TICKS)})

    def _floor(self) -> int:
        """Lowest position a sell may reach."""
        return 0 if self.long_only else -MAX_SHORT

    def _open_plans(self, now, market, pf) -> None:
        for p in self.new:
            s, pct = p["sym"], p["pct"]
            mode = RIDE if pct > 0 else FADE
            frac = size_fraction(designed_edge(mode, pct, p["over"]), 2 * self.fee)
            hist = market.prices(s)
            if frac <= 0 or s in self.plans or len(hist) < 2:
                continue
            if mode == RIDE:
                target = int(MAX_SHARES * frac)
            else:
                target = int(min(MAX_SHARES, pf.working(s) - self._floor()) * frac)
            self.plans[s] = EventPlan(s, mode, pct, hist[-2], now, p["ramp"], p["over"], target)
            self.window_until[s] = max(self.window_until.get(s, 0),
                                       now + DEADLINE_RAMPS * p["ramp"] + WINDOW_MARGIN)
        self.new = []

    def _book_fills(self, pf) -> None:
        for t, s, side, px, q in pf.fills[self.seen:]:
            if s in self.plans:
                self.plans[s].record(side, q, px)
        self.seen = len(pf.fills)

    def _work_plan(self, plan: EventPlan, now, market, pf) -> Signal | None:
        s, bid, ask = plan.symbol, market.best_bid(plan.symbol), market.best_ask(plan.symbol)
        if plan.mode == RIDE:
            room = plan.target_shares - plan.bought
            if now < plan.entry_end and room > 0:
                if ask and ask <= plan.buy_cap:
                    return Signal(symbol=s, side="buy", quantity=min(room, MAX_ORDER),
                                  price=round(plan.buy_cap, 2))
                return None
            if plan.held <= 0:
                plan.done = True
            elif bid and (bid >= plan.target or bid <= plan.stop_price or now >= plan.deadline):
                return Signal(symbol=s, side="sell", quantity=min(plan.held, MAX_ORDER),
                              price=round(bid * 0.997, 2))
            return None
        if now < plan.rebuy_start:
            room = min(plan.target_shares - plan.sold, pf.working(s) - self._floor())
            if room > 0 and bid and bid >= plan.sell_floor:
                return Signal(symbol=s, side="sell", quantity=min(room, MAX_ORDER),
                              price=round(plan.sell_floor, 2))
            return None
        if plan.owed <= 0:
            plan.done = True
        elif now >= plan.deadline and ask:
            return Signal(symbol=s, side="buy", quantity=min(plan.owed, MAX_ORDER),
                          price=round(ask * 1.003, 2))
        elif ask and ask <= plan.rebuy_cap:
            return Signal(symbol=s, side="buy", quantity=min(plan.owed, MAX_ORDER),
                          price=round(plan.rebuy_cap, 2))
        return None

    def on_tick(self, now, market, pf):
        self._open_plans(now, market, pf)
        self._book_fills(pf)
        out = []
        for s, plan in list(self.plans.items()):
            if pf.inflight[s]:                   # wait for the venue's answer
                continue
            sig = self._work_plan(plan, now, market, pf)
            if sig:
                out.append(sig)
            if plan.done:
                del self.plans[s]
        for s in EQUITIES:
            hist = market.prices(s)
            bid, ask = market.best_bid(s), market.best_ask(s)
            if s in self.plans or now <= self.window_until.get(s, -1) or len(hist) < LONG:
                continue
            short_ma = sum(hist[-SHORT:]) / SHORT
            long_ma = sum(hist[-LONG:]) / LONG
            state = (1 if short_ma > long_ma * (1 + self.buffer)
                     else -1 if short_ma < long_ma * (1 - self.buffer) else 0)
            changed = state != self.state.get(s, 0)
            self.state[s] = state
            if not changed or not (bid and ask):
                continue
            if state == 1:
                out.append(Signal(symbol=s, side="buy", quantity=QUANTITY,
                                  price=round(ask * 1.003, 2)))
            elif state == -1:
                qty = min(QUANTITY, pf.working(s) - self._floor())
                if qty >= 1:
                    out.append(Signal(symbol=s, side="sell", quantity=qty,
                                      price=round(bid * 0.997, 2)))
        return out


def sharpe(equity: list[float]) -> float:
    """Mean / sample std (ddof=1) of per-tick equity changes. Not annualised."""
    d = [b - a for a, b in zip(equity, equity[1:])]
    mu = sum(d) / len(d)
    var = sum((x - mu) ** 2 for x in d) / (len(d) - 1)
    return mu / math.sqrt(var) if var > 0 else float("nan")


def row(r) -> dict:
    return {"name": r.name, "pnl": r.pnl, "is": r.pnl_is, "oos": r.pnl_oos, "max_dd": r.max_dd,
            "sharpe": sharpe(r.equity), "fills": r.fills, "fees": r.fees, "digest": r.digest}


def taker_fee(W: str, week: int) -> float:
    return duckdb.sql(
        f"SELECT sum(fee) / sum(notional) FROM read_parquet('{W}/trade_execution/**/*.parquet', "
        f"hive_partitioning = true) WHERE week = {week} AND fee > 0").fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--warehouse", default=str(HERE / "build" / "tape.parquet"))
    ap.add_argument("--speed", type=float, default=30.0, help="paced replay for the digest proof")
    ap.add_argument("--report", default=str(HERE / "results" / "backtest.json"))
    a = ap.parse_args()
    W = a.warehouse
    weeks = sorted(int(p.name.split("=")[1]) for p in pathlib.Path(W, "book_snapshot").iterdir())
    report = {"split": SPLIT, "speed": a.speed, "weeks": {}}
    for week in weeks:
        fee = round(taker_fee(W, week), 6)
        cap, long_only = RULES[week]["position_limit"], not RULES[week]["shorts"]
        tape = load_tape(W, week, None, None)
        kw = dict(fee=fee, latency=1, mode="sweep", long_only=long_only, cap=cap, split=SPLIT)

        def me():
            return MidwayMomentum(fee, long_only)

        base = run(me(), tape, **kw)
        rows = [base, run(DoNothing(), tape, **kw), run(BuyAndHold(), tape, **kw),
                run(TickMomentum(), tape, **kw)]
        sens = []
        for label, over in (("latency 0", {"latency": 0}), ("latency 1 (base)", {}),
                            ("latency 2", {"latency": 2}), ("touch fills", {"mode": "touch"})):
            r = run(me(), tape, **dict(kw, **over))
            r.name = label
            sens.append(row(r))

        # Determinism: twice unpaced, once paced on the tape's ts.
        again = run(me(), tape, **kw)
        paced = run(me(), tape, **dict(kw, speed=a.speed))
        proof = {"unpaced": base.digest, "unpaced_again": again.digest, "paced": paced.digest,
                 "paced_wall_s": round(paced.wall, 1), "pnl_unpaced": base.pnl,
                 "pnl_paced": paced.pnl,
                 "identical": base.digest == again.digest == paced.digest}

        report["weeks"][str(week)] = {
            "fee": fee, "cap": cap, "long_only": long_only, "ticks": len(tape.ticks),
            "events": [(t, e["message"]) for t, evs in sorted(tape.events_at.items()) for e in evs
                       if e["event"] == "CALENDAR_EVENT"],
            "rows": [row(r) for r in rows], "sensitivity": sens, "digests": proof}
        print(f"\nweek {week}: fee {fee:.4%} · cap {cap} · long_only {long_only} · "
              f"{len(tape.ticks):,} ticks")
        for r in report["weeks"][str(week)]["rows"] + sens:
            print(f"  {r['name']:<18} P&L {r['pnl']:>+10,.0f}  IS {r['is']:>+9,.0f}  "
                  f"OOS {r['oos']:>+9,.0f}  DD {r['max_dd']:>8,.0f}  Sharpe {r['sharpe']:>+8.4f}  "
                  f"fills {r['fills']:>5}  fees {r['fees']:>8,.0f}  {r['digest']}")
        print("  digests", proof)
    os.makedirs(os.path.dirname(a.report), exist_ok=True)
    with open(a.report, "w") as f:
        json.dump(report, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
