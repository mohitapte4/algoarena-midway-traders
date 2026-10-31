#!/usr/bin/env python3
"""parquet_anatomy.py — why the tape shrinks 39x and a query gets 1000x faster.

Week 4, Part 1 (slides 4–8, lab step 2). Every number on those slides is
printed by this file. Run tape_to_parquet.py first.

  A. where the JSONL bytes go (by message type)
  B. the same question seven ways: AAPL mid for every tick of the block
       JSONL + json.loads on every line   /  JSONL + substring prefilter
       DuckDB read_json on the JSONL     /  SQLite, indexed (if sql_baseline.py ran)
       Parquet (pyarrow, DuckDB, Polars)
  C. inside one Parquet file: row groups and their min/max statistics,
     and predicate pushdown (a 70-tick event window reads 1 row group of N)
  D. dictionary encoding: the same trade table with and without it
  E. compression codecs on the book table

Usage:
    python parquet_anatomy.py <tape.jsonl> [--warehouse /tmp/w4/tape.parquet]
    python parquet_anatomy.py <tape.jsonl.gz>    # unzipped once into /tmp/w4/anatomy/
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import time
from collections import Counter

import duckdb
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

SCRATCH = "/tmp/w4/anatomy"


def best_of(fn, n: int = 3):
    """Minimum wall time of n runs (the least-disturbed run) and the result."""
    times, out = [], None
    for _ in range(n):
        t = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t)
    return min(times), out


def mb(b: int) -> str:
    return f"{b / 1e6:,.2f} MB"


def bytes_by_type(tape: str) -> Counter:
    c: Counter = Counter()
    with open(tape, "rb") as f:
        for line in f:
            i = line.find(b'"type"')
            j = line.find(b'"', i + 7)
            k = line.find(b'"', j + 1)
            c[line[j + 1:k].decode()] += len(line)
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tape")
    ap.add_argument("--warehouse", default="/tmp/w4/tape.parquet")
    ap.add_argument("--db", default="/tmp/w4/tape.sqlite", help="SQLite copy (sql_baseline.py)")
    a = ap.parse_args()
    os.makedirs(SCRATCH, exist_ok=True)
    book_dir = os.path.join(a.warehouse, "book_snapshot")
    trade_dir = os.path.join(a.warehouse, "trade_execution")
    print(f"pyarrow {pa.__version__} · duckdb {duckdb.__version__} · polars {pl.__version__}")

    # ── A ────────────────────────────────────────────────────────────────
    if a.tape.endswith(".gz"):          # part B times raw JSON parsing: unzip once
        plain = os.path.join(SCRATCH, os.path.basename(a.tape)[:-3])
        if not os.path.exists(plain):
            with gzip.open(a.tape, "rb") as src, open(plain, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        print(f"(unzipped {os.path.basename(a.tape)} -> {plain})")
        a.tape = plain
    c = bytes_by_type(a.tape)
    tot = sum(c.values())
    print(f"\nA. JSONL bytes by message type ({mb(tot)} total)")
    for k, v in c.most_common():
        print(f"   {k:<16} {mb(v):>12}  {v / tot:6.1%}")

    # ── B ────────────────────────────────────────────────────────────────
    def py_full():
        n = 0
        with open(a.tape) as f:
            for line in f:
                m = json.loads(line)["msg"]
                if m.get("type") == "book_snapshot" and m["symbol"] == "AAPL":
                    n += 1
        return n

    def py_prefilter():
        n = 0
        with open(a.tape) as f:
            for line in f:
                if '"book_snapshot"' in line and '"AAPL"' in line:
                    m = json.loads(line)["msg"]
                    if m.get("type") == "book_snapshot" and m["symbol"] == "AAPL":
                        n += 1
        return n

    def duck_jsonl():
        return duckdb.sql(
            f"SELECT count(*) FROM read_json('{a.tape}', format='newline_delimited', "
            f"maximum_object_size=1000000000) "
            f"WHERE msg.type='book_snapshot' AND msg.symbol='AAPL'").fetchone()[0]

    sq = sqlite3.connect(a.db) if os.path.exists(a.db) else None

    def sqlite_idx():
        return len(sq.execute("SELECT tick, mid FROM book WHERE symbol = 'AAPL'").fetchall())

    def pa_parquet():
        return ds.dataset(book_dir, format="parquet", partitioning="hive").to_table(
            columns=["tick", "mid"], filter=pc.field("symbol") == "AAPL").num_rows

    def duck_parquet():
        return duckdb.sql(
            f"SELECT count(*) FROM read_parquet('{book_dir}/**/*.parquet', hive_partitioning=true) "
            f"WHERE symbol='AAPL'").fetchone()[0]

    def pl_parquet():
        return (pl.scan_parquet(f"{book_dir}/**/*.parquet", hive_partitioning=True)
                .filter(pl.col("symbol") == "AAPL").select("tick", "mid").collect().height)

    print(f"\nB. 'AAPL mid at every tick of the block' — same answer, {6 + bool(sq)} ways (best of n)")
    ways = [("JSONL, json.loads every line", py_full, 1),
            ("JSONL, substring prefilter + json.loads", py_prefilter, 3),
            ("JSONL, DuckDB read_json", duck_jsonl, 1)]
    if sq:
        ways.append(("SQLite, index on (symbol, t)", sqlite_idx, 5))
    ways += [("Parquet, pyarrow.dataset", pa_parquet, 5),
             ("Parquet, DuckDB", duck_parquet, 5),
             ("Parquet, Polars scan_parquet", pl_parquet, 5)]
    rows = []
    for name, fn, n in ways:
        t, r = best_of(fn, n)
        rows.append((name, t, r))
    base = rows[0][1]
    for name, t, r in rows:
        print(f"   {name:<42} {t * 1e3:9.1f} ms   {r:,} rows   {base / t:8.0f}x")

    # ── C ────────────────────────────────────────────────────────────────
    book = ds.dataset(book_dir, format="parquet", partitioning="hive").to_table()
    book = book.sort_by([("tick", "ascending"), ("seq", "ascending")])
    flat = os.path.join(SCRATCH, "book_flat_rg4096.parquet")
    pq.write_table(book, flat, compression="zstd", row_group_size=4096)
    md = pq.ParquetFile(flat).metadata
    tick_col = book.schema.get_field_index("t")
    print(f"\nC. one flat file, all symbols, sorted by tick: {md.num_rows:,} rows, "
          f"{md.num_row_groups} row groups of ≤ 4,096 ({mb(os.path.getsize(flat))})")
    lo, hi = 390, 460
    hit = 0
    for i in range(md.num_row_groups):
        st = md.row_group(i).column(tick_col).statistics
        overlap = not (st.max < lo or st.min > hi)
        hit += overlap
        if i < 3 or overlap or i == md.num_row_groups - 1:
            print(f"   row group {i:>2}: t in [{st.min:>5}, {st.max:>5}]"
                  f"{'   <- read for t in [390, 460]' if overlap else ''}")
        elif i == 3:
            print("   ...")
    print(f"   predicate t BETWEEN {lo} AND {hi}: footer statistics say read {hit} of "
          f"{md.num_row_groups} row groups")

    def read_all():
        return pq.read_table(flat).filter(
            (pc.field("t") >= lo) & (pc.field("t") <= hi)).num_rows

    def read_pushdown():
        return pq.read_table(flat, filters=[("t", ">=", lo), ("t", "<=", hi)]).num_rows

    t_all, n_all = best_of(read_all, 5)
    t_push, n_push = best_of(read_pushdown, 5)
    print(f"   read everything then filter: {t_all * 1e3:6.1f} ms ({n_all:,} rows)")
    print(f"   filters= (pushdown)        : {t_push * 1e3:6.1f} ms ({n_push:,} rows)")

    # ── D ────────────────────────────────────────────────────────────────
    trades = ds.dataset(trade_dir, format="parquet", partitioning="hive").to_table()
    with_d = os.path.join(SCRATCH, "trades_dict.parquet")
    without = os.path.join(SCRATCH, "trades_plain.parquet")
    pq.write_table(trades, with_d, compression="zstd", use_dictionary=True)
    pq.write_table(trades, without, compression="zstd", use_dictionary=False)
    pq.write_table(trades, os.path.join(SCRATCH, "trades_plain_nocomp.parquet"),
                   compression="none", use_dictionary=False)
    pq.write_table(trades, os.path.join(SCRATCH, "trades_dict_nocomp.parquet"),
                   compression="none", use_dictionary=True)

    def col_sizes(path):
        m = pq.ParquetFile(path).metadata
        out = {}
        for i in range(m.num_columns):
            name = m.schema.column(i).name
            out[name] = (sum(m.row_group(g).column(i).total_compressed_size
                             for g in range(m.num_row_groups)),
                         m.row_group(0).column(i).encodings)
        return out

    d1, d0 = col_sizes(with_d), col_sizes(without)
    n0 = col_sizes(os.path.join(SCRATCH, "trades_plain_nocomp.parquet"))
    n1 = col_sizes(os.path.join(SCRATCH, "trades_dict_nocomp.parquet"))
    distinct = {k: pc.count_distinct(trades[k]).as_py() for k in d1}
    print(f"\nD. dictionary encoding, trade table ({trades.num_rows:,} rows)")
    print(f"   {'column':<12} {'distinct':>8} {'plain':>9} {'dict':>9} {'plain+zstd':>11} {'dict+zstd':>10}")
    for k in ["symbol", "taker_id", "maker_id", "aggressor", "trade_id", "price"]:
        print(f"   {k:<12} {distinct[k]:>8,} {n0[k][0]:>9,} {n1[k][0]:>9,} "
              f"{d0[k][0]:>11,} {d1[k][0]:>10,}")
    print(f"   file: plain {os.path.getsize(without):,} B vs dictionary {os.path.getsize(with_d):,} B "
          f"(both zstd)")

    # ── E ────────────────────────────────────────────────────────────────
    print("\nE. the book table (all depth levels) under each codec")
    for codec in ["none", "snappy", "zstd", "gzip"]:
        p = os.path.join(SCRATCH, f"book_{codec}.parquet")
        pq.write_table(book, p, compression=codec, row_group_size=32_768)
        t, _ = best_of(lambda: pq.read_table(p, columns=["t", "symbol", "mid"]), 5)
        print(f"   {codec:<7} {mb(os.path.getsize(p)):>10}   read 3 columns {t * 1e3:5.1f} ms")
    print(f"   (the book_snapshot lines in the JSONL: {mb(c['book_snapshot'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
