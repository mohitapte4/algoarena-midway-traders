# HW 7: Big data, vectorize your signal

**Team:** Midway Traders. **Machine:** Apple M1 Pro (6 performance and 2 efficiency cores), 16 GB of RAM, Python 3.13.9, pandas 2.3.3, numpy 2.5.4.

Run `python run_hw7.py`. It builds the seeded dataset in `build/` (0.5 GB, don't submit it), then runs every task. The worker functions are in `hw7_signal.py`. Timings are from one run and move by up to about 15% between runs. Over five full runs, the loop-vs-vectorized speedup ranged from 28x to 33x.

**Signal** (per symbol, float32): `clip((SMA20 − SMA100) / (std100(pct_change) · close), −3, 3)`. The volatility needs 101 closes, so each symbol's first 100 bars are NaN.

## 1. Memory

The dataset has 500 symbols × 10,140 one-minute bars = **5,070,000 rows**. Each symbol is a geometric random walk.

| Step | `memory_usage(deep=True).sum()` |
| --- | ---: |
| `read_csv` defaults (object strings, float64, int64) | **826.4 MB** |
| + `symbol` → `category` | 557.7 MB |
| + OHLC → `float32`, volume → `int32` | 456.3 MB |
| + `timestamp` → `DatetimeIndex` | **152.1 MB** (5.4x smaller) |

## 2. Naive loop vs vectorized

| | rows | time |
| --- | ---: | ---: |
| per-row Python loop (20 symbols) | 202,800 | 1.85 s |
| vectorized, same rows | 202,800 | 0.066 s (**28x faster**) |
| vectorized, full frame | 5,070,000 | 1.72 s (the loop would take ≈ 46 s) |

**Equivalence:**
- The NaN masks are identical: 2,000 warm-up NaNs, which is 20 symbols × 100.
- `np.allclose(rtol=1e-6, atol=1e-6)` is `True` on the rows that are not NaN.
- The largest absolute difference is **0.0**. The loop takes its returns in float32, as `pct_change` does, so the outputs are bit-identical.

## 3. Out of core

`chunked()` reads the CSV with `read_csv(chunksize=250_000)`. Before each chunk, it prepends the **last 100 rows of each symbol**, because the volatility at bar *t* needs closes *t−100…t*. Each run below was measured in a fresh process, so each peak is its own.

| Run | peak RSS |
| --- | ---: |
| in memory | **943 MB** |
| chunked (21 chunks) | **365 MB** |

**Equivalence:**
- Chunked equals in-memory on all 5,070,000 rows: `np.array_equal(..., equal_nan=True)` is `True`, and the largest difference is 0.0.
- With `carry=0`, **1,000,000 rows are wrong**. That is 20 chunk boundaries × 500 symbols × 100 warm-up bars.

## 4. Processes vs threads

The data is split into *n* partitions of whole symbols. The same partitions and the same worker (`signal_partition`) go through both executors. A single process with no pool takes **1.72 s**.

| Executor | workers | total wall | pool start | pickle partitions | speedup |
| --- | ---: | ---: | ---: | ---: | ---: |
| process | 1 | 2.52 s | 409 ms | 9 ms | 0.68x |
| process | 2 | 1.60 s | 433 ms | 27 ms | 1.08x |
| process | 4 | **1.17 s** | 378 ms | 29 ms | **1.47x** |
| process | 8 | 1.41 s | 614 ms | 22 ms | 1.22x |
| thread | 1 | 1.62 s | 13 ms | none | 1.06x |
| thread | 2 | 0.96 s | 13 ms | none | 1.78x |
| thread | 4 | 0.69 s | 13 ms | none | 2.50x |
| thread | 8 | **0.57 s** | 13 ms | none | **3.02x** |

**Notes:**
- Every parallel output is bit-identical to the single-process output.
- "Pool start" times the pool until every worker has answered a no-op task. For processes, that means each worker has spawned and imported pandas.
- Pickling is cheap (under 30 ms). The rest of the process overhead is start-up and copying the partitions and results through pipes.

## Explanations

**(a) Copies.** Our frame is sorted by time, so each `transform` gathers every symbol's rows into a fresh copy, three times over. `rolling` then casts each float32 slice to a float64 copy. Each `transform` writes its results into a new full-length array, and `fast − slow`, the division, `.to_numpy("float32")` and `np.clip` each allocate a temporary. Partitioning copies again, and the process pool pickles everything a second time. To avoid most of this: sort by `(symbol, timestamp)` so each symbol is a contiguous view, cast to float64 once, build the rolling windows from one `cumsum`, do the arithmetic in place with `out=`, and give workers shared memory or a memory-mapped file instead of pickles.

**(b) Look-ahead.** `signal_t` uses `close_t`, which is only known once bar *t* ends, so the earliest it can trade is bar *t+1*. On our random-walk data, where there is no edge to find, trading the unshifted signal against its own bar's return shows **+0.33 bp per bar with a t-stat of 18.2**. That is a fake edge, because `r_t` is part of the 20-bar average. After `groupby("symbol").shift(1)` the result is +0.03 bp with a t-stat of 1.5, which is noise. The shift must be done per symbol: a plain `.shift(1)` on our interleaved frame would hand one symbol's signal to the next symbol's row. Live, `on_tick` sees only prices that have printed, so a backtest has to apply the shift on purpose to match.

**(c) numba, polars, Dask.** We'd reach for numba when the logic is path-dependent and can't be written as rolling operations, such as a position state machine or a trailing stop. We'd use polars when the data reaches tens of GB or dozens of features, because its engine runs multithreaded without the GIL and without the copies in (a). Dask is for data that no longer fits on one machine, where a scheduler can do the chunking and partitioning for us. None of these is needed here: the tuned frame is 152 MB, a full pass takes 1.7 s, a numba compile would cost about as much as the whole computation, and the signal is already pure rolling and numpy work.

**(d) Processes vs threads.** **Threads win at every worker count.** The heavy steps release the GIL: pandas' rolling kernels (`roll_mean` and `roll_var`) run `nogil`, and large numpy operations do too. Only the per-group Python work in `groupby` holds the GIL; by Amdahl's law, 3.02x on 8 threads means about 24% of the run is serial. Processes avoid the GIL, but each pool costs 0.4–0.6 s to start (a `spawn` interpreter that imports pandas), and the data has to travel through pipes both ways. That overhead is a large share of a 1.7 s job, which is why one process is slower than no pool. **Adding processes stopped paying off after 4.** At 8, start-up rose to 0.61 s and the time after start-up didn't drop (0.79 s at 4, 0.80 s at 8), because the two efficiency cores hold back the equal-sized partitions. Processes would win on GIL-bound work, such as the naive loop, or on longer jobs where one pool is kept alive.
