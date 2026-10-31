#!/usr/bin/env python3
"""run_queries.py — run queries.sql against the Parquet tape with DuckDB, timed.

Week 4, Part 1 (slides 10–13, lab step 3).

    python run_queries.py                                  # /tmp/w4/tape.parquet
    python run_queries.py --warehouse /tmp/w4/tape.parquet --only q3_markouts_per_team
    python run_queries.py --rows 60                        # print more rows per query

queries.sql is split on '-- name: <id>' markers; {W} is replaced by the
warehouse path. Each query is run 5 times and the best time is printed
(the first run also pays for reading the Parquet footers).
"""
from __future__ import annotations

import argparse
import os
import re
import time

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))


def load_queries(path: str, warehouse: str) -> list[tuple[str, str]]:
    text = open(path, encoding="utf-8").read().replace("{W}", warehouse)
    parts = re.split(r"^-- name: (\S+)\s*$", text, flags=re.M)
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def show(rel_cols: list[str], rows: list[tuple], limit: int) -> None:
    def fmt(v):
        if isinstance(v, float):
            return f"{v:,.2f}" if abs(v) < 1e4 else f"{v:,.0f}"
        if isinstance(v, int):
            return f"{v:,}"
        return "" if v is None else str(v)
    cells = [[fmt(v) for v in r] for r in rows[:limit]]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(rel_cols)]
    print("  " + "  ".join(c.rjust(w) for c, w in zip(rel_cols, widths)))
    for r in cells:
        print("  " + "  ".join(v.rjust(w) for v, w in zip(r, widths)))
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more rows")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--warehouse", default="/tmp/w4/tape.parquet")
    ap.add_argument("--sql", default=os.path.join(HERE, "queries.sql"))
    ap.add_argument("--only", default=None, help="run one named query (after the views)")
    ap.add_argument("--rows", type=int, default=25)
    a = ap.parse_args()

    con = duckdb.connect()
    print(f"duckdb {duckdb.__version__} · warehouse {a.warehouse}")
    for name, sql in load_queries(a.sql, a.warehouse):
        if name == "views":
            con.execute(sql)
            continue
        if a.only and name != a.only:
            continue
        times, res, cols = [], None, None
        for _ in range(5):
            t0 = time.perf_counter()
            cur = con.execute(sql)
            res = cur.fetchall()
            times.append(time.perf_counter() - t0)
            cols = [d[0] for d in cur.description]
        print(f"\n== {name}   ({len(res)} rows · best {min(times) * 1e3:.1f} ms, "
              f"first {times[0] * 1e3:.1f} ms)")
        show(cols, res, a.rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
