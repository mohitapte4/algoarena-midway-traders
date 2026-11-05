# Phase 2 hypothesis (written before any Phase 2 result was computed)

**Signal.** The trend rule that ran live on seat `midway_traders_trader_1` in
Session 5 (`team/momentum_trader.py`): when the mean of the last 5 book mids
is more than a buffer above the mean of the last 20, rest a 5-share bid at
the touch; when it is more than the buffer below, offer at the touch. The
buffer is `max(0.3%, 1.25 × (taker − rebate))`. Quotes are pulled from a name
in the window around a scheduled event.

**Claim.** On this venue a move in a book's mid persists for several updates
before the house market makers re-anchor, so after the short average clears
the long one by the buffer, the mid keeps moving the same way by more than
the cost of trading it.

**Why it might be true.** The venue walks a shocked fair value to its target
over 15 ticks (`SHOCK_RAMP_TICKS`), and makers re-quote with a lag, so trends
should exist. **Why it might be false.** In Week 2 we measured short-horizon
moves reverting more often than continuing, and the crossover is pulled
during the scheduled events, which is where the persistent moves are.

**Prediction, stated in advance.** Outside event windows the signal does not
cover its costs: with the event trades switched off, its P&L on the Session 5
tape at the live fees (taker 30 bps, maker rebate 5 bps) is at or below zero,
and below the do-nothing baseline. Most of trader_1's live result comes from
the event trades, not from this signal.

**What would falsify the prediction (and support the claim).** Signal-only
P&L above do-nothing in most independent blocks of the tape, a hit rate on
closed round trips above 50%, and an edge that shrinks, rather than
vanishes or grows, when the signal is fed data one update older.

**Rules fixed before the run.** Parameters are the live ones (5, 20, 0.3%,
5 shares); nothing is tuned on the tape being evaluated, so there is no
in-sample fitting. Costs are the live fee schedule. The look-ahead check
feeds the same signal the next update's mid; a backtest without look-ahead
must lose that windfall.
