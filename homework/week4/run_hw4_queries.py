#!/usr/bin/env python3
"""run_hw4_queries.py — the same questions in SQLite and DuckDB/Parquet: timed, answers compared.

    python run_hw4_queries.py [--repeats 10]

Timing covers execute + fetchall on one open connection, best of --repeats. Also records the
numbers the comparison paragraph cites: files scanned and bytes per column or table.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import time

import duckdb
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
OWN = "'midway_traders_broker', 'midway_traders_trader_1', 'midway_traders_trader_2'"


def read_queries(path: str) -> dict[str, str]:
    """{name: sql} from a file of `-- name: x` sections."""
    with open(path) as f:
        parts = re.split(r"^-- name: (\w+)\s*$", f.read(), flags=re.M)
    return {parts[i]: parts[i + 1].strip().replace("OWN", OWN) for i in range(1, len(parts), 2)}


def timed(fn, n: int) -> tuple[float, list]:
    best, out = math.inf, None
    for _ in range(n):
        t0 = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1e3, out


def same(a, b, tol: float = 1e-3) -> bool:
    """Equal row by row; floats within tol (both stores round to 2-4 decimals in SQL)."""
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        for x, y in zip(ra, rb):
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                if not math.isclose(float(x), float(y), abs_tol=tol):
                    return False
            elif x != y:
                return False
    return True


def parquet_layout(W: str) -> dict:
    """Files, row groups and bytes per column of the book table."""
    files = [os.path.join(d, f) for d, _, fs in os.walk(f"{W}/book_snapshot") for f in fs]
    groups, most, cols = 0, 0, {}
    for p in files:
        meta = pq.ParquetFile(p).metadata
        groups += meta.num_row_groups
        most = max(most, meta.num_row_groups)
        for g in range(meta.num_row_groups):
            rg = meta.row_group(g)
            for c in range(rg.num_columns):
                col = rg.column(c)
                name = col.path_in_schema.split(".")[0]
                cols[name] = cols.get(name, 0) + col.total_compressed_size
    return {"files": len(files), "row_groups": groups, "max_row_groups_per_file": most,
            "bytes_per_column": cols, "bytes_total": sum(os.path.getsize(p) for p in files)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.path.join(HERE, "build", "tape.sqlite"))
    ap.add_argument("--warehouse", default=os.path.join(HERE, "build", "tape.parquet"))
    ap.add_argument("--repeats", type=int, default=10)
    ap.add_argument("--report", default=os.path.join(HERE, "results", "queries.json"))
    a = ap.parse_args()

    q_sq = read_queries(os.path.join(HERE, "queries_sqlite.sql"))
    q_dk = read_queries(os.path.join(HERE, "queries_duckdb.sql"))
    views = q_dk.pop("views").replace("{W}", a.warehouse)
    con = sqlite3.connect(a.db)
    dk = duckdb.connect()
    dk.execute(views)

    out = {"versions": {"sqlite": sqlite3.sqlite_version, "duckdb": duckdb.__version__},
           "repeats": a.repeats, "queries": {}}
    for name, sql in q_sq.items():
        sq_ms, sq_out = timed(lambda: con.execute(sql).fetchall(), a.repeats)
        dk_ms, dk_out = timed(lambda: dk.execute(q_dk[name]).fetchall(), a.repeats)
        analyze = "\n".join(r[1] for r in dk.execute("EXPLAIN ANALYZE " + q_dk[name]).fetchall())
        agree = same(sq_out, dk_out)
        print(f"== {name}  ({len(sq_out)} rows, stores agree: {agree})  "
              f"SQLite {sq_ms:.3f} ms | DuckDB/Parquet {dk_ms:.3f} ms")
        for row in sq_out:
            print("  ", row)
        if not agree:
            print("   DUCKDB:", dk_out[:6])
        out["queries"][name] = {"rows": len(sq_out), "agree": agree, "answer": sq_out,
                                "sqlite_ms": sq_ms, "duckdb_ms": dk_ms,
                                "duckdb_files_scanned": re.findall(r"Scanning Files: (\d+/\d+)", analyze)}

    out["parquet_book_layout"] = parquet_layout(a.warehouse)
    out["sqlite_object_bytes"] = dict(
        con.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name").fetchall())
    out["sqlite_db_bytes"] = os.path.getsize(a.db)
    out["parquet_bytes"] = sum(os.path.getsize(os.path.join(d, f))
                               for d, _, fs in os.walk(a.warehouse) for f in fs)
    con.close()
    os.makedirs(os.path.dirname(a.report), exist_ok=True)
    with open(a.report, "w") as f:
        json.dump(out, f, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
