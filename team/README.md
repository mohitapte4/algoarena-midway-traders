# Midway Traders

Budget invested: $1,000,000

- 🏦 `midway_traders_broker` ($400,000):

      TEAM_ID=midway_traders_broker python -m team.broker

- 🤖 `midway_traders_trader_1` ($300,000) — momentum + event ride/fade:

      make momentum          # 0.1 s loop

- 🤖 `midway_traders_trader_2` ($300,000) — mean reversion + event fade:

      make meanrev           # 0.1 s loop

## Where to write code

- `momentum_trader.py` → `MomentumTrader._signal()` — trader_1's edge
- `mean_reversion_trader.py` → `MeanReversionTrader._signal()` — trader_2's edge
- `base_trader.py` → starter order, stop-loss, maker quoting, the order gate
  and the event layer, shared by both
- `events.py` → event plans: edge-vs-fee check and price levels
- `venue.py` → own-order tracking and the bot wrapper (acks, rejects, cancels)
- `risk.py` → risk limits (`RiskLimits`) and event sizing (`EventLimits`)
- `trader.py` → `MyTrader.on_tick()` — the Level 1 single-order bot
- `broker.py` → `MyBroker.spread()/skew()` — quoting and inventory
- `config.py` → your tunables

Each class derives an `arena` base that handles all plumbing —
see `from arena import Trader, Broker, Exchange`.

## How the seats trade (week 2 onwards)

**Events first.** A `CALENDAR_EVENT` states the move's sign and size and how
it ramps in: up to 1.3× the move over ~15 s, then back to 1×. The seats act
on the message, not on the chart:

| leg | seat | entry | exit | designed edge |
|---|---|---|---|---|
| RIDE (good news) | trader_1 | take, up to pre × (1 + pct/2) | rest a sell at 90% of the peak; time-stop 3 ramps; stop pct/2 under entry | (0.9 × 1.3 − 0.5) × pct |
| FADE (bad news) | both | take: sell holdings down to pre × (1 + pct/2) | rest a bid after the ramp, ≤ settle level; at the ask after 3 ramps | (1.3 − 0.5) × \|pct\| |

A leg trades only if its edge beats the round-trip taker fee (from
`FEE_SCHEDULE`). The size scales from 0 at break-even to full size at 2× the
fee, capped by `EventLimits`: 12% of net worth, 60 shares per order, 200
shares. At the ~2-minute `CALENDAR` warning, the seats pull their quotes in
that name and stop strategy trading there until the event is over.

**Makers by default.** Strategy entries and exits rest at the touch and earn
the rebate. Only the races take liquidity: the event entry, the fade sale,
the stop-loss and the starter.

**Under 10 rejects a session.** Every order passes one gate in
`base_trader.py`:

- sells never exceed the shares held minus the shares already offered;
- buys are capped at 300 shares per symbol (the team limit is ±400) and by
  cash including the fee;
- there is one order per symbol and side, and an order never crosses the
  seat's own resting order;
- prices stay inside the LULD band;
- orders and cancels are limited to 4 a second (the team shares 20 per tick).

A reject pauses the symbol for 10 s. After 8 rejects the seat stops sending
orders. The engine's post-close flatten is switched off, because every order
it sent was rejected.

**Check a strategy against a recorded block** (the tapes go in `sessions/`):

    make backtest FILE=sessions/<file>.jsonl.gz STRATEGY=momentum
    python scripts/event_backtest.py sessions/<file> --compare
    python scripts/event_backtest.py sessions/<file> --bot momentum --tick 0.5 --fill touch
