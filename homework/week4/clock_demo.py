#!/usr/bin/env python3
"""clock_demo.py — the logical clock and the real clock, measured on the tape.

Week 4, Part 2 (slides 17–21, lab step 5). Run tape_to_parquet.py first.

  A. The tape carries two clocks. `tick` is the venue's logical clock (an
     integer the engine increments); `ts` is the wall clock at which the
     venue wrote the line. How far apart are they?
  B. Event time vs processing time: a live bot whose on_tick takes 400 ms
     decides on one book and lands its order on another. How different?
  C. Determinism, the war story: a halt that resumes on a wall-clock sleep
     inside a tick-driven simulator (sim/arena.py before the 2026-10-09 fix).
     Same seed, two CPU speeds, two answers. Count ticks instead and the
     answer is the same on every machine.

Usage:
    python clock_demo.py [--warehouse /tmp/w4/tape.parquet] [--skip-c]
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

CT = ZoneInfo("America/Chicago")


def part_a(W: str) -> None:
    ev = (pl.scan_parquet(f"{W}/session_event/**/*.parquet", hive_partitioning=True)
          .sort("seq").collect())
    book = (pl.scan_parquet(f"{W}/book_snapshot/**/*.parquet", hive_partitioning=True)
            .select("seq", "ts", "tick", "t", "symbol", "mid").sort("seq").collect())
    allrows = pl.concat([
        pl.scan_parquet(f"{W}/{k}/**/*.parquet", hive_partitioning=True)
          .select("seq", "ts", "tick").collect()
        for k in ("book_snapshot", "trade_execution", "order_ack", "order_cancel",
                  "order_reject", "session_event")]).unique("seq").sort("seq")

    opened = ev.filter(pl.col("event") == "SESSION_OPEN").row(0, named=True)
    closed = ev.filter(pl.col("event") == "SESSION_CLOSED").row(0, named=True)
    n_ticks = closed["tick"] - opened["tick"]
    secs = (closed["ts"] - opened["ts"]).total_seconds()
    print("A. two clocks on one tape")
    print(f"   SESSION_OPEN  tick {opened['tick']:,}  ts {opened['ts'].astimezone(CT):%H:%M:%S.%f}"[:-3] + " CT")
    print(f"   SESSION_CLOSED tick {closed['tick']:,}  ts {closed['ts'].astimezone(CT):%H:%M:%S.%f}"[:-3] + " CT")
    print(f"   {n_ticks:,} ticks in {secs:,.1f} s of wall clock: {secs / n_ticks:.3f} s per tick on average")

    # wall time per tick: first ts at which each tick appears
    first = allrows.group_by("tick").agg(pl.col("ts").min()).sort("tick")
    dt = first["ts"].diff().dt.total_microseconds().drop_nulls().to_numpy() / 1e6
    q = np.percentile(dt, [1, 50, 99])
    print(f"   one tick of wall clock: p1 {q[0]:.3f} s · median {q[1]:.3f} s · p99 {q[2]:.3f} s"
          f" · max {dt.max():.3f} s")
    for r in ev.filter(pl.col("event") == "CALENDAR_EVENT").iter_rows(named=True):
        naive = opened["ts"] + timedelta(seconds=r["t"])      # "one tick = one second"
        late = (r["ts"] - naive).total_seconds()
        print(f"   {r['message']:<42} t{r['t']}: a 1-tick-per-second clock says "
              f"{naive.astimezone(CT):%H:%M:%S}, the tape says {r['ts'].astimezone(CT):%H:%M:%S}"
              f" ({late:+.0f} s)")
    backwards = int((allrows["ts"].diff().dt.total_microseconds().drop_nulls() < 0).sum())
    same_tick = allrows.group_by("tick").len()["len"]
    print(f"   lines per tick: median {int(same_tick.median())}, max {int(same_tick.max())}"
          f" · lines whose ts is EARLIER than the line before: {backwards:,} of {allrows.height:,}")
    aapl = book.filter(pl.col("symbol") == "AAPL").group_by("t").len()["len"]
    print(f"   AAPL book snapshots per tick: median {int(aapl.median())}, max {int(aapl.max())}"
          f" -> several book states share one tick; order inside a tick = seq")


def part_b(W: str, delay: float = 0.400) -> None:
    b = (pl.scan_parquet(f"{W}/book_snapshot/**/*.parquet", hive_partitioning=True)
         .filter(pl.col("symbol") == "AAPL")
         .select("seq", "ts", "t", "mid", "best_bid", "best_ask")
         .sort("seq").collect())
    ts = b["ts"].dt.epoch("us").to_numpy() / 1e6
    mid = b["mid"].to_numpy()
    ask = b["best_ask"].fill_null(np.nan).to_numpy()
    t = b["t"].to_numpy()
    j = np.searchsorted(ts, ts + delay, side="right") - 1     # book visible when the order lands
    arrived = j - np.arange(len(ts))
    dmid = 1e4 * np.abs(mid[j] / mid - 1)
    ask_moved = ~np.isclose(ask[j], ask, equal_nan=True)

    def row(mask, label):
        print(f"   {label:<34} decisions {mask.sum():>5,} · snapshots arrive meanwhile: median "
              f"{np.median(arrived[mask]):.0f}, p90 {np.percentile(arrived[mask], 90):.0f}"
              f" · |Δmid| median {np.median(dmid[mask]):.1f} bp, p90 {np.percentile(dmid[mask], 90):.1f} bp"
              f" · best ask changed {100 * ask_moved[mask].mean():.0f} %")
    print(f"\nB. a live on_tick that takes {delay * 1e3:.0f} ms: the book you decided on vs the "
          f"book when your order arrives (AAPL)")
    row(np.ones(len(t), bool), "whole block")
    row((t >= 400) & (t < 415), "t400-414 (AAPL earnings ramp)")
    row((t >= 900) & (t < 915), "t900-914 (econ print ramp)")
    print("   In replay with no latency model the bot sees the decision book and fills on it: "
          "the gap above is P&L the backtest invents.")


# ── C. the halt bug, in miniature ─────────────────────────────────────────────
async def season(seed: int, work_ms: float, halt: str, ticks: int = 600) -> tuple[float, int]:
    """A tick loop with one halt at t200. A momentum bot trades every tick it can.

    halt='sleep'  resume after asyncio.sleep(0.05) of WALL clock — how the
                  simulator inherited the venue's _resume_symbol (exchange/server.py)
    halt='ticks'  resume after 5 ticks of the LOGICAL clock — the 2026-10-09 fix
    work_ms       CPU time one tick costs (a busy loop): a slow laptop has more
    """
    rng = random.Random(seed)
    px, cash, pos, halted, halted_ticks, resume_at = 100.0, 0.0, 0, False, 0, None
    hist = []

    async def resume_later():
        nonlocal halted
        await asyncio.sleep(0.05)
        halted = False

    for tick in range(ticks):
        end = time.perf_counter() + work_ms / 1e3
        while time.perf_counter() < end:      # the work a tick costs on this CPU
            pass
        await asyncio.sleep(0)                # let other tasks (the resume) run
        px *= 1 + rng.gauss(0.0003 if tick < 300 else -0.0003, 0.002)
        hist.append(px)
        if tick == 200:
            halted = True
            if halt == "sleep":
                asyncio.get_running_loop().create_task(resume_later())
            else:
                resume_at = tick + 5
        if halt == "ticks" and resume_at is not None and tick >= resume_at:
            halted, resume_at = False, None
        if halted:
            halted_ticks += 1
            continue
        if len(hist) > 20:
            want = 10 if hist[-1] > hist[-20] else -10
            cash -= (want - pos) * px * 1.0015 if want > pos else (want - pos) * px * 0.9985
            pos = want
    return cash + pos * px, halted_ticks


def part_c() -> None:
    print("\nC. same seed, two CPU speeds: a wall-clock halt vs a tick-counted halt")
    for halt in ("sleep", "ticks"):
        for work_ms, label in ((0.2, "fast laptop (0.2 ms/tick)"), (0.6, "slow laptop (0.6 ms/tick)")):
            pnl, h = asyncio.run(season(seed=20261009, work_ms=work_ms, halt=halt))
            print(f"   halt={halt:<6} {label:<26} halted for {h:>3} ticks   P&L {pnl:+9.2f}")
    print("   sleep(0.05) spans 0.05 s / (time per tick) ticks: it depends on the machine. "
          "tick+5 does not.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--warehouse", default="/tmp/w4/tape.parquet")
    ap.add_argument("--skip-c", action="store_true")
    a = ap.parse_args()
    part_a(a.warehouse)
    part_b(a.warehouse)
    if not a.skip_c:
        part_c()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
