#!/usr/bin/env python3
"""sql_baseline.py — the tape in a relational database (SQLite), measured.

Week 4, Parts 2–3 (lab step 1). The first answer to "where do I keep market
data?" is a SQL database. This script loads the venue's recording into SQLite
with an ordinary relational schema, indexes it, asks it the questions a
backtest asks, and times every step — so the next slides can say exactly
where SQL is fine and where it struggles with tick data.

  A. schema + load     one table per message type; the order-book depth
                       NORMALISED into book_level (one row per price level);
                       parse time, insert time, rows/s, file size
  B. commit per row    the write path of a naive live recorder: one INSERT
                       + COMMIT per message (what "write throughput" means)
  C. indexes           CREATE INDEX on (symbol, t, seq): build time, file size
  D. queries           the same five questions WITHOUT an index, WITH one,
                       and DuckDB over the Parquet tape (if it exists):
                         1 AAPL mid at every tick          (one symbol, 2 columns)
                         2 every book in t 390..460         (a time-range scan)
                         3 fills and fees per taker         (group-by over a table)
                         4 AAPL event window t400 +5/+15/+60 (four as-of lookups)
                         5 markout of every fill, 60 ticks later
                                                           (an as-of JOIN: 4,511 lookups)
  E. sizes             JSONL vs SQLite (with and without indexes) vs Parquet
  F. scale (opt-in)    the top-of-book table copied into --scale N "weeks"
                       (--scale 50 ≈ 2.8 M rows): SQLite (B-tree, row store)
                       vs DuckDB over Parquet (column store) on a full-scan
                       aggregate, a per-week aggregate and a point lookup.
                       Off by default since 2026-10-11: Week 4 stays on the
                       one tape; the scale comparison is Week 6 material

PostgreSQL would use the same schema and the same SQL (BIGINT/DOUBLE
PRECISION/TEXT, a B-tree on (symbol, t, seq)); SQLite is used here because
it is in the Python standard library and needs no server.

Usage:
    python sql_baseline.py sessions/session_week02_20261009_204000.jsonl      (or .jsonl.gz)
    python sql_baseline.py <tape> --db /tmp/w4/tape.sqlite --warehouse /tmp/w4/tape.parquet
    python sql_baseline.py <tape> --skip-noindex-join     # skip the slow un-indexed as-of join
    python sql_baseline.py <tape> --scale 50              # part F (Week 6): x50 the book

Needs: Python's sqlite3 (standard library); duckdb for the Parquet column.
Run tape_to_parquet.py first if you want the Parquet column filled in.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tape_to_parquet import _open, event_clock_offset  # noqa: E402  (same folder)

SCHEMA = """
CREATE TABLE book (            -- one row per book_snapshot: top of book
    seq INTEGER PRIMARY KEY,   -- line number in the recording (the order)
    tick INTEGER NOT NULL,     -- engine tick (logical clock)
    t INTEGER NOT NULL,        -- scenario tick (week02.json's t400)
    ts REAL NOT NULL,          -- wall clock the venue wrote the line at
    symbol TEXT NOT NULL,
    best_bid REAL, best_bid_qty REAL, best_ask REAL, best_ask_qty REAL,
    mid REAL, spread REAL, microprice REAL, obi REAL);
CREATE TABLE book_level (      -- the depth, normalised: one row per level
    seq INTEGER NOT NULL REFERENCES book(seq),
    side TEXT NOT NULL CHECK (side IN ('B', 'A')),
    lvl INTEGER NOT NULL,
    px REAL NOT NULL, qty REAL NOT NULL,
    PRIMARY KEY (seq, side, lvl)) WITHOUT ROWID;
CREATE TABLE trade (
    seq INTEGER PRIMARY KEY, tick INTEGER, t INTEGER, ts REAL, symbol TEXT,
    trade_id TEXT, price REAL, quantity INTEGER, aggressor TEXT,
    buyer_id TEXT, seller_id TEXT, taker_id TEXT, maker_id TEXT,
    fee REAL, maker_rebate REAL);
CREATE TABLE order_ack (
    seq INTEGER PRIMARY KEY, tick INTEGER, t INTEGER, ts REAL, symbol TEXT,
    order_id TEXT, team_id TEXT, side TEXT, price REAL, quantity INTEGER,
    queue_ahead INTEGER, level_qty INTEGER);
CREATE TABLE order_cancel (
    seq INTEGER PRIMARY KEY, tick INTEGER, t INTEGER, ts REAL, symbol TEXT,
    order_id TEXT, team_id TEXT, fee REAL);
CREATE TABLE order_reject (
    seq INTEGER PRIMARY KEY, tick INTEGER, t INTEGER, ts REAL,
    team_id TEXT, code TEXT, message TEXT);
CREATE TABLE session_event (
    seq INTEGER PRIMARY KEY, tick INTEGER, t INTEGER, ts REAL, event TEXT,
    message TEXT, kind TEXT, symbol TEXT, pct REAL, data TEXT);
CREATE TABLE leaderboard (
    seq INTEGER, tick INTEGER, t INTEGER, ts REAL, team_id TEXT, role TEXT,
    cash REAL, net_worth REAL, realized_pnl REAL, unrealized_pnl REAL,
    fees_paid REAL, rebates REAL, trade_count INTEGER);
"""
INDEXES = """
CREATE INDEX book_sym_t  ON book (symbol, t, seq);
CREATE INDEX book_t      ON book (t);
CREATE INDEX trade_sym_t ON trade (symbol, t);
CREATE INDEX event_kind  ON session_event (event, t);
"""
COLS = {"book": 13, "book_level": 5, "trade": 15, "order_ack": 12, "order_cancel": 8,
        "order_reject": 7, "session_event": 10, "leaderboard": 13}

# ── the five questions, in SQLite's SQL ─────────────────────────────────────
Q_SQLITE = {
    "1 AAPL mid at every tick":
        "SELECT t, mid FROM book WHERE symbol = 'AAPL'",
    "2 every book in t 390..460":
        "SELECT symbol, t, mid FROM book WHERE t BETWEEN 390 AND 460",
    "3 fills and fees per taker":
        "SELECT taker_id, count(*), sum(quantity), sum(fee) FROM trade "
        "GROUP BY taker_id ORDER BY 2 DESC",
    # an as-of lookup in plain SQL: ORDER BY ... LIMIT 1, once per point
    "4 AAPL event window t400":
        "WITH ev AS (SELECT t FROM session_event WHERE event = 'CALENDAR_EVENT' "
        "            AND kind = 'earnings' AND symbol = 'AAPL'), "
        "k(k) AS (VALUES (-1), (5), (15), (60)) "
        "SELECT k.k, (SELECT b.mid FROM book b WHERE b.symbol = 'AAPL' AND b.t <= ev.t + k.k "
        "             ORDER BY b.t DESC, b.seq DESC LIMIT 1) AS mid "
        "FROM ev, k ORDER BY k.k",
    # the as-of JOIN: one correlated subquery per fill (forward: first tick >= t+60,
    # last snapshot of that tick = the end-of-tick book)
    "5 markout of every fill +60":
        "SELECT count(*), avg(1e4 * sgn * (m60 - price) / price) FROM ("
        " SELECT f.price, CASE WHEN f.aggressor = 'buy' THEN 1 ELSE -1 END AS sgn,"
        "  (SELECT b.mid FROM book b WHERE b.symbol = f.symbol AND b.t >= f.t + 60"
        "   ORDER BY b.t ASC, b.seq DESC LIMIT 1) AS m60"
        " FROM trade f) WHERE m60 IS NOT NULL",
}
# ── the same questions in DuckDB over the Parquet tape ──────────────────────
Q_DUCK = {
    "1 AAPL mid at every tick": "SELECT t, mid FROM book WHERE symbol = 'AAPL'",
    "2 every book in t 390..460": "SELECT symbol, t, mid FROM book WHERE t BETWEEN 390 AND 460",
    "3 fills and fees per taker":
        "SELECT taker_id, count(*), sum(quantity), sum(fee) FROM trades "
        "GROUP BY taker_id ORDER BY 2 DESC",
    "4 AAPL event window t400":
        "WITH ev AS (SELECT t FROM events WHERE event = 'CALENDAR_EVENT' "
        "            AND kind = 'earnings' AND symbol = 'AAPL'), "
        "g AS (SELECT ev.t + k AS at, k FROM ev, (VALUES (-1), (5), (15), (60)) v(k)) "
        "SELECT g.k, b.mid FROM g ASOF JOIN (SELECT * FROM book_eot WHERE symbol = 'AAPL') b "
        "ON g.at >= b.t "
        "ORDER BY g.k",
    "5 markout of every fill +60":
        "SELECT count(*), avg(1e4 * sgn * (b.mid - f.price) / f.price) FROM ("
        " SELECT t, symbol, price, CASE WHEN aggressor = 'buy' THEN 1 ELSE -1 END AS sgn"
        " FROM trades) f ASOF JOIN book_eot b ON f.symbol = b.symbol AND b.t >= f.t + 60",
}
DUCK_VIEWS = """
CREATE OR REPLACE VIEW book   AS SELECT * FROM read_parquet('{W}/book_snapshot/**/*.parquet', hive_partitioning = true);
CREATE OR REPLACE VIEW trades AS SELECT * FROM read_parquet('{W}/trade_execution/**/*.parquet', hive_partitioning = true);
CREATE OR REPLACE VIEW events AS SELECT * FROM read_parquet('{W}/session_event/**/*.parquet', hive_partitioning = true);
CREATE OR REPLACE VIEW book_eot AS
    SELECT * FROM book QUALIFY row_number() OVER (PARTITION BY symbol, t ORDER BY seq DESC) = 1;
"""


def best_of(fn, n):
    times, out = [], None
    for _ in range(n):
        t0 = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t0)
    return min(times), out


def fmt_ms(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 1e-3:
        return f"{s * 1e3:.2f} ms"
    return f"{s * 1e3:,.1f} ms" if s < 10 else f"{s:,.1f} s"


def rows_from_tape(path: str):
    """Yield (table, row-tuple) for every line of the tape, both clocks kept."""
    t0, _ = event_clock_offset(path)
    with _open(path) as f:
        for seq, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            m, tick = rec["msg"], int(rec["tick"])
            kind, ts, t = m.get("type"), float(rec["ts"]), int(rec["tick"]) - t0
            if kind == "book_snapshot":
                bids, asks = m.get("bids") or [], m.get("asks") or []
                yield "book", (seq, tick, t, ts, m["symbol"],
                               bids[0][0] if bids else None, bids[0][1] if bids else None,
                               asks[0][0] if asks else None, asks[0][1] if asks else None,
                               m.get("mid_price"),
                               (asks[0][0] - bids[0][0]) if (bids and asks) else None,
                               m.get("microprice"), m.get("obi"))
                for side, lv in (("B", bids), ("A", asks)):
                    for i, (px, q) in enumerate(lv):
                        yield "book_level", (seq, side, i, px, q)
            elif kind == "trade_execution":
                yield "trade", (seq, tick, t, ts, m["symbol"], m.get("trade_id"),
                                float(m["price"]), int(m["quantity"]), m.get("aggressor"),
                                m.get("buyer_id"), m.get("seller_id"), m.get("taker_id"),
                                m.get("maker_id"), m.get("fee"), m.get("maker_rebate"))
            elif kind == "order_ack":
                yield "order_ack", (seq, tick, t, ts, m.get("symbol"), m.get("order_id"),
                                    m.get("team_id"), m.get("side"), m.get("price"),
                                    m.get("quantity"), m.get("queue_ahead"), m.get("level_qty"))
            elif kind == "order_cancel":
                yield "order_cancel", (seq, tick, t, ts, m.get("symbol"), m.get("order_id"),
                                       m.get("team_id"), m.get("fee"))
            elif kind == "order_reject":
                yield "order_reject", (seq, tick, t, ts, m.get("team_id"), m.get("code"),
                                       m.get("message"))
            elif kind == "session_event":
                d = m.get("data") or {}
                yield "session_event", (seq, tick, t, ts, m.get("event"), m.get("message"),
                                        d.get("kind"), d.get("symbol"), d.get("pct"),
                                        json.dumps(d, separators=(",", ":")))
            elif kind == "leaderboard":
                for p in (m.get("traders") or []) + (m.get("brokers") or []):
                    yield "leaderboard", (seq, tick, t, ts, p.get("team_id"), p.get("role"),
                                          p.get("cash"), p.get("net_worth"),
                                          p.get("realized_pnl"), p.get("unrealized_pnl"),
                                          p.get("total_fees_paid"),
                                          p.get("total_rebates_earned"), p.get("trade_count"))


def jsonl_bytes(p: str) -> int:
    if not p.endswith(".gz"):
        return os.path.getsize(p)
    with open(p, "rb") as f:
        f.seek(-4, os.SEEK_END)
        return int.from_bytes(f.read(4), "little")


def parquet_bytes(W: str) -> int | None:
    if not os.path.isdir(W):
        return None
    return sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(W) for fn in fns)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tape", help="session_*.jsonl or .jsonl.gz")
    ap.add_argument("--db", default="/tmp/w4/tape.sqlite")
    ap.add_argument("--warehouse", default="/tmp/w4/tape.parquet")
    ap.add_argument("--skip-noindex-join", action="store_true",
                    help="skip query 5 without an index (a minute or more)")
    ap.add_argument("--scale", type=int, default=0,
                    help="part F (Week 6): copies of the top-of-book table (0 = skip, the default)")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(a.db)), exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        if os.path.exists(a.db + suffix):
            os.remove(a.db + suffix)
    print(f"sqlite {sqlite3.sqlite_version} · tape {os.path.basename(a.tape)} · db {a.db}")

    # ── A. load ────────────────────────────────────────────────────────────
    t_start = time.perf_counter()
    batches: dict[str, list[tuple]] = {k: [] for k in COLS}
    for table, row in rows_from_tape(a.tape):
        batches[table].append(row)
    t_parse = time.perf_counter() - t_start

    con = sqlite3.connect(a.db)
    con.executescript(SCHEMA)
    t_ins = time.perf_counter()
    with con:                                            # one transaction for the load
        for table, rows in batches.items():
            con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * COLS[table])})", rows)
    t_ins = time.perf_counter() - t_ins
    n_rows = sum(len(r) for r in batches.values())
    size_noidx = os.path.getsize(a.db)
    print(f"\nA. load: one table per message type, depth normalised into book_level")
    print(f"   {'table':<15} {'rows':>9}")
    for table, rows in batches.items():
        print(f"   {table:<15} {len(rows):>9,}")
    print(f"   {'TOTAL':<15} {n_rows:>9,}")
    print(f"   parse JSON {t_parse:.1f} s · INSERT (one transaction, executemany) {t_ins:.1f} s "
          f"= {n_rows / t_ins:,.0f} rows/s · file {size_noidx:,} bytes")

    # ── B. commit per row ────────────────────────────────────────────────
    probe = a.db + ".probe"
    for suffix in ("", "-journal"):
        if os.path.exists(probe + suffix):
            os.remove(probe + suffix)
    pc = sqlite3.connect(probe, isolation_level=None)  # autocommit: every INSERT commits
    pc.executescript(SCHEMA)
    sample = batches["book"][:2000]
    t_row = time.perf_counter()
    for r in sample:
        pc.execute(f"INSERT INTO book VALUES ({','.join('?' * 13)})", r)
    t_row = time.perf_counter() - t_row
    pc.close()
    os.remove(probe)
    print(f"\nB. a naive live recorder: one INSERT + COMMIT per message (default journal, "
          f"synchronous=FULL)")
    print(f"   {len(sample):,} book rows in {t_row:.2f} s = {len(sample) / t_row:,.0f} rows/s "
          f"(vs {n_rows / t_ins:,.0f} rows/s batched)")

    # ── D (part 1). queries without an index ──────────────────────────────
    res: dict[str, dict] = {k: {} for k in Q_SQLITE}
    for name, sql in Q_SQLITE.items():
        if name.startswith("5") and a.skip_noindex_join:
            continue
        n = 1 if name.startswith("5") else 5
        t, out = best_of(lambda: con.execute(sql).fetchall(), n)
        res[name]["noidx"] = (t, out)

    # ── C. indexes ───────────────────────────────────────────────────────
    t_idx = time.perf_counter()
    con.executescript(INDEXES)
    con.commit()
    t_idx = time.perf_counter() - t_idx
    size_idx = os.path.getsize(a.db)
    plan = con.execute("EXPLAIN QUERY PLAN " + Q_SQLITE["1 AAPL mid at every tick"]).fetchall()
    print(f"\nC. CREATE INDEX on book(symbol, t, seq), book(t), trade(symbol, t), "
          f"session_event(event, t): {t_idx:.2f} s")
    print(f"   file {size_noidx:,} -> {size_idx:,} bytes (+{size_idx / size_noidx - 1:.0%})")
    print(f"   plan for query 1: {plan[-1][-1]}")

    for name, sql in Q_SQLITE.items():
        n = 3 if name.startswith("5") else 5
        res[name]["idx"] = best_of(lambda: con.execute(sql).fetchall(), n)

    # ── D (part 2). DuckDB on Parquet ─────────────────────────────────────
    have_pq = os.path.isdir(os.path.join(a.warehouse, "book_snapshot"))
    if have_pq:
        import duckdb
        dk = duckdb.connect()
        dk.execute(DUCK_VIEWS.replace("{W}", a.warehouse))
        for name, sql in Q_DUCK.items():
            res[name]["duck"] = best_of(lambda: dk.execute(sql).fetchall(), 5)

    print("\nD. the same five questions (best of n; query 5 without an index runs once)")
    print(f"   {'question':<30} {'rows':>6} {'SQLite, no index':>17} {'SQLite, indexed':>16} "
          f"{'DuckDB, Parquet':>16}")
    for name in Q_SQLITE:
        r = res[name]
        rows = len(r["idx"][1]) if not name.startswith("5") else r["idx"][1][0][0]
        print(f"   {name:<30} {rows:>6,} {fmt_ms(r.get('noidx', (None,))[0]):>17} "
              f"{fmt_ms(r['idx'][0]):>16} {fmt_ms(r.get('duck', (None,))[0]):>16}")
    ev_sql = res["4 AAPL event window t400"]["idx"][1]
    print("   query 4, AAPL mid at t400 + k:  " + "  ".join(
        f"k={k:+d} {m:.2f}" for k, m in ev_sql))
    q5 = res["5 markout of every fill +60"]
    line = f"   query 5, average markout +60: SQLite {q5['idx'][1][0][1]:+.3f} bp"
    if "duck" in q5:
        line += (f" · DuckDB {q5['duck'][1][0][1]:+.3f} bp over {q5['duck'][1][0][0]:,} fills"
                 f" · same answer: {abs(q5['idx'][1][0][1] - q5['duck'][1][0][1]) < 1e-9}")
    print(line)
    if have_pq:
        same4 = [round(m, 6) for _, m in ev_sql] == [round(m, 6) for _, m in res[
            "4 AAPL event window t400"]["duck"][1]]
        print(f"   query 4 same answer in both: {same4}")

    # ── E. sizes ──────────────────────────────────────────────────────────
    pqb = parquet_bytes(a.warehouse)
    jb = jsonl_bytes(a.tape)
    print("\nE. the same tape, stored three ways")
    print(f"   JSONL (as recorded)          {jb:>13,} bytes")
    print(f"   SQLite, no index             {size_noidx:>13,} bytes  ({jb / size_noidx:.1f}x smaller)")
    print(f"   SQLite, 4 indexes            {size_idx:>13,} bytes  ({jb / size_idx:.1f}x smaller)")
    if pqb:
        print(f"   Parquet (zstd, partitioned)  {pqb:>13,} bytes  ({jb / pqb:.1f}x smaller; "
              f"SQLite is {size_idx / pqb:.1f}x Parquet)")
    if a.scale > 0:
        scale(con, a.scale, os.path.dirname(os.path.abspath(a.db)), have_pq)
    con.close()
    return 0


def scale(con: sqlite3.Connection, n: int, scratch: str, have_duck: bool) -> None:
    """F. The book table N times over (one copy per 'week'), in both stores."""
    rows = con.execute("SELECT seq, tick, t, ts, symbol, best_bid, best_bid_qty, best_ask, "
                       "best_ask_qty, mid, spread, microprice, obi FROM book").fetchall()
    names = ["seq", "tick", "t", "ts", "symbol", "best_bid", "best_bid_qty", "best_ask",
             "best_ask_qty", "mid", "spread", "microprice", "obi"]
    db = os.path.join(scratch, "scale.sqlite")
    if os.path.exists(db):
        os.remove(db)
    sc = sqlite3.connect(db)
    sc.execute("CREATE TABLE book (week INTEGER, " + ", ".join(
        f"{c} {'TEXT' if c == 'symbol' else 'INTEGER' if c in ('seq', 'tick', 't') else 'REAL'}" for c in names) + ", PRIMARY KEY (week, seq))")
    t0 = time.perf_counter()
    with sc:
        for w in range(n):
            sc.executemany(f"INSERT INTO book VALUES ({','.join('?' * 14)})",
                           [(w,) + r for r in rows])
        sc.execute("CREATE INDEX b_sym ON book (symbol, week, t)")
    t_sq = time.perf_counter() - t0
    qs = {"full-scan aggregate: mean spread bp per symbol, all weeks":
              "SELECT symbol, avg(1e4 * spread / mid) FROM book WHERE spread IS NOT NULL "
              "GROUP BY symbol ORDER BY symbol",
          "per-week aggregate: AAPL mean mid per week":
              "SELECT week, avg(mid) FROM book WHERE symbol = 'AAPL' GROUP BY week ORDER BY week",
          "point lookup: AAPL, week 7, t 390..460":
              "SELECT t, mid FROM book WHERE symbol = 'AAPL' AND week = 7 "
              "AND t BETWEEN 390 AND 460 ORDER BY t, seq"}
    res = {k: [best_of(lambda: sc.execute(q).fetchall(), 3)] for k, q in qs.items()}
    print(f"\nF. scale: the top-of-book table x {n} weeks = {len(rows) * n:,} rows")
    print(f"   SQLite: load + index {t_sq:.1f} s · {os.path.getsize(db):,} bytes")
    if have_duck:
        import shutil

        import duckdb
        import pyarrow as pa
        import pyarrow.dataset as ds
        out = os.path.join(scratch, "scale.parquet")
        shutil.rmtree(out, ignore_errors=True)
        base = pa.table({c: pa.array(v) for c, v in zip(names, zip(*rows))})
        t0 = time.perf_counter()
        for w in range(n):
            ds.write_dataset(
                base.append_column("week", pa.array([w] * base.num_rows, pa.int16())), out,
                format="parquet", basename_template=f"w{w}-{{i}}.parquet",
                partitioning=ds.partitioning(pa.schema([("week", pa.int16()),
                                                        ("symbol", pa.string())]), flavor="hive"),
                file_options=ds.ParquetFileFormat().make_write_options(compression="zstd"),
                existing_data_behavior="overwrite_or_ignore")
        t_pq = time.perf_counter() - t0
        size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
        print(f"   Parquet: write {t_pq:.1f} s · {size:,} bytes "
              f"({os.path.getsize(db) / size:.1f}x smaller than SQLite)")
        dk = duckdb.connect()
        dk.execute(f"CREATE VIEW book AS SELECT * FROM read_parquet('{out}/**/*.parquet', "
                   "hive_partitioning = true)")
        for k, q in qs.items():
            t, out_d = best_of(lambda: dk.execute(q).fetchall(), 3)
            same = [tuple(round(x, 6) if isinstance(x, float) else x for x in r)
                    for r in res[k][0][1]] == [tuple(round(x, 6) if isinstance(x, float) else x
                                                     for x in r) for r in out_d]
            res[k].append((t, same))
    print(f"   {'question':<58} {'SQLite':>10} {'DuckDB/Parquet':>15}  same")
    for k, v in res.items():
        d = f"{fmt_ms(v[1][0]):>15}  {v[1][1]}" if len(v) > 1 else f"{'—':>15}"
        print(f"   {k:<58} {fmt_ms(v[0][0]):>10} {d}")
    sc.close()


if __name__ == "__main__":
    raise SystemExit(main())
