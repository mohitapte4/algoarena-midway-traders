#!/usr/bin/env python3
"""load_parquet.py — store each tape as new week=N folders of a hive-partitioned warehouse.

    python load_parquet.py --rebuild session_week*.jsonl.gz
    python load_parquet.py <new tape>     # adds week=N; older weeks are not opened

One dataset per message type, reusing the lab's schemas and parser from
tape_to_parquet.py. Tables whose rows have a symbol are partitioned by week/symbol;
order_reject, session_event and leaderboard have no per-row symbol (or only a nullable
one), so they are partitioned by week. A week that already exists is skipped, never
rewritten. Each table is sorted by (tick, seq).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from collections import defaultdict

import pyarrow as pa
import pyarrow.dataset as ds

from sql_baseline import jsonl_bytes
from tape_to_parquet import PARTITION, SCHEMAS, TS, convert_one, guess_week

HERE = os.path.dirname(os.path.abspath(__file__))


def tree_bytes(path: str) -> int:
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


def week_folders(out: str, week: int) -> list[str]:
    return [os.path.join(out, k, f"week={week}") for k in SCHEMAS
            if os.path.isdir(os.path.join(out, k, f"week={week}"))]


def write_week(cols, out: str, row_group: int) -> dict[str, int]:
    """Write one week's columns; returns rows per table."""
    rows = {}
    for kind, schema in SCHEMAS.items():
        data = cols.get(kind)
        if not data:
            rows[kind] = 0
            continue
        arrays = [pa.array(data[f.name], pa.int64()).cast(TS) if f.type == TS
                  else pa.array(data[f.name], f.type) for f in schema]
        table = pa.Table.from_arrays(arrays, schema=schema).sort_by(
            [("tick", "ascending"), ("seq", "ascending")])
        ds.write_dataset(
            table, os.path.join(out, kind), format="parquet",
            partitioning=ds.partitioning(pa.schema([schema.field(n) for n in PARTITION[kind]]),
                                         flavor="hive"),
            file_options=ds.ParquetFileFormat().make_write_options(compression="zstd"),
            max_rows_per_group=row_group, min_rows_per_group=min(row_group, 1024),
            existing_data_behavior="overwrite_or_ignore")
        rows[kind] = table.num_rows
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tapes", nargs="+")
    ap.add_argument("--out", default=os.path.join(HERE, "build", "tape.parquet"))
    ap.add_argument("--report", default=os.path.join(HERE, "results", "storage_parquet.json"))
    ap.add_argument("--row-group", type=int, default=32_768)
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()
    if a.rebuild:
        shutil.rmtree(a.out, ignore_errors=True)
    os.makedirs(a.out, exist_ok=True)
    report = {}
    if os.path.exists(a.report) and not a.rebuild:
        with open(a.report) as f:
            report = json.load(f)

    for path in a.tapes:
        week = guess_week(path)
        if week_folders(a.out, week):
            print(f"week {week}: already in {a.out}, skipped")
            continue
        t0 = time.perf_counter()
        cols: dict = defaultdict(lambda: defaultdict(list))
        info = convert_one(path, week, cols)
        rows = write_week(cols, a.out, a.row_group)
        secs = time.perf_counter() - t0
        size = sum(tree_bytes(p) for p in week_folders(a.out, week))
        report[str(week)] = {"tape": os.path.basename(path), "jsonl_bytes": jsonl_bytes(path),
                             "parquet_bytes": size, "convert_s": round(secs, 2), "rows": rows,
                             "t0": info["t0"], "t0_from": info["t0_from"]}
        print(f"week {week}: {size:,} bytes, {sum(rows.values()):,} rows, {secs:.1f} s, "
              f"t0 = tick {info['t0']} ({info['t0_from']})")

    os.makedirs(os.path.dirname(a.report), exist_ok=True)
    with open(a.report, "w") as f:
        json.dump(report, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
