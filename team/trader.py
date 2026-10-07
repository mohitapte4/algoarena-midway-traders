"""
Midway Traders — trader bot.

Implement on_tick(): decide a trade (or None) each tick. Everything else —
connection, auth, portfolio tracking — is handled by the base class.

Run one seat:
    TEAM_ID=midway_traders_trader_1 python -m team.trader
"""

from __future__ import annotations

from arena import Signal, Trader


class MyTrader(Trader):
    """Your alpha lives here."""

    def on_tick(self, market, portfolio):
        # TODO: implement your strategy.
        #
        # You can read:
        #   market.symbols()           tradeable symbols
        #   market.mid_price(sym)      current mid price
        #   market.prices(sym)         recent mids, oldest first
        #   market.best_bid/best_ask   the touch
        #   portfolio.cash             your cash
        #   portfolio.positions        symbol → shares held
        #
        # Return a Signal(symbol=..., side="buy"/"sell", quantity=..., price=...)
        # to trade, or None to sit out this tick.
        return None

    # Optional extra hooks:
    #   def on_fill(self, side, symbol, quantity, price): ...
    #   def on_event(self, event, message, data): ...   # shocks, calendar
    #   def on_ipo(self, symbol, lo, hi, shares, data): ...
    #       # An IPO book opened — return qty (bids top of range) or
    #       # (qty, max_price) as your indication, None to pass.


if __name__ == "__main__":
    MyTrader().run()
