"""Find the hidden week-8 lead-lag links in the week-8 history.

    python scripts/find_links.py path/to/week08_history [more parts ...]

For every (leader, follower, lag 1-10) it measures how well the leader's
one-tick mark return predicts the follower's return `lag` ticks later, with
the ticks around scheduled events and halts removed (a market-wide print
moves every name at once and looks like a link everywhere).

Why it is fast: DuckDB reads three columns out of the Parquet files in
parallel, the marks go into one dense array [session, tick, symbol], and
each lag is one matrix product over all 81 leader/follower pairs at once.
Nothing loops over rows. 7.2 million rows take about 10 s on a laptop.
"""

from __future__ import annotations

import sys
import time

import duckdb
import numpy as np

MAX_LAG = 10
EVENT_MARGIN = 60          # ticks either side of an event or halt left out
WARMUP = 40                # ticks after the open left out
SKIP = ("ARENA10", "LUNA")  # the index future and the IPO are not in the universe
EVENTS = ("CALENDAR_EVENT", "TRADING_HALT", "TRADING_RESUMED", "IPO_LISTED")


def load(roots: list[str]) -> tuple[np.ndarray, list[str]]:
    """Calm-market one-tick log returns as [session, tick, symbol], NaN where
    the tick is near an event, and the symbols in column order."""
    con = duckdb.connect()
    books = [f"{r}/books/*/*.parquet" for r in roots]
    events = [f"{r}/events/*.parquet" for r in roots]
    d = con.execute(
        "SELECT CAST(session AS INT) AS s, tick, symbol, mark "
        "FROM read_parquet(?, hive_partitioning = 1) WHERE symbol NOT IN ?",
        [books, list(SKIP)]).fetchnumpy()
    symbols = sorted(set(d["symbol"]))
    column = {s: i for i, s in enumerate(symbols)}
    sessions = np.unique(d["s"])
    marks = np.full((len(sessions), int(d["tick"].max()) + 1, len(symbols)), np.nan)
    marks[np.searchsorted(sessions, d["s"]), d["tick"],
          np.array([column[s] for s in d["symbol"]])] = d["mark"]
    returns = np.diff(np.log(marks), axis=1)       # returns[:, t - 1] is tick t

    calm = np.ones(returns.shape[:2], bool)
    calm[:, :WARMUP] = False
    for s, tick in con.execute(
            "SELECT CAST(session AS INT), tick FROM read_parquet(?) WHERE event IN ?",
            [events, list(EVENTS)]).fetchall():
        row = np.searchsorted(sessions, s)
        calm[row, max(0, tick - EVENT_MARGIN):tick + EVENT_MARGIN] = False
    returns[~calm] = np.nan
    return returns, symbols


def scan(returns: np.ndarray, symbols: list[str]) -> list[tuple]:
    """(|corr|, corr, beta, leader, follower, lag) for every candidate."""
    n = len(symbols)
    out = []
    for lag in range(1, MAX_LAG + 1):
        x = returns[:, :-lag].reshape(-1, n)
        y = returns[:, lag:].reshape(-1, n)
        ok = np.isfinite(x).all(1) & np.isfinite(y).all(1)
        x, y = x[ok], y[ok]
        x = x - x.mean(0)
        y = y - y.mean(0)
        cov = x.T @ y                              # [leader, follower]
        var_x, var_y = (x * x).sum(0), (y * y).sum(0)
        corr = cov / np.sqrt(np.outer(var_x, var_y))
        beta = cov / var_x[:, None]
        for a in range(n):
            for b in range(n):
                if a != b:
                    out.append((abs(corr[a, b]), corr[a, b], beta[a, b],
                                symbols[a], symbols[b], lag))
    return sorted(out, reverse=True)


def main() -> None:
    roots = sys.argv[1:] or ["week08_history"]
    t0 = time.perf_counter()
    returns, symbols = load(roots)
    t1 = time.perf_counter()
    ranked = scan(returns, symbols)
    t2 = time.perf_counter()

    print(f"{returns.shape[0]} sessions, {returns.shape[1] + 1} ticks, "
          f"{len(symbols)} symbols; {len(ranked)} candidates")
    print(f"load {t1 - t0:.1f} s, scan {t2 - t1:.1f} s\n")
    print("Best lag per pair (a link shows at its lag; the tick after has the "
          "opposite sign, as the echo walks back):")
    seen: set[tuple[str, str]] = set()
    shown = 0
    for _, corr, beta, lead, fol, lag in ranked:
        if (lead, fol) in seen:
            continue
        seen.add((lead, fol))
        print(f"  {lead:>5} -> {fol:<5} lag {lag:2d}  corr {corr:+.4f}  beta {beta:+.3f}")
        shown += 1
        if shown == 8:
            break


if __name__ == "__main__":
    main()
