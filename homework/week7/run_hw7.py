"""HW 7 — vectorize your signal.  Run:  python run_hw7.py

The signal functions (and the pool worker) live in hw7_signal.py, because a
process pool on macOS cannot run a function defined only in this __main__.
"""
import os
import resource
import sys
import pickle
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

from hw7_signal import chunked, naive, partition_by_symbol, signal_partition, vectorized, warmup

CSV = Path(__file__).resolve().parent / "build" / "ohlcv.csv"
COLS = {"usecols": ["timestamp", "symbol", "close"],
        "dtype": {"symbol": "category", "close": "float32"}}


def rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3     # macOS bytes, Linux KB


def mb(df): return df.memory_usage(deep=True).sum() / 1e6


def make_ohlcv(n_sym=500, days=26, bars=390, seed=7):
    """500 symbols x 26 days x 390 one-minute bars = 5,070,000 rows of OHLCV.

    Each symbol is a geometric random walk with its own vol and start price.
    Long format, sorted by (timestamp, symbol), the way a feed arrives.
    """
    rng = np.random.default_rng(seed)
    days_ = pd.bdate_range("2026-01-05", periods=days).values
    ts = (days_[:, None] + pd.to_timedelta(np.arange(bars) + 570, unit="min").values).ravel()
    sig = rng.uniform(0.15, 0.80, n_sym) / np.sqrt(252 * bars)          # per-bar vol
    s0 = rng.uniform(10, 500, n_sym)
    close = s0 * np.exp(np.cumsum(rng.standard_normal((len(ts), n_sym)) * sig, axis=0))
    open_ = np.vstack([s0, close[:-1]])
    wick = np.abs(rng.standard_normal((2, len(ts), n_sym))) * 0.3 * sig
    return pd.DataFrame({
        "timestamp": np.repeat(ts, n_sym),
        "symbol": np.tile([f"SYM{i:03d}" for i in range(n_sym)], len(ts)),
        "open": open_.ravel(),
        "high": (np.maximum(open_, close) * (1 + wick[0])).ravel(),
        "low": (np.minimum(open_, close) * (1 - wick[1])).ravel(),
        "close": close.ravel(),
        "volume": rng.lognormal(8, 1, close.shape).astype(np.int64).ravel() + 1,
    })


def in_memory():                    # run in a fresh process -> its own peak RSS
    df = pd.read_csv(CSV, **COLS)
    return vectorized(df), rss_mb()


def out_of_core(carry=100):         # run in a fresh process -> its own peak RSS
    return chunked(pd.read_csv(CSV, chunksize=250_000, **COLS), carry=carry), rss_mb()


def fresh_process(fn, *args):
    with ProcessPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


if __name__ == "__main__":
    if not CSV.exists():
        CSV.parent.mkdir(exist_ok=True)
        make_ohlcv().to_csv(CSV, index=False, float_format="%.4f")

    # 1. Memory before / after dtype tuning ----------------------------------
    df = pd.read_csv(CSV)                                       # object + float64 + int64
    print(df.shape, df.dtypes.to_dict())
    print(f"before: {mb(df):7.1f} MB")
    df["symbol"] = df["symbol"].astype("category")
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype("float32")
    df["volume"] = df["volume"].astype("int32")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")                              # proper DatetimeIndex
    print(f"after : {mb(df):7.1f} MB")

    # 2. Naive loop vs vectorized ---------------------------------------------
    sub = df[df["symbol"].cat.codes < 20]                       # full history of 20 symbols
    t0 = perf_counter(); a = naive(sub["symbol"].to_numpy(), sub["close"].to_numpy()); t_n = perf_counter() - t0
    t0 = perf_counter(); b = vectorized(sub); t_v = perf_counter() - t0
    m = ~np.isnan(a)
    assert np.array_equal(np.isnan(a), np.isnan(b))             # same NaN warm-up window
    assert np.allclose(a[m], b[m], rtol=1e-6, atol=1e-6)
    print(f"rows={len(sub):,}  naive={t_n:.2f}s  vectorized={t_v:.3f}s  {t_n / t_v:.0f}x  "
          f"warm-up NaNs={(~m).sum():,}  maxdiff={np.abs(a[m] - b[m]).max():.1e}")
    t0 = perf_counter(); ref = vectorized(df); t_f = perf_counter() - t0
    print(f"rows={len(df):,}  vectorized={t_f:.2f}s  (naive would be ~{t_n * len(df) / len(sub):.0f}s)")

    # 3. Out of core: chunked vs in-memory, each in its own process -----------
    full, rss_full = fresh_process(in_memory)
    chunk, rss_chunk = fresh_process(out_of_core)
    bug, _ = fresh_process(out_of_core, 0)                      # no carried state
    assert np.array_equal(full, chunk, equal_nan=True)
    print(f"in-memory peak RSS {rss_full:.0f} MB   chunked peak RSS {rss_chunk:.0f} MB   "
          f"chunked == in-memory: True")
    print(f"carry=0 (the bug): {(~np.isclose(full, bug, equal_nan=True)).sum():,} rows wrong")

    # 4. Processes vs threads -------------------------------------------------
    print(f"single process, no pool: {t_f:.2f}s")
    print(f"{'executor':<8} {'n':>2} {'total':>7} {'start':>7} {'pickle':>7} {'speedup':>8}")
    for Exec in (ProcessPoolExecutor, ThreadPoolExecutor):
        for n in sorted({1, 2, 4, os.cpu_count()}):
            parts = partition_by_symbol(df, n)
            t0 = perf_counter()
            blobs = [pickle.dumps(p) for p in parts]            # what a process pool sends
            t_pk = perf_counter() - t0 if Exec is ProcessPoolExecutor else 0.0

            t0 = perf_counter()
            pool = Exec(max_workers=n)
            seen = set()
            while len(seen) < n:                                # until every worker is up
                seen |= set(pool.map(warmup, range(n)))
            t_start = perf_counter() - t0
            out = np.empty(len(df), dtype=np.float32)
            for pos, sig in pool.map(signal_partition, parts):
                out[pos] = sig
            pool.shutdown()
            t_tot = perf_counter() - t0

            assert np.array_equal(out, ref, equal_nan=True)     # parallel == single-process
            kind = "process" if Exec is ProcessPoolExecutor else "thread"
            print(f"{kind:<8} {n:>2} {t_tot:6.2f}s {t_start * 1e3:5.0f}ms {t_pk * 1e3:5.0f}ms "
                  f"{t_f / t_tot:7.2f}x")

    # 5. Look-ahead: why shift(1) is mandatory --------------------------------
    sig = pd.Series(ref, index=df.index, dtype="float64")
    ret = df.groupby("symbol", observed=True)["close"].pct_change(fill_method=None)
    lag = sig.groupby(df["symbol"], observed=True).shift(1)     # per symbol!
    for name, pos in (("no shift", sig), ("shift(1)", lag)):
        pnl = (pos * ret).dropna()
        print(f"{name}: {pnl.mean() * 1e4:+.3f} bp/bar  t={pnl.mean() / pnl.std() * np.sqrt(len(pnl)):+.1f}")
