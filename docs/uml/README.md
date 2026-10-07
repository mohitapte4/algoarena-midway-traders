# UML class diagram

![Arena SDK class diagram](arena_sdk_class_diagram.png)

The design justification is in [DESIGN.md](DESIGN.md).

## Where each class in the diagram lives

| File | Classes |
| --- | --- |
| `arena/trader.py` | `Trader`, `_AdaptedTraderBot`, `_AdapterStrategy` |
| `arena/broker.py` | `Broker` |
| `arena/exchange.py` | `Exchange` |
| `team/trader.py` | `MyTrader` |
| `team/broker.py` | `MyBroker` |
| `engine/trader/trader.py` | `MarketData`, `Portfolio`, `Strategy`, `RiskManager`, `TraderBot` |
| `engine/shared/messages.py` | `Signal` |

`Signal` is re-exported by `arena/__init__.py`, so `from arena import Signal` and
`from shared.messages import Signal` both resolve to the same class.
