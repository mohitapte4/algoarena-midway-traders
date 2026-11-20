#!/usr/bin/env python
"""
scripts/risk_replay.py — how our seats behaved through a session's scenario
events, read from the session recording (Phase 3, Part B evidence).

    python scripts/risk_replay.py sessions/session_week06_20261106_214023_market.jsonl
    python scripts/risk_replay.py <tape> --team midway_traders

The tape is read once, line by line, in bounded memory (Lab 7, step 4):
what is kept is O(seats x symbols) state, not O(file). Real tapes reach 1 GB,
and scripts/replay_session.py::load holds every message in memory.

Most lines are book snapshots and almost all the parse time is spent on
them, so a line is only json-parsed when a cheap substring test says it
matters: every session event, every line naming one of our seats, and at
most one snapshot per symbol per second (enough for a mark). The timestamp is
read by slicing, without a parse.

Printed (markdown, ready for RISK.md):
  * every halt: our fills in the name while it was halted (must be 0) and
    the delay from the resume to our first fill in it;
  * every scenario event: our fills in the name in the 60 s after it;
  * per seat: fills, the largest traded position in any name (no runaway).

Positions are what each seat traded (signed fills); the venue's opening
grant and an IPO allocation arrive without a fill and are not on the tape.
"""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
from collections import defaultdict
from datetime import datetime
from time import perf_counter
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
EVENTS = {"CALENDAR_EVENT", "SHOCK", "FLASH", "IPO_LISTED", "DIVIDEND"}
HALT, RESUMED = "TRADING_HALT", "TRADING_RESUMED"
WINDOW_SEC = 60.0
MARK_EVERY_SEC = 1.0


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, CHICAGO).strftime("%H:%M:%S")


def open_tape(path: pathlib.Path):
    return gzip.open(path, "rt") if path.suffix == ".gz" else open(path)


def scan(path: pathlib.Path, team: str) -> dict:
    """One streaming pass; returns the events, halts and our fills."""
    events: list[dict] = []              # scenario events, in order
    halts: list[dict] = []               # {symbol, start, end, reason}
    open_halt: dict[str, dict] = {}
    fills: list[tuple] = []              # (ts, seat, symbol, side, qty, price, maker)
    marks: dict[str, float] = {}
    next_mark: dict[str, float] = defaultdict(float)
    parsed = lines = 0
    with open_tape(path) as fh:
        for line in fh:
            lines += 1
            if '"book_snapshot"' in line:
                # {"ts": 1794001223.423, ... — read ts without parsing the line
                ts = float(line[7:line.index(",", 7)])
                sym_at = line.find('"symbol":"')
                if sym_at < 0:
                    continue
                sym = line[sym_at + 10:line.index('"', sym_at + 10)]
                if ts < next_mark[sym]:
                    continue
                next_mark[sym] = ts + MARK_EVERY_SEC
                parsed += 1
                msg = json.loads(line)["msg"]
                mark = msg.get("ref_price") or msg.get("mid_price")
                if mark:
                    marks[sym] = mark
                continue
            if '"session_event"' not in line and team not in line:
                continue
            parsed += 1
            rec = json.loads(line)
            ts, msg = float(rec["ts"]), rec["msg"]
            kind = msg.get("type")
            if kind == "trade_execution":
                for role, side in (("buyer_id", "buy"), ("seller_id", "sell")):
                    seat = msg.get(role) or ""
                    if seat.startswith(team):
                        fills.append((ts, seat, msg["symbol"], side, int(msg["quantity"]),
                                      float(msg["price"]), msg.get("maker_id") == seat))
            elif kind == "session_event":
                ev, data = msg.get("event"), msg.get("data") or {}
                sym = data.get("symbol")
                if ev == HALT and sym:
                    open_halt[sym] = {"symbol": sym, "start": ts, "end": None,
                                      "reason": msg.get("message", "")}
                    halts.append(open_halt[sym])
                elif ev == RESUMED and sym:
                    if sym in open_halt:
                        open_halt.pop(sym)["end"] = ts
                    else:                # halted before the recording began
                        halts.append({"symbol": sym, "start": None, "end": ts,
                                      "reason": "(halted before the tape starts)"})
                elif ev in EVENTS:
                    syms = data.get("affected") or ([sym] if sym else [])
                    events.append({"ts": ts, "event": ev, "symbols": syms,
                                   "message": msg.get("message", ""),
                                   "marks": {s: marks.get(s) for s in syms}})
    return {"events": events, "halts": halts, "fills": fills,
            "lines": lines, "parsed": parsed}


def report(r: dict, team: str) -> str:
    fills = sorted(r["fills"])
    seats = sorted({f[1] for f in fills})
    out = []

    out.append("#### Halts: our fills while halted, and re-entry after the resume\n")
    out.append("| Symbol | Halted | Resumed | Our fills while halted | First fill after resume |")
    out.append("| --- | --- | --- | --- | --- |")
    for h in r["halts"]:
        start = h["start"] or -float("inf")
        end = h["end"] or float("inf")
        inside = [f for f in fills if f[2] == h["symbol"] and start <= f[0] < end]
        after = next((f for f in fills if f[2] == h["symbol"] and f[0] >= end), None)
        first = (f"{after[0] - end:.0f} s ({after[1].removeprefix(team + '_')})"
                 if after and h["end"] else "none")
        out.append(f"| {h['symbol']} | {clock(h['start']) if h['start'] else 'before open'} "
                   f"| {clock(h['end']) if h['end'] else 'not in tape'} | {len(inside)} | {first} |")

    out.append("\n#### Scenario events: our fills in the name over the next 60 s\n")
    out.append("| Time | Event | " + " | ".join(s.removeprefix(team + "_") for s in seats) + " |")
    out.append("| --- | --- |" + " --- |" * len(seats))
    for e in r["events"]:
        cells = []
        for seat in seats:
            mine = [f for f in fills if f[1] == seat and f[2] in e["symbols"]
                    and e["ts"] <= f[0] < e["ts"] + WINDOW_SEC]
            net = sum(q if s == "buy" else -q for _, _, _, s, q, _, _ in mine)
            cells.append(f"{len(mine)} fills, net {net:+d}" if mine else "—")
        label = e["message"][:60] or e["event"]
        out.append(f"| {clock(e['ts'])} | {label} | " + " | ".join(cells) + " |")

    out.append("\n#### Per seat: no runaway position\n")
    out.append("| Seat | Fills | Maker share | Largest traded position | End-of-block traded positions |")
    out.append("| --- | --- | --- | --- | --- |")
    for seat in seats:
        pos: dict[str, int] = defaultdict(int)
        peak = (0, "-")
        mine = [f for f in fills if f[1] == seat]
        for _, _, sym, side, qty, _, _ in mine:
            pos[sym] += qty if side == "buy" else -qty
            if abs(pos[sym]) > abs(peak[0]):
                peak = (pos[sym], sym)
        maker = sum(f[6] for f in mine) / len(mine)
        end = ", ".join(f"{s} {q:+d}" for s, q in sorted(pos.items()) if q) or "flat"
        out.append(f"| {seat.removeprefix(team + '_')} | {len(mine)} | {maker:.0%} "
                   f"| {peak[0]:+d} {peak[1]} | {end} |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tape", type=pathlib.Path)
    ap.add_argument("--team", default="midway_traders")
    args = ap.parse_args()

    t0 = perf_counter()
    r = scan(args.tape, args.team)
    dt = perf_counter() - t0
    print(report(r, args.team))
    print(f"\n{r['lines']:,} lines, {r['parsed']:,} parsed ({r['parsed'] / r['lines']:.0%}), "
          f"{len(r['fills'])} fills of ours, {dt:.2f} s")


if __name__ == "__main__":
    main()
