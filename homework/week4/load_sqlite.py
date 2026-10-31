#!/usr/bin/env python3
"""load_sqlite.py — store each tape in SQLite, one load per week.

    python load_sqlite.py --rebuild session_week*.jsonl.gz
    python load_sqlite.py <new tape>     # appends the new week; old rows are not touched

Each tape is loaded into its own build/sqlite/weekNN.sqlite first, which is how one week's
size without and with indexes is measured, then appended to build/tape.sqlite, the database
the queries read. A week already loaded is skipped.

Every table's primary key starts with (week, seq): seq and t restart in each tape, so
neither identifies a row or orders it across weeks.

Indexes, and why each exists:
    book_sym_t   book(week, symbol, t, seq)  the event window, the forward markout and the
                                             point lookup: the latest or first book of a
                                             symbol around tick t, last by seq inside a tick
    book_sym_seq book(week, symbol, seq)     the backward as-of: the last book of this symbol
                                             written before the fill's line
    trade_buyer  trade(buyer_id)             find our own fills without reading every trade
    trade_seller trade(seller_id)            the same for the sell side
No index on session_event: under 20 rows a week, so a scan is the cheapest plan.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time

from sql_baseline import jsonl_bytes, rows_from_tape
from tape_to_parquet import guess_week

HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE book (
    week INTEGER NOT NULL, seq INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL,
    ts REAL NOT NULL, symbol TEXT NOT NULL,
    best_bid REAL, best_bid_qty REAL, best_ask REAL, best_ask_qty REAL,
    mid REAL, spread REAL, microprice REAL, obi REAL,
    PRIMARY KEY (week, seq)) STRICT;
CREATE TABLE book_level (
    week INTEGER NOT NULL, seq INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('B', 'A')), lvl INTEGER NOT NULL,
    px REAL NOT NULL, qty REAL NOT NULL,
    PRIMARY KEY (week, seq, side, lvl)) WITHOUT ROWID, STRICT;
CREATE TABLE trade (
    week INTEGER NOT NULL, seq INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL,
    ts REAL NOT NULL, symbol TEXT NOT NULL, trade_id TEXT, price REAL NOT NULL,
    quantity INTEGER NOT NULL, aggressor TEXT, buyer_id TEXT, seller_id TEXT,
    taker_id TEXT, maker_id TEXT, fee REAL, maker_rebate REAL,
    PRIMARY KEY (week, seq)) STRICT;
CREATE TABLE order_ack (
    week INTEGER NOT NULL, seq INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL,
    ts REAL NOT NULL, symbol TEXT, order_id TEXT, team_id TEXT, side TEXT, price REAL,
    quantity INTEGER, queue_ahead INTEGER, level_qty INTEGER,
    PRIMARY KEY (week, seq)) STRICT;
CREATE TABLE order_cancel (
    week INTEGER NOT NULL, seq INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL,
    ts REAL NOT NULL, symbol TEXT, order_id TEXT, team_id TEXT, fee REAL,
    PRIMARY KEY (week, seq)) STRICT;
CREATE TABLE order_reject (
    week INTEGER NOT NULL, seq INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL,
    ts REAL NOT NULL, team_id TEXT, code TEXT, message TEXT,
    PRIMARY KEY (week, seq)) STRICT;
CREATE TABLE session_event (
    week INTEGER NOT NULL, seq INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL,
    ts REAL NOT NULL, event TEXT, message TEXT, kind TEXT, symbol TEXT, pct REAL, data TEXT,
    PRIMARY KEY (week, seq)) STRICT;
CREATE TABLE leaderboard (
    week INTEGER NOT NULL, seq INTEGER NOT NULL, tick INTEGER NOT NULL, t INTEGER NOT NULL,
    ts REAL NOT NULL, team_id TEXT NOT NULL, role TEXT, cash REAL, net_worth REAL,
    realized_pnl REAL, unrealized_pnl REAL, fees_paid REAL, rebates REAL, trade_count INTEGER,
    PRIMARY KEY (week, seq, team_id)) STRICT;
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS book_sym_t   ON book (week, symbol, t, seq);
CREATE INDEX IF NOT EXISTS book_sym_seq ON book (week, symbol, seq);
CREATE INDEX IF NOT EXISTS trade_buyer  ON trade (buyer_id);
CREATE INDEX IF NOT EXISTS trade_seller ON trade (seller_id);
"""
TABLES = ["book", "book_level", "trade", "order_ack", "order_cancel", "order_reject",
          "session_event", "leaderboard"]


def load_week(path: str, week: int, db: str) -> dict:
    """One tape into its own file: rows, bytes without and with indexes, seconds."""
    if os.path.exists(db):
        os.remove(db)
    t0 = time.perf_counter()
    batches: dict[str, list[tuple]] = {k: [] for k in TABLES}
    for table, row in rows_from_tape(path):
        batches[table].append((week,) + row)
    t_parse = time.perf_counter() - t0
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    with con:                                     # one transaction for the whole load
        for table, rows in batches.items():
            if rows:
                marks = ",".join("?" * len(rows[0]))
                con.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    t_insert = time.perf_counter() - t0 - t_parse
    bytes_plain = os.path.getsize(db)
    t1 = time.perf_counter()
    con.executescript(INDEXES)
    con.commit()
    t_index = time.perf_counter() - t1
    con.close()
    return {"tape": os.path.basename(path), "jsonl_bytes": jsonl_bytes(path),
            "sqlite_bytes_no_index": bytes_plain, "sqlite_bytes_indexed": os.path.getsize(db),
            "parse_s": round(t_parse, 2), "insert_s": round(t_insert, 2),
            "index_s": round(t_index, 3), "load_s": round(t_parse + t_insert + t_index, 2),
            "rows": {k: len(v) for k, v in batches.items()}}


def append_week(db: str, week_db: str) -> None:
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS") + INDEXES)
    con.execute("ATTACH DATABASE ? AS wk", (week_db,))
    with con:
        for table in TABLES:
            con.execute(f"INSERT INTO main.{table} SELECT * FROM wk.{table}")
    con.execute("DETACH DATABASE wk")
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tapes", nargs="+")
    ap.add_argument("--db", default=os.path.join(HERE, "build", "tape.sqlite"))
    ap.add_argument("--report", default=os.path.join(HERE, "results", "storage_sqlite.json"))
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()
    week_dir = os.path.join(os.path.dirname(os.path.abspath(a.db)), "sqlite")
    if a.rebuild:
        shutil.rmtree(week_dir, ignore_errors=True)
        if os.path.exists(a.db):
            os.remove(a.db)
    os.makedirs(week_dir, exist_ok=True)
    report = {}
    if os.path.exists(a.report) and not a.rebuild:
        with open(a.report) as f:
            report = json.load(f)

    for path in a.tapes:
        week = guess_week(path)
        if str(week) in report:
            print(f"week {week}: already in {a.db}, skipped")
            continue
        week_db = os.path.join(week_dir, f"week{week:02d}.sqlite")
        info = load_week(path, week, week_db)
        append_week(a.db, week_db)
        report[str(week)] = info
        print(f"week {week}: {info['sqlite_bytes_no_index']:,} -> "
              f"{info['sqlite_bytes_indexed']:,} bytes, {sum(info['rows'].values()):,} rows, "
              f"{info['load_s']:.1f} s")

    os.makedirs(os.path.dirname(a.report), exist_ok=True)
    with open(a.report, "w") as f:
        json.dump(report, f, indent=1)
    print(f"{a.db}: {os.path.getsize(a.db):,} bytes, sqlite {sqlite3.sqlite_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
