# Midway Traders: Phase 1

We run three bots, each in its own terminal: `midway_traders_trader_1`
(momentum), `midway_traders_trader_2` (mean reversion) and
`midway_traders_broker` (our market maker). All our code is in `team/`. 
Everything below comes from the Session 4 block on Oct 23 (open at
15:40:25, close at 16:30:00) and our logs from that run,
`logs/midway_traders_{trader_1,trader_2,broker}_20261023_1530*.log`.

## How we track state

We parse every message from the exchange into its typed model from
`shared.messages`, so we never pick fields out of raw JSON by hand. Each bot
keeps track of three things:

- **Its orders.** `OwnOrders` (`team/venue.py`) remembers every order we
  send. When the `OrderAck` comes back we attach the exchange's order id, a
  fill lowers the open quantity, an error removes the order, and if we hear
  nothing for 5 seconds we assume it's gone. The broker does the same thing
  for each of its quotes.
- **Its positions, for trading decisions.** We update our position the
  moment a fill arrives, so the next order already knows about it. A
  second later we switch to the exchange's numbers.
- **Its own books, to check our work** (`team/books.py`). Each bot keeps
  its own cash, positions, average cost, realized P&L, fees and rebates,
  built only from its own fills and the events that move money without a
  fill (starting shares, the IPO allocation, dividends and so on). These
  books never copy the exchange. At the close, each bot compares them with
  its row on the exchange's closing leaderboard and logs `RECON PASS` or
  `RECON BREAK`.

At the Session 4 close all three bots logged `RECON PASS`. Every position
matched, and fees, rebates and realized P&L matched to the cent. Cash was
off only by the interest the exchange pays on idle cash without reporting
it ($689, $1,469 and $1,251), which our books estimate.

| Bot      | Fills | Fees (ours / exchange) | Session P&L (exchange) |
| -------- | ----- | ---------------------- | ---------------------- |
| trader_1 | 118   | 240.52 / 240.52        | +$2,409                |
| trader_2 | 62    | 177.70 / 177.70        | +$1,781                |
| broker   | 352   | 339.40 / 339.40        | −$2,995                |

Every order passes one check before it goes out (never sell more than we
hold, never buy past our cash or share cap, stay inside the price band,
pace our messages). During the block both traders had zero rejected orders
and the broker had one. That one was our own fault: a trader's buy crossed
our broker's ask, and the exchange's self-trade check cancelled the quote.
The broker's 13 `ORDER_NOT_FOUND` errors are failed cancels on quotes that
had just filled, not rejected orders. Keeping our traders from trading into
our own broker is the first thing we're fixing next.

## How we reconnect

`team/reconnect.py` opens a new connection whenever the old one ends, for
any reason. It waits 1 s, then doubles the wait each retry up to 30 s, with
a bit of randomness so our three bots don't all retry at once. An error in
one of our handlers gets logged instead of killing the connection. The
exchange cancels our orders when we disconnect, so each bot starts fresh
and requotes.

This got tested for real. At 15:35:56 all three bots lost their connection
when the exchange's keepalive timed out, and each was back within 1-2
seconds (trader_1 log, 15:35:56-15:35:57). After reconnecting, each bot
checks its books against the exchange. Every position and the cash matched
exactly, but the check still printed `BREAK`: it expected interest, and the
exchange doesn't pay any before the session opens. We're fixing that rule.
In our local practice run, where we cut the connection for 10 seconds on
purpose, all three bots logged `BOOKS reconnect check PASS`.

## IPO decision: ORCA

**We bid.** Each trader asked for 200 of the 4,000 shares at $28, the top
of the $24-28 range. The broker stayed out.

- **The odds favor buying.** According to the exchange, ORCA's real value
  is the offer price times exp(N(12%, 18%)). That works out to about a 15%
  pop on average, with roughly a 25% chance it trades below the offer.
- **Bidding the top of the range doesn't cost us anything.** The deal
  prices at the highest level that fills the book, and the pop is measured
  from whatever that price ends up being, so a higher limit doesn't lower
  our expected return.
- **It fits our limits.** 400 shares keeps us at half of the team's
  800-share limit. We also set each allocation to sell at the expected pop,
  with a stop 8% below the offer in case it went the wrong way.

**What happened.** The deal was oversubscribed (5,917 shares requested for
4,000) and priced at $28.00, so each trader got 143 shares. ORCA opened
pretty much flat, so our take-profit orders never filled. At the close it
was around $28.14, about a $40 gain across both bots. Not the pop we hoped
for, but it was a small, controlled bet.

## Purchase window 1: we didn't buy anything

None of the upgrades pay for themselves this phase. The best one,
`fee_tier` ($40k), would earn our broker about $1.1k a session, or about
$7.7k over the 7 sessions left. Adding a seat just moves money we already
have into another bot, and that bot would share our message quota and share
limit. We'd rather wait for the Week 7 window, when we'll know which of our
strategies is actually working.
