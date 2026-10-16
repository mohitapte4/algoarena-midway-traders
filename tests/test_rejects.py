"""The order gate must keep each seat under 10 rejected orders a session."""

from __future__ import annotations

import asyncio
import json
import logging

from team.base_trader import ORDERS_PER_SEC, REJECT_BUDGET, SEAT_MAX_SHARES
from team.momentum_trader import MomentumTrader
from team.venue import CANCEL_GRACE_SEC, OwnOrders, TeamBot
from tests.test_momentum_trader import Clock, FakeMarket, FakePortfolio, flat, trend

logging.disable(logging.CRITICAL)


def ready(positions=None, books=None, cash=300_000.0):
    """A trader past its starter, holding the venue's view of positions."""
    clock = Clock()
    t = MomentumTrader(clock=clock)
    m = FakeMarket(books or {"AAA": flat()})
    p = FakePortfolio(cash=cash, positions=positions or {"ZZZ": 6},
                      net_worth=300_000.0)
    s = t.on_tick(m, p)
    t.on_fill(s.side, s.symbol, s.quantity, s.price)
    p.positions[s.symbol] = p.positions.get(s.symbol, 0) + (
        s.quantity if s.side == "buy" else -s.quantity)
    clock.t += 2
    t._market, t._portfolio = m, p
    t._sync_ledger(p, clock.t)            # adopt the venue's positions
    return t, m, p, clock


def test_sells_never_exceed_shares_held_while_the_venue_catches_up():
    t, m, p, c = ready(positions={"DN": 10, "ZZZ": 6})
    assert t.sellable("DN") == 10
    t.on_fill("sell", "DN", 10, 99.0)     # filled; portfolio not updated yet
    assert t.sellable("DN") == 0
    c.t += 0.1
    t.on_tick(m, p)                       # stale venue positions: still 0
    assert t.sellable("DN") == 0


def test_shares_already_offered_cannot_be_offered_again():
    t, m, p, c = ready(positions={"DN": 8, "ZZZ": 6},
                       books={"AAA": flat(), "DN": trend(step=-0.001)})
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("DN", "sell", 5)
    assert t.sellable("DN") == 3
    assert t._place("DN", "sell", 5, 99.0, c.t, "test", taker=True) is None  # one per side


def test_buys_stop_at_the_seat_share_cap():
    t, m, p, c = ready(positions={"AAA": SEAT_MAX_SHARES - 3 + 5, "ZZZ": 6})
    held = p.positions["AAA"]             # the starter sold 5 of them
    s = t._place("AAA", "buy", 50, 100.05, c.t, "test", taker=True)
    assert s.quantity == SEAT_MAX_SHARES - held


def test_buys_stop_at_cash_including_the_fee():
    t, m, p, c = ready()
    p.cash = 1_000.0
    s = t._place("AAA", "buy", 50, 100.0, c.t, "test", taker=True)
    assert s.quantity == 9                # $1,000 / 1.003 / $100


def test_prices_are_kept_inside_the_venue_band():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    s = t._place("AAA", "sell", 1, 150.0, c.t, "test", taker=False)
    assert s.price <= 100.0 * 1.09 + 0.01


def test_never_crosses_its_own_resting_order():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    ask = t._place("AAA", "sell", 5, 100.10, c.t, "test", taker=False)
    t.on_ack("s1", "AAA", "sell", ask.price)
    c.t += 1
    assert t._place("AAA", "buy", 5, 100.20, c.t, "test", taker=True) is None
    assert t.take_cancels() == [("s1", "AAA")]
    c.t += CANCEL_GRACE_SEC
    t.orders.expire(c.t)
    assert t._place("AAA", "buy", 5, 100.20, c.t, "test", taker=True) is not None


def test_no_cancel_right_after_a_fill():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    ask = t._place("AAA", "sell", 10, 100.10, c.t, "test", taker=False)
    t.on_ack("s1", "AAA", "sell", ask.price)
    t.on_fill("sell", "AAA", 4, 100.10)   # partly filled just now
    t._cancel(t.orders.live("AAA", "sell"), c.t)
    assert t.take_cancels() == []


def test_order_rate_is_paced():
    t, m, p, c = ready(positions={s: 20 for s in "ABCDEFGH"})
    sent = sum(t._place(s, "sell", 1, 100.0, c.t, "test", taker=False) is not None
               for s in "ABCDEFGH")
    assert sent == ORDERS_PER_SEC


def test_reject_budget_stops_all_orders():
    t, m, p, c = ready(books={"AAA": flat(), "UP": trend()})
    for _ in range(REJECT_BUDGET):
        t.on_reject("SHORTS_LOCKED", "You hold 0 AAA — sell at most that many.")
    c.t += 60
    assert t.on_tick(m, p) is None
    assert sum(t.rejects.values()) == REJECT_BUDGET < 10


def test_a_reject_cools_the_symbol_down():
    t, m, p, c = ready(books={"AAA": flat(), "UP": trend()})
    s = t.on_tick(m, p)
    assert s.symbol == "UP"
    t.on_reject("PRICE_BAND", "UP buy outside the band")
    assert t.orders.live("UP", "buy") is None          # the refused order is gone
    c.t += 1
    assert t.on_tick(m, p) is None                     # UP cooling down
    c.t += 10
    assert t.on_tick(m, p).symbol == "UP"


def test_stp_and_not_found_errors_remove_the_order():
    book = OwnOrders()
    from team.venue import Order
    a = Order("AAPL", "buy", 196.5, 1, 0.0, "quote", False, order_id="o1")
    b = Order("NFLX", "sell", 1337.25, 4, 0.0, "quote", False, order_id="o2")
    book.add(a)
    book.add(b)
    book.on_reject("STP_CANCEL", "Your resting buy 1 AAPL @ 196.5 was cancelled "
                   "by self-trade prevention (your own order crossed it)")
    book.on_reject("ORDER_NOT_FOUND", "Order 'o2' not found or not owned by x")
    assert len(book) == 0


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)


def test_no_close_out_orders_after_the_session_closes():
    t, m, p, _ = ready(positions={"AAA": 20})
    bot = TeamBot(t)
    bot._ws = _FakeSocket()
    bot.portfolio.positions = {"AAA": 20, "MSFT": 5}
    asyncio.run(bot.flatten_all_positions())
    assert bot._ws.sent == []


def test_bot_routes_acks_and_errors_to_the_trader(monkeypatch):
    import trader.config as cfg
    monkeypatch.setattr(cfg, "TEAM_ID", "me")
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    t._place("AAA", "sell", 5, 100.10, c.t, "test", taker=False)
    bot = TeamBot(t)
    bot._peek(json.dumps({"type": "order_ack", "order_id": "k1", "team_id": "me",
                          "symbol": "AAA", "side": "sell", "price": 100.10,
                          "quantity": 5}))
    assert t.orders.live("AAA", "sell").order_id == "k1"
    bot._peek(json.dumps({"type": "error", "code": "RATE_LIMITED",
                          "message": "Message quota exceeded"}))
    assert t.rejects["RATE_LIMITED"] == 1


# ── broker width ────────────────────────────────────────────────────────────

def test_broker_keeps_the_default_width_when_calm():
    from team.broker import volatility_spread
    assert volatility_spread(195.0, [195.0] * 20) is None
    assert volatility_spread(195.0, [195.0] * 5) is None          # too little history
    assert volatility_spread(None, [195.0] * 20) is None


def test_broker_widens_on_a_ramp_but_never_past_the_cap():
    from team.broker import MAX_WIDEN_BPS, volatility_spread
    ramp = [195.0 * (1 + 0.004 * i) for i in range(12)]           # +4.4% in 12 updates
    width = volatility_spread(ramp[-1], ramp)
    assert width > 0.30
    assert width <= ramp[-1] * MAX_WIDEN_BPS / 10_000 + 1e-9
    chop = [195.0, 195.3] * 6                                      # 15 bp chop
    assert volatility_spread(195.0, chop) == __import__("pytest").approx(0.6)   # 2 × range
