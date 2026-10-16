# Midway Traders

Budget invested: $1,000,000

- 🏦 `midway_traders_broker` ($400,000):

      TEAM_ID=midway_traders_broker python -m team.broker

- 🤖 `midway_traders_trader_1` ($300,000) — momentum:

      make momentum

- 🤖 `midway_traders_trader_2` ($300,000) — mean reversion:

      make meanrev

## Where to write code

- `momentum_trader.py` → `MomentumTrader._signal()` — trader_1's edge
- `mean_reversion_trader.py` → `MeanReversionTrader._signal()` — trader_2's edge
- `base_trader.py` → starter order, stop-loss, risk checks shared by both
- `risk.py` → risk limits
- `trader.py` → `MyTrader.on_tick()` — the Level 1 single-order bot
- `broker.py` → `MyBroker.spread()/skew()` — quoting and inventory
- `config.py` → your tunables

Each class derives an `arena` base that handles all plumbing —
see `from arena import Trader, Broker, Exchange`.
