"""
Midway Traders — broker (market maker).

Posts two-sided quotes and earns the spread plus maker rebates on every
passive fill. Three decisions, three methods — override any of them.
Inventory management (skew) is the key to staying solvent: the exchange
charges margin interest and liquidates teams below maintenance.

Run:
    TEAM_ID=midway_traders_broker python -m team.broker
"""

from __future__ import annotations

from arena import Broker

# Volatility-adjusted width. `history` holds one venue mark per book update,
# about 1-2 a second per name, so 10 points is the last ~5-10 seconds.
LOOKBACK = 10
CALM_BPS = 10.0         # recent range below this: keep the engine default
WIDEN_FACTOR = 2.0      # quote width = 2 × the recent high-low range
MAX_WIDEN_BPS = 150.0   # never wider than 1.5% of the price
DEFAULT_WIDTH = 0.30    # engine BASE_SPREAD...
DEFAULT_CAP_BPS = 25.0  # ...capped at 25 bp of the price


def volatility_spread(price, history) -> float | None:
    """Dollar width for a choppy market, or None when it is calm.

    A scheduled event ramps the price several percent in ~15 s; a quote left
    at the default width is bought or sold by every fast trader on the way
    (week 2: one broker lost $2,964 on AAPL asks in a single minute).
    Widening with the recent range keeps quotes out of reach while the price
    is moving and costs nothing when it is not.
    """
    if not price or price <= 0 or len(history) < LOOKBACK:
        return None
    recent = history[-LOOKBACK:]
    move = max(recent) - min(recent)
    if move < price * CALM_BPS / 10_000:
        return None
    default = min(DEFAULT_WIDTH, price * DEFAULT_CAP_BPS / 10_000)
    return max(default, min(WIDEN_FACTOR * move, price * MAX_WIDEN_BPS / 10_000))


class MyBroker(Broker):
    """Your market-making logic. An empty subclass already quotes."""

    def spread(self, symbol, price, history):
        # Level 3: widen the quotes when the market gets choppy.
        return volatility_spread(price, history)

    def skew(self, symbol, inventory):
        # TODO Level 4: shift both quotes to flatten your inventory.
        # Long (inventory > 0) → quote lower to attract sellers.
        return None

    # Optional extra hooks:
    #   def toxic(self, trader_id): ...                 # Level 5
    #   def on_fill(self, side, symbol, quantity, price): ...


if __name__ == "__main__":
    from team import logs
    logs.setup()
    MyBroker().run()
