# Phase 2: A real edge, backtested

**Team:** Midway Traders (2026Autumn 21): Mohit Parag Apte, Amy Wang, Akshay Shivdasani, Hoang Son Pham.
**Seat:** `midway_traders_trader_1`. **Session:** 5 (Thu Oct 29, 17:10–18:00 CT).
**Reproduce:** `python scripts/phase2_backtest.py sessions/session_week05_20261029_221018_market.jsonl.gz` (about 30 s; deterministic). Numbers: `results/phase2/results.json`. Plot: `docs/phase2/equity_week05.png`.

## 1. Hypothesis

We tested the trend signal trader_1 ran live in Session 5 (`team/momentum_trader.py`). It rests a 5-share bid at the touch when the mean of the last 5 book mids is more than 0.3% above the mean of the last 20, and offers when it is more than 0.3% below. Quotes are pulled from a stock around its scheduled events, which the separate event module trades.

**Claim:** a move in a book's mid persists for several updates before the market makers re-anchor, so once the short average clears the long one by the buffer, the mid keeps going far enough to pay for the trade. **Prediction:** outside event windows it does not. With the event trades switched off, the signal loses to doing nothing at the live fees, and trader_1's live result comes from the event trades. We committed this, with the falsification test, before running anything (`docs/phase2/HYPOTHESIS.md`, commit `18fca4b`).

## 2. Data and method

- **Data:** the recorded Session 5 market tape: 49,461 book updates, 3,656 trades and 27 session events over 50 minutes. It contains the NFLX (+7.6%) and AMD (−8.3%) earnings, a −3.0% market-wide econ print, and six circuit-breaker halts. Every trade on it names its buyer and seller, so trader_1's 84 live fills are on it too.
- **Engine:** `scripts/event_backtest.py`, which runs our real trader code unchanged. Taker orders walk the recorded book; maker quotes fill from later prints at half the printed size, with no queue position; orders arrive 50 ms after they're sent; the venue's rejects and interest on idle cash are modelled.
- **Costs:** the fees Session 5 actually charged, read from the tape: **taker 30 bps, maker rebate 5 bps**. Each bot starts with $300,000 and 20 shares per stock.
- **Bots:** the live bot (signal plus event trades); the signal alone (event trades off, quotes still pulled around events); do-nothing; and two look-ahead variants.
- **No fitting:** the parameters are the live ones, unchanged in `team/` since before the session, and nothing was tuned on this tape.

## 3. Results (P&L includes fees and rebates; "vs DN" is P&L minus do-nothing)

| Bot | P&L | vs DN | Max drawdown | Fills | Hit rate (closed round trips) |
|---|---|---|---|---|---|
| Live bot | +$2,556 | **+$2,431** | $1,373 | 97 | 45/60 (75%) |
| **Signal only** | −$98 | **−$223** | $2,595 | 55 | 14/30 (47%) |
| Do nothing | +$125 | 0 | $3,008 | 0 | – |

**Dispersion.** We split the tape into five independent 10-minute blocks, each started fresh. All figures are vs DN:

- **The signal alone** averaged **−$172 per block (sd $282)** and was ahead of do-nothing in **0 of 5** blocks (−48, −73, −734, −3, −4).
- **The live bot** averaged +$433 (sd $764). It was ahead in 2 of 5 blocks, and almost all of that came from the block holding the NFLX earnings (+1,937).

**Placebo across seeds.** We ran the same rule in the house simulator over **10 price seeds** of 3,000 ticks, at a 30 bps taker fee. Its prices are a random walk, so a genuine backtest must find no edge, and it found none: a mean of **−$14 (sd $107)**, with 1 of 10 seeds positive and a worst drawdown of $214. The simulator's `seed` argument alone does not change its prices, because they come from `FUNDAMENTAL_SEED`. We found this and set the price seed for each run.

**Verdict:** the prediction held and the claim is rejected. The trend signal did not cover its costs in Session 5. The reason is visible in the data: the signal's feature is *negatively* correlated with the very next mid move (−0.19 over 45,391 updates). On this venue a short rise is more often followed by a fall, so a trend rule is pointed the wrong way at its own horizon. The live bot's edge in the replay comes from the event trades.

## 4. Look-ahead check

We measured each signal entry by the mid's move over the next 20 updates, in the signal's direction and before costs. The signal is then rebuilt from shifted data.

| Signal built from mids up to | Entries | Mean move | t-stat |
|---|---|---|---|
| t (as live) | 109 | −37.6 bps | −1.30 |
| t − 1 (one update old) | 109 | +12.9 bps | +0.52 |
| t + 1 (one update ahead) | 109 | +7.3 bps | +0.32 |
| **t + 20 (planted bug)** | 107 | **+110.7 bps** | **+2.74** |

The planted bug shows the harness catches look-ahead: a signal that has seen the outcome shows an inflated, significant "edge", and the live signal shows none. Lagging by one update moves the result toward zero (next-move correlation −0.19 → −0.09), which is how a real relationship decays as its data ages. A one-update *peek* is not decisive here: one extra mid is a fifth of the short average, and the next move is mostly bid-ask bounce. None of the first three rows is significant (|t| < 1.3), and at trade level the lagged and peeked signals (+$204 and −$521 vs DN) differ by less than one block's standard deviation.

## 5. Live vs backtest

| On the same tape | Fills | Trading P&L | Fees | Rebates | NFLX P&L |
|---|---|---|---|---|---|
| **Live** (trader_1's fills on the tape) | 84 | **−$292** | $205 | $84 | −$169 |
| Replay of the live bot | 97 | **+$2,349** | $321 | $45 | +$2,261 |
| Replay, signal only | 55 | −$274 | $75 | $23 | – |

Trading P&L is cash from fills, fees and rebates, plus the bot's net position marked at the mid. Trader_1's whole live result (−$292) is close to what the signal *alone* made in the replay (−$274). In other words, the roughly +$2,600 that the replay credits to the event trades did not happen live. The gap is $2,640, and **$2,430 of it is NFLX**:

1. **Queue position and latency.** At the NFLX news, the replay bought 30 shares at **$839** in 0.10 s, from asks still showing in the recorded book. Live, faster bots took those asks first. Trader_1's bid sat at its cap and filled at **$867.19**, 3.3% worse on the same entry. A replay can't see competitors, so it hands us liquidity that was really gone. This is the largest effect.
2. **Inventory caught in a halt.** Live filled 53 shares at the cap and sold 32 at $906.75 within a second. That left 21 shares when NFLX was **halted 7.2 s after the news, for 5 minutes**, then halted again later. NFLX closed at $786. The replay bought exactly 30, sold them at $910.97 and was flat before the halt.
3. **Fees are not the story.** Live paid less in fees than the replay, and earned more in rebates, because more of its fills were maker fills.
4. **Our own market impact.** The tape already contains our live prints, so the replay trades against a book our live orders had moved. This is a small effect next to the first two.

The plot shows the two curves level through the NFLX entry. They split when the stuck NFLX shares are marked down after the halts and the econ print, while the replay stays flat.

## 6. What we take from it

The trend signal has no edge here at its 5-to-20-update horizon: it loses about its trading cost in every block, so we'll retire it at this size or slow it down past the bounce. The event trades are where the replay finds money, but it overstates them because it can't model the race for liquidity in the first second. Before trusting it again we'll fill event entries at the plan's price cap rather than the stale ask, and size rides so they can be exited before the venue's 5%-in-60-s velocity halt trips.
