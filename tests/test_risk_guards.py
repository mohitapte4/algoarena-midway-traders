"""Phase 3: one test per risk guard, each showing the trigger and what the seat
does about it. RISK.md has the limit table and the reasoning.

(tests/test_risk_controls.py is the course's test of the venue's own
controls, not ours.)

Run with:  make test   or   pytest tests/test_risk_guards.py -v
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from shared.messages import ErrorMsg, PlaceOrder
from team.base_trader import REJECT_BUDGET, SEAT_MAX_SHARES
from team.broker import MAX_INVENTORY, MAX_WIDEN_BPS, inventory_skew, range_spread
from team.broker import REJECT_BUDGET as BROKER_REJECT_BUDGET
from team.momentum_trader import MomentumTrader
from team.reconnect import run_with_backoff
from team.risk import (BROKER_KILL_LOSS, BROKER_SESSION_LOSS, DROP_WINDOW_SEC,
                       HARD_GROSS, MAX_DROPS, RESUME_SETTLE_SEC, TOXIC_QUOTES,
                       TOXIC_WIDEN, KillSwitch, RiskLimits, ToxicityMeter,
                       TradedCost, position_stop)
from tests.test_broker import ack_all, cancelled, event, handle, make, places, run
from tests.test_momentum_trader import FakeMarket, FakePortfolio, flat, trend
from tests.test_rejects import ready
from tests.test_week5 import live_broker

logging.disable(logging.CRITICAL)

NW = 300_000.0          # the trader fixtures' net worth


def kill_file(t, tmp_path, name: str, text: str = "") -> None:
    """Point the seat's switch at tmp_path and drop a kill file there."""
    t.kill.kill_dir = tmp_path
    t.kill._next_poll = -1e18
    (tmp_path / name).write_text(text)


def send(b, side, symbol, qty, price=100.0) -> bool:
    return asyncio.run(b._send(PlaceOrder(order_type="limit", team_id="me",
                                          symbol=symbol, side=side,
                                          price=price, quantity=qty)))


# ── 1-4. Hard limits, checked before an order is sent ──────────────────────

def test_1_trader_buy_is_cut_at_the_per_symbol_share_cap():
    t, m, p, c = ready(positions={"AAA": SEAT_MAX_SHARES - 3 + 5, "ZZZ": 6})
    held = p.positions["AAA"]
    s = t._place("AAA", "buy", 50, 100.05, c.t, "test", taker=True)
    assert s.quantity == SEAT_MAX_SHARES - held       # never past 200 a name


def test_2_broker_bids_stop_at_its_inventory_cap():
    b, sent, _ = make(positions={"AAPL": MAX_INVENTORY})
    run(b.quote_symbol("AAPL"))
    assert places(sent, "buy") == []                  # full: asks only
    assert places(sent, "sell")


def test_3_any_buy_events_included_is_cut_at_the_hard_gross_limit():
    t, m, p, c = ready(books={"AAA": flat(), "BIG": flat()})
    t._bought["BIG"], t._cost["BIG"] = 900, 90_000.0  # $90k traded already
    traded = t._traded_gross(m)
    s = t._place("AAA", "buy", SEAT_MAX_SHARES, 100.0, c.t, "RIDE entry", taker=True)
    assert s.quantity < SEAT_MAX_SHARES - 5           # the 200 cap did not bind
    assert traded + s.quantity * 100.0 <= HARD_GROSS * NW
    assert traded + (s.quantity + 1) * 100.0 > HARD_GROSS * NW
    # a sell (an exit) is never cut by the gross limit
    p.positions["BIG"] = 900
    t._sync_ledger(p, c.t + 5)
    assert t._place("BIG", "sell", 50, 100.0, c.t + 5, "exit", taker=True).quantity == 50


def test_4_broker_refuses_a_bid_past_a_fifth_of_equity_in_traded_gross(monkeypatch):
    b, sent, _ = live_broker(monkeypatch, cash=20_000)
    assert send(b, "buy", "AAPL", 30)                 # $3k of the $4k budget
    assert not send(b, "buy", "AMD", 20)              # $5k: refused, never sent
    assert [m.symbol for m in sent] == ["AAPL"]


# ── 5-8. Stop-loss: per position and per session ────────────────────────────

def test_5_trader_stop_loss_sells_at_the_bid_once_down_1_5_percent():
    t, m, p, c = ready()                               # starter bought 5 AAA ~100.05
    m.books["AAA"] = flat(px=98.0)                     # bid 97.95: -2.1%
    c.t += 1
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("AAA", "sell", 5)
    assert t.orders.live("AAA", "sell").purpose == "stop-loss"


def test_6_trader_session_loss_allows_exits_only():
    p = FakePortfolio(positions={"DN": 10})
    t = MomentumTrader(clock=(c := _Clock()))
    m = FakeMarket({"AAA": flat(), "UP": trend(), "DN": trend(step=-0.001)})
    s = t.on_tick(m, p)
    t.on_fill("buy", s.symbol, s.quantity, s.price)
    c.t += 1
    p.nw *= 0.985                                       # -1.5%: past 1%, short of 2%
    s = t.on_tick(m, p)
    assert (s.symbol, s.side) == ("DN", "sell")         # the exit goes out
    c.t += 1
    assert t.on_tick(m, p) is None                      # the UP buy does not
    assert not t.kill.tripped


def test_7_broker_equity_20_percent_down_allows_reducing_orders_only(monkeypatch):
    from shared.messages import PortfolioUpdate
    b, sent, _ = live_broker(monkeypatch, cash=10_000)
    lost = 10_000 * (BROKER_SESSION_LOSS + BROKER_KILL_LOSS) / 2   # 22.5%
    b._on_portfolio(PortfolioUpdate(team_id="me", cash=9_000 - lost, positions={"AAPL": 10},
                                    net_worth=10_000 - lost, realized_pnl=-lost,
                                    unrealized_pnl=0, total_fees_paid=0))
    assert not send(b, "buy", "AMD", 1)                 # adds risk: refused
    assert send(b, "sell", "AAPL", 5)                   # reduces it: sent
    assert not b.kill.tripped


def test_8_broker_position_stop_quotes_only_the_reducing_side(monkeypatch):
    b, sent, now = live_broker(monkeypatch)
    b._book_fill("AAPL", "buy", 100.0, 20, taker=True)  # traded +20 at 100
    b.state.positions["AAPL"] = 20
    b.state.exchange_prices["AAPL"] = 98.0              # -2%: past the 1.5% stop
    run(b.quote_symbol("AAPL"))
    assert places(sent, "buy") == []
    assert places(sent, "sell")
    assert "AAPL" in b._stopped_names
    assert position_stop(20, 100.0, 98.0) and not position_stop(20, 100.0, 98.6)
    assert position_stop(-20, 100.0, 101.6)            # a short, the other way


# ── 9-12. Kill switch ───────────────────────────────────────────────────────

def test_9_human_kill_file_stops_new_orders_and_cancels_resting(tmp_path):
    t, m, p, c = ready(positions={"AAA": 20, "ZZZ": 6})
    ask = t._place("AAA", "sell", 5, 100.10, c.t, "quote", taker=False)
    t.on_ack("s1", "AAA", "sell", ask.price)
    kill_file(t, tmp_path, f"KILL_{t.kill.seat}")
    c.t += 2
    assert t.on_tick(m, p) is None
    assert t.kill.tripped and "human" in t.kill.reason
    assert t.take_cancels() == [("s1", "AAA")]
    assert t._place("AAA", "buy", 5, 99.0, c.t, "test", taker=False) is None


def test_9b_human_kill_file_stops_the_broker(monkeypatch, tmp_path):
    b, sent, _ = live_broker(monkeypatch)
    kill_file(b, tmp_path, "KILL")
    run(b.quote_symbol("AAPL"))
    assert b.kill.tripped
    assert not places(sent)
    assert not send(b, "buy", "AAPL", 5)


def test_10_reject_budget_trips_the_kill_switch():
    t, m, p, c = ready()
    for _ in range(REJECT_BUDGET):
        t.on_reject("SHORTS_LOCKED", "You hold 0 AAA — sell at most that many.")
    assert t.kill.tripped and "reject budget" in t.kill.reason


def test_10b_broker_reject_budget_trips_the_kill_switch(monkeypatch):
    b, sent, _ = live_broker(monkeypatch)
    for _ in range(BROKER_REJECT_BUDGET):
        run(b._on_error(ErrorMsg(code="PRICE_BAND", message="outside the band")))
    assert b.kill.tripped
    assert not send(b, "buy", "AAPL", 5)


def test_11_hard_loss_trips_the_kill_switch():
    t, m, p, c = ready()
    p.nw = NW * (1 - RiskLimits().kill_loss) - 1        # -2%
    c.t += 1
    assert t.on_tick(m, p) is None
    assert t.kill.tripped and "below the open" in t.kill.reason


def test_11b_broker_hard_loss_trips_the_kill_switch(monkeypatch):
    from shared.messages import PortfolioUpdate
    b, sent, _ = live_broker(monkeypatch, cash=10_000)
    left = 10_000 * (1 - BROKER_KILL_LOSS) - 1           # 25% down
    b._on_portfolio(PortfolioUpdate(team_id="me", cash=left, positions={},
                                    net_worth=left, realized_pnl=left - 10_000,
                                    unrealized_pnl=0, total_fees_paid=0))
    assert not send(b, "sell", "AAPL", 1)
    assert b.kill.tripped


def test_12_repeated_disconnects_trip_the_kill_switch():
    now = [0.0]
    k = KillSwitch("seat", lambda: now[0])
    for _ in range(MAX_DROPS - 1):                       # Nov 6: at most 4 in 5 min
        k.on_disconnect()
        now[0] += 10
    assert not k.tripped
    now[0] += DROP_WINDOW_SEC                            # old drops age out
    for _ in range(MAX_DROPS - 1):
        k.on_disconnect()
    assert not k.tripped
    k.on_disconnect("other venue")                       # links count apart
    assert not k.tripped
    k.on_disconnect()
    assert k.tripped and "disconnects" in k.reason


def test_12b_the_reconnect_loop_reports_drops_but_not_failed_connects():
    from websockets.exceptions import ConnectionClosed
    drops = []
    outcomes = [OSError("refused")] * 3 + [ConnectionClosed(None, None)] * 2

    async def serve():
        if not outcomes:
            raise asyncio.CancelledError
        raise outcomes.pop()

    async def sleep(_):
        pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_with_backoff(serve, "test", sleep=sleep,
                                     on_drop=lambda: drops.append(1)))
    assert len(drops) == 2                               # the 3 refusals are not drops


# ── 13-14. Halts: stop sending, no rejects, deliberate re-entry ─────────────

def test_13_halt_blocks_every_order_without_spending_a_reject():
    t, m, p, c = ready(books={"AAA": flat(), "UP": trend()})
    t.on_event("TRADING_HALT", "", {"symbol": "UP"})
    for _ in range(30):
        c.t += 1
        s = t.on_tick(m, p)
        assert s is None or s.symbol != "UP"
    assert sum(t.rejects.values()) == 0


def test_13b_a_halt_pulls_the_resting_orders_in_the_name():
    t, m, p, c = ready(positions={"AAA": 20, "BBB": 20, "ZZZ": 6},
                       books={"AAA": flat(), "BBB": flat()})
    for oid, sym in (("s1", "AAA"), ("s2", "BBB")):
        o = t._place(sym, "sell", 5, 100.10, c.t, "quote", taker=False)
        t.on_ack(oid, sym, "sell", o.price)
    c.t += 2
    t.on_event("TRADING_HALT", "", {"symbol": "AAA", "duration": 300})
    assert t.take_cancels() == [("s1", "AAA")]          # BBB still trades


def test_13c_a_halt_pulls_the_brokers_quotes_in_the_name():
    b, sent, _ = make()
    run(b.quote_symbol("AAPL"))
    ids = ack_all(b, sent)
    handle(b, event("TRADING_HALT", symbol="AAPL", duration=300))
    assert cancelled(sent) == set(ids)
    assert not places(sent)


def test_14_resume_waits_out_the_settle_and_refills_the_windows():
    t, m, p, c = ready(books={"AAA": flat(), "UP": trend()})
    t.on_book("UP", 100.0)
    assert "UP" in t._stats
    t.on_event("TRADING_HALT", "", {"symbol": "UP"})
    t.on_event("TRADING_RESUMED", "", {"symbol": "UP"})
    assert "UP" not in t._stats                         # pre-halt mids dropped
    assert t._place("UP", "buy", 5, 100.0, c.t, "test", taker=False) is None
    c.t += RESUME_SETTLE_SEC - 1
    assert t._place("UP", "buy", 5, 100.0, c.t, "test", taker=False) is None
    c.t += 1
    assert t._place("UP", "buy", 5, 100.0, c.t, "test", taker=False) is not None


# ── 15-17. Broker quoting: skew, volatility, toxicity ───────────────────────

def test_15_long_inventory_shades_both_quotes_down():
    assert inventory_skew(75, 150, 0.30) == pytest.approx(-0.15)
    assert inventory_skew(-150, 150, 0.30) == pytest.approx(0.30)
    flat_b, flat_sent, _ = make()
    long_b, long_sent, _ = make(positions={"AAPL": 100})
    run(flat_b.quote_symbol("AAPL"))
    run(long_b.quote_symbol("AAPL"))
    flat_bid = places(flat_sent, "buy")[0].price        # flat and long-only: no ask
    flat_ask = 2 * 100.0 - flat_bid                     # its mirror around the mark
    assert places(long_sent, "buy")[0].price < flat_bid
    assert places(long_sent, "sell")[0].price < flat_ask


def test_16_quotes_widen_with_volatility_up_to_the_cap():
    assert range_spread(195.0, 0.10) is None            # calm: default width
    assert range_spread(195.0, 0.30) == pytest.approx(0.60)
    assert range_spread(195.0, 10.0) == pytest.approx(195.0 * MAX_WIDEN_BPS / 10_000)


def test_17_one_sided_flow_against_the_quotes_widens_them(monkeypatch):
    now = [0.0]
    meter = ToxicityMeter(lambda: now[0])
    for _ in range(TOXIC_QUOTES - 1):
        meter.fill("AAPL", "buy", 10)
    meter.fill("AAPL", "sell", 10)                      # two-sided: not toxic
    assert meter.factor("AAPL", 10) == 1.0
    meter.fill("AAPL", "buy", 10)                       # 3 quote sizes hit our bids
    assert meter.factor("AAPL", 10) == TOXIC_WIDEN
    now[0] += 31
    assert meter.factor("AAPL", 10) == 1.0              # the window has passed

    b, sent, _ = live_broker(monkeypatch)
    base = b._spread("AAPL")
    for _ in range(TOXIC_QUOTES):
        b._book_fill("AAPL", "sell", 100.0, 10, taker=False)
    assert b._spread("AAPL") == pytest.approx(base * TOXIC_WIDEN)


def test_traded_cost_follows_the_inventory_through_a_flip():
    c = TradedCost()
    c.fill("buy", 10, 100.0)
    c.fill("buy", 10, 102.0)
    assert (c.qty, c.avg) == (20, 101.0)
    c.fill("sell", 5, 105.0)
    assert (c.qty, c.avg) == (15, 101.0)
    c.fill("sell", 25, 99.0)
    assert (c.qty, c.avg) == (-10, 99.0)


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t
