"""Week 4 rules: the ORCA bookbuild, shorts, and the opening auction."""

from __future__ import annotations

import asyncio
import logging

from shared.messages import SessionEvent
from team.base_trader import IPO_SHARES, IPO_STOP
from team.mean_reversion_trader import MeanReversionTrader
from team.momentum_trader import QUANTITY, MomentumTrader
from team.risk import RiskLimits
from team.venue import OwnOrders, TeamBot
from tests.test_broker import ack_all, make as make_broker, places, run as run_broker
from tests.test_event_layer import fill, fire, setup
from tests.test_momentum_trader import flat, trend

logging.disable(logging.CRITICAL)

DEAL = {"symbol": "ORCA", "offer_range": [24.0, 28.0], "shares": 4000}
SHORTS_ON = {"scenario": {"flags": {"shorts_allowed": True}}}


def opened(cls=MomentumTrader, **kw):
    """A trader past its starter on a venue that has announced shorts."""
    t, m, p, c = setup(cls, **kw)
    t.on_event("SESSION_OPEN", "open", SHORTS_ON)
    return t, m, p, c


# ── the bookbuild ─────────────────────────────────────────────────────────

def test_ipo_bids_the_seat_size_at_the_top_of_the_range():
    t, m, p, c = setup()
    t.on_tick(m, p)
    assert t.on_ipo("ORCA", 24.0, 28.0, 4000, DEAL) == (IPO_SHARES, 28.0)


def test_ipo_passes_when_cash_cannot_back_the_indication():
    t, m, p, c = setup()
    p.cash = 1_000.0
    t.on_tick(m, p)
    assert t.on_ipo("ORCA", 24.0, 28.0, 4000, DEAL) is None


def test_an_ipo_reject_is_not_an_order_reject():
    t, m, p, c = setup(positions={"AAA": 20, "ZZZ": 6})
    t._place("AAA", "sell", 5, 100.10, c.t, "test", taker=False)
    t.on_reject("IPO_REJECTED", "indication of 200 up to $28.00 needs ...")
    assert t.orders.live("AAA", "sell") is not None
    assert sum(t.rejects.values()) == 0
    assert OwnOrders().on_reject("IPO_REJECTED", "x") is None


def priced(offer=26.0, allocated=150):
    """A trader whose ORCA indication was filled at the offer."""
    t, m, p, c = setup()
    t.on_event("IPO_PRICED", "ORCA priced", {"symbol": "ORCA", "offer_price": offer})
    p.positions["ORCA"] = allocated           # the venue's portfolio update
    t.on_tick(m, p)
    c.t += 1
    return t, m, p, c


def test_the_allocation_is_booked_at_the_offer():
    t, m, p, c = priced()
    assert t._bought["ORCA"] == 150
    assert t._cost["ORCA"] == 150 * 26.0


def test_the_allocation_rests_for_the_pop_once_listed():
    t, m, p, c = priced()
    m.books["ORCA"] = flat(px=26.0)
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("ORCA", "sell", 150)
    assert s.price > 26.0 * 1.05              # offer x 1.12, inside the band
    assert not t.orders.live("ORCA", "sell").taker


def test_a_small_dip_after_listing_is_not_a_stop():
    t, m, p, c = priced()
    m.books["ORCA"] = flat(px=26.0 * 0.97)    # past the 1.5% strategy stop
    t.on_tick(m, p)
    assert t.orders.live("ORCA", "sell").purpose == "ipo exit"
    assert not t.orders.live("ORCA", "sell").taker


def test_a_listing_below_the_ipo_stop_sells_at_the_bid():
    t, m, p, c = priced()
    m.books["ORCA"] = flat(px=26.0 * (1 - IPO_STOP) - 0.1)
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("ORCA", "sell", 150)
    assert t.orders.live("ORCA", "sell").taker


# ── the opening auction ───────────────────────────────────────────────────

def test_session_open_ends_the_preopen_without_an_auction_result():
    t, m, p, c = setup()
    t.on_event("SESSION_PREOPEN", "pre-open", {})
    assert t.on_tick(m, p) is None
    t.on_event("SESSION_OPEN", "joined mid-session", SHORTS_ON)
    assert not t._preopen


def test_a_reconnect_does_not_inherit_the_preopen():
    t, m, p, c = setup()
    t.on_event("SESSION_PREOPEN", "pre-open", {})
    t.on_connect()
    assert not t._preopen


def test_the_bot_forwards_session_open_to_the_trader(monkeypatch):
    import trader.config as cfg
    monkeypatch.setattr(cfg, "TEAM_ID", "me")
    t, m, p, c = setup()
    t.on_event("SESSION_PREOPEN", "pre-open", {})
    bot = TeamBot(t)
    asyncio.run(bot._on_session_event(SessionEvent(
        event="SESSION_OPEN", message="open", data=SHORTS_ON)))
    assert not t._preopen
    assert t._shorts_allowed


# ── momentum shorts ───────────────────────────────────────────────────────

def test_momentum_offers_a_short_on_a_downtrend_when_flat():
    t, m, p, c = opened(books={"AAA": flat(), "DN": trend(step=-0.001)})
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("DN", "sell", QUANTITY)
    assert s.price >= m.best_ask("DN") - 0.01      # rests above the bid


def test_no_short_before_the_venue_allows_it():
    t, m, p, c = setup(books={"AAA": flat(), "DN": trend(step=-0.001)})
    assert t.on_tick(m, p) is None


def test_meanrev_never_sells_below_flat():
    t, m, p, c = opened(MeanReversionTrader)
    assert t.sellable("AAPL", allow_short=True) == 0


def test_a_short_is_no_bigger_than_a_long_may_be():
    t, m, p, c = opened()
    t.on_tick(m, p)
    by_concentration = int(RiskLimits().max_symbol * 300_000.0 // 195.0)
    assert t.sellable("AAPL", allow_short=True) == min(
        MomentumTrader.MAX_SHORT, by_concentration)
    p.positions["AAPL"] = -by_concentration
    t._sync_ledger(p, c.t)
    assert t.sellable("AAPL", allow_short=True) == 0


def test_shorts_together_stay_inside_the_gross_limit():
    t, m, p, c = opened(books={"AAA": flat(), "AAPL": flat(px=195.0),
                               "BIG": flat(px=500.0)})
    p.positions["BIG"] = -120                 # $60k short: the 20% gross line
    t.on_tick(m, p)
    assert t.sellable("AAPL", allow_short=True) == 0


def test_the_index_future_is_never_ordered(monkeypatch):
    import trader.config as cfg
    from shared.messages import BookSnapshot
    monkeypatch.setattr(cfg, "TEAM_ID", "me")
    t, m, p, c = opened(books={"AAA": flat(), "ARENA10": trend(step=-0.001)})
    TeamBot(t)._peek(BookSnapshot(
        symbol="ARENA10", bids=[], asks=[], mid_price=100.0, spread=0.1,
        asset_type="future").model_dump_json())
    assert t.on_tick(m, p) is None            # no crossover short in it
    fire(t, -0.02, symbol="ARENA10")
    c.t += 20
    assert t.on_tick(m, p) is None            # and no event reaction


def test_a_bad_news_fade_shorts_from_flat_and_buys_back():
    t, m, p, c = opened(books={"AAA": flat(), "AAPL": flat(px=195.0)})
    fire(t, -0.06)
    s = t.on_tick(m, p)
    assert (s.symbol, s.side) == ("AAPL", "sell")
    assert s.quantity > 0
    fill(t, p, s)
    assert t._shorted["AAPL"] == s.quantity
    assert p.positions["AAPL"] < 0


def test_a_short_that_runs_against_us_is_covered_at_the_ask():
    t, m, p, c = opened(books={"AAA": flat(), "UP": flat()})
    t.on_fill("sell", "UP", 5, 100.0)
    p.positions["UP"] = -5
    c.t += 2
    m.books["UP"] = flat(px=100.0 * (1 + RiskLimits().stop_loss) + 0.1)
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("UP", "buy", 5)
    assert t.orders.live("UP", "buy").taker
    t.on_fill("buy", "UP", 5, s.price)
    assert "UP" not in t._shorted


def test_a_short_sale_rule_reject_ends_shorting_in_that_name():
    t, m, p, c = opened(books={"AAA": flat(), "DN": trend(step=-0.001)})
    s = t.on_tick(m, p)
    t.on_reject("SSR_RESTRICTED", "DN is under the short-sale rule")
    assert t.sellable(s.symbol, allow_short=True) == 0


def test_no_shorts_in_a_new_listing():
    t, m, p, c = opened()
    t.on_event("IPO_PRICED", "ORCA priced", {"symbol": "ORCA", "offer_price": 26.0})
    assert t.sellable("ORCA", allow_short=True) == 0


# ── broker ────────────────────────────────────────────────────────────────

def test_broker_shorts_no_further_than_its_inventory_cap():
    from team.broker import MAX_INVENTORY
    b, sent, _ = make_broker(positions={"AAPL": -MAX_INVENTORY + 25},
                             flags={"shorts_allowed": True})
    run_broker(b.quote_symbol("AAPL"))
    assert sum(m.quantity for m in places(sent, "sell")) == 25


def test_broker_stays_out_of_a_new_listing_at_first():
    b, sent, now = make_broker(prices={"AAPL": 100.0, "ORCA": 26.0},
                               flags={"shorts_allowed": True})
    run_broker(b._on_session_event(SessionEvent(
        event="IPO_LISTED", message="ORCA is now trading",
        data={"symbol": "ORCA", "offer_price": 26.0})))
    run_broker(b.quote_symbol("ORCA"))
    assert places(sent) == []


def test_a_cancel_that_lost_the_race_to_a_fill_does_not_spend_the_budget():
    from shared.messages import ErrorMsg
    from team.broker import REJECT_BUDGET
    b, sent, _ = make_broker()
    for i in range(REJECT_BUDGET):
        run_broker(b._peek(ErrorMsg(
            code="ORDER_NOT_FOUND",
            message=f"Order 'x{i}' not found or not owned by me",
        ).model_dump_json()))
    run_broker(b.quote_symbol("AAPL"))
    assert places(sent)


def test_a_trader_cancel_race_does_not_spend_the_budget():
    t, m, p, c = setup()
    for _ in range(12):
        t.on_reject("ORDER_NOT_FOUND", "Order 'zz' not found or not owned by me")
    assert not t._stopped


# ── broker: never cross, smaller inventory after an event ─────────────────

def _book(b, symbol, bid, ask):
    from shared.messages import BookSnapshot
    run_broker(b._peek(BookSnapshot(
        symbol=symbol, bids=[[bid, 10]], asks=[[ask, 10]],
        mid_price=(bid + ask) / 2, spread=ask - bid).model_dump_json()))


def test_broker_bids_never_cross_the_ask():
    # The venue's mark (100) sits above the book (99.40/99.50): Session 3's
    # NFLX, where every bid around the mark took the offer as a taker.
    b, sent, _ = make_broker(prices={"AAPL": 100.0}, flags={"shorts_allowed": True})
    _book(b, "AAPL", 99.40, 99.50)
    run_broker(b.quote_symbol("AAPL"))
    assert max(m.price for m in places(sent, "buy")) <= 99.49


def test_broker_asks_never_cross_the_bid():
    b, sent, _ = make_broker(prices={"AAPL": 100.0}, flags={"shorts_allowed": True})
    _book(b, "AAPL", 100.60, 100.70)
    run_broker(b.quote_symbol("AAPL"))
    assert min(m.price for m in places(sent, "sell")) >= 100.61


def test_after_an_event_the_broker_holds_less_of_that_name():
    from team.broker import EVENT_MAX_INVENTORY
    b, sent, now = make_broker(prices={"AAPL": 100.0, "NVDA": 160.0},
                               positions={"NVDA": EVENT_MAX_INVENTORY - 10})
    run_broker(b._on_session_event(SessionEvent(
        event="CALENDAR_EVENT", message="NVDA -3.8%",
        data={"symbol": "NVDA", "affected": ["NVDA"], "ramp_ticks": 15})))
    now[0] = 100.0                            # past the pause
    run_broker(b.quote_symbol("NVDA"))
    run_broker(b.quote_symbol("AAPL"))
    assert sum(m.quantity for m in places(sent, "buy") if m.symbol == "NVDA") == 10
    assert sum(m.quantity for m in places(sent, "buy") if m.symbol == "AAPL") == 60


def test_a_market_wide_print_caps_every_name():
    from team.broker import EVENT_MAX_INVENTORY
    b, sent, now = make_broker(positions={"AAPL": EVENT_MAX_INVENTORY})
    run_broker(b._on_session_event(SessionEvent(
        event="CALENDAR_EVENT", message="econ -1.2%",
        data={"market_wide": True, "ramp_ticks": 15})))
    now[0] = 100.0
    run_broker(b.quote_symbol("AAPL"))
    assert places(sent, "buy") == []
    assert places(sent, "sell")               # still unwinds


# ── broker: inventory skew ────────────────────────────────────────────────

def test_skew_is_zero_flat_and_one_width_at_the_limit():
    from team.broker import inventory_skew
    assert inventory_skew(0, 150, 0.30) == 0.0
    assert inventory_skew(150, 150, 0.30) == -0.30       # long: quote lower
    assert inventory_skew(-75, 150, 0.30) == 0.15        # short: quote higher
    assert inventory_skew(400, 150, 0.30) == -0.30       # capped at one width


def _quotes(positions):
    b, sent, _ = make_broker(prices={"AAPL": 100.0}, positions=positions,
                             flags={"shorts_allowed": True})
    run_broker(b.quote_symbol("AAPL"))
    return (max(m.price for m in places(sent, "buy")),
            min(m.price for m in places(sent, "sell")))


def test_a_long_broker_quotes_lower_to_sell_down():
    flat_bid, flat_ask = _quotes({})
    long_bid, long_ask = _quotes({"AAPL": 100})
    assert long_bid < flat_bid and long_ask < flat_ask


def test_a_short_broker_quotes_higher_to_buy_back():
    flat_bid, flat_ask = _quotes({})
    short_bid, short_ask = _quotes({"AAPL": -100})
    assert short_bid > flat_bid and short_ask > flat_ask


def test_a_fill_that_moves_the_position_a_quote_size_requotes():
    from broker.config import QUOTE_SIZE
    b, sent, _ = make_broker(prices={"AAPL": 100.0}, flags={"shorts_allowed": True})
    run_broker(b.quote_symbol("AAPL"))
    centre = b.quote_centre("AAPL")
    b.state.positions["AAPL"] = QUOTE_SIZE - 1
    assert not b.needs_requote("AAPL", centre)
    b.state.positions["AAPL"] = QUOTE_SIZE
    assert b.needs_requote("AAPL", centre)


def _portfolio(b, positions):
    from shared.messages import PortfolioUpdate
    b._on_portfolio(PortfolioUpdate(
        team_id="me", cash=300_000.0, positions=positions, realized_pnl=0.0,
        unrealized_pnl=0.0, total_fees_paid=0.0, net_worth=300_000.0))


def test_the_opening_grant_is_not_inventory_to_skew_away():
    b, sent, _ = make_broker(prices={"AAPL": 100.0}, flags={"shorts_allowed": True})
    _portfolio(b, {"AAPL": 20})               # the shares granted at the open
    run_broker(b.quote_symbol("AAPL"))
    granted = (max(m.price for m in places(sent, "buy")),
               min(m.price for m in places(sent, "sell")))
    assert granted == _quotes({})             # quoted as if flat


def test_inventory_is_measured_from_the_opening_position():
    b, sent, _ = make_broker(prices={"AAPL": 100.0}, flags={"shorts_allowed": True})
    _portfolio(b, {"AAPL": 20})
    _portfolio(b, {"AAPL": 120})              # later fills: 100 bought
    run_broker(b.quote_symbol("AAPL"))
    traded = (max(m.price for m in places(sent, "buy")),
              min(m.price for m in places(sent, "sell")))
    assert traded == _quotes({"AAPL": 100})


def test_our_own_resting_ask_does_not_pin_our_next_bid():
    b, sent, _ = make_broker(prices={"AAPL": 100.0}, flags={"shorts_allowed": True})
    run_broker(b.quote_symbol("AAPL"))
    own_ask = min(m.price for m in places(sent, "sell"))
    ack_all(b, sent)
    from shared.messages import BookSnapshot
    run_broker(b._peek(BookSnapshot(               # our ask, then someone else's
        symbol="AAPL", bids=[[99.80, 10]], asks=[[own_ask, 10], [101.00, 5]],
        mid_price=100.0, spread=own_ask - 99.80).model_dump_json()))
    sent.clear()
    b.state.exchange_prices["AAPL"] = 100.60          # the mark moves up
    run_broker(b.quote_symbol("AAPL"))
    top_bid = max(m.price for m in places(sent, "buy"))
    assert own_ask <= top_bid <= 100.99
