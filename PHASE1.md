# Midway Traders: Phase 1

Three seats, each in its own process: `midway_traders_trader_1` (momentum),
`midway_traders_trader_2` (mean reversion) and `midway_traders_broker`
(market maker). The source is in `team/`. The engine under `engine/` is not
modified; the seats subclass its bots.

## How we track state

Each seat keeps three kinds of state, all built from the venue's typed
messages (`shared.messages`, parsed with `parse_message`). Nothing reads raw
JSON fields.

- **Orders.** `OwnOrders` (`team/venue.py`) records every order we send.
  An `OrderAck` attaches the venue's order id, a fill reduces the open
  quantity, an error drops the order, and an order that is neither acked nor
  filled within 5 s is forgotten. Cancels go only to orders still resting.
  The broker keeps the same record per quote (`_live` in `team/broker.py`).
- **Positions for trading decisions.** A fill updates the seat's ledger at
  once, so the next order sees it. One second after the last fill, the
  ledger takes the venue's `PortfolioUpdate` positions, which also picks up
  IPO allocations that arrive without a fill.
- **Independent books for reconciliation** (`team/books.py`). Cash,
  positions, average cost, realized P&L, fees and rebates, built only from
  our own `TradeExecution` messages (taker fee or maker rebate per side),
  IPO pricing and dividends. These books never copy the venue. After the
  close, each seat compares them with its entry in the venue's closing
  leaderboard and logs `RECON PASS` or `RECON BREAK` with every row:
  positions, fees, rebates, realized P&L, cash and P&L since the open.
  Cash differs by the venue's per-tick interest on cash, which no message
  reports; the recon line shows that amount next to its estimate.

Order sending is gated so the venue has no reason to reject: sells never
exceed shares held (plus the short allowance once shorts are on), buys stay
inside cash and the seat's share cap, one order per symbol and side, prices
inside the LULD band, and a token bucket keeps the team under its
20-messages-per-tick quota. Each seat stops sending after 8 venue errors, so
it stays under the limit of 10.

## How we reconnect

`team/reconnect.py` runs one connection at a time and starts a new one
whenever it ends: after a clean close, a dropped socket, a refused handshake
or a failed dial. The wait doubles from 1 s up to 30 s, with 20% jitter so
the three seats do not redial together, and returns to 1 s once a
connection has stayed up for 30 s. An exception inside a fill or event
handler is logged and does not drop the connection.

The venue cancels a seat's resting orders when it disconnects, so each new
connection starts with no orders tracked and requotes from scratch. On
reconnect, the first `PortfolioUpdate` is checked against the independent
books. A match logs `BOOKS reconnect check PASS`. A mismatch (for example
an IPO allocation that arrived while we were offline) is logged row by row,
and the books restart from the venue's state. The close recon reports how
many restarts happened.

In the week-3 rehearsal on Oct 15, the exchange was killed and restarted
mid-session. Both trader_1 and the broker logged the drop, retried at
about 1, 2, 4 and 8 s, and reconnected about 15 s later
(`logs/midway_traders_trader_1_20261015_204829.log`, lines 141-145).
Trader_1 had no rejected orders that session. The broker had one, a cancel
for a quote that had already filled; the broker now cancels only quotes it
still sees resting.

## IPO decision: ORCA

**Decision: bid.** Each trader seat indicates 200 of the 4,000 shares at
the top of the $24-28 range. The broker does not bid; it stays out of the
ORCA book for its first 60 s of trading while the price is discovered.

Reasoning:

- **Expected value is positive.** The venue draws ORCA's true value as
  offer × exp(N(12%, 18%)). That is an average pop of about +15% and a
  25% chance of breaking issue. On 400 shares at $24, the expected gain is
  about $1,400.
- **Bidding the top of the range costs nothing extra.** The deal prices at
  the highest level that fills the book. If demand is thin it prices at
  the bottom of the range regardless of our limit. The pop is a percentage
  of whatever the offer turns out to be, so a higher offer does not reduce
  the expected return.
- **The size fits our limits.** 400 shares leaves the team at 400 of its
  800-share limit, with room for the other strategies. A seat passes if
  its cash does not cover 110% of the indication.
- **The downside is capped.** The allocation rests an ask at 12% above the
  offer (the median pop) and is sold at the bid if the listing falls 8%
  below the offer. That stop is wider than usual because a new listing
  moves about 3% a tick.

Result in the live session: _to fill in after the block (allocation,
offer price, exit price, P&L on ORCA)._

## Purchase window 1: no purchase

- **No upgrade pays back this phase.** The best candidate, `fee_tier`
  ($40k), earns the broker about $1.1k a session in the season simulator.
  Over the 7 remaining sessions that is about $7.7k. The other upgrades
  cost $25k-45k and earn less.
- **A new seat moves capital but adds risk.** Its cash comes out of our
  existing bots, so net worth does not change. But a fourth seat would share
  the team's 20-messages-per-tick quota (we use 16) and its 800-share limit
  (we plan for about 550 plus ORCA), and it is one more seat that can be
  rejected or disconnected.
- **Week 7 is the better window.** By then the season will show whether
  one trader strategy clearly beats the other, which is the case for adding
  a seat.
