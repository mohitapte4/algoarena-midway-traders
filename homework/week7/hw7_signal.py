"""hw7_signal.py — one signal, four ways (naive, vectorized, chunked, partitioned).

The signal: vol-normalized 20/100-bar momentum, per symbol,

    fast_t  = mean(close[t-19 .. t])                  20-bar SMA
    slow_t  = mean(close[t-99 .. t])                  100-bar SMA
    vol_t   = std(r[t-99 .. t], ddof=1),  r_t = close_t / close_{t-1} - 1
    signal  = clip((fast - slow) / (vol * close), -3, 3)     float32

(fast - slow) / close is the trend as a fraction of price; dividing by the
per-bar return vol turns it into "how many sigmas of trend", so one threshold
works for a $10 stock and a $500 stock. First valid value is the 101st bar of
each symbol (vol needs 100 returns = 101 closes); everything before is NaN.

The worker functions live here, in a .py module, so a ProcessPoolExecutor on
macOS (spawn start method) can import them in the child processes.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

import numpy as np
import pandas as pd

import os
import threading
import time

FAST, SLOW, CLIP = 20, 100, 3.0
# closes per symbol to carry across a chunk boundary (see chunked())
CARRY = SLOW


# --------------------------------------------------------------------------- naive
def naive(symbols, closes) -> np.ndarray:
    """A per-row Python loop. Every row recomputes its windows from scratch.

    Returns are computed once per row as they arrive, in the same dtype as the
    close column (float32 after tuning), so the loop and the vectorized
    version do the same arithmetic on the same inputs. The window statistics
    (means, std) are plain Python float64.
    """
    closes = np.asarray(closes)
    one = closes.dtype.type(1)
    # last 100 closes, per symbol
    px = defaultdict(lambda: deque(maxlen=SLOW))
    # last 100 returns, per symbol
    rets = defaultdict(lambda: deque(maxlen=SLOW))
    last = {}
    out = np.full(len(closes), np.nan, dtype=np.float32)
    for i in range(len(closes)):                       # <- the loop over rows
        s, c = symbols[i], closes[i]
        if s in last:
            rets[s].append(float(c / last[s] - one))
        last[s] = c
        w = px[s]
        w.append(float(c))
        r = rets[s]
        if len(r) < SLOW:                              # warm-up: 100 returns needed
            continue
        wl = list(w)
        fast = sum(wl[-FAST:]) / FAST
        slow = sum(wl) / SLOW
        m = sum(r) / SLOW
        vol = math.sqrt(sum((x - m) ** 2 for x in r) / (SLOW - 1))
        out[i] = min(max((fast - slow) / (vol * float(c)), -CLIP), CLIP)
    return out


# ---------------------------------------------------------------------- vectorized
def vectorized(df: pd.DataFrame) -> np.ndarray:
    """Zero Python-level loops over rows: the HW 7 groupby/transform/rolling idiom.

    (The lambdas run once per *symbol*, not once per row; all row work is in
    pandas' compiled rolling kernels.) Returns float32, aligned to df's rows.
    """
    g = df.groupby("symbol", observed=True, sort=False)["close"]
    fast = g.transform(lambda s: s.rolling(FAST, min_periods=FAST).mean())
    slow = g.transform(lambda s: s.rolling(SLOW, min_periods=SLOW).mean())
    vol = g.transform(lambda s: s.pct_change(
        fill_method=None).rolling(SLOW).std())
    sig = ((fast - slow) / (vol * df["close"])).to_numpy(dtype="float32")
    return np.clip(sig, -CLIP, CLIP)


# ------------------------------------------------------------------------- chunked
def chunked(chunks, carry: int = CARRY) -> np.ndarray:
    """Out-of-core: run vectorized() chunk by chunk, carrying a per-symbol tail.

    A row needs its own close plus the previous 100 (vol at t uses returns
    t-99..t, i.e. closes t-100..t). So before each chunk we prepend the last
    `carry` rows of every symbol seen so far, compute, and keep only the new
    rows' output. Memory is O(chunk + n_symbols * carry), not O(file).

    `carry=0` is the bug the homework asks us to prove we did not write: every
    symbol restarts its warm-up at each chunk boundary.
    """
    tail = None                                        # <- carried ACROSS chunks
    pieces = []
    for chunk in chunks:
        work = chunk[["symbol", "close"]]
        if tail is not None:
            work = pd.concat([tail, work], ignore_index=True)
        pieces.append(vectorized(work)[len(work) - len(chunk):])
        # groupby().tail keeps rows in their original (time) order
        tail = work.groupby("symbol", observed=True, sort=False).tail(carry) if carry else None
    return np.concatenate(pieces)


# --------------------------------------------------------------------- partitioned
def partition_by_symbol(df: pd.DataFrame, n_parts: int) -> list[pd.DataFrame]:
    """Split [symbol, close] into n_parts frames, whole symbols per part.

    Each part keeps the original row positions as its index so the parent can
    scatter the results back into one array.
    """
    base = df[["symbol", "close"]].reset_index(drop=True)
    syms = base["symbol"].unique()
    groups = np.array_split(np.asarray(syms), n_parts)
    return [base[base["symbol"].isin(g)] for g in groups if len(g)]


def signal_partition(part: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """The pool worker: (row positions, signal) for one symbol partition."""
    return part.index.to_numpy(), vectorized(part)


def warmup(_=None) -> tuple[int, int]:
    """No-op task used to force every pool worker to start (and import pandas).

    Returns (pid, thread id) so the caller can tell when all n workers answered.
    """
    time.sleep(0.01)
    return os.getpid(), threading.get_ident()
