# Capital allocation

Midway Traders registered with the standard $1,000,000 budget. We put all of it into three seats and did not buy an exchange licence.

| Seat | Bot ID | Capital |
|---|---|---|
| Broker desk | `midway_traders_broker` | $400,000 |
| Trader seat 1 | `midway_traders_trader_1` | $300,000 |
| Trader seat 2 | `midway_traders_trader_2` | $300,000 |
| **Total** | | **$1,000,000** |

## No exchange licence

The licence costs $300,000, and that money does not come back. It pays for the right to charge fees on other teams' trades, and a venue only earns when those teams choose to send it orders. Early in the season there is little reason for anyone to move their flow, so the income was uncertain. Spending 30% of the budget on it made less sense than putting the full amount into seats we control.

## The broker gets the largest share

A market maker quotes both sides of ten stocks at once, and every time someone trades against it, it ends up holding the other side. When the market trends, that inventory builds up faster than the broker can work it off. The broker needs enough cash to carry those positions and keep quoting through a bad stretch. $400,000 gives it that room, four times the $100,000 minimum, while staying under half the budget, so a bad day at the desk cannot sink the whole team.

## Two traders, split evenly

One trader seat runs one strategy. Two seats let us run two different approaches on the same market and compare them directly, for example trend-following on one and mean reversion on the other. They also mean a single crash or dropped connection does not take us out of the market, because the other bot keeps trading.

We split the $600,000 evenly because we have no evidence yet that either approach will do better, and an uneven split would be a bet we cannot back up. Each seat is six times the $50,000 minimum, and our first strategy risks at most $5,000 per order, so neither trader is short of capital.

## What this split costs us

- **The free starting shares are divided.** At the start of each session the arena gives each team 20 shares of every stock, shared across its seats. With three seats, each bot gets 6 and the remaining 2 are lost to rounding.
- **Most of the capital sits idle at first.** Idle cash still earns the arena's interest on positive balances, about 0.7% over an hour-long session. That is the return a strategy has to beat to be worth running at all.

## When we will revisit it

In weeks 4 and 7 the arena opens a purchase window, where a team can add seats funded from the cash its bots already hold. If one trader clearly outperforms the other, we can add a seat for that strategy then. If the broker's inventory turns out to be the real constraint, we can move capital toward market making instead.
