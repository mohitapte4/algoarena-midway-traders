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


class MyBroker(Broker):
    """Your market-making logic. An empty subclass already quotes."""

    def spread(self, symbol, price, history):
        # TODO Level 3: widen your quotes when the market gets choppy.
        # `history` is the recent reference prices, oldest first.
        # Return the dollar width, or None for the fixed default.
        return None

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
