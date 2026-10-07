# Design justification

## Why Trader is an abstract base class

Trader is an abstract base class because a trading bot is more than a function that makes a single decision. The bot may need to maintain state across ticks, such as whether it has already placed an order, and it may also need to respond to other events such as fills. Using a class gives each trader object a natural place to store this state and allows the framework to provide optional methods such as on_fill and on_event. Making on_tick abstract also guarantees that every Trader subclass implements the one method the framework requires; if it does not, Python will prevent the class from being instantiated, so the error is caught before the trading loop begins. A function-registration design would be simpler and require less boilerplate, especially for a small stateless strategy, but it would provide less structure as the strategy becomes more complex. The ABC design therefore trades some simplicity for a clearer interface, state management, and early enforcement of the framework's requirements.

## Log excerpt: connect and order

A Level 1 session on the class arena, from `logs/level1_ack_20261006_203235.log`. The bot connects under its seat id, sends one order, receives the order_ack, and is filled.

```
20:32:36  INFO      Connected to wss://feed.algoarena-st.duckdns.org as midway_traders_trader_1 (trader)
20:32:37  INFO      ORDER  buy 5 AAPL @ 236.00 limit (notional 1180.00)
20:32:37  DEBUG     Placed: buy 5 AAPL @ 236.0000
20:32:37  DEBUG     Ack: buy 5 AAPL @ 236.0000
20:32:37  INFO      Fill: bought 5 AAPL @ 236.0000 (fee=1.7700)
20:32:37  INFO      FILL   #1 buy    5 AAPL   @   236.00  notional   1180.00  vs limit +0.0000
```
