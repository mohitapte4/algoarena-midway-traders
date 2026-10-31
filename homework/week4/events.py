"""
Midway Traders — scheduled-event plans (the week-2 "shock switch").

When a CALENDAR_EVENT fires, the venue states the move's sign and size and
how it will arrive: the price ramps to overshoot × pct over ramp_ticks
exchange ticks (1 s each), then settles back to pct (engine/exchange/
calendar.py, Ramp.fraction). The next ~20 s are known the moment the message
lands, so the trade is chosen from the message, not from the chart:

    RIDE  good news. Buy at once (taking liquidity: it is a race), then
          offer the shares at 90% of the overshoot peak with a resting sell.
    FADE  bad news. Sell the shares held at once (taking liquidity), then buy
          them back with a resting bid once the ramp is over, never above
          the settle level.

A leg is traded only if its designed edge beats the round-trip fee:
    ride   (0.9 × overshoot − 0.5) × |pct|   the entry pays up to pct/2
    fade   (overshoot − 0.5) × |pct|         the sale gets at least pct/2
The recap's third leg (buy extra at the low, sell at the settle level) earns
(overshoot − 1) × |pct|: 0.59% on a 2% print against a 0.60% round trip at
30 bp. It is not traded.

Everything here is pure: prices and times in, prices and times out.
"""

from __future__ import annotations

from dataclasses import dataclass

EXCHANGE_TICK_SEC = 1.0      # the venue advances its price ramps once a second
DEFAULT_RAMP_TICKS = 15      # engine/exchange/config.py SHOCK_RAMP_TICKS
DEFAULT_OVERSHOOT = 1.3      # engine/exchange/config.py SHOCK_OVERSHOOT
EXIT_FRACTION = 0.9          # ride exit at 90% of the overshoot peak
DEADLINE_RAMPS = 3           # give up on the design path after 3 ramps

RIDE, FADE = "ride", "fade"


def designed_edge(mode: str, pct: float, overshoot: float) -> float:
    """Gross edge of one leg, as a fraction of price, from the message alone."""
    if mode == RIDE:
        return max(0.0, (EXIT_FRACTION * overshoot - 0.5) * abs(pct))
    return max(0.0, (overshoot - 0.5) * abs(pct))


def size_fraction(edge: float, round_trip: float) -> float:
    """Share of the full size worth trading: 0 at break-even, 1 at 2× the fee.

    A 6% earnings move (4.2% edge vs 0.6%) trades full size; a print whose
    edge only just clears the fee trades a sliver.
    """
    if round_trip <= 0:
        return 1.0 if edge > 0 else 0.0
    return min(1.0, max(0.0, (edge - round_trip) / round_trip))


@dataclass
class EventPlan:
    """One symbol's response to one event, from the message to flat again."""

    symbol: str
    mode: str                 # RIDE or FADE
    pct: float                # signed move announced by the venue
    pre: float                # last mid before the message
    t0: float                 # when the message arrived (bot clock)
    ramp_sec: float
    overshoot: float
    target_shares: int        # ride: shares to buy; fade: shares to sell
    label: str = ""
    bought: int = 0
    sold: int = 0
    buy_value: float = 0.0
    sell_value: float = 0.0
    stop_buys: bool = False   # set when the venue refuses a buy (position limit)
    done: bool = False

    # ── ride levels ─────────────────────────────────────────────────────

    @property
    def buy_cap(self) -> float:
        """Highest price the ride entry pays: half the move already gone."""
        return self.pre * (1 + self.pct / 2)

    @property
    def target(self) -> float:
        """Ride exit: 90% of the way to the overshoot peak."""
        return self.pre * (1 + EXIT_FRACTION * self.overshoot * self.pct)

    @property
    def entry_end(self) -> float:
        """No new ride buys after half the ramp: the cheap shares are gone."""
        return self.t0 + self.ramp_sec / 2

    @property
    def stop_price(self) -> float:
        """Ride stop: the price has given back half the move below entry."""
        return self.entry_vwap * (1 - abs(self.pct) / 2)

    # ── fade levels ─────────────────────────────────────────────────────

    @property
    def sell_floor(self) -> float:
        """Lowest price the fade sale accepts: half the drop already gone."""
        return self.pre * (1 + self.pct / 2)

    @property
    def rebuy_start(self) -> float:
        """Buy back once the ramp has reached its low."""
        return self.t0 + self.ramp_sec

    @property
    def rebuy_cap(self) -> float:
        """Never pay more than the settle level to buy back."""
        return self.pre * (1 + self.pct)

    # ── shared ──────────────────────────────────────────────────────────

    @property
    def deadline(self) -> float:
        """After this the design path has failed: get out at the touch."""
        return self.t0 + DEADLINE_RAMPS * self.ramp_sec

    @property
    def entry_vwap(self) -> float:
        return self.buy_value / self.bought if self.bought else self.pre

    @property
    def held(self) -> int:
        """Ride shares bought and not yet sold."""
        return self.bought - self.sold

    @property
    def owed(self) -> int:
        """Fade shares sold and not yet bought back."""
        return self.sold - self.bought

    def record(self, side: str, quantity: int, price: float) -> None:
        if side == "buy":
            self.bought += quantity
            self.buy_value += quantity * price
        else:
            self.sold += quantity
            self.sell_value += quantity * price

    def gross_pnl(self) -> float:
        """Realised trading P&L of the matched shares, before fees."""
        matched = min(self.bought, self.sold)
        if not matched:
            return 0.0
        avg_buy = self.buy_value / self.bought
        avg_sell = self.sell_value / self.sold
        return matched * (avg_sell - avg_buy)
