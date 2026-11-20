# Midway Traders

Budget invested: $1,000,000, across three seats:

| Seat                      | Capital  | What it runs                | Command         |
| ------------------------- | -------- | --------------------------- | --------------- |
| `midway_traders_trader_1` | $300,000 | momentum + event ride/fade  | `make momentum` |
| `midway_traders_trader_2` | $300,000 | mean reversion + event fade | `make meanrev`  |
| `midway_traders_broker`   | $400,000 | market maker                | `make broker`   |

## Week 8 checklist — Friday November 20

The graded block is **Friday Nov 20, 15:30–16:30 Chicago (CST)**:
*Multi-venue and big data*. The scenario switches in at 15:30:20 with the
market closed, the opening auction runs from 15:40:00, and continuous trading
starts after the 12-tick cross at 15:40:12 (tick 0). The block ends on a
10-tick closing cross at 16:30.

| Requirement | How the seats meet it |
| --- | --- |
| Orders on both venues, routed on the NBBO and the fee difference | Every seat connects to the four team venues and to the house venue (`EXCHANGE_URLS`, below). The broker quotes on the team venues only. Each trader seat runs its strategy on the first venue in the list and holds one more connection per other venue; before a link order goes out, `team/routing.py` compares every venue's touch and fees and sends it to one of them. See "Routing" below. |
| A bid in the LUNA bookbuild | Each trader seat indicates 200 shares at the top of the range ($52) when the book opens at t150, as it did for ORCA and VLTA (`BaseTrader.on_ipo`). Both seats did so in the local run below. |
| Trade the hidden links | `team/leadlag.py`. After a leader's mark moves, each trader seat rests a 2-share maker order in the follower, in the predicted direction, before the echo is due, and cancels it one tick after. |
| Connected ≥ 80% of the block | Unchanged: reconnect with backoff (`team/reconnect.py`). |

**The three links** (from all four parts of the week-8 history, 1,000
sessions, 28.7 million rows; `python scripts/find_links.py <part1> <part2>
<part3> <part4>`, 9 s):

| Leader | Follower | Lag | Echo | Correlation at the lag |
| --- | --- | --- | --- | --- |
| AMD | GOOGL | 6 ticks | +0.36 × the leader's move | +0.62 |
| NFLX | AMZN | 8 ticks | −0.30 × (opposite direction) | −0.33 |
| MSFT | TSLA | 4 ticks | −0.36 × (opposite direction) | −0.17 |

The scan covers every leader, follower and lag from 1 to 10, with the ticks
within 60 of an earnings release, the econ print or a halt removed. The next
candidates are the same pairs read backwards (at most 0.11), which is each
leader's own tick-to-tick pattern showing through the echo. Every other pair
is under 0.01. With the events left in, the trap the starter warns about is
plain: all 84 unlinked pairs score above 0.05, the largest 0.70, and
MSFT → TSLA shows up at lag 1 with the wrong sign (+0.61). After a leader move above 5 bp the follower went the predicted
way 96–99% of the time.

**The echo is not worth crossing the spread for.** It lasts one tick and
walks back on the next. Outside the events the largest leader moves are
14–18 bp (AMD) and 3–6 bp (NFLX, MSFT), so the echo is at most about 7 bp
against a 16 bp spread in every follower. Buying at the ask one tick after the
move and selling at the bid on the echo tick lost 16 bp a round trip in the
history, before fees, at every threshold we tried. During the NFLX earnings
ramp the echo in AMZN is about 18 bp a tick for 15 ticks, still under the
house venue's taker fee, and about level with a team venue's spread plus fees. So the link orders are small maker
orders: they cost $0.05 to cancel if nobody trades with them, and they are on
the right side if somebody does. If the venue charges makers (see below) they
are 1 share.

A leader move counts from about its 99th-percentile one-tick move: AMD 10 bp,
NFLX 4 bp, MSFT 3 bp. That is 20–40 cues a session per link in calm trading,
plus every tick of the NFLX earnings ramp (t700) and of the econ print
(t1000), when all three leaders move 10–25 bp a tick. The check needs 10
orders with 60% in the right direction. While a move is inside its lag, a
smaller move the other way does not flip the follower's direction.

**Event schedule (Chicago):**

| Time     | Tick  | Event                               |
| -------- | ----- | ----------------------------------- |
| 15:42:42 | t150  | LUNA bookbuild opens                |
| 15:45:12 | t300  | GOOGL earnings, 3–8%                |
| 15:47:42 | t450  | LUNA bookbuild closes and is priced |
| 15:49:22 | t550  | LUNA lists                          |
| 15:51:52 | t700  | NFLX earnings, 4–9%                 |
| 15:56:52 | t1000 | econ print, market-wide, 1.5–3%     |

Direction is unknown for all of them. GOOGL is a follower and NFLX is a
leader: the NFLX release sends 15 cues into AMZN.

**Local runs, Nov 19.** All three seats ran for 7.5 minutes against the
course engine's week-8 venue with the same three pairs, lags and directions
planted (`LEADLAG_SEED=15740536`; the betas differ from the hosted ones) and
the events moved up to t100, t230 and t320.

*One venue:* trader_1 saw 243 leader moves and sent 99 link orders (GOOGL
40, AMZN 44, TSLA 15); trader_2 sent 104. Every one went out before its echo
was due, in the predicted direction.

*Three venues, the Friday layout:* two venues at team fees (8/6 bp and
14/11 bp) and one at the house's new fees (400/−100 bp), house last, cues
from the house's marks, broker on the two team venues only.

| Seat | Leader moves (house marks) | Link orders | To the 11 bp venue | To the 6 bp venue | To the house | LUNA indications | Rejects | `RATE_LIMITED` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| trader_1 | 177 | 75 | 49 | 26 | 0 | 3 (one per venue), all allocated | 0 | 0 |
| trader_2 | 177 | 75 | 49 | 26 | 0 | 3, all allocated | 0 | 0 |

The 6 bp venue won an order only when its touch was better by more than
the 5 bp difference in rebate, for example `ROUTE  sell AMZN -> ...` with the
ask 8.6 bp lower. No link order filled in any run, so each cost one $0.05
cancel. No tracebacks. The local house venue had no market maker, so it never
showed a two-sided book and was never a candidate; on Friday it will be, and
loses on its −100 bp rebate. The local venue is thinner and jumpier than the
hosted one: it produces more cues, and on its tape the followers went the
predicted way after only 55–62% of 5 bp moves (96% or more in the history).
The runs show the plumbing, not the edge.

To repeat it, start each venue without `SESSION_AUTOOPEN`, connect the
seats, then type `open` in each venue's terminal. If a session opens before
the seats connect, nobody is granted shares and the book stays empty.

    TAKER_FEE_RATE=0.0008 MAKER_REBATE_RATE=0.0006 LEADLAG_SEED=15740536 make exchange SCENARIO_PATH=season/week08.json EXCHANGE_PORT=8797
    TAKER_FEE_RATE=0.04 MAKER_REBATE_RATE=-0.01 LEADLAG_SEED=15740536 make exchange SCENARIO_PATH=season/week08.json EXCHANGE_PORT=8795
    make momentum EXCHANGE_URLS=ws://127.0.0.1:8797,ws://127.0.0.1:8795 LINK_FEED_URL=ws://127.0.0.1:8795

**House venue fees (Ed, Nov 19).** From this block the house venue charges
400 bp to take and **charges makers 100 bp**, and a cancel there costs $0.50.
The team venues charge 8–14 bp to take, pay makers 5–11 bp and bill $0.05 a
cancel. The course's advice is to route everything to the team venues, not to
quote on the house, and to treat it as the venue of last resort. So the house
goes **last** in `EXCHANGE_URLS`: no strategy runs there, the broker does not
connect to it, and a trader seat sends it an order only when no team venue
can take one.

**Routing.** A resting order goes to the venue with the highest
`rebate − gap`, where the gap is how far that venue's touch sits behind the
best touch across the venues. So the order joins the national best unless
another venue's rebate beats it by more than the price gap. The house's
rebate is −100 bp, so it loses to any team venue with a book. A taker order
would go to the lowest ask plus taker fee (or the highest bid less the fee);
the link orders are makers, so only the maker rule is used live. A venue whose
mid is more than 2% from the first venue's is left out: in open play the
venues have sat 5–50% apart, and that is not one market. A sell is routed only
to a venue where the seat holds the shares, and nothing is routed to a venue
that is offline, halted in that name, or showing a one-sided book. Each routed
order logs a `ROUTE` line with every venue's touch and rebate.

Only the first venue runs the seat's strategy (crossover or mean reversion,
event trades, starter). The other connections take routed link orders, bid
200 shares in a LUNA bookbuild if their venue holds one, and work that
allocation's exit. They never short. The lead-lag cues come from one venue's
marks, named by `LINK_FEED_URL`: use the house venue, where the links were
measured and where there is always a book.

**Before the block:** the steps under Week 7 below are unchanged except for
step 3. Check `/api/venues` for the day's fees and `/api/nbbo` for which team
venues are priced near each other. Put the team venue you want the strategy
on first and the house last:

    H=wss://feed.algoarena-st.duckdns.org
    T=wss://feed-8773.algoarena-st.duckdns.org,wss://feed-8767.algoarena-st.duckdns.org,wss://feed-8770.algoarena-st.duckdns.org,wss://feed-8769.algoarena-st.duckdns.org
    T1=wss://feed-8773.algoarena-st.duckdns.org,wss://feed-8767.algoarena-st.duckdns.org,wss://feed-8770.algoarena-st.duckdns.org,wss://feed-8769.algoarena-st.duckdns.org
    T2=wss://feed-8767.algoarena-st.duckdns.org,wss://feed-8773.algoarena-st.duckdns.org,wss://feed-8770.algoarena-st.duckdns.org,wss://feed-8769.algoarena-st.duckdns.org
    make momentum EXCHANGE_URLS=$T1,$H LINK_FEED_URL=$H
    make meanrev  EXCHANGE_URLS=$T2,$H LINK_FEED_URL=$H
    make broker   EXCHANGE_URLS=$T

The two traders run their strategies on different venues, so one thin book
does not stop both: trader_1 on 3-sigma and trader_2 on STT_11, the two
cheapest to take on (6 and 7 bp on Nov 19). If the fees have moved, put the
two lowest taker fees first.

(8773 is 3-sigma, 8767 STT_11, 8770 Group 8, 8769 2026Autumn 3.) Each trader
prints `Connected to ...` once per venue, then `ROUTE  5 venues: strategy on
feed-8773, ...; link cues from feed`. Plain `make momentum` still runs a seat
on the house venue alone, which now costs 1% a maker fill: do not use it.

`make season WEEK=8` (run Nov 19, 3 s) plays the week with the house bots, not
our seats. It confirms the engine and `season/week08.json` are current: the
links are active and LUNA lists at t550.

**The earnings echo trade.** NFLX is a leader and has the t700 release. Its
ramp moves it the same way for 15 ticks, so AMZN's echoes pile up: by tick 22
AMZN has drifted about 0.105 × the release the other way (0.68% on the average
6.5% move), and it gives that back within eight ticks. Each trader seat takes
AMZN against NFLX's direction as soon as the release arrives (the first echo
lands at tick 8) and takes the other way at tick 22. In the 1,000 history
sessions that made +0.39% a share net of a 16 bp spread and 16 bp of fees, and
won 89% of the time; leaving at tick 30 instead lost 0.16%. It trades only
when the expected drift clears the venue's spread plus two taker fees by
1.25×, so never on the house and not in a wide book. Size is one event order,
at most 60 shares. On a team venue's $75,000 the event limit (12% of net
worth) allows about 35 AMZN shares, so expect about $35 a seat. GOOGL's
release (t300) has no echo trade: GOOGL is a follower.

Local run, Nov 19 (one venue at 8/6 bp, NFLX release of −6.0%): trader_1
bought AMZN at 197.35 the moment the release arrived, got the 10 shares at the
touch, cancelled the other 50 at tick 7, and sold the 10 at 199.02 at tick 22:
+0.85% before 16 bp of fees. trader_2, on the same venue, found nothing left
to take and cancelled. Both seats on one book compete for the same shares,
which is one more reason to start them on different venues.

A rehearsal note: our broker pulls every quote about 100 ticks ahead of a
market-wide print. If a local scenario puts the econ print within 100 ticks
after the NFLX release and our broker is the only market maker, the book is
empty at the release and nothing trades. Friday's two events are 300 ticks
apart.

**Hosted open play, Nov 19, 21:18–21:28.** All three seats ran the Friday
commands for ten minutes. Nobody else was trading, so this checks the
connections, not prices or P&L.

- Each trader held five connections for ten minutes with no reconnects, no
  rejects and no rate limiting. trader_1 sent 43 link orders (31 to 3-sigma,
  6 to Group 8, 5 to 2026Autumn 3, 1 to STT_11); trader_2 sent 43 (20, 12, 9
  and 2 across the four). None went to the house.
- The venues were 2–4% apart, so most `ROUTE` lines left one to three venues
  out of the comparison.
- A team venue funds each trader seat with $75,000 (the house: about
  $420,000), so event trades there are about a quarter of their old size.
- **The broker sent nothing on two of the four venues.** A venue sends the
  portfolio with a grant or a fill, and on a venue where the broker already
  held a book it got neither, so its risk check refused every quote. It now
  takes cash and positions from its leaderboard row until a portfolio
  arrives (`RISK   portfolio taken from the leaderboard`). Re-run at 21:29:
  it quoted on all four.

**Open items**

- *Routing covers the link orders only.* The crossover, mean-reversion and
  event orders are not routed: they stay on the house venue, where their
  signals are computed. The second venue has never been traded by a trader
  seat on the hosted arena, only locally.
- *Event liquidity on the team venues is unknown.* The event rides and the
  echo trade take liquidity, and our own broker pulls its quotes when an event
  fires. If the other brokers do too, there is nothing to take.
- *The LUNA bookbuild.* If it runs only on the house venue, each seat's
  house connection bids there and, if allocated, later exits at the house's
  fees (1% for the resting exit at +12%, 4% for the stop at −8%).

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
