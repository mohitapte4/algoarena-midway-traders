# Phase 2: A real edge, backtested

Team: Midway Traders (2026Autumn 21), Mohit Parag Apte, Amy Wang, Akshay Shivdasani, Hoang Son Pham.
Seat `midway_traders_trader_1`, Session 5 (Thu Oct 29, 17:10–18:00 CT).

To reproduce, put the Session 5 tape in `sessions/` and run
`python scripts/phase2_backtest.py sessions/session_week05_20261029_221018_market.jsonl.gz`.
It takes about 30 seconds and gives the same numbers every time. They're saved to `results/phase2/results.json`, and the plot to `docs/phase2/equity_week05.png`.

## 1. Hypothesis

We tested the trend signal trader_1 ran live in Session 5 (`team/momentum_trader.py`). It rests a 5-share bid at the touch when the average of the last 5 book mids is more than 0.3% above the average of the last 20, and offers when it's more than 0.3% below. Around a scheduled event it pulls its quotes in that stock and leaves the trading to our event module.

The idea behind it is that a move in the mid keeps going for a few updates before the market makers catch up, so once the short average clears the long one by the buffer, there's enough move left to pay for the trade. We didn't expect it to work outside the event windows. Our prediction was that with the event trades turned off, the signal would do worse than doing nothing at the live fees, and that trader_1's live result came from the event trades. We wrote this down and committed it before running anything (`docs/phase2/HYPOTHESIS.md`, commit `18fca4b`).

## 2. Data and method

The data is the recorded market tape from Session 5: 49,461 book updates, 3,656 trades and 27 session events over 50 minutes. It covers the NFLX (+7.6%) and AMD (−8.3%) earnings, a −3.0% market-wide econ print and six circuit-breaker halts. Every trade on the tape names the buyer and seller, so trader_1's 84 live fills are on it as well.

We replayed the tape with `scripts/event_backtest.py`, which runs our actual trader code. Taker orders walk the recorded book. Maker quotes fill from later prints at half the printed size, with no queue position. Orders reach the venue 50 ms after they're sent, and the venue's rejects and the interest on idle cash are modelled. We used the fees Session 5 actually charged, which we read off the tape's fills: 30 bps for takers and a 5 bps maker rebate. Each bot starts with $300,000 and 20 shares of each stock.

We ran the live bot (signal plus event trades), the signal on its own (event trades off, quotes still pulled around events), a do-nothing baseline and two look-ahead variants. The parameters are the ones that ran live. They haven't changed in `team/` since before the session, and we didn't tune anything on this tape.

## 3. Results

P&L includes fees and rebates. "vs DN" is P&L minus the do-nothing baseline.

| Bot | P&L | vs DN | Max drawdown | Fills | Hit rate (closed round trips) |
|---|---|---|---|---|---|
| Live bot | +$2,556 | +$2,431 | $1,373 | 97 | 45/60 (75%) |
| Signal only | −$98 | −$223 | $2,595 | 55 | 14/30 (47%) |
| Do nothing | +$125 | 0 | $3,008 | 0 | – |

To see how much of this is luck, we split the tape into five 10-minute blocks and ran each one from scratch. Against do-nothing, the signal on its own averaged −$172 a block (sd $282) and never came out ahead: −48, −73, −734, −3 and −4. The live bot averaged +$433 (sd $764) and was ahead in two blocks, but nearly all of that was the block with the NFLX earnings (+1,937).

We also ran the same rule in the house simulator over 10 price seeds of 3,000 ticks each, at a 30 bps taker fee. The simulator's prices are a random walk, so there's nothing for the rule to find, and it found nothing: −$14 on average (sd $107), one seed in ten positive, worst drawdown $214. One thing we had to fix first was that the simulator's `seed` argument doesn't change its prices, because they come from `FUNDAMENTAL_SEED`. We set that for each seed.

So the prediction held and the claim doesn't. The trend signal didn't cover its costs in Session 5, and the data shows why. The signal's feature is negatively correlated with the very next mid move (−0.19 over 45,391 updates). A small rise on this venue is more often followed by a fall, so a trend rule is pointed the wrong way at the horizon it trades. The live bot's profit in the replay comes from the event trades.

## 4. Look-ahead check

For each time the signal entered, we measured how far the mid moved over the next 20 updates, in the signal's direction and before costs. Then we rebuilt the signal from shifted data.

| Signal built from mids up to | Entries | Mean move | t-stat |
|---|---|---|---|
| t (as live) | 109 | −37.6 bps | −1.30 |
| t − 1 (one update old) | 109 | +12.9 bps | +0.52 |
| t + 1 (one update ahead) | 109 | +7.3 bps | +0.32 |
| t + 20 (planted bug) | 107 | +110.7 bps | +2.74 |

The planted bug is there to show the check works. A signal that has seen 20 updates of the future looks like a large, significant edge, and the live signal looks nothing like that. Lagging the signal by one update moves it toward zero, and the next-move correlation drops from −0.19 to −0.09, which is what you'd expect from a real relationship as its data gets older. Peeking one update ahead doesn't tell us much here, because one extra mid is only a fifth of the short average and the next move is mostly bid-ask bounce. None of the first three rows is significant (|t| < 1.3). At the trade level, the lagged and peeked signals made +$204 and −$521 against do-nothing, a smaller difference than one block's standard deviation.

## 5. Live vs backtest

| On the same tape | Fills | Trading P&L | Fees | Rebates | NFLX P&L |
|---|---|---|---|---|---|
| Live (trader_1's fills on the tape) | 84 | −$292 | $205 | $84 | −$169 |
| Replay of the live bot | 97 | +$2,349 | $321 | $45 | +$2,261 |
| Replay, signal only | 55 | −$274 | $75 | $23 | – |

Trading P&L here is cash from fills, fees and rebates, plus the bot's net position marked at the mid. Trader_1's whole live result (−$292) is about what the signal made on its own in the replay (−$274), so the roughly $2,600 the replay credits to the event trades never showed up live. The gap is $2,640, and $2,430 of it is NFLX. We see four reasons.

1. Queue position and latency. When the NFLX news came out, the replay bought 30 shares at $839 within 0.10 s, from asks that were still in the recorded book. Live, faster bots took those asks first, and trader_1's bid filled at its cap of $867.19, 3.3% worse. A replay can't see other bots, so it hands us liquidity that was already gone. This was the biggest effect.
2. Shares stuck in a halt. Live filled 53 shares at the cap and sold 32 at $906.75 within a second, which left 21. NFLX was halted 7.2 s after the news for five minutes, and halted again later, and it closed at $786. The replay bought exactly 30, sold them at $910.97 and was flat before the halt.
3. Fees weren't the problem. Live paid less in fees than the replay and earned more in rebates, because more of its fills were maker fills.
4. Our own trades are on the tape. The replay trades against a book that our live orders had already moved. This is small next to the first two.

On the plot, the live curve follows the replay through the NFLX entry, about $500 lower because of the worse fill. The two split after the halts and the econ print, when the leftover NFLX shares get marked down and the replay stays flat.

## 6. What we take from it

The trend signal has no edge here at 5 to 20 updates. It lost money in every block, so we'll either stop running it at this size or slow it down enough that the bounce doesn't dominate. The event trades are where the replay finds money, but the backtest overstates them because it can't model the race for liquidity in the first second. Before we trust it again we'll fill event entries at the plan's price cap instead of the stale ask, and size rides small enough to get out before the venue's 5%-in-60-seconds halt trips.
