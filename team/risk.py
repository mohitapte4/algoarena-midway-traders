"""
Midway Traders — risk limits for the trading bots.

Every rule is a pure function of numbers the bot already has (net worth,
the value it holds, the current price), so each one can be tested on its
own. All limits are fractions of the seat's live net worth, which keeps them
meaningful whatever the seat's starting capital.

    Session loss limit   stop opening positions once net worth is this far
                         below its value at the open (exits still allowed)
    Order size           one order's notional
    Concentration        notional held in a single symbol
    Gross exposure       notional held across all symbols
    Stop-loss            sell a position whose bid has fallen this far
                         below its average entry price
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    """Moderate limits by default; every field is a fraction."""

    max_session_loss: float = 0.01
    max_order: float = 0.02
    max_symbol: float = 0.05
    max_gross: float = 0.20
    stop_loss: float = 0.015

    def halted(self, net_worth: float, start_net_worth: float | None) -> bool:
        """True once the session loss limit has been breached."""
        if start_net_worth is None or start_net_worth <= 0:
            return False
        return net_worth < start_net_worth * (1 - self.max_session_loss)

    def buy_quantity(self, wanted: int, price: float, symbol_value: float,
                     gross_value: float, net_worth: float) -> tuple[int, str]:
        """Largest quantity up to `wanted` that respects every buy limit.

        `symbol_value` and `gross_value` are what is already held, at market.
        Returns the quantity and, when it is below `wanted`, the name of the
        limit that bound; an empty string means nothing was cut.
        """
        if wanted < 1 or price <= 0 or net_worth <= 0:
            return 0, "no price or net worth"
        room = {
            "order size": self.max_order * net_worth,
            "concentration": self.max_symbol * net_worth - symbol_value,
            "gross exposure": self.max_gross * net_worth - gross_value,
        }
        binding = min(room, key=room.get)
        qty = min(wanted, int(max(room[binding], 0.0) // price))
        return qty, ("" if qty == wanted else binding)

    def stop_hit(self, bid: float | None, avg_entry: float | None) -> bool:
        """True when the bid is at or below the stop under the average entry."""
        if not bid or not avg_entry:
            return False
        return bid <= avg_entry * (1 - self.stop_loss)
