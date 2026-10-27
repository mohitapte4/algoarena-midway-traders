# Midway Traders

Budget invested: $1,000,000, across three seats:

| Seat                      | Capital  | What it runs                | Command                                           |
| ------------------------- | -------- | --------------------------- | ------------------------------------------------- |
| `midway_traders_trader_1` | $300,000 | momentum + event ride/fade  | `make momentum`                                   |
| `midway_traders_trader_2` | $300,000 | mean reversion + event fade | `make meanrev`                                    |
| `midway_traders_broker`   | $400,000 | market maker                | `make broker` |

## Week 5 checklist — Thursday October 29

The graded risk block is **Thursday Oct 29, 17:00–18:00 Chicago (CDT)**.
Pre-open starts at 17:00; the market opens at 17:10, with the 12-tick
opening cross ending at 17:10:12. Friday Oct 30 is the online midterm,
with no lecture or Friday live block.

The Week 5 requirements are: no forced liquidation, positions inside the
published scenario limit, and activity resuming after a trading halt.
Both traders and the broker now track symbol and market-wide halts and
wait for explicit resume events. A symbol resume does not lift a market
halt. The send gates recheck halts, including after broker pacing waits.
Existing accepted orders are retained in our bookkeeping through a halt;
the exchange keeps them resting.

Each seat uses the smaller of its existing cap and one third of the
published team limit. With the bundled 800-share Week 5 limit, the existing
200/200/150 long caps remain unchanged. Outstanding orders count too.
Do not run duplicate processes for a seat: these budgets assume one process
per seat and exactly these three seats.

The broker waits for an authoritative portfolio after connecting and uses
conservative additional entry limits: no borrowing, 10% cash reserve,
20% gross exposure including outstanding orders, and no new exposure below
80% of its first observed equity. Risk-reducing trades remain allowed when
funded. Cancels retain reservations through the reconciliation grace period;
unknown unacknowledged orders keep their reservation until reconciled or a
reconnect. These limits reduce risk; they do not guarantee against all
market gaps, pre-existing positions or venue changes.

**Event schedule (Chicago):** NFLX earnings at 17:15:12, AMD earnings at
17:22:42, and a market-wide economic print at 17:27:42. Direction is unknown.

Run `make test`, including `tests/test_week5.py`, before launching. That file
exercises the actual team classes through halts/resumes, pending order cash
and position limits, and an in-process Week 5 exchange replay with an adverse
move and maintenance enabled. `make season WEEK=5` is useful as a scenario
smoke test, but its default population is preset bots, not our three seats.
Neither offline test proves the hosted login or predicts the live score.

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
       make broker

   Start the seats before the teacher opens the session. They connect, then
   wait for `SESSION_OPEN`.

4. Check that each terminal prints `Connected to wss://... as <seat id>`.

**During the block**

- Leave the seats running. A dropped connection reconnects by itself. The
  log shows `reconnecting in 2.1s (attempt 2)`, with the wait doubling up to
  about 30 s.
- Restart a seat only if its process has exited. To restart, run the same
  command again.
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

To count rejects for a seat after the block:

    grep -c "Exchange error" logs/midway_traders_trader_1_*.log

## If something goes wrong

| Symptom                                        | What to do                                                                                                                                          |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AUTH_FAILED`, or `handshake rejected`         | The token in `.env` is wrong. Get the current `.env` from Amy.                                                                                      |
| `cannot connect ...` repeats                   | The venue is down or not open yet. Leave the seat running; it keeps retrying.                                                                       |
| A Python traceback, and the process exits      | Run the same command again, and note the time for the writeup.                                                                                      |
| A seat has stopped itself at its reject budget | Leave it stopped and tell Amy (see the checklist).                                                                                                  |
| `RATE_LIMITED` errors                          | The team sent more than 20 orders and cancels in one tick. The seats are paced to stay under this (4 + 4 + 8 a second), so report it if you see it. |

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

**The broker** quotes three levels a side, and widens its quotes when the
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
- Orders and cancels are paced: 4 a second for each trader seat and 8 for the
  broker. The team shares 20 a tick.
- Nothing is sent after `SESSION_CLOSED`.

A reject pauses that symbol for 10 s. At 8 rejects, the seat stops sending
orders for the rest of the session.

## Where the code lives

| File                       | What it owns                                                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `momentum_trader.py`       | `MomentumTrader._signal()`: trader_1's strategy.                                                                                      |
| `mean_reversion_trader.py` | `MeanReversionTrader._signal()`: trader_2's strategy.                                                                                 |
| `base_trader.py`           | What both trader seats share: the event layer, the reaction bid, the stop-loss, the starter order, maker quoting, and the order gate. |
| `events.py`                | Event plans: the edge-versus-fee check and the price levels.                                                                          |
| `venue.py`                 | Own-order tracking (`OwnOrders`), and `TeamBot`, which routes acks, rejects and cancels to the trader.                                |
| `reconnect.py`             | `run_with_backoff`, the reconnect policy both bots use.                                                                               |
| `broker.py`                | `MyBroker` (quote width and skew) and `TeamBrokerBot` (long-only asks, inventory cap, pacing, event pauses, reject budget).           |
| `risk.py`                  | `RiskLimits` (strategy limits) and `EventLimits` (event sizing).                                                                      |
| `trader.py`                | The Level 1 single-order bot. It is not used on Fridays.                                                                              |

Do not edit `arena/` or `engine/`. Change behaviour by overriding in `team/`.

## Testing a change before Friday

Run the unit tests:

    make test

To run against a local copy of the week-3 venue, use four terminals:

    make exchange SCENARIO_PATH=season/week03.json EXCHANGE_PORT=8799
    make momentum EXCHANGE_URL=ws://localhost:8799
    make meanrev EXCHANGE_URL=ws://localhost:8799
    make broker EXCHANGE_URL=ws://localhost:8799

In the exchange terminal, type `open` to start the session. Type
`shock earnings_miss` to inject a shock, and `close` to end the session. The
NVDA earnings event fires 300 s after `open`. To test reconnecting, stop the
exchange with Ctrl-C and start it again.

> **Put `EXCHANGE_URL=...` after `make`, never before it.** `make` loads
> `.env`, which points at the hosted venue. A variable set in front of `make`
> (`EXCHANGE_URL=... make momentum`) is overridden by `.env`, so the seat
> connects to the real venue instead of the local one.

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
