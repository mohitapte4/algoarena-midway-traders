"""The broker must send nothing the week-3 venue rejects."""

from __future__ import annotations

import asyncio
import itertools
import logging

import pytest

import broker.config as _config
from shared.messages import (CancelOrder, ErrorMsg, OrderAck, PlaceOrder,
                             SessionEvent, TradeExecution)
from team import broker as team_broker
from team.broker import (MAX_INVENTORY, MESSAGES_PER_SEC, REJECT_BUDGET,
                         MyBroker, TeamBrokerBot)

logging.disable(logging.CRITICAL)

ME = _config.TEAM_ID
_ids = itertools.count(1)


def run(coro):
    return asyncio.run(coro)


def event(name, **data):
    return SessionEvent(event=name, message="", data=data)


def make(positions=None, prices=None, flags=None):
    """A broker past a long-only open, with a recording socket."""
    b = TeamBrokerBot(MyBroker(), "ws://test")
    sent = []

    async def send(msg):
        sent.append(msg)

    b._send = send
    now = [0.0]
    b._clock = lambda: now[0]
    run(b._on_session_event(event(
        "SESSION_OPEN",
        scenario={"flags": {"shorts_allowed": False} if flags is None else flags})))
    b.state.positions = dict(positions or {})
    b.state.exchange_prices.update(prices or {"AAPL": 100.0})
    return b, sent, now


def places(sent, side=None):
    return [m for m in sent if isinstance(m, PlaceOrder)
            and (side is None or m.side == side)]


def cancelled(sent):
    return {m.order_id for m in sent if isinstance(m, CancelOrder)}


def ack_all(b, sent):
    """Ack every order placed so far; returns order id -> PlaceOrder."""
    ids = {}
    for m in places(sent):
        oid = f"o{next(_ids)}"
        ids[oid] = m
        b._on_order_ack(OrderAck(order_id=oid, team_id=ME, symbol=m.symbol,
                                 side=m.side, price=m.price,
                                 quantity=m.quantity))
    sent.clear()
    return ids


def test_flat_inventory_quotes_bids_only():
    b, sent, _ = make()
    run(b.quote_symbol("AAPL"))
    assert len(places(sent, "buy")) == _config.QUOTE_LEVELS
    assert places(sent, "sell") == []


def test_asks_never_offer_more_than_the_inventory():
    b, sent, _ = make(positions={"AAPL": 25})
    run(b.quote_symbol("AAPL"))
    assert [m.quantity for m in places(sent, "sell")] == [10, 15]
    assert [m.quantity for m in places(sent, "buy")] == [10, 20, 30]


def test_a_week_that_unlocks_shorts_quotes_full_asks_while_flat():
    b, sent, _ = make(flags={"shorts_allowed": True})
    run(b.quote_symbol("AAPL"))
    assert [m.quantity for m in places(sent, "sell")] == [10, 20, 30]


def test_an_open_without_the_flag_stays_long_only():
    b, sent, _ = make(flags={})
    run(b.quote_symbol("AAPL"))
    assert places(sent, "sell") == []


def test_a_missing_ask_with_nothing_to_sell_does_not_churn_the_bids():
    b, sent, _ = make()
    run(b.quote_symbol("AAPL"))
    ack_all(b, sent)
    run(b.quote_symbol("AAPL"))
    assert sent == []


def test_a_filled_quote_is_not_cancelled_on_requote():
    b, sent, _ = make(positions={"AAPL": 25})
    run(b.quote_symbol("AAPL"))
    ids = ack_all(b, sent)
    top_bid = next(i for i, m in ids.items() if m.side == "buy")
    b._on_trade(TradeExecution(
        trade_id="t1", symbol="AAPL", price=ids[top_bid].price, quantity=10,
        buyer_id=ME, seller_id="someone", aggressor="sell", fee=0.0))
    b.state.exchange_prices["AAPL"] = 101.0          # the market moved
    run(b.quote_symbol("AAPL"))
    assert cancelled(sent) == set(ids) - {top_bid}
    assert places(sent)


def test_a_partly_filled_quote_is_still_cancelled():
    b, sent, _ = make()
    run(b.quote_symbol("AAPL"))
    ids = ack_all(b, sent)
    top_bid = next(iter(ids))
    b._on_trade(TradeExecution(
        trade_id="t1", symbol="AAPL", price=ids[top_bid].price, quantity=4,
        buyer_id=ME, seller_id="someone", aggressor="sell", fee=0.0))
    b.state.exchange_prices["AAPL"] = 101.0
    run(b.quote_symbol("AAPL"))
    assert cancelled(sent) == set(ids)


def test_a_quote_filled_as_taker_before_its_ack_is_not_cancelled():
    b, sent, _ = make()
    run(b.quote_symbol("AAPL"))
    top_bid = places(sent, "buy")[0]
    b._on_trade(TradeExecution(
        trade_id="t1", symbol="AAPL", price=top_bid.price - 0.05,
        quantity=top_bid.quantity, buyer_id=ME, seller_id="someone",
        aggressor="buy", fee=0.0))
    ids = ack_all(b, sent)
    first = next(iter(ids))
    b.state.exchange_prices["AAPL"] = 101.0
    run(b.quote_symbol("AAPL"))
    assert cancelled(sent) == set(ids) - {first}


def test_a_quote_pulled_by_self_trade_prevention_is_not_cancelled():
    b, sent, _ = make()
    run(b.quote_symbol("AAPL"))
    ids = ack_all(b, sent)
    oid, quote = list(ids.items())[1]
    run(b._peek(ErrorMsg(
        code="STP_CANCEL",
        message=(f"Your resting buy {quote.quantity} AAPL @ {quote.price} was "
                 f"cancelled by self-trade prevention (your own order "
                 f"crossed it)"),
    ).model_dump_json()))
    b.state.exchange_prices["AAPL"] = 101.0
    run(b.quote_symbol("AAPL"))
    assert cancelled(sent) == set(ids) - {oid}


def test_order_not_found_drops_the_id():
    b, sent, _ = make()
    run(b.quote_symbol("AAPL"))
    ids = ack_all(b, sent)
    gone = list(ids)[1]
    run(b._peek(ErrorMsg(
        code="ORDER_NOT_FOUND",
        message=f"Order {gone!r} not found or not owned by {ME}",
    ).model_dump_json()))
    b.state.exchange_prices["AAPL"] = 101.0
    run(b.quote_symbol("AAPL"))
    assert cancelled(sent) == set(ids) - {gone}


def test_calendar_event_pulls_that_symbol_until_three_ramps_pass():
    b, sent, now = make(prices={"AAPL": 100.0, "NVDA": 500.0})
    run(b.quote_symbol("AAPL"))
    run(b.quote_symbol("NVDA"))
    ids = ack_all(b, sent)
    run(b._on_session_event(event(
        "CALENDAR_EVENT", kind="earnings", symbol="NVDA", market_wide=False,
        affected=["NVDA"], ramp_ticks=15)))
    assert cancelled(sent) == {i for i, m in ids.items() if m.symbol == "NVDA"}

    sent.clear()
    b.state.exchange_prices["AAPL"] = 101.0
    now[0] = 44.0
    run(b.quote_symbol("NVDA"))
    run(b.quote_symbol("AAPL"))
    assert {m.symbol for m in places(sent)} == {"AAPL"}

    sent.clear()
    now[0] = 46.0
    run(b.quote_symbol("NVDA"))
    assert places(sent, "buy")


@pytest.mark.xfail(strict=True, reason="the event cancel races the fill behind it")
def test_a_quote_filled_just_behind_the_event_is_not_cancelled():
    """The venue sends the event and then the fill it set off; the broker
    must book the fill before it pulls the symbol's quotes, or the cancel
    for the filled quote comes back ORDER_NOT_FOUND (week-4 run, AMZN)."""
    b, sent, _ = make(prices={"AAPL": 100.0, "NVDA": 500.0})
    run(b.quote_symbol("AAPL"))
    run(b.quote_symbol("NVDA"))
    ids = ack_all(b, sent)
    nvda = {i for i, m in ids.items() if m.symbol == "NVDA"}
    top_bid = max(nvda, key=lambda i: ids[i].price)    # flat: bids only
    frames = [
        event("CALENDAR_EVENT", kind="earnings", symbol="NVDA",
              market_wide=False, affected=["NVDA"],
              ramp_ticks=15).model_dump_json(),
        TradeExecution(trade_id="t1", symbol="NVDA", price=ids[top_bid].price,
                       quantity=ids[top_bid].quantity, buyer_id=ME,
                       seller_id="someone", aggressor="sell",
                       fee=0.0).model_dump_json(),
    ]

    class Socket:
        async def __aiter__(self):
            for raw in frames:
                yield raw
            await asyncio.sleep(0.2)          # the connection stays up

    async def session():
        b._ws = Socket()
        await b.listen_to_exchange()
        await b.quote_symbol("NVDA")

    run(session())
    assert cancelled(sent) == nvda - {top_bid}
    assert places(sent) == []


def test_imminent_calendar_warning_pulls_the_symbol_before_the_fire():
    b, sent, now = make(prices={"NVDA": 500.0})
    run(b.quote_symbol("NVDA"))
    ids = ack_all(b, sent)
    run(b._on_session_event(event(
        "CALENDAR", tick=300, announce_lead=120, early=False, events=[],
        imminent=[{"kind": "earnings", "symbol": "NVDA", "tick": 420}])))
    assert cancelled(sent) == set(ids)
    sent.clear()
    now[0] = 120.0 + 44.0
    run(b.quote_symbol("NVDA"))
    assert places(sent) == []


def test_session_closed_cancels_and_places_nothing():
    b, sent, _ = make(positions={"AAPL": 25})
    run(b.quote_symbol("AAPL"))
    ids = ack_all(b, sent)
    run(b._on_session_event(event("SESSION_CLOSED")))
    assert places(sent) == []
    assert cancelled(sent) == set(ids)


def test_the_reject_budget_pulls_every_quote_and_stops_quoting():
    b, sent, _ = make()
    run(b.quote_symbol("AAPL"))
    ids = ack_all(b, sent)
    rate_limited = ErrorMsg(code="RATE_LIMITED", message="slow down")
    for _ in range(REJECT_BUDGET - 1):
        run(b._peek(rate_limited.model_dump_json()))
    assert sent == []
    run(b._peek(rate_limited.model_dump_json()))
    assert cancelled(sent) == set(ids)

    sent.clear()
    b.state.exchange_prices["AAPL"] = 101.0
    run(b.quote_symbol("AAPL"))
    assert places(sent) == []


def test_every_venue_gets_a_team_broker_sharing_one_yahoo_feed(monkeypatch):
    started = []

    async def fake_run(self):
        started.append(self)

    monkeypatch.setattr(team_broker._config, "EXCHANGE_URLS",
                        ["ws://a:8765", "ws://b:8766"])
    monkeypatch.setattr(TeamBrokerBot, "run", fake_run)
    run(MyBroker()._run_venues())
    assert [type(b) for b in started] == [TeamBrokerBot, TeamBrokerBot]
    assert started[1].state.yahoo_prices is started[0].state.yahoo_prices
    assert [b._own_yahoo_feed for b in started] == [True, False]


def test_a_malformed_event_or_trade_never_reaches_the_socket_loop():
    b, sent, _ = make()
    run(b._on_session_event(event("CALENDAR", imminent=["not a dict"])))
    run(b._on_session_event(event("SHOCK", affected=7)))
    b._on_trade(TradeExecution.model_construct(
        trade_id="t1", symbol="AAPL", price=None, quantity=None,
        buyer_id=ME, seller_id="x", aggressor="sell", fee=0.0))


def test_bids_stop_at_the_inventory_cap():
    b, sent, _ = make(positions={"AAPL": MAX_INVENTORY - 15})
    run(b.quote_symbol("AAPL"))
    assert [m.quantity for m in places(sent, "buy")] == [10, 5]


def test_a_full_inventory_quotes_asks_only_and_does_not_churn():
    b, sent, _ = make(positions={"AAPL": MAX_INVENTORY})
    run(b.quote_symbol("AAPL"))
    assert places(sent, "buy") == []
    assert places(sent, "sell")
    ack_all(b, sent)
    run(b.quote_symbol("AAPL"))
    assert sent == []


def test_the_fire_cuts_short_the_warning_pause_so_the_broker_requotes_in_time():
    b, sent, now = make(prices={"NVDA": 500.0})
    run(b._on_session_event(event(
        "CALENDAR", tick=300, announce_lead=120, early=False, events=[],
        imminent=[{"kind": "earnings", "symbol": "NVDA", "tick": 420}])))
    now[0] = 30.0
    run(b._on_session_event(event(
        "CALENDAR_EVENT", kind="earnings", symbol="NVDA", market_wide=False,
        affected=["NVDA"], ramp_ticks=15)))
    sent.clear()
    now[0] = 30.0 + 44.0
    run(b.quote_symbol("NVDA"))
    assert places(sent) == []
    now[0] = 30.0 + 46.0
    run(b.quote_symbol("NVDA"))
    assert places(sent, "buy")


def test_a_new_connection_forgets_the_quotes_the_venue_swept():
    b, sent, _ = make()
    run(b.quote_symbol("AAPL"))
    ack_all(b, sent)
    assert b._live

    async def down():
        raise OSError("venue down")

    b.connect_to_exchange = down
    try:
        run(b._serve())
    except OSError:
        pass
    assert b._live == {}
    run(b.quote_symbol("AAPL"))
    assert places(sent, "buy")
    assert cancelled(sent) == set()


def test_orders_and_cancels_are_paced_inside_the_team_quota():
    b = TeamBrokerBot(MyBroker(), "ws://test")
    now = [0.0]
    sent_at = []

    class Socket:
        async def send(self, raw):
            sent_at.append(now[0])

    async def sleep(seconds):
        now[0] += seconds

    b._ws, b._clock, b._sleep = Socket(), (lambda: now[0]), sleep

    async def burst():
        for i in range(40):
            await b._send(CancelOrder(team_id=ME, order_id=f"o{i}", symbol="AAPL"))

    run(burst())
    assert len(sent_at) == 40
    for start in sent_at:
        in_one_tick = [t for t in sent_at if start <= t < start + 1.0]
        assert len(in_one_tick) <= 2 * MESSAGES_PER_SEC
    assert max(sent_at) >= (40 - MESSAGES_PER_SEC) / MESSAGES_PER_SEC - 1e-6


def test_an_order_still_waiting_when_the_session_closes_is_dropped():
    b = TeamBrokerBot(MyBroker(), "ws://test")
    sent = []

    class Socket:
        async def send(self, raw):
            sent.append(raw)

    b._ws = Socket()
    b._session_open = False
    run(b._send(PlaceOrder(team_id=ME, symbol="AAPL", side="buy",
                           order_type="limit", price=100.0, quantity=10)))
    run(b._send(CancelOrder(team_id=ME, order_id="o1", symbol="AAPL")))
    assert len(sent) == 1 and '"cancel_order"' in sent[0]
