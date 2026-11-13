# Midway Traders

Budget invested: $1,000,000, across three seats:

| Seat                      | Capital  | What it runs                | Command         |
| ------------------------- | -------- | --------------------------- | --------------- |
| `midway_traders_trader_1` | $300,000 | momentum + event ride/fade  | `make momentum` |
| `midway_traders_trader_2` | $300,000 | mean reversion + event fade | `make meanrev`  |
| `midway_traders_broker`   | $400,000 | market maker                | `make broker`   |

## Week 7 checklist — Friday November 13

The graded block is **Friday Nov 13, 15:30–16:30 Chicago (CST)**: *Execution
in a fast market*. The scenario switches in at 15:30:20 with the market
closed, the opening auction runs from 15:40:00, and continuous trading starts
after the 12-tick cross at 15:40:12 (tick 0). The block ends on a 10-tick
closing cross at 16:30. This is Part A of Phase 4, scored from the session
recording.

What is new this week, and what the seats do about it:

| Requirement | How the seats meet it |
| --- | --- |
| Quota rejections under 5% of messages | The venue meters 8 orders + cancels per 1 s tick for the whole team on each venue (burst 24). The seats pace themselves to 7.5 a second: broker 4.5 (per venue), each trader 1.5 (burst 3). The broker quotes one level a side instead of three, so a requote is at most 4 messages, and requotes after an 8 bp move instead of 5. A `RATE_LIMITED` answer pauses the seat for a tick and does not count toward the reject budget, so it can never stop a seat. |
| An order on the flashed symbol, on that venue, within 5 s, for at least a quarter of the flashes | The venue sends `FLASH` one tick *before* the price jumps. On the event each trader sends its entry at once, from the event handler, not on the next tick: a maker sell half way into a jump up, or a maker bid half way into a drop, priced from the event's `start_price` (`team/flash.py`). So the order rests before the jump arrives. The broker does the same on each of its two venues, after pulling its quotes in the name. Each seat logs the reaction time of every flash. |
| Hot path O(1) | The strategies' averages and standard deviation are rolling windows fed by each book update (`team/rolling.py`); the broker's quote width uses a rolling high/low. Nothing rescans the price history per message. The windows take at most one mid per 0.5 s (`Sampler`), the pace their sizes were tested at: on the 0.2 s feed, 10/40 would otherwise span 2 s / 8 s, which lost money on the Week 5 tape. |
| Connected ≥ 80% of the block | Unchanged: reconnect with backoff (`team/reconnect.py`). |
| Know the cancellation-fee bill | Every cancel is billed, $0.05 each (Ed #102). The broker keeps a quote whose price and size have not changed instead of cancelling and re-placing it, and refreshes unmoved quotes after 300 s instead of 30 s (on Nov 6 that refresh was most of its 3,071 requotes). Every seat logs its orders, cancels, quota rejections and the cancel bill once a minute (`MSGS`) and at the close. |
| Decide what to buy in purchase window 2 | See below. |

A flash is an unannounced 1.5–2.5% jump in one symbol on one venue that
walks back within 2 ticks. It would look like a trend to the momentum seat
and would trip the 1.5% stop-loss on a drop, so for 10 s after a flash the
strategy, the starter and the stop-loss leave that name alone. A filled flash
entry is closed by a maker order at the pre-flash mid; an unfilled entry is
cancelled once the flash should be over (revert ticks + 1 s), and an unfilled
exit after 30 s.

**Event schedule (Chicago):**

| Time     | Tick  | Event                               |
| -------- | ----- | ----------------------------------- |
| 15:46:52 | t400  | TSLA earnings, 5–10%                |
| 15:51:02 | t650  | econ print, market-wide, 1.5–3%     |
| 15:55:12 | t900  | AAPL earnings, 3–8%                 |
| 16:00:12 | t1200 | META dividend, $0.50 a share        |
| 15:40–16:30 | — | about 15–20 unannounced flashes, at least 40 ticks from the events |

Direction is unknown for all of them. Borrow tightens: the short locate cap
is 1,200 shares per symbol across the whole market, so other teams' shorts
use it up too. A seat refused a short (`BORROW_UNAVAILABLE` or
`SSR_RESTRICTED`) stops shorting that name for the rest of the session, so
it does not spend its reject budget retrying.

**Fixed on Nov 12: the risk limits counted the starting grant.** The
broker's gross limit (20% of equity) and the traders' concentration and
gross limits counted the shares the venue grants at the open. From Week 5
on, the broker sent no bids at all (0 buys in Weeks 5 and 6), and on Nov 6
trader_2's buys were cut to 0. The limits now count only what each seat
traded. A refused broker order is logged as `RISK ... not sent: <reason>`.

**Purchase window 2.** Our plan is to buy nothing. Every add-on's price is
paid from the bots' cash and counts against this block's score, and none of
them pays back $125k–$250k in weeks 7–10 at our size. Colocation ($250k)
matters for flashes, but a flash trade here is 10 shares into a 1–1.25% move,
about $10–$100 each, and 15–20 flashes a week. The quota increase ($150k) is
not needed: the seats are paced inside 8 a tick. If the team decides
otherwise, buy in the dashboard (MY TEAM → UPGRADE SHOP) between 15:30 and
16:30 only.

Flashes and the fast feed run on the hosted venue only in the graded
block, but the course engine (pulled Nov 12, Ed #104) plays them locally.
`season/week07.json` has the real rules: snapshots every 0.2 s and a flash
about every 150 ticks.

We ran all three seats against a local week-7 venue on Nov 12 for 12
minutes, with flashes every ~50 ticks so more of them fit in. There were
10 flashes (6 drops, 4 rises), the TSLA earnings and the econ print.

| Seat | Flashes with an order within 5 s | Reaction (median) | `RATE_LIMITED` | Cancel bill |
| --- | --- | --- | --- | --- |
| broker | 9 of 10 | 52 ms | 0 | $35.80 |
| trader_1 | 10 of 10 | 0 ms | 0 | $4.80 |
| trader_2 | 10 of 10 | 0 ms | 0 | $0.70 |

No rejects and no errors. Each seat handled about 55 venue messages a
second at a p99 of about 0.2 ms each; `on_tick` p99 was about 0.5 ms, and
the 0.1 s tick loop kept pace. The broker's one miss was a buy its 20%
traded-exposure limit refused, which binds sooner on the local venue's
$158k equity than on the house venue's ~$300k. The local market drifts much
more than the hosted one (AMD moved 5% in 7 minutes), so the broker
requoted on real 8 bp moves more often than it will on Friday.

To repeat it (two terminals, plus one per seat as below):

    GAME_WEEK=7 SESSION_AUTOOPEN=true make exchange EXCHANGE_PORT=8799
    make momentum EXCHANGE_URL=ws://127.0.0.1:8799 EXCHANGE_URLS=ws://127.0.0.1:8799

Offline, `make season WEEK=7` plays the week against the house bots (not
our code), and `make backtest` replays a recorded tape through a trader
seat.

**Before the block**

1. Pull the latest code and run the tests. All of them must pass.

       git pull
       make install        # only if requirements.txt changed
       make test

2. Check that `.env` exists in the repo root. It holds the team token and the
   venue address. Get it from Amy over a private channel. Never commit it,
   and never paste it into chat.
3. Open three terminals in the repo root and start one seat in each:

       make momentum
       make meanrev
       make broker EXCHANGE_URLS=wss://feed.algoarena-st.duckdns.org,wss://feed-8773.algoarena-st.duckdns.org

   On Windows, without `make` (one seat per terminal):

       $env:PYTHONUTF8 = "1"; $env:TICK_INTERVAL = "0.1"
       $env:TEAM_ID = "midway_traders_trader_1"; python -m team.momentum_trader
       $env:TEAM_ID = "midway_traders_trader_2"; python -m team.mean_reversion_trader
       $env:EXCHANGE_URLS = "wss://feed.algoarena-st.duckdns.org,wss://feed-8773.algoarena-st.duckdns.org"; $env:TEAM_ID = "midway_traders_broker"; python -m team.broker

   The broker quotes on the house venue and on 3-sigma's venue, which pays
   makers 6 bp (the house venue now pays nothing). 3-sigma funds a quarter of
   the broker's capital. The traders stay on the house venue, where the VLTA
   bookbuild runs. If 3-sigma's venue misbehaves, restart the broker with
   plain `make broker`.

   Start the seats before the teacher opens the session. They connect, then
   wait for `SESSION_OPEN`.

4. Check that each terminal prints `Connected to wss://... as <seat id>`
   (the broker prints it twice, once for each venue).

**During the block**

- Leave the seats running. A dropped connection reconnects by itself. The
  log shows `reconnecting in 2.1s (attempt 2)`, with the wait doubling up to
  about 30 s.
- Restart a seat only if its process has exited. To restart, run the same
  command again.
- `QUOTA  RATE_LIMITED` lines are not fatal: the seat pauses for a tick and
  carries on. Check the `MSGS` line: the share of rate-limited messages must
  stay under 5%.
- Do not restart a seat that has logged `REJECT budget of 8 reached` (trader)
  or `venue errors this session` (broker). The seat stopped on purpose to stay
  under the 10-reject limit. A restart resets its counter, and further rejects
  would fail the check. Tell Amy instead.

**After the block**

1. Keep the logs. They are under `logs/` (gitignored), and the Phase 1
   writeup needs them as evidence.
2. Put the session recording from Canvas in `sessions/`, also gitignored,
   and replay it through the seats (see [Backtest](#backtest-and-the-official-score)).

## Historical Phase 1 checks (Weeks 1–4)

Part A is scored for each seat from the session recording.

| Check                                         | How the code meets it                                                                                                                                                                                                                                                           |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Connected for at least 80% of the block       | `team/reconnect.py` redials with exponential backoff (1, 2, 4 … 30 s). A bug in an event or fill handler is logged instead of dropping the connection.                                                                                                                          |
| An order or cancel within 60 ticks of a shock | Every `SHOCK` and `CALENDAR_EVENT` makes each trader seat cancel its other orders in the symbol. Unless an event trade goes first, the seat also rests a 5-share bid at the settle level 15 s later. The broker pulls its quotes in the symbol at once and requotes 45 s later. |
| Fewer than 10 rejected orders                 | Every order passes one gate before it is sent (see [Rejects](#rejects)). Each seat stops itself at 8 rejects.                                                                                                                                                                   |
| Typed messages only                           | Every message is built and read through the pydantic models in `engine/shared/messages.py`.                                                                                                                                                                                     |

## What to look for in the logs

Every seat logs to `logs/<seat>_<timestamp>.log` as well as the terminal.

| Log line                                      | Meaning                                                                               |
| --------------------------------------------- | ------------------------------------------------------------------------------------- |
| `Connected to ... as midway_traders_trader_1` | Handshake accepted.                                                                   |
| `Ack: buy 5 NVDA @ 131.00`                    | The venue accepted an order.                                                          |
| `FILL #3 buy 5 NVDA @ 131.00 ...`             | Something actually traded. The position and average entry are updated from this line. |
| `EVENT  NVDA reaction: ...`                   | The seat saw a shock or earnings event and is reacting.                               |
| `ORDER` / `CANCEL`                            | What the seat sent and why. The reason is at the end of the line.                     |
| `reconnecting in Xs (attempt N)`              | The connection dropped. The seat is redialling by itself.                             |
| `Exchange error [CODE]` or `REJECT #N`        | The venue refused something. Each one counts toward the limit of 10.                  |
| `FLASH  AAPL +2.00% ...` / `order N ms after` | A flash arrived and the seat's entry went out, with its reaction time.                |
| `QUOTA  RATE_LIMITED ...`                     | Over the team's 8 messages a tick. The seat pauses one tick; it is not a reject.      |
| `HOTPATH ...` (once a minute)                 | Time per tick and per venue message (mean, p50, p99, max), and the gap between ticks. |
| `MSGS   ...` (once a minute)                  | Orders, cancels, quota rejections (graded under 5%) and the cancellation-fee bill.    |

To count rejects for a seat after the block:

    grep -c "Exchange error" logs/midway_traders_trader_1_*.log

## If something goes wrong

| Symptom                                        | What to do                                                                                                                                          |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AUTH_FAILED`, or `handshake rejected`         | The token in `.env` is wrong. Get the current `.env` from Amy.                                                                                      |
| `cannot connect ...` repeats                   | The venue is down or not open yet. Leave the seat running; it keeps retrying.                                                                       |
| A Python traceback, and the process exits      | Run the same command again, and note the time for the writeup.                                                                                      |
| A seat has stopped itself at its reject budget | Leave it stopped and tell Amy (see the checklist).                                                                                                  |
| `RATE_LIMITED` errors                          | The team went over 8 orders and cancels in one tick (week 7). The seats are paced to 1.5 + 1.5 + 4.5 a second, so a few are possible after a burst; the seat pauses a tick. Report it if the `MSGS` line shows more than 2%. |

## How the seats trade

**Events come first.** A `CALENDAR` warning arrives about 2 minutes before
each scheduled event. On the warning, every seat pulls its quotes in that
symbol. When the event fires (`CALENDAR_EVENT`, or a teacher-injected
`SHOCK`), the message states the move's sign and size. The price then ramps
to 1.3 times the move over about 15 s and settles back to 1 times. The trader
seats trade on the message, not the chart:

| Leg              | Seat     | Entry                                                        | Exit                                                                                                    |
| ---------------- | -------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| RIDE (good news) | trader_1 | Take liquidity, up to pre × (1 + pct/2).                     | Rest a sell at 90% of the peak. Sell at the bid after 3 ramps, or if the price falls pct/2 below entry. |
| FADE (bad news)  | both     | Take liquidity: sell held shares, down to pre × (1 + pct/2). | Rest a buy-back no higher than the settle level. Buy at the ask after 3 ramps.                          |

A leg trades only if its expected edge beats the round-trip taker fee. Its
size is capped by `EventLimits` in `risk.py`.

If no leg trades, for example because the seat is flat on bad news, the
**reaction bid** still goes out. This is a resting 5-share bid at the settle
level, placed 15 s after the event. It exists for the 60-tick check.

**The seats are makers by default.** Strategy entries and exits rest at the
touch and earn the maker rebate. Only the races take liquidity: event
entries, fade sales, the stop-loss, the starter order, and the buy-back at
the deadline.

**The broker** quotes one level a side (three before week 7), and widens its quotes when the
price is moving (`volatility_spread`). While shorts are locked, it never
offers more shares than it holds. It holds and bids for at most 150 shares
per symbol. It pulls its quotes in a symbol for the event warning and for
45 s after the event.

### Rejects

Every trader order passes one gate (`_place` in `base_trader.py`):

- A sell never exceeds the shares held minus the shares already offered,
  because the venue is long-only.
- A buy is capped at 200 shares per symbol, counting the shares on order, and
  by cash including the fee. The venue's limit is 600 per symbol for the
  whole team, and it counts resting orders. Two trader seats at 200 each
  plus the broker at 150 stay under it.
- There is at most one order per symbol and side, and an order never crosses
  the seat's own resting order. A cross would trigger self-trade prevention.
- Prices stay inside the venue's price band.
- Orders and cancels are paced: 1.5 a second for each trader seat (burst 3)
  and 4.5 for the broker on each venue. From week 7 the team shares 8 a tick.
- Nothing is sent after `SESSION_CLOSED`.

A reject pauses that symbol for 10 s. At 8 rejects, the seat stops sending
orders for the rest of the session.

## Where the code lives

| File                       | What it owns                                                                                                                                                                                              |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `momentum_trader.py`       | `MomentumTrader._signal()`: trader_1's strategy.                                                                                                                                                          |
| `mean_reversion_trader.py` | `MeanReversionTrader._signal()`: trader_2's strategy.                                                                                                                                                     |
| `base_trader.py`           | What both trader seats share: the event layer, the reaction bid, the stop-loss, the starter order, maker quoting, the ORCA bid (`on_ipo`) and the order gate.                                             |
| `events.py`                | Event plans: the edge-versus-fee check and the price levels.                                                                                                                                              |
| `flash.py`                 | Week-7 flashes: reading the `FLASH` event, the entry and exit prices, the plan and the reaction log.                                                                                                     |
| `rolling.py`               | O(1) rolling mean / standard deviation and high / low, fed by every book update.                                                                                                                         |
| `hotpath.py`               | The once-a-minute `HOTPATH` and `MSGS` log lines: time per tick and per message, messages sent, quota rejections, cancel bill.                                                                          |
| `venue.py`                 | Own-order tracking (`OwnOrders`), and `TeamBot`, which routes acks, rejects and cancels to the trader.                                                                                                    |
| `reconnect.py`             | `run_with_backoff`, the reconnect policy both bots use.                                                                                                                                                   |
| `broker.py`                | `MyBroker` (quote width) and `TeamBrokerBot`: quotes that never cross the book, inventory skew from the opening position, inventory caps (50 a name after an event), pacing, event pauses, reject budget. |
| `risk.py`                  | `RiskLimits` (strategy limits) and `EventLimits` (event sizing).                                                                                                                                          |
| `trader.py`                | The Level 1 single-order bot. It is not used on Fridays.                                                                                                                                                  |

Do not edit `arena/` or `engine/`. Change behaviour by overriding in `team/`.

## Testing a change before Friday

There are four ways to test. Only the local venue (2) runs our own seats
under the week's rules, so use it before every Friday.

### 1. Unit tests (seconds)

    make test

The week-4 rules (IPO, shorts, the opening auction, the broker's quoting)
are covered in `tests/test_week4.py`.

### 2. Local venue: the dress rehearsal (about 22 minutes)

Run the week's scenario on your own machine, in four terminals:

    make exchange SCENARIO_PATH=season/week06.json EXCHANGE_PORT=8799
    make momentum EXCHANGE_URL=ws://127.0.0.1:8799 EXCHANGE_URLS=ws://127.0.0.1:8799
    make meanrev  EXCHANGE_URL=ws://127.0.0.1:8799 EXCHANGE_URLS=ws://127.0.0.1:8799
    make broker   EXCHANGE_URL=ws://127.0.0.1:8799 EXCHANGE_URLS=ws://127.0.0.1:8799

> **Put `EXCHANGE_URL=...` after `make`, never before it.** `make` loads
> `.env`, which points at the hosted venue. A variable set in front of `make`
> (`EXCHANGE_URL=... make momentum`) is overridden by `.env`, so the seat
> connects to the real venue instead of the local one.

In the exchange terminal, type `open`. The 12-tick opening auction runs,
then the scenario's events fire on their own, one tick a second. Week 6:

| Tick | Event                         |
| ---- | ----------------------------- |
| t150 | VLTA bookbuild opens          |
| t250 | INTC earnings                 |
| t420 | VLTA bookbuild closes, priced |
| t520 | VLTA lists                    |
| t600 | MSFT earnings                 |
| t800 | NVDA dividend                 |
| t950 | econ print, market-wide       |

Type `close` after the econ print has settled, about t1050; the closing
auction then runs for 10 ticks. Use `127.0.0.1`, not `localhost`: on
Windows `localhost` tries IPv6 first, the local venue only listens on IPv4,
and the seats time out connecting. Other console
commands: `shock earnings_miss` injects a shock.

To test a reconnect, stop a seat with Ctrl-C during the bookbuild and start
it again: the venue replays the open book and the seat re-submits its
indication. To test a late join, start one seat after the cross.

Check afterwards:

- `grep "REJECT #" logs/*.log` finds nothing new.
- Each trader log shows `IPO_SUB_RESULT` for VLTA and `ORDER` lines after
  `AUCTION_RESULT`.
- The exchange terminal prints `EOD RECON PASS`.

The only liquidity is our own broker, which steps aside around events, so
rides and buy-backs often find nothing to trade against. That is expected:
this tests readiness and order handling, not profit.

### 3. Season simulator (house bots only)

    make season WEEK=4
    python scripts/season_sim.py --compare-upgrade fee_tier --upgrade-target mm_aggressive --seed 1

`season_sim` runs the house bots, not our code. Use it to see what a week's
market and IPO look like, and to price a shop upgrade (`--compare-upgrade`,
run 2–3 seeds). Do not add `--week 4` to `--compare-upgrade`: argparse
reads it as `--weeks 4` and silently skips the comparison.

### 4. Replay a recorded block

The class posts each Friday's tape on Canvas. Unzip it into `sessions/`
(ignored by git). Two tools read it:

    python scripts/seat_attribution.py sessions/session_week03_20261016_204000_market.jsonl
    make backtest FILE=sessions/<file>.jsonl.gz STRATEGY=momentum

`seat_attribution.py` shows each of our seats' fills, maker share, fees,
rebates and **P&L against doing nothing**, which is the comparison with the
idle teams on the dashboard. Add `--team <prefix>` to read another team's
seats, for example the winner's. On Session 3 it showed the broker losing
$376 against idle: $304 of taker fees against $127 of rebates, and 145 NVDA
bought from the winner's timed exit and held to the close.

`event_backtest.py` (section below) replays the tape through our trader
seats. It enforces week-3 rules (no shorts, 400-share cap) and does not run
the IPO, so use it for event behaviour, not week-4 readiness.

## Backtest and the official score

`scripts/event_backtest.py` replays a recorded block through a seat. It
replays the events, fills, rejects and cancels the way the venue would:

    make backtest FILE=sessions/<file>.jsonl.gz STRATEGY=momentum
    python scripts/event_backtest.py sessions/<file>.jsonl.gz --compare

The `score` column is the venue's season score:

- **Snapshots:** net worth is marked at the reference price every 60 ticks,
  and idle cash earns the venue's interest rate.
- **Formula:** the mean return above that rate, divided by the volatility of
  the returns, shrunk by the maximum drawdown.

A seat that does nothing scores about 0.

Results on the week-2 tape, at a 0.30% taker fee:

| Seat                     | Score  |
| ------------------------ | ------ |
| momentum                 | +0.161 |
| meanrev                  | +0.067 |
| a seat that does nothing | −0.004 |

Almost all of our score comes from the event trades. Pushing the maker share
higher made the score worse on that tape:

- **Resting the deadline buy-back** lowered it.
- **Selling the granted shares at the open** lowered it.

So do not chase maker share for its own sake. Re-check each idea against the
`score` column. The tape is one 50-minute block with two events, so treat any
difference under about 0.02 as noise.

## Still to do for Phase 1

- [ ] **Bookkeeping.** Track realized P&L from fills alongside position and
      average entry. Reconcile against the dashboard at the close.
- [x] **The ORCA IPO.** Week 4 IPO handling was merged before this change.
      Keep the session evidence and explain the bid/pass decision in the writeup.
- [ ] **`PHASE1.md`.** Cover how we track state, how we reconnect, and the IPO
      decision. Attach a session log that shows the handshake, acked orders,
      fills, and one survived disconnect.
