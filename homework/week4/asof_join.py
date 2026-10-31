#!/usr/bin/env python3
"""asof_join.py — the as-of join, the one join a trading desk lives on, measured.

Week 4, Parts 3–5 (lab step 3). Run tape_to_parquet.py first (and
sql_baseline.py if you want the SQLite row).

An as-of join matches each row on the left with the NEAREST row on the right
by time — not an equal time — within the same symbol:

  A. BACKWARD ("prevailing quote"): for every trade, the last book the venue
     published BEFORE it (by recording order, seq). This is kdb+'s `aj`, the
     classic trade-and-quote join: how much did the taker pay over the touch mid?
  B. FORWARD ("markout"): for every trade, the end-of-tick book 60 ticks
     LATER (first tick >= t + 60, last snapshot of that tick by seq). Fine for
     a post-trade report — and a look-ahead bug if a backtest decision used it.

Each is run in DuckDB (ASOF JOIN), pandas (merge_asof) and Polars
(join_asof), END TO END from Parquet, best of 5, and every answer is checked
equal. B is also run in SQLite as a correlated subquery, the way plain SQL
has to say it (needs /tmp/w4/tape.sqlite from sql_baseline.py).

The same joins in q (kdb+ is licensed: ILLUSTRATIVE, not run here):

    / A. prevailing quote at each trade: the last quote at or before its time
    aj[`sym`time; trade; quote]
    / B. the quote prevailing 60 s later: shift the key, then the same aj
    aj[`sym`time; update time:time+00:01:00 from trade; quote]

Usage:
    python asof_join.py [--warehouse /tmp/w4/tape.parquet] [--db /tmp/w4/tape.sqlite]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
import warnings

import duckdb
import numpy as np
import pandas as pd
import polars as pl


def best_of(fn, n=5):
    times, out = [], None
    for _ in range(n):
        t = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t)
    return min(times), out


def summarise_a(df) -> tuple:
    """(fills against a two-sided book, mean half-spread paid vs the touch mid in bp,
    % of fills at the prevailing touch or through it)."""
    d = pd.DataFrame(df) if not isinstance(df, pd.DataFrame) else df
    d = d.dropna(subset=["best_bid", "best_ask"])
    touch_mid = (d["best_bid"] + d["best_ask"]) / 2
    sgn = np.where(d["aggressor"] == "buy", 1.0, -1.0)
    cost = 1e4 * sgn * (d["price"] - touch_mid) / touch_mid
    at = np.where(d["aggressor"] == "buy", d["price"] >= d["best_ask"], d["price"] <= d["best_bid"])
    return len(d), round(float(cost.mean()), 6), round(100.0 * float(np.mean(at)), 6)


def summarise_b(df) -> tuple:
    """(fills with a book 60 ticks later, mean markout in bp, sum in $)."""
    d = pd.DataFrame(df) if not isinstance(df, pd.DataFrame) else df
    d = d.dropna(subset=["mid60"])
    sgn = np.where(d["aggressor"] == "buy", 1.0, -1.0)
    bp = 1e4 * sgn * (d["mid60"] - d["price"]) / d["price"]
    usd = sgn * (d["mid60"] - d["price"]) * d["quantity"]
    return len(d), round(float(bp.mean()), 6), round(float(usd.sum()), 4)


# Polars warns that it cannot verify sortedness inside `by` groups; we sort first.
warnings.filterwarnings("ignore", message="Sortedness of columns cannot be checked")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--warehouse", default="/tmp/w4/tape.parquet")
    ap.add_argument("--db", default="/tmp/w4/tape.sqlite")
    a = ap.parse_args()
    W = a.warehouse
    B, T = f"{W}/book_snapshot", f"{W}/trade_execution"
    print(f"duckdb {duckdb.__version__} · pandas {pd.__version__} · polars {pl.__version__} "
          f"· warehouse {W}")

    con = duckdb.connect()
    con.execute(f"""
        CREATE VIEW book   AS SELECT * FROM read_parquet('{B}/**/*.parquet', hive_partitioning = true);
        CREATE VIEW trades AS SELECT * FROM read_parquet('{T}/**/*.parquet', hive_partitioning = true);
        CREATE VIEW book_eot AS SELECT * FROM book
            QUALIFY row_number() OVER (PARTITION BY symbol, t ORDER BY seq DESC) = 1;""")

    # ── A. backward: the prevailing book at each trade ──────────────────────
    def duck_a():
        return con.execute("""
            SELECT f.seq, f.symbol, f.price, f.aggressor, b.best_bid, b.best_ask
            FROM trades f ASOF JOIN book b
              ON f.symbol = b.symbol AND f.seq > b.seq       -- latest book BEFORE the trade
            ORDER BY f.seq""").df()

    def pd_a():
        f = pd.read_parquet(T, columns=["seq", "symbol", "price", "aggressor"]).astype(
            {"symbol": str}).sort_values("seq")
        b = pd.read_parquet(B, columns=["seq", "symbol", "best_bid", "best_ask"]).astype(
            {"symbol": str}).sort_values("seq")
        return pd.merge_asof(f, b, on="seq", by="symbol", direction="backward",
                             allow_exact_matches=False)

    def pl_a():
        f = (pl.scan_parquet(f"{T}/**/*.parquet", hive_partitioning=True)
             .select("seq", "symbol", "price", "aggressor").sort("seq"))
        b = (pl.scan_parquet(f"{B}/**/*.parquet", hive_partitioning=True)
             .select("seq", "symbol", "best_bid", "best_ask").sort("seq"))
        return f.join_asof(b, on="seq", by="symbol", strategy="backward",
                           allow_exact_matches=False).collect().to_pandas()

    print("\nA. BACKWARD as-of: the book prevailing just before each trade (kdb+ aj)")
    print(f"   {'engine':<26} {'time':>9}   fills  paid vs touch mid  at/through the touch")
    ans_a = []
    for name, fn in (("DuckDB ASOF JOIN", duck_a), ("pandas merge_asof", pd_a),
                     ("Polars join_asof", pl_a)):
        t, out = best_of(fn)
        s = summarise_a(out)
        ans_a.append(s)
        print(f"   {name:<26} {t * 1e3:7.1f} ms   {s[0]:,}  {s[1]:+15.2f} bp  {s[2]:18.1f} %")
    print(f"   same answer in all three: {len(set(ans_a)) == 1}")

    # ── B. forward: the end-of-tick book 60 ticks later ─────────────────────
    def duck_b():
        return con.execute("""
            SELECT f.seq, f.symbol, f.price, f.quantity, f.aggressor, b.mid AS mid60
            FROM trades f ASOF JOIN book_eot b
              ON f.symbol = b.symbol AND b.t >= f.t + 60     -- first tick >= t+60
            ORDER BY f.seq""").df()

    def pd_eot():
        b = pd.read_parquet(B, columns=["seq", "t", "symbol", "mid"]).astype({"symbol": str})
        b = b.sort_values("seq").drop_duplicates(["symbol", "t"], keep="last")
        return b.rename(columns={"mid": "mid60"})[["t", "symbol", "mid60"]].sort_values("t")

    def pd_b():
        f = pd.read_parquet(T, columns=["seq", "t", "symbol", "price", "quantity", "aggressor"]
                            ).astype({"symbol": str})
        f = f.assign(at=f["t"] + 60).sort_values("at")
        return pd.merge_asof(f, pd_eot(), left_on="at", right_on="t", by="symbol",
                             direction="forward", suffixes=("", "_book"))

    def pl_b():
        b = (pl.scan_parquet(f"{B}/**/*.parquet", hive_partitioning=True)
             .select("seq", "t", "symbol", "mid").sort("seq")
             .group_by("symbol", "t").agg(pl.col("mid").last().alias("mid60"))
             .rename({"t": "bt"}).sort("bt"))
        f = (pl.scan_parquet(f"{T}/**/*.parquet", hive_partitioning=True)
             .select("seq", "t", "symbol", "price", "quantity", "aggressor")
             .with_columns(at=pl.col("t") + 60).sort("at"))
        return f.join_asof(b, left_on="at", right_on="bt", by="symbol",
                           strategy="forward").collect().to_pandas()

    rows = [("DuckDB ASOF JOIN", duck_b, 5), ("pandas merge_asof", pd_b, 5),
            ("Polars join_asof", pl_b, 5)]
    if os.path.exists(a.db):
        sq = sqlite3.connect(a.db)

        def sqlite_b():
            return pd.read_sql_query("""
                SELECT f.seq, f.symbol, f.price, f.quantity, f.aggressor,
                       (SELECT b.mid FROM book b
                         WHERE b.symbol = f.symbol AND b.t >= f.t + 60
                         ORDER BY b.t ASC, b.seq DESC LIMIT 1) AS mid60
                FROM trade f ORDER BY f.seq""", sq)
        idx = {r[1] for r in sq.execute("PRAGMA index_list(book)")}
        rows.append((f"SQLite correlated subq.{' (indexed)' if 'book_sym_t' in idx else ''}",
                     sqlite_b, 3))
    print("\nB. FORWARD as-of: the end-of-tick book 60 ticks after each trade (markout)")
    print(f"   {'engine':<34} {'time':>9}   fills  mean markout   sum markout")
    ans_b = []
    for name, fn, n in rows:
        t, out = best_of(fn, n)
        s = summarise_b(out)
        ans_b.append(s)
        print(f"   {name:<34} {t * 1e3:7.1f} ms   {s[0]:,}  {s[1]:+9.3f} bp  {s[2]:+12,.2f} $")
    print(f"   same answer in all {len(ans_b)}: {len(set(ans_b)) == 1}")
    print("\n   The tie rule matters: a symbol gets up to 17 books in one tick. Join on t\n"
          "   alone and each engine keeps a different one; join to the END-OF-TICK book\n"
          "   (last by seq) and they agree to the cent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
