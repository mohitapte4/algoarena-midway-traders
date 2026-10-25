# Midway Traders: Phase 1

Three seats, each in its own process: `midway_traders_trader_1` (momentum),
`midway_traders_trader_2` (mean reversion) and `midway_traders_broker`
(market maker). The source is in `team/`; the seats subclass the engine's
bots and never modify `engine/`. Evidence below is from the Session 4 block
(Oct 23, open 15:40:25, close 16:30:00) and the logs
`logs/midway_traders_{trader_1,trader_2,broker}_20261023_1530*.log`.

## How we track state

Every message is parsed into the venue's typed models (`shared.messages`);
nothing reads raw JSON fields. Each seat keeps three kinds of state:

- **Orders.** `OwnOrders` (`team/venue.py`) records each order we send. An
  `OrderAck` attaches the venue's order id, a fill reduces the open
  quantity, an error drops the order, and an order neither acked nor filled
  within 5 s is forgotten. The broker keeps the same record per quote.
- **Positions for trading.** A fill updates the seat's ledger at once; one
  second after the last fill, the ledger takes the venue's positions.
- **Independent books** (`team/books.py`). Cash, positions, average cost,
  realized P&L, fees and rebates, built only from our own fills and the
  events that move money without one (starting shares, IPO allocation,
  dividends, futures settlement, purchases, liquidation). They never copy
  the venue. At the close each seat compares them with its row on the
  venue's closing leaderboard and logs `RECON PASS` or `RECON BREAK`.

At the Session 4 close all three seats logged `RECON PASS`: every position
matched, and fees, rebates and realized P&L matched to the cent. Cash
differed only by the venue's interest on idle cash, which no message
reports ($689, $1,469 and $1,251 against our estimates).

| Seat     | Fills | Fees (own / venue) | Session P&L (venue) |
| -------- | ----- | ------------------ | ------------------- |
| trader_1 | 118   | 240.52 / 240.52    | +$2,409             |
| trader_2 | 62    | 177.70 / 177.70    | +$1,781             |
| broker   | 352   | 339.40 / 339.40    | −$2,995             |

Orders are gated so the venue has no reason to reject them: sells never
exceed shares held plus the short allowance, buys stay inside cash and the
share cap, prices stay inside the LULD band, and a token bucket keeps the
team under its message quota. In the block, the traders had no rejected
orders and the broker had one: a self-trade-prevention cancel, where one
of our trader's buys crossed our broker's resting ask. Failed cancels for
quotes that had just filled (`ORDER_NOT_FOUND`, 13 on the broker) are
error replies, not rejected orders. Stopping our traders from crossing our
own broker is the next fix.

## How we reconnect

`team/reconnect.py` starts a new connection whenever one ends (clean close,
dropped socket, refused handshake, failed dial). The wait doubles from 1 s
to 30 s with 20% jitter, and resets once a connection stays up 30 s. An
exception in a fill or event handler is logged and never drops the link.
The venue cancels a seat's orders when it disconnects, so each connection
starts with no orders tracked and requotes from scratch.

At 15:35:56 the venue's keepalive timed out on all three seats; each was
reconnected within 1-2 s (trader_1 log, 15:35:56-15:35:57). On reconnect
each seat checks its books against the venue. That check matched every
position and cash to the cent but printed `BREAK`, because it expected
interest the venue does not pay before the open; the books restarted from
the venue's figures, which were identical. The interest rule is being
corrected. A local rehearsal with a forced 10 s drop logged
`BOOKS reconnect check PASS` on all three seats.

## IPO decision: ORCA

**Decision: bid.** Each trader indicated 200 of the 4,000 shares at the top
of the $24-28 range; the broker did not bid.

- **Positive expected value.** The venue draws ORCA's value as offer ×
  exp(N(12%, 18%)): an average pop of about +15%, with a 25% chance of
  breaking issue.
- **The top of the range costs nothing extra.** The deal prices at the
  highest level that fills the book, and the pop is a percentage of that
  offer, so a higher limit does not lower the expected return.
- **Fits our limits.** 400 shares keeps the team at half its 800-share
  limit. Each allocation rests an ask at the median pop and has a stop 8%
  below the offer.

**Result.** The book was oversubscribed (5,917 shares for 4,000) and priced
at $28.00. Each trader received 143 shares. ORCA listed flat; the
take-profit asks did not fill, and at the close it traded near $28.14, a
gain of about $40 across both seats.

## Purchase window 1: no purchase

No upgrade pays back this phase: the best, `fee_tier` ($40k), earns the
broker about $1.1k a session, about $7.7k over the 7 sessions left. A new
seat moves capital without adding any, while sharing the team's message
quota and share limit. Week 7, when one strategy has proved itself, is the
better window.
