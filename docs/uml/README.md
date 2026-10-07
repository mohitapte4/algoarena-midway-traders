Trader is an abstract base class because a trading bot is more than a function that makes a single decision. The bot may need to maintain state across ticks, such as whether it has already placed an order, and it may also need to respond to other events such as fills. Using a class gives each trader object a natural place to store this state and allows the framework to provide optional methods such as on_fill and on_event. Making on_tick abstract also guarantees that every Trader subclass implements the one method the framework requires; if it does not, Python will prevent the class from being instantiated, so the error is caught before the trading loop begins. A function-registration design would be simpler and require less boilerplate, especially for a small stateless strategy, but it would provide less structure as the strategy becomes more complex. The ABC design therefore trades some simplicity for a clearer interface, state management, and early enforcement of the framework's requirements.
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
