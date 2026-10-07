# Midway Traders

Budget invested: $1,000,000

- 🏦 `midway_traders_broker` ($400,000):

      TEAM_ID=midway_traders_broker python -m team.broker

- 🤖 `midway_traders_trader_1` ($300,000):

      TEAM_ID=midway_traders_trader_1 python -m team.trader

- 🤖 `midway_traders_trader_2` ($300,000):

      TEAM_ID=midway_traders_trader_2 python -m team.trader

## Where to write code

- `trader.py` → `MyTrader.on_tick()` — your edge
- `broker.py` → `MyBroker.spread()/skew()` — quoting and inventory
- `config.py` → your tunables

Each class derives an `arena` base that handles all plumbing —
see `from arena import Trader, Broker, Exchange`.
