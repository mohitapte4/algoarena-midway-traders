"""Tests for the event layer in team.base_trader (no network)."""

from __future__ import annotations

import logging

import trader.config as trader_config
from team.base_trader import STARTER_QUANTITY, BaseTrader
from team.mean_reversion_trader import MeanReversionTrader
from team.momentum_trader import MomentumTrader
from tests.test_momentum_trader import Clock, FakeMarket, FakePortfolio, flat, trend

logging.disable(logging.CRITICAL)

PX = 195.0


def setup(cls=MomentumTrader, positions=None, books=None, cash=300_000.0):
    """A trader past its starter (filled on AAA), with fees announced."""
    clock = Clock()
    t = cls(clock=clock)
    m = FakeMarket(books or {"AAA": flat(), "AAPL": flat(px=PX)})
    p = FakePortfolio(cash=cash, positions=positions or {"ZZZ": 6})
    t.on_event("FEE_SCHEDULE", "fees", {"taker": 0.003, "rebate": 0.001})
    s = t.on_tick(m, p)
    t.on_fill("buy", s.symbol, s.quantity, s.price)
    p.positions[s.symbol] = p.positions.get(s.symbol, 0) + s.quantity
    clock.t += 2
    return t, m, p, clock


def fire(t, pct, symbol="AAPL"):
    t.on_event("CALENDAR_EVENT", f"Earnings: {symbol} moving {pct:+.1%}", {
        "kind": "earnings", "symbol": symbol, "affected": [symbol],
        "pct": pct, "ramp_ticks": 15, "overshoot": 1.3})


def fill(t, p, s, price=None):
    """The venue fills a whole order and reports the new position."""
    t.on_fill(s.side, s.symbol, s.quantity, price or s.price)
    p.positions[s.symbol] = p.positions.get(s.symbol, 0) + (
        s.quantity if s.side == "buy" else -s.quantity)


def run(t, m, p, c, seconds, symbol="AAPL"):
    """Tick once a second; return (seconds elapsed, signal) for `symbol`."""
    start, out = c.t, []
    while c.t < start + seconds:
        s = t.on_tick(m, p)
        if s is not None and s.symbol == symbol:
            out.append((c.t - start, s))
        c.t += 1
    return out


def assert_reaction_bid(out, settle):
    """One resting 5-share bid, after the 15 s ramp and inside 45 s."""
    assert len(out) == 1
    at, s = out[0]
    assert 15 <= at < 45
    assert (s.side, s.quantity) == ("buy", STARTER_QUANTITY)
    assert s.price <= settle


def test_fee_schedule_sets_the_fee():
    t, *_ = setup()
    assert (t.taker, t.rebate) == (0.003, 0.001)


def test_ride_takes_in_chunks_up_to_the_cap_then_rests_the_exit():
    t, m, p, c = setup()
    fire(t, 0.062)
    buys = []
    for _ in range(6):
        s = t.on_tick(m, p)
        if s is not None:
            assert s.side == "buy" and s.quantity <= 60
            assert s.price >= m.best_ask("AAPL")          # marketable: takes
            assert s.price <= PX * (1 + 0.031)            # never above the cap
            fill(t, p, s, m.best_ask("AAPL"))
            buys.append(s.quantity)
        c.t += 0.3
    # 12% of $300k at the cap is 179 shares, below the 200-share cap.
    assert sum(buys) == 179 and len(buys) == 3
    c.t += 8                                              # entry window over
    s = t.on_tick(m, p)
    target = PX * (1 + 0.9 * 1.3 * 0.062)
    assert (s.side, s.quantity) == ("sell", 179)
    assert s.price >= target - 0.01                       # rests at the target
    assert t.orders.live("AAPL", "sell").taker is False
    fill(t, p, s)
    c.t += 1
    assert t.on_tick(m, p) is None
    assert "AAPL" not in t._plans and t.finished_plans[-1].gross_pnl() > 0


def test_ride_time_stop_takes_the_bid():
    t, m, p, c = setup()
    fire(t, 0.062)
    s = t.on_tick(m, p)
    fill(t, p, s, m.best_ask("AAPL"))
    c.t += 8
    rest = t.on_tick(m, p)                                # resting exit
    t.on_ack("x1", "AAPL", "sell", rest.price)
    c.t += 40                                             # past 3 ramps
    assert t.on_tick(m, p) is None
    assert t.take_cancels() == [("x1", "AAPL")]           # pull the resting exit
    c.t += 1
    s = t.on_tick(m, p)
    assert (s.side, s.quantity, s.price) == ("sell", 60, round(m.best_bid("AAPL"), 2))


def test_fade_sells_now_and_rests_the_rebuy_at_the_settle_level():
    t, m, p, c = setup(positions={"AAPL": 50, "ZZZ": 6})
    fire(t, -0.02)
    s = t.on_tick(m, p)
    assert (s.side, s.quantity) == ("sell", 50)
    assert s.price <= m.best_bid("AAPL") and s.price >= PX * 0.99 - 0.01
    fill(t, p, s, m.best_bid("AAPL"))
    c.t += 16                                             # ramp over
    m.books["AAPL"] = flat(px=PX * 0.975)                 # the low
    s = t.on_tick(m, p)
    assert (s.side, s.quantity) == ("buy", 50)
    assert s.price <= m.best_bid("AAPL") and s.price <= PX * 0.98   # passive, capped
    assert t.orders.live("AAPL", "buy").taker is False


def test_fade_rebuys_at_the_ask_if_the_settle_level_never_comes():
    t, m, p, c = setup(positions={"AAPL": 50, "ZZZ": 6})
    fire(t, -0.02)
    fill(t, p, t.on_tick(m, p), m.best_bid("AAPL"))
    c.t += 16
    s = t.on_tick(m, p)                                   # resting at the cap
    t.on_ack("b1", "AAPL", "buy", s.price)
    c.t += 30                                             # past the deadline
    assert t.on_tick(m, p) is None
    assert t.take_cancels() == [("b1", "AAPL")]
    c.t += 1
    s = t.on_tick(m, p)
    assert (s.side, s.quantity, s.price) == ("buy", 50, round(m.best_ask("AAPL"), 2))


def test_no_plan_when_the_edge_does_not_pay_the_fee():
    t, m, p, _ = setup()
    fire(t, 0.008)
    assert t.on_tick(m, p) is None and not t._plans


def test_mean_reversion_seat_never_rides():
    t, m, p, _ = setup(MeanReversionTrader)
    fire(t, 0.062)
    assert t.on_tick(m, p) is None and not t._plans


def test_loss_limit_blocks_new_rides():
    t, m, p, _ = setup()
    p.nw *= 0.98
    fire(t, 0.062)
    assert t.on_tick(m, p) is None and not t._plans


def test_shock_derives_the_move_from_target_prices():
    t, m, p, _ = setup()
    t.on_event("SHOCK", "Flash rally", {"shock_id": "x", "affected": ["AAPL"],
                                         "prices": {"AAPL": PX * 1.05}})
    s = t.on_tick(m, p)
    assert (s.symbol, s.side) == ("AAPL", "buy")
    assert t._plans["AAPL"].pct == __import__("pytest").approx(0.05, abs=1e-6)


def test_calendar_warning_pulls_quotes_and_pauses_the_strategy():
    books = {"AAA": flat(), "AAPL": trend(px=PX), "UP": trend()}
    t, m, p, c = setup(books=books)
    s = t.on_tick(m, p)
    assert s.symbol == "AAPL" and s.side == "buy"         # crossover quote
    t.on_ack("q1", "AAPL", "buy", s.price)
    c.t += 1
    t.on_event("CALENDAR", "Upcoming: earnings AAPL @ t400", {
        "tick": 480, "announce_lead": 120,
        "imminent": [{"kind": "earnings", "tick": 400, "symbol": "AAPL"}]})
    s = t.on_tick(m, p)
    assert s.symbol == "UP"                               # AAPL left alone
    assert ("q1", "AAPL") in t.take_cancels()             # its quote pulled


def test_market_wide_warning_pauses_every_symbol():
    books = {"AAA": flat(), "UP": trend()}
    t, m, p, _ = setup(books=books)
    t.on_event("CALENDAR", "Upcoming: econ_print MARKET @ t900", {
        "tick": 980, "announce_lead": 120,
        "imminent": [{"kind": "econ_print", "tick": 900, "market_wide": True}]})
    assert t.on_tick(m, p) is None


def test_stop_loss_leaves_plan_symbols_to_the_plan():
    t, m, p, c = setup()
    fire(t, 0.062)
    fill(t, p, t.on_tick(m, p), m.best_ask("AAPL"))
    c.t += 1
    m.books["AAPL"] = flat(px=PX * 0.98)                  # 2% under entry
    s = t.on_tick(m, p)
    # The plan's own stop is half the move (3.1%) under entry, not 1.5%.
    assert s is None or s.side == "buy"


def test_base_trader_trades_no_events_by_default():
    assert BaseTrader.EVENT_MODES == frozenset()


def test_fade_rebuys_restore_inventory_without_becoming_strategy_shares():
    # Rebought shares are the holdings sold ahead of the drop: the stop-loss
    # and the mean-reversion exit must not treat them as a new position.
    t, m, p, c = setup(MeanReversionTrader, positions={"AAPL": 50, "ZZZ": 6})
    fire(t, -0.02)
    fill(t, p, t.on_tick(m, p), m.best_bid("AAPL"))
    c.t += 16
    m.books["AAPL"] = flat(px=PX * 0.975)
    fill(t, p, t.on_tick(m, p))
    assert p.positions["AAPL"] == 50
    assert "AAPL" not in t._bought


def test_mean_reversion_seat_bids_after_good_news_it_does_not_ride():
    t, m, p, c = setup(MeanReversionTrader)
    fire(t, 0.062)
    out = run(t, m, p, c, 60)
    assert_reaction_bid(out, PX * 1.062)
    assert out[0][1].price == round(m.best_bid("AAPL"), 2)   # rests at the bid


def test_flat_seat_on_bad_news_cancels_its_quote_then_bids():
    t, m, p, c = setup(books={"AAA": flat(), "AAPL": trend(px=PX)})
    s = t.on_tick(m, p)
    assert (s.symbol, s.side) == ("AAPL", "buy")          # crossover quote
    t.on_ack("q1", "AAPL", "buy", s.price)
    c.t += 1
    pre = m.mid_price("AAPL")
    fire(t, -0.02)
    assert t.take_cancels() == [("q1", "AAPL")]
    m.books["AAPL"] = flat(px=pre * 0.975)                # the low, no crossover
    assert_reaction_bid(run(t, m, p, c, 60), pre * 0.98)


def test_quote_acked_after_the_event_is_cancelled_too():
    t, m, p, c = setup(books={"AAA": flat(), "AAPL": trend(px=PX)})
    s = t.on_tick(m, p)
    c.t += 0.05
    fire(t, -0.02)                                        # before the ack
    assert t.take_cancels() == []
    t.on_ack("q1", "AAPL", "buy", s.price)
    c.t += 1
    t.on_tick(m, p)
    assert t.take_cancels() == [("q1", "AAPL")]


def test_sub_fee_shock_still_gets_a_reaction():
    t, m, p, c = setup()
    t.on_event("SHOCK", "Small lift", {"shock_id": "x", "affected": ["AAPL"],
                                        "prices": {"AAPL": PX * 1.004}})
    out = run(t, m, p, c, 60)
    assert not t._plans
    assert_reaction_bid(out, PX * 1.004)


def test_no_reaction_once_the_ride_has_traded():
    t, m, p, c = setup()
    fire(t, 0.062)
    fill(t, p, t.on_tick(m, p), m.best_ask("AAPL"))
    out = run(t, m, p, c, 60)
    assert [s for at, s in out if s.side == "buy" and at >= 8] == []


def test_halted_seat_only_cancels():
    t, m, p, c = setup(books={"AAA": flat(), "AAPL": trend(px=PX)})
    s = t.on_tick(m, p)
    t.on_ack("q1", "AAPL", "buy", s.price)
    c.t += 1
    p.nw *= 0.98                                          # session loss limit
    fire(t, 0.062)
    assert t.take_cancels() == [("q1", "AAPL")]
    assert run(t, m, p, c, 60) == []


def test_event_before_the_server_portfolio_still_gets_a_reaction():
    clock = Clock()
    t = MomentumTrader(clock=clock)
    m = FakeMarket({"AAA": flat(), "AAPL": flat(px=PX)})
    p = FakePortfolio(cash=trader_config.STARTING_CASH, positions={})
    starter = t.on_tick(m, p)
    assert starter.symbol == "AAA"                        # sent, never filled
    t.on_ack("s1", "AAA", "buy", starter.price)
    clock.t += 1
    fire(t, -0.02)
    out = run(t, m, p, clock, 25)                         # before the starter retry
    assert t._start_net_worth is None
    assert_reaction_bid(out, PX * 0.98)
