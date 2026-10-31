#!/usr/bin/env python3
"""tape_to_parquet.py — turn an AlgoArena recording (JSONL) into a Parquet warehouse.

Week 4, Part 1 (slides 5–9, lab steps 1–2).

A recording line is  {"ts": <epoch s>, "tick": <engine tick>, "aux": <bool?>, "msg": {...}}.
One JSONL file mixes eight message types with different shapes. Parquet wants one
schema per table, so we write ONE TABLE PER MESSAGE TYPE, each with an explicit
pyarrow schema, partitioned by week and symbol (hive style: week=2/symbol=AAPL/):

    tape.parquet/
      book_snapshot/    week=2/symbol=AAPL/part-0.parquet ...  (top of book + full depth)
      trade_execution/  week=2/symbol=AAPL/...                 (every fill)
      order_ack/        week=2/symbol=AAPL/...                 (every accepted order, aux)
      order_cancel/     week=2/symbol=AAPL/...
      order_reject/     week=2/part-0.parquet                  (no symbol on a reject)
      session_event/    week=2/...                             (CALENDAR, CALENDAR_EVENT, ...)
      leaderboard/      week=2/...                             (one row per team per board)

Every row keeps BOTH clocks from the tape:
    tick   the engine's logical clock (an integer; the order things happened in)
    ts     the wall clock when the venue wrote the line (timestamp, UTC)
plus  seq  (line number in the file: the recording order, the tie-breaker)
and   t    (tick relative to the scenario's event clock, so "t400" in week02.json
           is t == 400 here; derived from the CALENDAR announcement vs the
           CALENDAR_EVENT that fired, falling back to the SESSION_OPEN tick).

Usage:
    python tape_to_parquet.py sessions/session_week02_20261009_204000.jsonl
    python tape_to_parquet.py <tape.jsonl> --out /tmp/w4/tape.parquet --week 2
    python tape_to_parquet.py a.jsonl b.jsonl            # several weeks, one warehouse
    python tape_to_parquet.py <tape.jsonl> --compression snappy --out /tmp/w4/snappy.parquet

Needs: pip install pyarrow   (tested with pyarrow 26.0.0, Python 3.13)
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import statistics
import time
from collections import defaultdict

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

TS = pa.timestamp("us", tz="UTC")
LIST_F = pa.list_(pa.float64())

# One explicit schema per message type. Strings that repeat (symbol, team ids,
# side, codes) are dictionary-encoded by Parquet automatically; we say so in
# the slide, and measure it in --report.
COMMON = [("seq", pa.int64()), ("ts", TS), ("tick", pa.int64()), ("t", pa.int32())]
SCHEMAS: dict[str, pa.Schema] = {
    "book_snapshot": pa.schema(COMMON + [
        ("week", pa.int16()), ("symbol", pa.string()),
        ("best_bid", pa.float64()), ("best_bid_qty", pa.float64()),
        ("best_ask", pa.float64()), ("best_ask_qty", pa.float64()),
        ("mid", pa.float64()), ("spread", pa.float64()),
        ("microprice", pa.float64()), ("obi", pa.float64()), ("ref_price", pa.float64()),
        ("bid_px", LIST_F), ("bid_qty", LIST_F), ("ask_px", LIST_F), ("ask_qty", LIST_F),
    ]),
    "trade_execution": pa.schema(COMMON + [
        ("week", pa.int16()), ("symbol", pa.string()), ("trade_id", pa.string()),
        ("price", pa.float64()), ("quantity", pa.int32()), ("notional", pa.float64()),
        ("aggressor", pa.string()), ("buyer_id", pa.string()), ("seller_id", pa.string()),
        ("taker_id", pa.string()), ("maker_id", pa.string()),
        ("fee", pa.float64()), ("maker_rebate", pa.float64()),
    ]),
    "order_ack": pa.schema(COMMON + [
        ("week", pa.int16()), ("symbol", pa.string()), ("order_id", pa.string()),
        ("team_id", pa.string()), ("side", pa.string()), ("price", pa.float64()),
        ("quantity", pa.int32()), ("queue_ahead", pa.int32()), ("level_qty", pa.int32()),
    ]),
    "order_cancel": pa.schema(COMMON + [
        ("week", pa.int16()), ("symbol", pa.string()), ("order_id", pa.string()),
        ("team_id", pa.string()), ("fee", pa.float64()),
    ]),
    "order_reject": pa.schema(COMMON + [
        ("week", pa.int16()), ("team_id", pa.string()), ("code", pa.string()),
        ("message", pa.string()),
    ]),
    "session_event": pa.schema(COMMON + [
        ("week", pa.int16()), ("event", pa.string()), ("message", pa.string()),
        ("kind", pa.string()), ("symbol", pa.string()), ("pct", pa.float64()),
        ("ramp_ticks", pa.int32()), ("overshoot", pa.float64()), ("data", pa.string()),
    ]),
    "leaderboard": pa.schema(COMMON + [
        ("week", pa.int16()), ("team_id", pa.string()), ("role", pa.string()),
        ("cash", pa.float64()), ("net_worth", pa.float64()),
        ("realized_pnl", pa.float64()), ("unrealized_pnl", pa.float64()),
        ("fees_paid", pa.float64()), ("rebates", pa.float64()), ("trade_count", pa.int32()),
    ]),
}
PARTITION = {"book_snapshot": ["week", "symbol"], "trade_execution": ["week", "symbol"],
             "order_ack": ["week", "symbol"], "order_cancel": ["week", "symbol"],
             "order_reject": ["week"], "session_event": ["week"], "leaderboard": ["week"]}


def _open(path: str):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path)


def guess_week(path: str) -> int:
    m = re.search(r"week(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else 0


def event_clock_offset(path: str) -> tuple[int, str]:
    """Absolute engine tick of the scenario's t0.

    A CALENDAR message announces 'earnings AAPL @ t400' (scenario time); the
    CALENDAR_EVENT that fires carries the absolute engine tick. Their
    difference is the offset. Without events, use the SESSION_OPEN tick.
    """
    announced: dict[tuple, int] = {}
    offsets: list[int] = []
    open_tick = None
    with _open(path) as f:
        for line in f:
            if '"session_event"' not in line:
                continue
            rec = json.loads(line)
            m, d = rec["msg"], rec["msg"].get("data") or {}
            if m.get("event") == "SESSION_OPEN" and open_tick is None:
                open_tick = int(d.get("tick", rec["tick"]))
            elif m.get("event") == "CALENDAR":
                for e in d.get("imminent") or []:
                    announced[(e.get("kind"), e.get("symbol"))] = int(e["tick"])
            elif m.get("event") == "CALENDAR_EVENT":
                key = (d.get("kind"), d.get("symbol"))
                if key in announced:
                    offsets.append(int(d.get("tick", rec["tick"])) - announced[key])
    if offsets:
        return int(statistics.median(offsets)), "CALENDAR vs CALENDAR_EVENT (%d events)" % len(offsets)
    if open_tick is not None:
        return open_tick, "SESSION_OPEN tick"
    return 0, "no session_event: t = tick"


def _f(x):
    return None if x is None else float(x)


def convert_one(path: str, week: int, cols: dict[str, dict[str, list]]) -> dict:
    """Stream one JSONL tape into per-type column lists. Returns counters."""
    t0, how = event_clock_offset(path)
    seen: dict[str, int] = defaultdict(int)
    with _open(path) as f:
        for seq, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            m = rec["msg"]
            kind = m.get("type")
            seen[kind] += 1
            if kind not in SCHEMAS:
                continue
            tick = int(rec["tick"])
            base = {"seq": seq, "ts": int(round(float(rec["ts"]) * 1e6)), "tick": tick,
                    "t": tick - t0, "week": week}
            c = cols[kind]
            if kind == "book_snapshot":
                bids, asks = m.get("bids") or [], m.get("asks") or []
                row = dict(base, symbol=m["symbol"],
                           best_bid=bids[0][0] if bids else None,
                           best_bid_qty=bids[0][1] if bids else None,
                           best_ask=asks[0][0] if asks else None,
                           best_ask_qty=asks[0][1] if asks else None,
                           mid=_f(m.get("mid_price")),
                           spread=(asks[0][0] - bids[0][0]) if (bids and asks) else None,
                           microprice=_f(m.get("microprice")), obi=_f(m.get("obi")),
                           ref_price=_f(m.get("ref_price")),
                           bid_px=[b[0] for b in bids], bid_qty=[b[1] for b in bids],
                           ask_px=[a[0] for a in asks], ask_qty=[a[1] for a in asks])
            elif kind == "trade_execution":
                row = dict(base, symbol=m["symbol"], trade_id=m.get("trade_id"),
                           price=float(m["price"]), quantity=int(m["quantity"]),
                           notional=float(m["price"]) * int(m["quantity"]),
                           aggressor=m.get("aggressor"), buyer_id=m.get("buyer_id"),
                           seller_id=m.get("seller_id"), taker_id=m.get("taker_id"),
                           maker_id=m.get("maker_id"), fee=_f(m.get("fee")),
                           maker_rebate=_f(m.get("maker_rebate")))
            elif kind == "order_ack":
                row = dict(base, symbol=m.get("symbol"), order_id=m.get("order_id"),
                           team_id=m.get("team_id"), side=m.get("side"),
                           price=_f(m.get("price")), quantity=m.get("quantity"),
                           queue_ahead=m.get("queue_ahead"), level_qty=m.get("level_qty"))
            elif kind == "order_cancel":
                row = dict(base, symbol=m.get("symbol"), order_id=m.get("order_id"),
                           team_id=m.get("team_id"), fee=_f(m.get("fee")))
            elif kind == "order_reject":
                row = dict(base, team_id=m.get("team_id"), code=m.get("code"),
                           message=m.get("message"))
            elif kind == "session_event":
                d = m.get("data") or {}
                row = dict(base, event=m.get("event"), message=m.get("message"),
                           kind=d.get("kind"), symbol=d.get("symbol"), pct=_f(d.get("pct")),
                           ramp_ticks=d.get("ramp_ticks"), overshoot=_f(d.get("overshoot")),
                           data=json.dumps(d, separators=(",", ":")))
            elif kind == "leaderboard":
                for p in (m.get("traders") or []) + (m.get("brokers") or []):
                    r = dict(base, team_id=p.get("team_id"), role=p.get("role"),
                             cash=_f(p.get("cash")), net_worth=_f(p.get("net_worth")),
                             realized_pnl=_f(p.get("realized_pnl")),
                             unrealized_pnl=_f(p.get("unrealized_pnl")),
                             fees_paid=_f(p.get("total_fees_paid")),
                             rebates=_f(p.get("total_rebates_earned")),
                             trade_count=p.get("trade_count"))
                    for k, v in r.items():
                        c[k].append(v)
                continue
            for k, v in row.items():
                c[k].append(v)
    return {"path": path, "week": week, "t0": t0, "t0_from": how, "counts": dict(seen)}


def write_warehouse(cols, out: str, compression: str, row_group: int) -> dict[str, int]:
    """One dataset per message type, hive-partitioned. Returns bytes per table."""
    if os.path.exists(out):
        shutil.rmtree(out)
    sizes = {}
    for kind, schema in SCHEMAS.items():
        data = cols.get(kind)
        if not data:
            continue
        arrays = []
        for field in schema:
            vals = data[field.name]
            if field.type == TS:
                arrays.append(pa.array(vals, pa.int64()).cast(TS))
            else:
                arrays.append(pa.array(vals, field.type))
        table = pa.Table.from_arrays(arrays, schema=schema).sort_by([("tick", "ascending"),
                                                                     ("seq", "ascending")])
        part = ds.partitioning(pa.schema([schema.field(n) for n in PARTITION[kind]]),
                               flavor="hive")
        ds.write_dataset(
            table, os.path.join(out, kind), format="parquet", partitioning=part,
            file_options=ds.ParquetFileFormat().make_write_options(compression=compression),
            max_rows_per_group=row_group, min_rows_per_group=min(row_group, 1024),
            existing_data_behavior="overwrite_or_ignore")
        sizes[kind] = sum(os.path.getsize(os.path.join(dp, fn))
                          for dp, _, fns in os.walk(os.path.join(out, kind)) for fn in fns)
    return sizes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tapes", nargs="+", help="session_*.jsonl (or .jsonl.gz)")
    ap.add_argument("--out", default="/tmp/w4/tape.parquet")
    ap.add_argument("--week", type=int, default=None,
                    help="week number (default: from the file name, e.g. session_week02_…)")
    ap.add_argument("--compression", default="zstd", choices=["zstd", "snappy", "gzip", "none"])
    ap.add_argument("--row-group", type=int, default=32_768, help="max rows per row group")
    a = ap.parse_args()

    cols: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    t_start = time.perf_counter()
    infos = []
    for p in a.tapes:
        info = convert_one(p, a.week if a.week is not None else guess_week(p), cols)
        infos.append(info)
    t_parse = time.perf_counter() - t_start
    sizes = write_warehouse(cols, a.out, a.compression, a.row_group)
    t_total = time.perf_counter() - t_start

    def jsonl_bytes(p: str) -> int:
        """Uncompressed size: a .gz stores it (mod 2**32) in its last 4 bytes."""
        if not p.endswith(".gz"):
            return os.path.getsize(p)
        with open(p, "rb") as f:
            f.seek(-4, os.SEEK_END)
            return int.from_bytes(f.read(4), "little")

    jsonl = sum(jsonl_bytes(p) for p in a.tapes)
    for info in infos:
        print(f"{os.path.basename(info['path'])}: week {info['week']}, t0 = tick {info['t0']} "
              f"({info['t0_from']})")
        print("  lines by type: " + ", ".join(f"{k} {v:,}" for k, v in
                                              sorted(info["counts"].items(), key=lambda kv: -kv[1])))
    print(f"\n{'table':<16} {'rows':>9} {'bytes':>12}")
    for kind, b in sizes.items():
        n = len(cols[kind]["seq"])
        print(f"{kind:<16} {n:>9,} {b:>12,}")
    tot = sum(sizes.values())
    print(f"{'TOTAL parquet':<16} {'':>9} {tot:>12,}   ({a.compression}, row groups ≤ {a.row_group:,})")
    print(f"{'input JSONL':<16} {'':>9} {jsonl:>12,}   -> {jsonl / tot:.1f}x smaller")
    print(f"parse {t_parse:.1f} s, total {t_total:.1f} s  ->  {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
