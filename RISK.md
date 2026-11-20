# Midway Traders: Phase 3, Risk & Robustness

**Team:** Akshay Shivdasani, Amy Wang, Mohit Apte, Hoang Pham.
**Seats:** `trader_1` (momentum), `trader_2` (mean reversion), `broker` (market maker).
We hold no exchange licence, so the exchange-role requirements do not apply.

Risk code: `team/risk.py`, enforced in each seat's order gate
(`BaseTrader._place`, `TeamBrokerBot._send` / `_risk_refusal`). Tests: one per
guard in `tests/test_risk_guards.py` (23 tests; `make test` runs 518, all pass).

## Limits

Every limit is checked before an order is sent, and an order that would breach one
is cut or never sent. Exposure counts what a seat has **traded**, not the shares the
venue grants at the open: on Nov 6 the grant alone was 78% of the broker's equity.

| Guard | Seat | Threshold | Action | Why this number |
| --- | --- | --- | --- | --- |
| Shares per symbol | trader | 200 held + on order (and ≤ ⅓ of the week's team limit) | buy cut | 2 × 200 + broker 150 stays under the venue's 800 team limit, which counts resting orders |
| Inventory per symbol | broker | 150 (50 after an event in the name) | that side not quoted | Session 3: informed flow sold us 110 NVDA in 3 s after an event |
| Order / symbol / gross notional | trader | 2% / 5% / 20% of net worth | strategy buy cut | about 10 names at 2% each before the gross cap |
| **Hard gross** | trader | 35% of net worth, every buy (events included) | buy cut in the gate | events get 12% a name; 35% allows 2–3 at once and stops a market-wide print from riding all 12 |
| Gross notional | broker | 20% of equity | order refused | a market maker should end the day near flat |
| Net notional | both | \|net\| ≤ gross, so the gross cap binds | — | no separate limit needed |
| Stop-loss, per position | trader | bid 1.5% under average entry (ask 1.5% over, for a short); IPO 8% | taker exit | about twice the 0.8% round-trip taker fee, so trading costs alone do not trip it; earnings events move 3–10%, so it does fire when one goes against us |
| Stop-loss, per position | broker | traded inventory 1.5% under water | quote only the side that reduces it, at full skew | same reasoning as the traders' stop |
| Session loss | trader | net worth 1% below the open | exits only | a bad session for our seats was −$3k (Session 4, broker) = 1% of $300k |
| Session loss | broker | equity 20% below the open | reducing orders only | the venue's opening grant is about 78% of the broker's equity, so equity moves with the market: a −3% market-wide print alone is −2.3%. A tight floor would stop quoting on the market's move, not ours |
| **Kill**: hard loss | trader / broker | net worth 2% / equity 25% below the open | kill switch | past the soft limit: the exits-only rule did not hold the loss. The trader figure includes the grant (about 25% of net worth), so a market-wide drop of about 8% would also trip it; at that point stopping is the safe choice |
| **Kill**: errors | both | 8 rejects (the venue's limit is 10) | kill switch | keeps the seat under the graded limit |
| **Kill**: disconnects | both | 6 dropped connections on one link in 300 s (failed reconnect attempts do not count) | kill switch | Nov 6, the hosted feed's worst day, peaked at 4 per link in 5 min, and the seats still traded |
| **Kill**: human | both | file `KILL` (all seats) or `KILL_<seat>` in the repo root | kill switch | needs no terminal, and works on Windows |
| Volatility width | broker | 2 × the 5 s high-low range, capped at 150 bp | wider quotes | Week 2: a quote left at the default width lost $2,964 in a minute |
| Toxicity width | broker | one side filled for ≥ 3 quote sizes in 30 s | ×2 width | one-sided flow is informed flow |
| Inventory skew | broker | both quotes shift up to one width at the inventory cap | long → shade down | sells the inventory without crossing |

**Kill switch** (`team/risk.py::KillSwitch`). Once tripped, the seat sends no new
orders for the session and cancels what it has resting. The broker pulls every quote
on every venue. It does not flatten: positions are left for the venue to mark at the
close. To trip it by hand, `touch KILL_midway_traders_trader_2` (or `touch KILL` for
every seat). To clear it, delete the file and restart the seat.

## What happens on a halt

1. `TRADING_HALT` / `MARKET_HALT` (`on_event` → `TradingState`): the name is
   blocked in both gates, so **nothing is sent and no `SYMBOL_HALTED` reject is
   spent**. A halt we missed while disconnected is learned from the reject's
   "Resume in Ns".
2. **Resting orders in the name are cancelled.** The venue keeps them on the book
   through the halt and fills them in the reopening at pre-halt prices. On Nov 6
   our quotes filled 0 s after NFLX, AMD and VLTA resumed.
3. `TRADING_RESUMED`: the name stays blocked for **10 s** (`RESUME_SETTLE_SEC`). A
   missed resume lifts at the stated duration + 10 s; a halt with no duration is
   never lifted by a guess. Why: the venue's velocity check looks back 60 s and still
   holds the move that halted the name. On Nov 6 trader_1 requoted TSLA every second
   from 7 s after its resume until TSLA halted again at 15:46:19.
4. LULD: limit prices are clamped inside the week's `LULD_BAND_PCT` around the mark,
   less 1 bp.

## Session 6 (Nov 6, the VLTA IPO)

### Replayed: the Session 6 recording through `scripts/replay_session.py`

Our three seats, running this PR's code, connected to `scripts/replay_session.py`
serving the Session 6 recording. They received Nov 6's halts, resumes and events as
they happened. Times below are tape time (Chicago).

| Halt (tape) | Resumed | trader_1 | trader_2 | broker |
| --- | --- | --- | --- | --- |
| TSLA 15:40:40 | 15:45:40 | nothing sent | nothing sent | nothing during; first quote 11 s after |
| INTC 15:45:27 | 15:50:28 | nothing during; event bid 10 s after | nothing during; event bid 10 s after | nothing during; first quote 12 s after |
| TSLA 15:46:19 | 15:51:20 | nothing sent | nothing sent | nothing during; first quote 12 s after |
| VLTA 16:01:21 | 16:06:21 | nothing sent | nothing sent | nothing during; first quote 10 s after |
| NFLX 16:01:29 | 16:06:29 | nothing sent | nothing sent | nothing during; first quote 11 s after |
| NFLX, AMD (halted before the recording starts) | 15:45:23 | nothing sent | nothing sent | quoted twice in each (the tape never announces these halts); first quote 13–14 s after |

Through every halt the tape announces, no seat sends anything into the halted name,
and each re-enters 10–14 s after the resume, never at the first tick. The INTC
earnings reaction shows the policy end to end (trader_1's log, tape times added):

```
00:16:09  EVENT  CALENDAR_EVENT Earnings: INTC moving -5.8%                    15:45:16
00:16:09  EVENT  INTC reaction: ... bid 5 at no more than 16.25 in 15s
00:16:20  EVENT  TRADING_HALT   Trading halted on INTC: velocity 5.1% in 60s   15:45:27   (bid not sent)
00:21:21  EVENT  TRADING_RESUMED Trading resumed on INTC                      15:50:28
00:21:31  ORDER  buy  5 INTC @ 16.24 rest (notional 81.20) — event reaction   15:50:38   (+10 s)
```

The broker on VLTA (broker log, tape times added):

```
00:15:11  EVENT TRADING_HALT: VLTA quotes pulled for 0s                       16:01:21
00:20:12  Trading resumed: Trading resumed on VLTA                            16:06:21
00:20:22  Quoted VLTA bid=31.13 x0 ask=31.63 x0 ...                           16:06:31   (+10 s)
```

**How this evidence was collected.**

- **The tape.** We replayed two windows of
  `sessions/session_week06_20261106_214023_market.jsonl`, 15:40:23–15:52:30 and
  15:57:30–16:07:30, which cover every halt and resume on the tape. Each window was a
  trimmed copy of the recording, served on its own port. In the copies we made two
  changes:
  - We removed the 136 trade prints of our own Nov 6 fills. The replayed seats would
    otherwise book them as fills of orders they never sent.
  - The second window starts with the tape's own `SESSION_OPEN` record, re-stamped
    1 s before the window. Without it the seats would never see the session open.
- **The run.** A 33-line wrapper around `replay_session.py`'s own `load` and
  `ReplayServer` held the stream until all three seats had connected, because the
  tape's `SESSION_OPEN` comes in its first 20 ms. Playback was at ×1, so the 10 s
  wait keeps its tape-time meaning.
- **The seats.** The seats were started with the normal commands (`python -m
  team.momentum_trader`, `team.mean_reversion_trader`, `team.broker`), pointed at the
  replay with `EXCHANGE_URL`. Their logs are
  `logs/midway_traders_{trader_1,trader_2,broker}_20261120_001113.log` (first window)
  and `…_001116.log` (second window).
- **The table.** We read the table from those logs. Each log line's wall-clock time
  was mapped to tape time using the moment each stream started. Trader logs record
  whole seconds, so the delays are accurate to about 1 s.
- **What the replay server cannot do.** It plays the tape and ignores what the seats
  send: no acks, fills, rejects or portfolio. So the traders, never sent a portfolio,
  only send their starter orders and event reactions, not strategy trades. The broker
  decides its quotes, but its own check refuses to send them without a portfolio,
  which is why its quote lines show size `x0`. They still show when and where it
  would quote. For the same reason the replay cannot show orders cancelled at a halt
  (an order that was never acked cannot be cancelled), so that guard's evidence is
  `test_13b` and `test_13c`.

### Live: what the seats did on Nov 6

These are the seats as they ran on Nov 6, before the Phase 3 changes; points 2–3 of
the halt policy fix what this session showed. From the seat logs in
`logs/*20261106*`.

| Halt (seat logs) | Resumed | What our seats did around it |
| --- | --- | --- |
| NFLX, AMD at the open (session band, 25%) | 15:45:23 | Broker's resting asks, left on the book through the halt, filled at the reopen (AMD 212.87, NFLX 496.44) |
| TSLA 15:40:40 (velocity) | 15:45:40 | trader_1 requoted TSLA every second from 15:45:47; TSLA halted again at 15:46:19 |
| INTC 15:45:27 (velocity, 11 s after INTC earnings −5.8%) | 15:50:28 | trader_1 cancelled its fade sell during the halt |
| TSLA 15:46:19 (velocity) | 15:51:20 | — |
| VLTA 16:01:21 (velocity) | 16:06:21 | trader_2's IPO stop (sent 16:00:50 at $31.14) rested through the halt and filled at the reopen, 16:06:22 |
| NFLX 16:01:29 (velocity) | 16:06:29 | Broker's resting asks filled at the reopen, 16:06:29 |

No order was refused for a halt (`SYMBOL_HALTED`): the seats sent nothing into a
halted name. Rejected orders in the block: trader_1 1 (`SSR_RESTRICTED` TSLA,
15:46:16), trader_2 1 (`STP_CANCEL` INTC, 16:00:39), broker 0 (3 cancels lost a race
to a fill), against a limit of 10 a seat, so no reject storm. Both traders indicated
200 VLTA at $34. Connectivity: before the block opened, the hosted feed dropped each
seat 17–19 times (13:42–15:30, at most 4 in 5 min on a link). In the block each
trader dropped once (16:27:37), the broker twice on 3-sigma's venue and once on ours,
and every seat was back within about 2 s (`team/reconnect.py`). This history sets the
6-in-300-s disconnect trip.
