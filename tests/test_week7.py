"""Week 7: message quota, flashes, and an O(1) hot path."""

from __future__ import annotations

import asyncio
import logging
import random
import statistics

import pytest

from shared.messages import BookSnapshot, ErrorMsg, PlaceOrder, SessionEvent
from team import broker as team_broker
from team.base_trader import ORDER_BURST, ORDERS_PER_SEC, REJECT_BUDGET
from team.broker import LOOKBACK, MESSAGES_PER_SEC, QUOTE_LEVELS, volatility_spread
from team.flash import FLASH_SHARES, QUIET_SEC, entry_order, exit_price, parse_flash, start_price
from team.hotpath import CANCEL_FEE_PER_ORDER, LatencyMeter, MessageMeter
from team.mean_reversion_trader import MeanReversionTrader
from team.momentum_trader import LONG_WINDOW, SHORT_WINDOW, MomentumTrader
from team.rolling import RollingRange, RollingWindow, Sampler
from team.venue import TeamBot
from tests.test_broker import ack_all, cancelled, handle, make as make_broker, places
from tests.test_momentum_trader import Clock, FakeMarket, trend
from tests.test_rejects import ready
from tests.test_week5 import live_broker

logging.disable(logging.CRITICAL)

ORDER_QUOTA_MSG = ("Message quota exceeded — you may send 8 order/cancel messages "
                   "per tick. Slow your requoting or buy a quota increase.")
CANCEL_QUOTA_MSG = ("Message quota exceeded — you may send 8 order/cancel messages "
                    "per tick")


def flash(t, symbol="AAA", pct=0.02, revert=2):
    t.on_event("FLASH", f"{symbol} flash", {"symbol": symbol, "pct": pct,
                                           "revert_ticks": revert, "tick": 500})


# ── rolling statistics ──────────────────────────────────────────────────────

def test_rolling_window_matches_a_full_recompute_through_many_resyncs():
    rng = random.Random(7)
    w = RollingWindow(20)
    xs = []
    price = 512.37
    for _ in range(5_000):                    # five resyncs
        price *= 1 + rng.gauss(0, 0.0005)
        xs.append(price)
        w.push(price)
        tail = xs[-20:]
        assert w.mean == pytest.approx(statistics.fmean(tail), rel=1e-12)
        assert w.pstdev == pytest.approx(statistics.pstdev(tail), rel=1e-6, abs=1e-9)
    assert w.last == xs[-1] and w.full


def test_rolling_window_of_a_flat_tape_has_no_spread():
    w = RollingWindow(20, [100.0] * 50)
    assert w.mean == 100.0
    assert w.pstdev == pytest.approx(0.0, abs=1e-9)


def test_rolling_range_matches_max_and_min_of_the_window():
    rng = random.Random(3)
    r = RollingRange(LOOKBACK)
    xs = []
    for _ in range(2_000):
        x = 100 + rng.gauss(0, 1)
        xs.append(x)
        r.push(x)
        tail = xs[-LOOKBACK:]
        assert (r.high, r.low) == (max(tail), min(tail))


def test_live_rolling_windows_give_the_same_averages_as_the_history():
    mids = trend(n=LONG_WINDOW + 30)
    offline = MomentumTrader(clock=Clock())
    clock = Clock()
    live = MomentumTrader(clock=clock)
    for x in mids:                           # one snapshot every 0.5 s
        live.on_book("AAA", x)
        clock.t += 0.5
    market = FakeMarket({"AAA": mids})
    for size in (SHORT_WINDOW, LONG_WINDOW):
        assert live._rolling(market, "AAA", size).mean == pytest.approx(
            offline._rolling(market, "AAA", size).mean, rel=1e-12)


def test_mean_reversion_reads_the_live_window():
    clock = Clock()
    t = MeanReversionTrader(clock=clock)
    for x in [100.0] * 19 + [97.0]:
        t.on_book("AAA", x)
        clock.t += 0.5
    w = t._rolling(None, "AAA", 20)          # no market needed once fed live
    assert w.full and w.last == 97.0
    assert w.mean == pytest.approx(statistics.fmean([100.0] * 19 + [97.0]))


def test_a_fast_feed_fills_the_windows_at_the_tested_pace():
    # Week 7 snapshots every 0.2 s; the windows were sized at 0.5 s.
    clock = Clock()
    t = MomentumTrader(clock=clock)
    for i in range(500):                     # 100 s of a 0.2 s feed
        t.on_book("AAA", 100.0 + i)
        clock.t += 0.2
    w = t._rolling(None, "AAA", LONG_WINDOW)
    assert w.full
    spacing = (w.last - w.mean) / ((LONG_WINDOW - 1) / 2)   # mids rise 1 per 0.2 s
    assert spacing == pytest.approx(2.5, rel=0.05)         # one mid per 0.5 s


def test_sampler_keeps_one_value_per_period_on_average():
    s = Sampler(0.5)
    taken = sum(s.due("A", i * 0.2) for i in range(1000))  # 200 s
    assert taken == pytest.approx(400, abs=2)


# ── flash pricing ───────────────────────────────────────────────────────────

def test_parse_flash_reads_fractions_and_percent_units():
    assert parse_flash({"symbol": "AAA", "pct": 0.02, "revert_ticks": 2}) == ("AAA", 0.02, 2)
    assert parse_flash({"symbol": "AAA", "pct": -2.0, "revert_ticks": 2}) == ("AAA", -0.02, 2)
    assert parse_flash({"symbol": "AAA", "pct": 0.0}) is None
    assert parse_flash({"pct": 0.02}) is None


def test_a_jump_up_is_offered_half_way_into_it_as_a_maker():
    side, price, take = entry_order(0.02, 100.0, 99.95, 100.05, taker=0.004, rebate=0.0)
    assert (side, price, take) == ("sell", 101.0, False)


def test_a_drop_is_bid_half_way_down_as_a_maker():
    side, price, take = entry_order(-0.02, 100.0, 99.95, 100.05, taker=0.004, rebate=0.0)
    assert (side, price, take) == ("buy", 99.0, False)


def test_the_entry_takes_only_when_the_move_pays_the_taker_fee():
    # The book has already jumped 1.6%: half way in is 101, the bid is 101.55.
    assert entry_order(0.02, 100.0, 101.55, 101.65, taker=0.004, rebate=0.0) == \
        ("sell", 101.55, True)
    # At a 2% taker fee taking loses money: rest at the ask instead.
    assert entry_order(0.02, 100.0, 101.55, 101.65, taker=0.02, rebate=0.0) == \
        ("sell", 101.65, False)
    assert entry_order(-0.02, 100.0, 98.35, 98.45, taker=0.02, rebate=0.0) == \
        ("buy", 98.35, False)


def test_a_resting_entry_never_crosses_the_book():
    for pct in (0.015, 0.025, -0.015, -0.025):
        for bid, ask in ((99.95, 100.05), (100.9, 101.0), (98.9, 99.0)):
            side, price, take = entry_order(pct, 100.0, bid, ask, taker=1.0, rebate=0.0)
            assert not take
            assert price > bid if side == "sell" else price < ask


def test_the_exit_rests_at_the_pre_flash_mid_without_crossing():
    assert exit_price("buy", 100.0, 99.0, 99.5) == 99.49
    assert exit_price("buy", 100.0, 101.0, 101.1) == 100.0
    assert exit_price("sell", 100.0, 100.2, 100.3) == 100.21


# ── the trader seats on a flash ─────────────────────────────────────────────

def test_a_flash_up_sends_a_sell_at_once():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    flash(t, pct=0.02)
    s = t.flash_signal()
    assert (s.symbol, s.side, s.quantity, s.price) == ("AAA", "sell", FLASH_SHARES, 101.0)
    assert t.orders.live("AAA", "sell").purpose == "flash entry"
    assert t.flash_log.reactions == [0.0]
    assert t.flash_signal() is None            # one entry per flash


def test_a_flash_down_sends_a_bid():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    flash(t, pct=-0.02)
    s = t.flash_signal()
    assert (s.side, s.price) == ("buy", 99.0)


def test_the_tick_loop_sends_the_entry_if_the_event_path_did_not():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    flash(t, pct=0.02)
    c.t += 0.1
    s = t.on_tick(m, p)
    assert (s.side, s.price, s.quantity) == ("sell", 101.0, FLASH_SHARES)


def test_the_flash_entry_rests_beside_an_existing_quote():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    quote = t._place("AAA", "sell", 5, 100.05, c.t, "quote", taker=False, purpose="quote")
    assert quote is not None
    c.t += 1
    flash(t, pct=0.02)
    assert t.flash_signal() is not None


def test_an_unfilled_entry_is_cancelled_once_the_flash_is_over():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    flash(t, pct=0.02, revert=2)
    s = t.flash_signal()
    t.on_ack("f1", "AAA", "sell", s.price)
    c.t += 3.5                                 # revert_ticks + 1 = 3 s
    t.on_tick(m, p)
    assert [oid for oid, _ in t.take_cancels()] == ["f1"]


def test_a_filled_entry_is_closed_at_the_pre_flash_mid():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    flash(t, pct=0.02)
    s = t.flash_signal()
    t.on_ack("f1", "AAA", "sell", s.price)
    t.on_fill("sell", "AAA", FLASH_SHARES, s.price)
    assert t._flashes["AAA"].filled == FLASH_SHARES
    c.t += 0.5
    exit_ = t.on_tick(m, p)
    assert (exit_.side, exit_.quantity) == ("buy", FLASH_SHARES)
    assert exit_.price == 100.0                # the touch is 99.95 / 100.05
    assert t.orders.live("AAA", "buy").purpose == "flash exit"


def test_the_strategy_and_stop_loss_leave_a_flashed_name_alone():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    t._bought["AAA"], t._cost["AAA"] = 10, 1_000.0      # avg entry 100
    m.books["AAA"] = [97.5] * (LONG_WINDOW + 5)          # -2.5%: past the 1.5% stop
    flash(t, pct=-0.025)
    assert t._in_window("AAA", c.t)
    assert t._stop_loss(m, p, c.t) is None
    t._flashes.clear()
    c.t += QUIET_SEC + 1
    assert not t._in_window("AAA", c.t)
    stop = t._stop_loss(m, p, c.t)
    assert stop is not None and stop.side == "sell"


def test_team_bot_sends_the_flash_entry_the_moment_the_event_arrives(monkeypatch):
    import trader.config as cfg
    monkeypatch.setattr(cfg, "TEAM_ID", "me")
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    bot = TeamBot(t)
    sent = []

    async def place(signal):
        sent.append(signal)

    bot._place_order = place
    asyncio.run(bot._on_session_event(SessionEvent(
        event="FLASH", message="AAA flash",
        data={"symbol": "AAA", "pct": 0.02, "revert_ticks": 2, "tick": 500})))
    assert [(s.symbol, s.side) for s in sent] == [("AAA", "sell")]


# ── the message quota ───────────────────────────────────────────────────────

def test_the_team_stays_inside_eight_messages_a_tick():
    # broker + two trader seats, sustained and in a burst (venue burst is 24)
    assert MESSAGES_PER_SEC + 2 * ORDERS_PER_SEC <= 8.0
    assert MESSAGES_PER_SEC + 2 * ORDER_BURST + (MESSAGES_PER_SEC + 2 * ORDERS_PER_SEC) <= 24
    assert QUOTE_LEVELS == 1


def test_quota_answers_do_not_stop_the_seat():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    for _ in range(REJECT_BUDGET + 5):
        t._place("AAA", "sell", 1, 100.10, c.t, "test", taker=False, second=True)
        t.on_reject("RATE_LIMITED", ORDER_QUOTA_MSG)
    assert not t._stopped
    assert t.messages.rate_limited == REJECT_BUDGET + 5
    assert t._budget_rejects() == 0
    assert t.orders.live("AAA", "sell") is None          # refused orders dropped


def test_a_refused_cancel_is_sent_again_after_the_pause():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    s = t._place("AAA", "sell", 5, 100.10, c.t, "test", taker=False)
    t.on_ack("k1", "AAA", "sell", s.price)
    t._cancel(t.orders.live("AAA", "sell"), c.t)
    assert [oid for oid, _ in t.take_cancels()] == ["k1"]
    t.on_reject("RATE_LIMITED", CANCEL_QUOTA_MSG)
    assert t.orders.live("AAA", "sell").cancel_sent_at is None
    assert t.take_cancels() == []                        # paused for a tick
    c.t += 1.1
    assert [oid for oid, _ in t.take_cancels()] == ["k1"]


def test_message_meter_reports_the_cancel_bill():
    meter = MessageMeter()
    for _ in range(40):
        meter.order()
    for _ in range(10):
        meter.cancel()
    meter.not_found()
    meter.limited()
    assert meter.messages == 50
    assert meter.limited_share == pytest.approx(0.02)
    assert meter.cancel_bill == pytest.approx(10 * CANCEL_FEE_PER_ORDER)
    assert "$0.50" in meter.report()


def test_hot_path_meters_record_every_tick():
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    before = t.tick_time.count
    for _ in range(5):
        c.t += 0.1
        t.on_tick(m, p)
    assert t.tick_time.count == before + 5
    assert t.tick_gap.count >= 5
    meter = LatencyMeter("x")
    for us in range(1, 101):
        meter.add(us / 1e6)
    assert meter.percentile(99) == pytest.approx(99e-6)
    t.session_summary()                                  # logs, never raises


# ── the broker ──────────────────────────────────────────────────────────────

def test_the_broker_quotes_one_level_a_side():
    b, sent, _ = make_broker(positions={"AAPL": 25})
    asyncio.run(b.quote_symbol("AAPL"))
    assert [m.quantity for m in places(sent, "buy")] == [10]
    assert [m.quantity for m in places(sent, "sell")] == [10]


def test_broker_width_from_the_rolling_range_matches_the_history():
    b, sent, now = make_broker()
    rng = random.Random(5)
    marks = []
    for _ in range(LOOKBACK * 3):
        px = 100 + rng.gauss(0, 0.2)
        marks.append(px)
        asyncio.run(b._peek(BookSnapshot(symbol="AAPL", bids=[], asks=[], mid_price=px,
                                         spread=0.0, ref_price=px).model_dump_json()))
        now[0] += 0.5
    b.state.exchange_prices["AAPL"] = marks[-1]
    expected = volatility_spread(marks[-1], marks)
    assert expected is not None
    assert b._spread("AAPL") == pytest.approx(expected)


def test_broker_quota_answers_are_not_errors():
    b, sent, _ = make_broker()
    for _ in range(team_broker.REJECT_BUDGET + 3):
        asyncio.run(b._peek(ErrorMsg(code="RATE_LIMITED",
                                     message=ORDER_QUOTA_MSG).model_dump_json()))
    assert b._errors == 0
    assert b.messages.rate_limited == team_broker.REJECT_BUDGET + 3
    asyncio.run(b.quote_symbol("AAPL"))
    assert places(sent, "buy")                           # still quoting


def test_broker_offers_into_a_flash_up_after_pulling_its_quotes():
    b, sent, _ = make_broker(positions={"AAPL": 20})
    b._centre["AAPL"] = 100.0
    handle(b, SessionEvent(event="FLASH", message="AAPL flash",
                           data={"symbol": "AAPL", "pct": 0.02, "revert_ticks": 2}))
    sells = places(sent, "sell")
    assert [(m.price, m.quantity) for m in sells] == [(101.0, 10)]
    assert b._paused("AAPL")
    assert b.flash_log.reactions


def test_broker_bids_into_a_flash_down():
    b, sent, _ = make_broker()
    b._centre["AAPL"] = 100.0
    handle(b, SessionEvent(event="FLASH", message="AAPL flash",
                           data={"symbol": "AAPL", "pct": -0.02, "revert_ticks": 2}))
    assert [(m.price, m.quantity) for m in places(sent, "buy")] == [(99.0, 10)]


# ── broker: exposure from the opening grant, and quotes that stay put ──────

def _send(b, side, symbol, qty, price=100.0):
    return asyncio.run(b._send(PlaceOrder(order_type="limit", team_id="me",
                                          symbol=symbol, side=side,
                                          price=price, quantity=qty)))


def test_broker_bids_although_its_opening_grant_is_most_of_its_equity(monkeypatch):
    # Nov 6: the grant was 78% of equity and every bid was dropped unsent.
    b, sent, _ = live_broker(monkeypatch, cash=20_000,
                             positions={"AAPL": 100, "AMD": 100})
    assert _send(b, "buy", "AAPL", 10)
    assert places(sent, "buy")


def test_broker_traded_exposure_still_stops_at_a_fifth_of_equity(monkeypatch, caplog):
    b, sent, _ = live_broker(monkeypatch, cash=20_000,
                             positions={"AAPL": 100, "AMD": 100})
    assert _send(b, "buy", "AAPL", 50)                   # $5k of $8k
    with caplog.at_level(logging.INFO, logger="team.broker"):
        assert not _send(b, "buy", "AMD", 40)            # $9k: past it
    assert len(places(sent, "buy")) == 1
    assert "traded gross" in caplog.text                 # said why, in the log


def test_broker_keeps_a_quote_that_has_not_changed():
    b, sent, _ = make_broker()
    asyncio.run(b.quote_symbol("AAPL"))
    ack_all(b, sent)
    asyncio.run(b._requote("AAPL", b.quote_centre("AAPL")))
    assert not cancelled(sent) and not places(sent)      # no billed cancel


def test_broker_replaces_a_quote_whose_price_moved():
    b, sent, _ = make_broker()
    asyncio.run(b.quote_symbol("AAPL"))
    first = ack_all(b, sent)
    asyncio.run(b._requote("AAPL", b.quote_centre("AAPL") + 1.0))
    assert cancelled(sent) == set(first)
    assert places(sent, "buy")


def test_a_bid_the_risk_check_refused_does_not_trigger_a_requote(monkeypatch):
    # Local week-7 run: a refused bid read as a filled one, so the broker
    # requoted every pass and paid a cancel fee each time its ask moved a cent.
    b, sent, _ = make_broker()
    async def refuse_buys(msg):
        if isinstance(msg, PlaceOrder) and msg.side == "buy":
            return False
        sent.append(msg)
        return True
    b._send = refuse_buys
    b.state.positions["AAPL"] = 20
    asyncio.run(b.quote_symbol("AAPL"))
    ack_all(b, sent)
    assert not b.needs_requote("AAPL", b.quote_centre("AAPL"))


def test_broker_stops_shorting_a_name_the_venue_will_not_lend():
    b, sent, _ = make_broker(positions={"AAPL": 0}, flags={"shorts_allowed": True})
    assert b._ask_room("AAPL") > 0
    asyncio.run(b._on_error(ErrorMsg(
        code="BORROW_UNAVAILABLE",
        message="No borrow: 1195 of 1200 AAPL shares already short across the market")))
    assert b._ask_room("AAPL") == 0
    assert b._ask_room("AMD") > 0


def test_the_venue_start_price_is_the_pre_flash_level():
    assert start_price({"symbol": "AAA", "pct": -0.02, "start_price": 101.25}) == 101.25
    assert start_price({"symbol": "AAA", "pct": -0.02}) is None
