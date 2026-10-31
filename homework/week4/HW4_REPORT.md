# HW 4: Your tapes in SQL and Parquet, and a backtest you can trust

**Team:** Midway Traders. **Tapes:** Week 2 (Oct 9), Week 3 (Oct 16) and Week 4 (Oct 23).

Sizes, row counts, answers, P&L and digests repeat exactly on a rerun. Timings are from one run on an Apple M1 Pro with Python 3.13, SQLite 3.50.2, DuckDB 1.5.6, pyarrow 26.0.0 and polars 2.0.0, and will vary by machine.

## Rebuild

Rename `queries_*.sql.txt` to `queries_*.sql` (changed for submission to Canvas)

Run everything from this folder, with the tapes in the same folder as the scripts:

```text
session_week02_20261009_204000.jsonl.gz
session_week03_20261016_204000_market.jsonl.gz
session_week04_20261023_204025_market.jsonl.gz
```

```bash
pip install -r requirements.txt
python load_parquet.py --rebuild session_week*.jsonl.gz   # Parquet store
python load_sqlite.py  --rebuild session_week*.jsonl.gz   # SQLite store
python run_hw4_queries.py                                 # Task 2
python backtest_hw4.py                                    # Task 3
python clock_page.py                                      # Task 4
```

The stores go in `build/` and the numbers in `results/`. Don't submit `build/` or the tapes.

`clock_page.py` is the one script that also needs the team repository: this folder must be inside its git checkout, because the replay reads the bot's code at the commit that traded from git history.

## 1. Store it twice

| Week | JSONL bytes in | SQLite, no added indexes | SQLite, indexed | Parquet   | SQLite load | Parquet convert |
| ---- | -------------- | ------------------------ | --------------- | --------- | ----------- | --------------- |
| 2    | 264,960,858    | 59,883,520               | 62,365,696      | 6,740,873 | 5.6 s       | 5.0 s           |
| 3    | 22,973,410     | 26,791,936               | 28,725,248      | 1,774,914 | 1.3 s       | 0.7 s           |
| 4    | 19,630,409     | 22,568,960               | 24,199,168      | 1,648,291 | 1.1 s       | 0.5 s           |

| SQLite table  | Week 2  | Week 3  | Week 4  |
| ------------- | ------- | ------- | ------- |
| book          | 55,375  | 44,978  | 36,096  |
| book_level    | 862,067 | 783,924 | 654,169 |
| trade         | 4,511   | 3,090   | 4,506   |
| order_ack     | 34,881  | 0       | 0       |
| order_cancel  | 28,291  | 0       | 0       |
| order_reject  | 21,294  | 0       | 0       |
| session_event | 7       | 7       | 19      |
| leaderboard   | 120,132 | 0       | 0       |

Parquet holds the same market data with depth as list columns on `book_snapshot`, so it has no `book_level` table and fewer rows: 264,491, 48,075 and 40,621. Neither store keeps `participant` lines or the leaderboard's position maps, because no query uses them. The combined `tape.sqlite` is 112,140,288 bytes and the whole warehouse is 10,164,078 bytes.

- **Reading the sizes:** Weeks 3 and 4 are market-only recordings with books, trades and session events, so their other tables are empty. On those tapes SQLite is bigger than the JSONL (Week 4: 22,568,960 vs 19,630,409 bytes), because normalising depth costs a keyed row per level. Week 2 looks far better for SQLite only because, as the lab shows, 82% of that tape's bytes are leaderboard data.
- **"No added indexes":** this still includes each table's primary-key B-tree. Load and convert times include parsing the `.jsonl.gz`.
- **Schema:** one typed `STRICT` table per message type, with depth normalised into `book_level`. Both clocks (`tick`, `t`, `ts`) and `seq` are in every table of both stores. Every primary key starts with `(week, seq)`, and every as-of join includes `week`, because `seq` and `t` restart in each tape.
- **Parquet layout:** `tape.parquet/<table>/week=N/symbol=S/`, sorted by `(tick, seq)`, compressed with zstd. `order_reject`, `session_event` and `leaderboard` are partitioned by week only, because their rows have no symbol or only a nullable one.

**Indexes, and why each exists:**

| Index                                 | Why                                                                                                                                |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `book(week, symbol, t, seq)`          | Event window, forward markout and point lookups: the latest or first book of a symbol around a tick, last by `seq` inside the tick |
| `book(week, symbol, seq)`             | Backward as-of: the last book of a symbol written before a fill's line                                                             |
| `trade(buyer_id)`, `trade(seller_id)` | Find our own fills without reading every trade                                                                                     |

`session_event` deliberately has no index: with under 20 rows a week, a scan is the cheapest plan.

**Adding a week:** we loaded Weeks 2 and 3, then added Week 4 as a separate load. The older Parquet folders and per-week SQLite files were unchanged afterwards (same file sizes and modification times for Parquet, same hashes for SQLite). Week 4 only added new `week=4` folders and new rows in `tape.sqlite`. Loading a week that is already there skips it.

## 2. The same questions, both stores, timed

Each time is the best of 10 runs on one open connection, covering execute and fetch. SQLite has its indexes. "Agree" compares every output row across the two stores.

| Query                                  | Rows | SQLite    | DuckDB, Parquet | Faster | Agree |
| -------------------------------------- | ---- | --------- | --------------- | ------ | ----- |
| Event window (-1, +5, +15, +60 ticks)  | 28   | 0.310 ms  | 12.118 ms       | SQLite | yes   |
| As-of backward: own fills vs touch mid | 9    | 3.173 ms  | 17.297 ms       | SQLite | yes   |
| As-of forward: own fills, +60 markout  | 9    | 7.290 ms  | 18.869 ms       | SQLite | yes   |
| Full scan: spread and depth            | 31   | 51.843 ms | 5.462 ms        | DuckDB | yes   |
| Point lookup: one book, all levels     | 20   | 0.019 ms  | 4.038 ms        | SQLite | yes   |
| Point lookup: one book, top only       | 1    | 0.006 ms  | 2.962 ms        | SQLite | yes   |

**Which store won, and why.** SQLite's B-trees won every selective question, and DuckDB's column store won the full scan. A point lookup took 0.006 ms in SQLite against 2.962 ms in DuckDB, because a B-tree descends straight to the row. DuckDB still has to plan the query, open a file and decode a row group, even after partition pruning cut the scan to 1 of 34 files. Row-group statistics can add little here: the book table has 68 row groups across those 34 files, at most 2 per file, so the `week` and `symbol` folders do the pruning, not the min/max footers. The event window (0.310 vs 12.118 ms) is 112 lookups, and our as-of joins cover only our 1,241 fills, so one index probe per row stays cheap: backward 3.173 vs 17.297 ms, forward 7.290 vs 18.869 ms. On the full scan DuckDB won 5.462 to 51.843 ms (9.5x), because the four columns it reads occupy 301,337 of the book table's 4,601,038 Parquet bytes (6.5%), while SQLite walks whole rows of a 12,103,680-byte table. SQLite also costs about 11x the disk (112,140,288 vs 10,164,078 bytes), of which `book_level` alone is 60,575,744.

**As-of direction, and ties inside a tick:**

- **Event window: backward.** It uses the latest end-of-tick book at or before `t + k`. "Before the event" is `k = -1`, so no book from the event's own tick counts as pre-event.
- **Own fills against the touch: backward on `seq`, strictly before the fill's line.** It joins on `seq`, not tick, because a later snapshot in the same tick already contains our fill and isn't the book we traded against. `seq` is unique, so there are no ties. Fills against a one-sided book have no touch mid and drop out of the averages (the "Two-sided book" column).
- **Markout: forward.** It uses the first tick at or after `t + 60` with a book for that symbol, and that tick's last snapshot by `seq`. A tick can hold several snapshots, and the last is the state a tick-driven reader sees at the end of the tick. This is fine in a post-trade report and would be a look-ahead bug in a decision. 16 of our fills have no book 60 ticks later and drop out.

### Query outputs

Event window, with mids and % return from the pre-event mid:

| Week | Kind       | Symbol  | t    | Announced % | Pre     | +5      | +15     | +60     | ret +5 | ret +15 | ret +60 |
| ---- | ---------- | ------- | ---- | ----------- | ------- | ------- | ------- | ------- | ------ | ------- | ------- |
| 2    | earnings   | AAPL    | 400  | 6.2         | 194.03  | 199.25  | 209.05  | 210.80  | +2.69  | +7.74   | +8.64   |
| 2    | econ_print | AAPL    | 900  | -1.97       | 209.41  | 208.53  | 206.78  | 209.37  | -0.42  | -1.25   | -0.02   |
| 2    | econ_print | AMD     | 900  | -1.97       | 230.09  | 228.81  | 225.32  | 239.82  | -0.56  | -2.07   | +4.23   |
| 2    | econ_print | AMZN    | 900  | -1.97       | 155.15  | 153.79  | 151.50  | 154.64  | -0.88  | -2.35   | -0.33   |
| 2    | econ_print | ARENA10 | 900  | -1.97       | 367.89  | 364.78  | 358.82  | 365.72  | -0.84  | -2.46   | -0.59   |
| 2    | econ_print | GOOGL   | 900  | -1.97       | 178.44  | 176.53  | 174.28  | 181.34  | -1.07  | -2.33   | +1.63   |
| 2    | econ_print | INTC    | 900  | -1.97       | 20.24   | 20.04   | 19.78   | 21.36   | -0.99  | -2.27   | +5.51   |
| 2    | econ_print | META    | 900  | -1.97       | 720.49  | 714.49  | 702.82  | 705.06  | -0.83  | -2.45   | -2.14   |
| 2    | econ_print | MSFT    | 900  | -1.97       | 404.55  | 400.87  | 394.93  | 402.63  | -0.91  | -2.38   | -0.47   |
| 2    | econ_print | NFLX    | 900  | -1.97       | 1395.89 | 1379.71 | 1357.98 | 1357.22 | -1.16  | -2.72   | -2.77   |
| 2    | econ_print | NVDA    | 900  | -1.97       | 115.03  | 114.11  | 112.51  | 117.36  | -0.80  | -2.19   | +2.03   |
| 2    | econ_print | TSLA    | 900  | -1.97       | 299.36  | 297.85  | 292.76  | 293.72  | -0.50  | -2.20   | -1.88   |
| 3    | earnings   | NVDA    | 300  | -3.81       | 168.87  | 167.12  | 162.44  | 164.46  | -1.03  | -3.81   | -2.61   |
| 3    | earnings   | TSLA    | 700  | -5.17       | 230.10  | 223.65  | 224.07  | 223.78  | -2.81  | -2.62   | -2.75   |
| 4    | earnings   | AMZN    | 350  | -7.91       | 177.16  | 165.41  | 165.64  | 166.82  | -6.63  | -6.50   | -5.84   |
| 4    | earnings   | GOOGL   | 800  | -5.66       | 173.94  | 165.65  | 168.20  | 171.50  | -4.77  | -3.30   | -1.41   |
| 4    | econ_print | AAPL    | 1150 | -2.49       | 223.21  | 220.43  | 219.79  | 219.26  | -1.25  | -1.53   | -1.77   |
| 4    | econ_print | AMD     | 1150 | -2.49       | 193.92  | 190.31  | 187.75  | 191.67  | -1.86  | -3.18   | -1.16   |
| 4    | econ_print | AMZN    | 1150 | -2.49       | 166.62  | 164.16  | 164.53  | 163.91  | -1.48  | -1.25   | -1.63   |
| 4    | econ_print | ARENA10 | 1150 | -2.49       | 274.92  | 272.56  | 266.04  | 269.32  | -0.86  | -3.23   | -2.04   |
| 4    | econ_print | GOOGL   | 1150 | -2.49       | 169.78  | 166.58  | 167.63  | 167.20  | -1.89  | -1.27   | -1.52   |
| 4    | econ_print | INTC    | 1150 | -2.49       | 24.38   | 23.93   | 24.07   | 24.63   | -1.82  | -1.29   | +1.02   |
| 4    | econ_print | META    | 1150 | -2.49       | 663.43  | 654.67  | 654.53  | 646.39  | -1.32  | -1.34   | -2.57   |
| 4    | econ_print | MSFT    | 1150 | -2.49       | 420.43  | 413.90  | 414.32  | 411.36  | -1.55  | -1.45   | -2.16   |
| 4    | econ_print | NFLX    | 1150 | -2.49       | 548.55  | 546.85  | 543.65  | 535.00  | -0.31  | -0.89   | -2.47   |
| 4    | econ_print | NVDA    | 1150 | -2.49       | 112.86  | 110.58  | 111.62  | 111.24  | -2.02  | -1.09   | -1.43   |
| 4    | econ_print | ORCA    | 1150 | -2.49       | 28.61   | 28.26   | 28.22   | 27.68   | -1.22  | -1.35   | -3.22   |
| 4    | econ_print | TSLA    | 1150 | -2.49       | 238.34  | 234.84  | 235.12  | 233.24  | -1.47  | -1.35   | -2.14   |

Our fills against the prevailing touch mid (positive means worse than mid):

| Week | Seat     | Fills | As taker | Two-sided book | All, bp | Taker, bp | Maker, bp |
| ---- | -------- | ----- | -------- | -------------- | ------- | --------- | --------- |
| 2    | broker   | 25    | 25       | 25             | +4.05   | +4.05     | n/a       |
| 2    | trader_1 | 328   | 211      | 262            | -0.95   | +4.60     | -10.07    |
| 2    | trader_2 | 57    | 43       | 55             | +5.66   | +9.91     | -8.04     |
| 3    | broker   | 199   | 32       | 199            | -3.20   | +0.79     | -3.97     |
| 3    | trader_1 | 71    | 17       | 70             | -23.49  | +28.58    | -38.92    |
| 3    | trader_2 | 32    | 12       | 31             | -28.45  | +2.61     | -45.53    |
| 4    | broker   | 349   | 5        | 349            | -3.72   | -1.54     | -3.75     |
| 4    | trader_1 | 118   | 23       | 108            | -17.24  | +19.62    | -26.67    |
| 4    | trader_2 | 62    | 16       | 58             | -35.67  | +5.87     | -51.49    |

Our fills, +60 tick markout (positive means the mid moved our way):

| Week | Seat     | Fills | Markout, bp | Markout, $ |
| ---- | -------- | ----- | ----------- | ---------- |
| 2    | broker   | 25    | -18.44      | -913.05    |
| 2    | trader_1 | 328   | +31.44      | +272.88    |
| 2    | trader_2 | 57    | -6.90       | -705.42    |
| 3    | broker   | 199   | -41.33      | -919.81    |
| 3    | trader_1 | 71    | -27.46      | -434.88    |
| 3    | trader_2 | 32    | +188.24     | +320.85    |
| 4    | broker   | 333   | -19.69      | -1,608.32  |
| 4    | trader_1 | 118   | +54.06      | +3,113.89  |
| 4    | trader_2 | 62    | +81.16      | +939.37    |

Full scan, two-sided books:

| Week | Symbol | Snapshots | Mean spread, bp | Mean top depth, shares |
| ---- | ------ | --------- | --------------- | ---------------------- |
| 2    | AAPL   | 5,108     | 9.57            | 17.37                  |
| 2    | AMD    | 3,344     | 6.97            | 17.94                  |
| 2    | AMZN   | 5,058     | 10.11           | 14.13                  |
| 2    | GOOGL  | 5,146     | 8.34            | 14.81                  |
| 2    | INTC   | 5,183     | 15.40           | 21.64                  |
| 2    | META   | 5,062     | 3.18            | 19.10                  |
| 2    | MSFT   | 5,014     | 6.92            | 14.56                  |
| 2    | NFLX   | 1,782     | 46.29           | 14.95                  |
| 2    | NVDA   | 5,127     | 11.05           | 15.31                  |
| 2    | TSLA   | 5,111     | 7.52            | 24.43                  |
| 3    | AAPL   | 4,064     | 28.83           | 16.03                  |
| 3    | AMD    | 4,148     | 5.97            | 14.25                  |
| 3    | AMZN   | 4,000     | 5.68            | 14.01                  |
| 3    | GOOGL  | 4,225     | 22.89           | 14.93                  |
| 3    | INTC   | 1,872     | 60.72           | 23.14                  |
| 3    | META   | 4,012     | 1.88            | 18.59                  |
| 3    | MSFT   | 4,083     | 6.07            | 14.53                  |
| 3    | NFLX   | 4,067     | 6.36            | 13.63                  |
| 3    | NVDA   | 4,233     | 7.53            | 34.95                  |
| 3    | TSLA   | 4,310     | 14.43           | 18.71                  |
| 4    | AAPL   | 3,067     | 4.21            | 28.27                  |
| 4    | AMD    | 3,027     | 13.84           | 18.55                  |
| 4    | AMZN   | 3,192     | 16.33           | 27.76                  |
| 4    | GOOGL  | 3,220     | 15.09           | 30.42                  |
| 4    | INTC   | 3,195     | 16.01           | 36.04                  |
| 4    | META   | 3,038     | 5.97            | 32.04                  |
| 4    | MSFT   | 2,983     | 2.73            | 28.68                  |
| 4    | NFLX   | 2,991     | 2.98            | 26.13                  |
| 4    | NVDA   | 3,069     | 6.54            | 28.95                  |
| 4    | ORCA   | 2,526     | 12.50           | 23.84                  |
| 4    | TSLA   | 3,028     | 5.15            | 22.38                  |

Point lookup: AAPL at Week 4 t300, end of tick (seq 4824), has best bid 219.98, best ask 219.99 and mid 219.985, with 20 depth levels in both stores.

## 3. A backtest you can trust

**Strategy.** `MidwayMomentum` in `backtest_hw4.py` ports our momentum seat. It rides good news and fades bad news from the `CALENDAR_EVENT` message, using `events.py` (a copy of `team/events.py`), and otherwise trades the 5/20 moving-average crossover. It runs on the lab's engine and reads the Parquet tape. A decision at tick `t` sees books up to `t`, an order fills on the first book of `t + 1` or later, and each fill pays the taker fee measured from that week's tape (0.30% in all three). The strategy is handed the tick and reads no clock.

**What was fixed in advance.** The split is t = 1400, the lab's, set in the code before the first run; the IS column runs through the end of that tick. Parameters are the live seat's, and no parameter was changed after seeing results.

**How it differs from the live seat.** The engine models taker fills only, while the live seat rests quotes and earns rebates. So the port crosses the spread, needs its crossover to clear a taker round trip (`max(0.3%, 2 × fee)`), and trades when the signal changes state rather than on every tick it holds. It trades the ten equities, not ORCA, and doesn't model the opening auction, the short-sale rule, the AAPL dividend or the IPO. Treat this as a test of the logic under worse execution, not a forecast of live P&L.

**Week 2** (2,827 ticks, cap 400, long-only; events at t400 and t900)

| Strategy        | P&L, $  | IS t≤1400 | OOS    | Max DD | Sharpe/tick | Fills | Fees, $ | Digest         |
| --------------- | ------- | --------- | ------ | ------ | ----------- | ----- | ------- | -------------- |
| midway_momentum | -29     | -645      | +616   | 1,141  | -0.0005     | 97    | 616     | `4e408abc270c` |
| do_nothing      | +198    | -1,257    | +1,455 | 1,931  | +0.0030     | 0     | 0       | `4f53cda18c2b` |
| buy_hold        | +1,203  | +792      | +411   | 3,308  | +0.0067     | 56    | 170     | `b70459ca4a6a` |
| momentum        | -16,501 | -8,708    | -7,793 | 16,858 | -0.0547     | 1187  | 9,931   | `17238e463468` |

**Week 3** (2,634 ticks, cap 600, long-only; events at t300 and t700)

| Strategy        | P&L, $ | IS t≤1400 | OOS    | Max DD | Sharpe/tick | Fills | Fees, $ | Digest         |
| --------------- | ------ | --------- | ------ | ------ | ----------- | ----- | ------- | -------------- |
| midway_momentum | +66    | -67       | +133   | 1,241  | +0.0014     | 31    | 70      | `f04a2a7fac1c` |
| do_nothing      | -522   | -593      | +71    | 1,392  | -0.0112     | 0     | 0       | `4f53cda18c2b` |
| buy_hold        | -2,874 | -3,063    | +188   | 4,241  | -0.0224     | 62    | 321     | `a7b55c491c62` |
| momentum        | -2,781 | +1,751    | -4,532 | 9,129  | -0.0089     | 1175  | 6,014   | `0db244802d63` |

**Week 4** (2,398 ticks, cap 800, shorts allowed; events at t350, t800 and t1150)

| Strategy        | P&L, $ | IS t≤1400 | OOS  | Max DD | Sharpe/tick | Fills | Fees, $ | Digest         |
| --------------- | ------ | --------- | ---- | ------ | ----------- | ----- | ------- | -------------- |
| midway_momentum | +908   | +832      | +76  | 1,508  | +0.0103     | 129   | 1,216   | `7d0ed2e4ed33` |
| do_nothing      | -1,152 | -1,231    | +80  | 1,596  | -0.0218     | 0     | 0       | `4f53cda18c2b` |
| buy_hold        | -1,591 | -1,908    | +317 | 6,459  | -0.0091     | 73    | 472     | `3964e9da732f` |
| momentum        | +1,040 | +663      | +378 | 5,575  | +0.0106     | 464   | 3,027   | `77e99e576484` |

Sharpe is the mean over the sample standard deviation (`ddof=1`) of per-tick equity changes, and is not annualised. A fill is one price level swept, as in the lab's engine. All runs start from $250,000 and 20 granted shares per name, so `do_nothing` is the mark on those shares: judge a strategy against it, not against zero. The Week 2 baselines reproduce the lab's P&Ls and digests.

**Reading it.** This is not evidence of an edge. Over the three weeks the strategy made +$945 against -$1,476 for doing nothing and -$3,262 for buy-and-hold, and it beat doing nothing in 2 of the 3 weeks. Its Sharpes are near zero (-0.0005, +0.0014, +0.0103). The naive momentum rule lost $18,242 overall, paying the fee on 2,826 fills against our 257, though it beat us in Week 4 (+$1,040 vs +$908). Every scheduled event falls in-sample, so the OOS half tests only the crossover, never the event logic. Week 4 also supplies nearly all the profit and is fragile, as the sensitivity table shows.

**Determinism.** The digest hashes the full fill list. It is identical unpaced, unpaced again and paced at 30x on the tape's `ts`:

| Week | Unpaced        | Unpaced again  | Paced at 30x   | Paced wall time | P&L unpaced / paced | Identical |
| ---- | -------------- | -------------- | -------------- | --------------- | ------------------- | --------- |
| 2    | `4e408abc270c` | `4e408abc270c` | `4e408abc270c` | 100 s           | -29.04 / -29.04     | yes       |
| 3    | `f04a2a7fac1c` | `f04a2a7fac1c` | `f04a2a7fac1c` | 100 s           | +65.81 / +65.81     | yes       |
| 4    | `7d0ed2e4ed33` | `7d0ed2e4ed33` | `7d0ed2e4ed33` | 100 s           | +908.00 / +908.00   | yes       |

**Sensitivity: latency 0/1/2 ticks, and touch vs depth fills.** Week 4's +$908 falls to +$53 at 2 ticks of latency and to +$6 with touch-only fills, so it depends on reaching the depth in the first ticks of a print.

| Week | Setting          | P&L, $ | IS   | OOS  | Max DD | Fills | Fees, $ |
| ---- | ---------------- | ------ | ---- | ---- | ------ | ----- | ------- |
| 2    | latency 0        | +38    | -578 | +616 | 1,141  | 104   | 616     |
| 2    | latency 1 (base) | -29    | -645 | +616 | 1,141  | 97    | 616     |
| 2    | latency 2        | -372   | -613 | +241 | 1,150  | 85    | 552     |
| 2    | touch fills      | +26    | -642 | +668 | 1,180  | 99    | 605     |
| 3    | latency 0        | +70    | -26  | +96  | 1,237  | 33    | 75      |
| 3    | latency 1 (base) | +66    | -67  | +133 | 1,241  | 31    | 70      |
| 3    | latency 2        | -38    | -225 | +187 | 1,422  | 27    | 61      |
| 3    | touch fills      | +120   | -9   | +128 | 1,052  | 27    | 67      |
| 4    | latency 0        | +1,055 | +979 | +76  | 2,008  | 132   | 1,222   |
| 4    | latency 1 (base) | +908   | +832 | +76  | 1,508  | 129   | 1,216   |
| 4    | latency 2        | +53    | -46  | +99  | 1,108  | 89    | 762     |
| 4    | touch fills      | +6     | -63  | +69  | 1,210  | 86    | 602     |

## 4. Logical vs real clock: Friday Oct 23

All numbers are from the Week 4 tape. The comparison covers our two trader seats; the broker has no `on_tick` to replay. The replay runs commit `9485c5f`, the last before the block opened, though the tape can't prove that is exactly what ran. Its `scripts/event_backtest.py` runs the unchanged `on_tick` on a clock driven by the tape's `ts`, every 0.1 s with 0.05 s latency, a 0.30% taker fee and a 0.05% rebate. That script predates Week 4, so for this run sells may go short and ORCA is left out of the start-up check. It has no IPO allocation.

**Live vs the same `on_tick` replayed:**

| Seat     | Run    | Fills | Avg price | P&L, $ |
| -------- | ------ | ----- | --------- | ------ |
| trader_1 | live   | 118   | 255.03    | +2,337 |
| trader_1 | replay | 110   | 255.31    | +1,308 |
| trader_2 | live   | 62    | 217.69    | +815   |
| trader_2 | replay | 79    | 264.16    | +1,251 |

P&L is the fills' cash flow, net of fees and rebates, plus the inventory they left at the block's last mid, computed the same way for both runs. It excludes granted shares' marks, interest, dividends and any IPO allocation.

**What changed.** Replay reacted about as fast as live but didn't reproduce the fills or the money: trader_1 had 118 fills live and 110 in replay (+$2,337 vs +$1,308), and trader_2 had 62 and 79 (+$815 vs +$1,251). Two model limits are the likely causes. Resting orders fill for half of each print with no queue, where live quotes wait in a real queue among bots that react to them. The replay also starts with 20 granted shares per name, and the tape doesn't record the live holdings that size a fade.

**Wall-clock length of a tick.** 2,397 ticks took 2,985 s, so a tick averaged 1.262 s: p1 0.146 s, median 1.265 s, p99 2.535 s, max 10.841 s. Our code assumes 1.0 s (`EXCHANGE_TICK_SEC`), so a fade that waits for the venue's 15-tick ramp by timing 15 s waits only about 11.9 ticks at this pace.

**Ticks to react to each event.** This is the first fill in the event's direction after the event's own line, since a fill earlier in the same tick came before the message. Maker fills count, because a fade's first order can rest. The tape has no order sends or acks, so a fill is the earliest evidence available, and a maker fill shows when the order was hit, not when it was sent.

| t    | Event                                | Seat     | Live, ticks | Replay, ticks |
| ---- | ------------------------------------ | -------- | ----------- | ------------- |
| 350  | Earnings: AMZN moving -7.9%          | trader_1 | 1           | 0             |
| 350  | Earnings: AMZN moving -7.9%          | trader_2 | 2           | 0             |
| 800  | Earnings: GOOGL moving -5.7%         | trader_1 | 0           | 0             |
| 800  | Earnings: GOOGL moving -5.7%         | trader_2 | 0           | 0             |
| 1150 | Econ Print: market-wide moving -2.5% | trader_1 | 0           | 0             |
| 1150 | Econ Print: market-wide moving -2.5% | trader_2 | 0           | 0             |

Both traders filled within 2 ticks of all three events live, and at tick 0 in replay.

**Every read of `time.time()`, `time.monotonic()` or `datetime.now()` in `team/` at `9485c5f`.** This list was made by hand from `git grep -nE 'time\.time|time\.monotonic|datetime\.now' 9485c5f -- 'team/*.py'` and from reading the code around each hit:

| Where                      | Code                                                              | Path     | What it does                                                                                                                                            |
| -------------------------- | ----------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `team/base_trader.py:129`  | `clock: Callable[[], float] = time.monotonic,`                    | decision | default of BaseTrader's clock: on_tick reads it for event plans and windows, reject cooldowns, order expiry and reaction expiry, all counted in seconds |
| `team/base_trader.py:1055` | `path = logs / f"{seat}_{datetime.now():%Y%m%d_%H%M%S}.log"`      | edge     | log file name                                                                                                                                           |
| `team/broker.py:185`       | `self._clock = time.monotonic`                                    | decision | the broker's quote pause around events and listings, and its message pacing                                                                             |
| `team/broker.py:314`       | `age = time.time() - self.state.last_quote_time.get(symbol, 0.0)` | decision | requote when a quote is older than 30 s                                                                                                                 |
| `team/broker.py:380`       | `self.state.last_quote_time[symbol] = time.time()`                | decision | stamps that quote age                                                                                                                                   |
| `team/logs.py:42`          | `stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")`            | edge     | log line timestamp                                                                                                                                      |
| `team/logs.py:72`          | `path = LOG_DIR / f"{bot}_{datetime.now():%Y%m%d_%H%M%S}.log"`    | edge     | log file name                                                                                                                                           |
| `team/reconnect.py:45`     | `clock: Callable[[], float] = time.monotonic,`                    | edge     | reconnect backoff                                                                                                                                       |

Four of the eight reads sit in a decision path. The two `time.monotonic` defaults supply `now` to everything counted in seconds. The replay injects a clock into the trader seats, but it is still seconds of `ts`, not ticks.
