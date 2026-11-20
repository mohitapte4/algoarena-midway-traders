"""Week 8: the hidden lead-lag links (team.leadlag) and the orders they raise."""

from __future__ import annotations

import logging

from team.base_trader import LINK_SHARES, RoutedSeat, link_feed_index
from team.leadlag import LINKS, TICK_SEC, Link, LinkTracker
from team.routing import MAX_VENUE_GAP, Router, VenueQuote, choose
from team.venue import venue_name
from tests.test_momentum_trader import FakeMarket, FakePortfolio, flat
from tests.test_rejects import ready

logging.disable(logging.CRITICAL)

AMD_GOOGL, NFLX_AMZN, MSFT_TSLA = LINKS
UP = Link("LEAD", "AAA", lag=6, beta=+0.4, threshold=0.0010)
DOWN = Link("LEAD", "AAA", lag=6, beta=-0.4, threshold=0.0010)


def moved(tracker, symbol, pct, now, base=100.0):
    """Two marks one tick apart: `pct` is the leader's one-tick return."""
    tracker.on_mark(symbol, base, now - TICK_SEC)
    return tracker.on_mark(symbol, base * (1 + pct), now)


# ── the tracker ─────────────────────────────────────────────────────────────

def test_the_links_are_the_three_found_in_the_history():
    assert [(k.leader, k.follower, k.lag) for k in LINKS] == [
        ("AMD", "GOOGL", 6), ("NFLX", "AMZN", 8), ("MSFT", "TSLA", 4)]
    assert AMD_GOOGL.beta > 0 > NFLX_AMZN.beta and MSFT_TSLA.beta < 0
    assert all(0.3 <= abs(k.beta) <= 0.5 for k in LINKS)   # the published range


def test_a_leader_move_cues_its_follower_in_the_direction_of_beta():
    up = moved(LinkTracker(), "AMD", +0.0015, now=10.0)
    assert (up.link.follower, up.side) == ("GOOGL", "buy")
    assert up.echo_at == 10.0 + 6 * TICK_SEC
    down = moved(LinkTracker(), "AMD", -0.0015, now=10.0)
    assert down.side == "sell"
    inverse = moved(LinkTracker(), "NFLX", +0.0010, now=10.0)
    assert (inverse.link.follower, inverse.side) == ("AMZN", "sell")
    assert moved(LinkTracker(), "MSFT", -0.0010, now=10.0).side == "buy"


def test_small_moves_repeated_marks_and_other_symbols_raise_nothing():
    t = LinkTracker()
    assert moved(t, "AMD", +0.0005, now=10.0) is None       # under the threshold
    assert t.on_mark("AMD", 100.05, 10.5) is None           # same tick again
    assert moved(t, "GOOGL", +0.02, now=12.0) is None       # a follower leads nothing
    assert t.pending(12.0) == []


def test_a_gap_in_the_marks_is_not_a_one_tick_move():
    t = LinkTracker()
    t.on_mark("AMD", 100.0, 10.0)
    assert t.on_mark("AMD", 101.0, 20.0) is None            # 10 s apart: reconnect
    assert t.on_mark("AMD", 101.3, 21.0) is not None        # next tick counts again


def test_a_smaller_opposite_move_inside_the_lag_does_not_flip_the_cue():
    t = LinkTracker()
    big = moved(t, "AMD", +0.0030, now=10.0)
    assert t.on_mark("AMD", 100.3 * (1 - 0.0012), 11.0) is None
    assert t.pending(11.0) == [big]
    bigger = t.on_mark("AMD", 100.3 * (1 - 0.0012) * (1 - 0.0040), 12.0)
    assert bigger.side == "sell" and t.pending(12.0) == [bigger]


def test_a_smaller_opposite_move_counts_once_the_echo_has_passed():
    t = LinkTracker()
    moved(t, "MSFT", +0.0030, now=10.0)                     # lag 4: echo at 14
    t.on_mark("MSFT", 100.3, 14.0 - 0.5)                    # unchanged since
    t.on_mark("MSFT", 100.31, 14.0)
    late = t.on_mark("MSFT", 100.31 * (1 - 0.0010), 15.0)
    assert late is not None and late.side == "buy"


def test_a_cue_is_pending_until_sent_or_too_close_to_its_echo():
    t = LinkTracker()
    cue = moved(t, "MSFT", +0.0010, now=10.0)               # lag 4: echo at 14
    assert t.pending(12.9) == [cue]
    assert t.pending(13.1) == []                            # under a second left
    cue.sent = True
    assert t.pending(10.5) == []


# ── the seat ────────────────────────────────────────────────────────────────

def link_seat(link, positions=None):
    t, m, p, c = ready(positions=positions or {"AAA": 20, "ZZZ": 6})
    t.links = LinkTracker((link,))
    return t, m, p, c


def leader_moves(t, c, pct):
    t.on_mark("LEAD", 50.0)
    c.t += TICK_SEC
    t.on_mark("LEAD", 50.0 * (1 + pct))


def test_seat_rests_a_bid_in_the_follower_when_the_echo_is_up():
    t, m, p, c = link_seat(UP)
    leader_moves(t, c, +0.0020)
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("AAA", "buy", LINK_SHARES)
    assert s.price == m.best_bid("AAA")                     # a maker order
    assert t.link_counts["cues"] == 1 and t.link_counts["orders"] == 1


def test_seat_offers_the_follower_when_the_link_is_inverse():
    t, m, p, c = link_seat(DOWN)
    leader_moves(t, c, +0.0020)
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.price) == ("AAA", "sell", m.best_ask("AAA"))


def test_one_order_per_leader_move():
    t, m, p, c = link_seat(UP)
    leader_moves(t, c, +0.0020)
    assert t.on_tick(m, p) is not None
    c.t += 0.1
    t.on_tick(m, p)
    assert t.link_counts["orders"] == 1
    assert sum(o.purpose == "link" for o in t.orders) == 1


def test_link_order_is_cancelled_after_the_echo_tick_not_before():
    t, m, p, c = link_seat(UP)
    leader_moves(t, c, +0.0020)
    s = t.on_tick(m, p)
    t.on_ack("L1", s.symbol, s.side, s.price)
    c.t += UP.lag * TICK_SEC                                # the echo tick itself
    t.on_tick(m, p)
    assert t.take_cancels() == []
    c.t += TICK_SEC + 0.1
    t.on_tick(m, p)
    assert ("L1", "AAA") in t.take_cancels()


def test_a_cue_too_old_to_beat_its_echo_is_not_sent():
    t, m, p, c = link_seat(UP)
    leader_moves(t, c, +0.0020)
    c.t += UP.lag * TICK_SEC                                # the seat was busy
    t.on_tick(m, p)
    assert t.link_counts["orders"] == 0


def test_no_link_order_in_a_halted_follower():
    t, m, p, c = link_seat(UP)
    t.on_event("TRADING_HALT", "AAA halted", {"symbol": "AAA"})
    leader_moves(t, c, +0.0020)
    t.on_tick(m, p)
    assert t.link_counts["orders"] == 0


def test_a_reconnect_forgets_cues_and_link_orders():
    t, m, p, c = link_seat(UP)
    leader_moves(t, c, +0.0020)
    t.on_connect()
    assert t.links.pending(c.t) == [] and t._link_orders == []


def test_link_order_is_one_share_when_the_venue_charges_makers():
    t, m, p, c = link_seat(UP)
    t.on_event("FEE_SCHEDULE", "fees", {"taker": 0.04, "rebate": -0.01})
    leader_moves(t, c, +0.0020)
    assert t.on_tick(m, p).quantity == 1


# ── routing (team.routing) ──────────────────────────────────────────────────

def q(venue, bid, ask, taker=0.0040, rebate=0.0):
    return VenueQuote(venue, bid, ask, taker, rebate)


def test_a_taker_goes_where_the_price_after_the_fee_is_best():
    house = q("house", 99.90, 100.10, taker=0.0040)
    cheap = q("team", 99.88, 100.20, taker=0.0006)          # worse ask, lower fee
    assert choose("buy", True, [house, cheap]).venue == "team"     # 100.26 < 100.50
    assert choose("sell", True, [house, cheap]).venue == "team"    # 99.82 > 99.50
    level = q("team", 99.88, 100.20, taker=0.0040)
    assert choose("buy", True, [house, level]).venue == "house"    # same fee: price


def test_a_maker_joins_the_national_best_unless_a_rebate_beats_the_gap():
    house = q("house", 100.00, 100.10, rebate=0.0)
    near = q("team", 99.98, 100.12, rebate=0.0006)          # 2 bp behind, pays 6
    far = q("team", 99.90, 100.20, rebate=0.0006)           # 10 bp behind, pays 6
    assert choose("buy", False, [house, near]).venue == "team"
    assert choose("sell", False, [house, near]).venue == "team"
    assert choose("buy", False, [house, far]).venue == "house"
    assert choose("sell", False, [house, far]).venue == "house"


def test_a_venue_that_charges_makers_loses_the_resting_order():
    house = q("house", 100.00, 100.10, rebate=-0.0100)      # open play, Nov 19
    team = q("team", 99.95, 100.15, rebate=0.0004)
    assert choose("buy", False, [house, team]).venue == "team"


def test_home_wins_a_tie_and_a_venue_far_from_home_is_not_compared():
    house = q("house", 100.00, 100.10)
    twin = q("team", 100.00, 100.10)
    assert choose("buy", False, [house, twin]).venue == "house"
    wild = q("team", 130.00, 130.10, taker=0.0, rebate=0.0010)   # 30% away
    assert abs(wild.mid / house.mid - 1) > MAX_VENUE_GAP
    assert choose("buy", False, [house, wild]).venue == "house"
    assert choose("sell", True, [house, wild]).venue == "house"
    assert choose("buy", False, []) is None


def test_venue_names_are_short_and_distinct():
    assert venue_name("wss://feed.algoarena-st.duckdns.org") == "feed"
    assert venue_name("wss://feed-8773.algoarena-st.duckdns.org") == "feed-8773"
    assert venue_name("ws://127.0.0.1:8799") == "127.0.0.1:8799"


# ── one seat on two venues ──────────────────────────────────────────────────

def two_venues(link=UP, away_mid=100.0, away_rebate=0.0006, away_positions=None):
    home, m, p, c = link_seat(link)
    home.on_connect()
    home.links = LinkTracker((link,))
    home._market = m
    away = RoutedSeat(clock=c)
    away.links, away.link_feed = home.links, False
    home.venue, away.venue = "house", "team"
    home.router = away.router = Router([home, away])
    away.on_connect()
    away.on_event("FEE_SCHEDULE", "fees", {"taker": 0.0006, "rebate": away_rebate})
    am = FakeMarket({"AAA": flat(px=away_mid)})
    ap = FakePortfolio(positions=away_positions or {"AAA": 5}, net_worth=75_000.0)
    assert away.on_tick(am, ap) is None                     # no strategy of its own
    return home, m, p, away, am, ap, c


def test_link_order_goes_to_the_venue_with_the_better_rebate_at_the_same_price():
    home, m, p, away, am, ap, c = two_venues()
    leader_moves(home, c, +0.0020)
    assert home.on_tick(m, p) is None                       # routed away
    s = away.on_tick(am, ap)
    assert (s.symbol, s.side, s.quantity) == ("AAA", "buy", LINK_SHARES)
    assert home.link_counts["orders"] == 0 and away.link_counts["orders"] == 1
    c.t += 0.1
    assert home.on_tick(m, p) is None and away.on_tick(am, ap) is None   # sent once


def test_link_order_stays_home_when_the_other_venue_pays_no_more():
    home, m, p, away, am, ap, c = two_venues(away_rebate=0.0)
    leader_moves(home, c, +0.0020)
    assert home.on_tick(m, p).symbol == "AAA"
    assert away.on_tick(am, ap) is None


def test_link_order_stays_home_when_the_other_venue_is_another_market():
    home, m, p, away, am, ap, c = two_venues(away_mid=130.0)
    leader_moves(home, c, +0.0020)
    assert home.on_tick(m, p).symbol == "AAA"


def test_link_order_stays_home_while_the_other_venue_is_offline_or_halted():
    home, m, p, away, am, ap, c = two_venues()
    away.on_disconnect()
    leader_moves(home, c, +0.0020)
    assert home.on_tick(m, p).symbol == "AAA"

    home, m, p, away, am, ap, c = two_venues()
    away.on_event("TRADING_HALT", "AAA halted", {"symbol": "AAA"})
    leader_moves(home, c, +0.0020)
    assert home.on_tick(m, p).symbol == "AAA"


def test_a_sell_is_not_routed_to_a_venue_with_no_shares_there():
    home, m, p, away, am, ap, c = two_venues(link=DOWN, away_positions={"ZZZ": 1})
    leader_moves(home, c, +0.0020)
    s = home.on_tick(m, p)
    assert (s.symbol, s.side) == ("AAA", "sell")
    assert away.on_tick(am, ap) is None


def test_only_one_venue_feeds_the_tracker_and_every_venue_bids_in_its_ipo():
    home, m, p, away, am, ap, c = two_venues()
    away.on_mark("LEAD", 50.0)
    c.t += 1.0
    away.on_mark("LEAD", 51.0)
    assert home.links.pending(c.t) == [] and away.link_counts["cues"] == 0
    assert away.on_ipo("LUNA", 45.0, 52.0, 3000, {}) == (200, 52.0)
    away.on_connect()                                       # a reconnect there
    leader_moves(home, c, +0.0020)
    assert len(home.links.pending(c.t)) == 1                # keeps home's cues


def test_link_cues_come_from_the_named_venue_or_the_first(monkeypatch):
    urls = ["wss://team-a", "wss://team-b", "wss://house"]
    monkeypatch.delenv("LINK_FEED_URL", raising=False)
    assert link_feed_index(urls) == 0
    monkeypatch.setenv("LINK_FEED_URL", "wss://house")
    assert link_feed_index(urls) == 2
    monkeypatch.setenv("LINK_FEED_URL", "wss://elsewhere")
    assert link_feed_index(urls) == 0


def test_with_three_venues_the_order_goes_to_the_best_of_them():
    home, m, p, away, am, ap, c = two_venues(away_rebate=0.0006)
    third = RoutedSeat(clock=c)
    third.links, third.link_feed, third.venue = home.links, False, "team-2"
    third.on_connect()
    third.on_event("FEE_SCHEDULE", "fees", {"taker": 0.0014, "rebate": 0.0011})
    tm = FakeMarket({"AAA": flat()})
    tp = FakePortfolio(positions={"AAA": 5}, net_worth=75_000.0)
    home.router = away.router = third.router = Router([home, away, third])
    third.on_tick(tm, tp)
    leader_moves(home, c, +0.0020)
    assert home.on_tick(m, p) is None and away.on_tick(am, ap) is None
    assert third.on_tick(tm, tp).symbol == "AAA"            # 11 bp beats 6 and 0


# ── the earnings echo trade ─────────────────────────────────────────────────

from team.leadlag import ECHO_DRIFT, echo_plans  # noqa: E402


def echo_seat(taker=0.0008, positions=None):
    t, m, p, c = ready(positions=positions or {"AMZN": 80, "NFLX": 10, "ZZZ": 6},
                       books={"AMZN": flat(px=250.0), "NFLX": flat(px=80.0),
                              "AAA": flat()})
    t.on_event("FEE_SCHEDULE", "fees", {"taker": taker, "rebate": 0.0005})
    t.on_tick(m, p)
    return t, m, p, c


def release(t, pct, affected=("NFLX",)):
    t.on_event("CALENDAR_EVENT", f"Earnings: NFLX moving {pct:+.1%}", {
        "kind": "earnings", "symbol": affected[0], "affected": list(affected),
        "pct": pct, "ramp_ticks": 15, "overshoot": 1.3})


def next_order(t, m, p, c, symbol, seconds=3.0):
    """Tick until the seat sends an order in `symbol`, or time runs out."""
    for _ in range(int(seconds / 0.1)):
        c.t += 0.1
        s = t.on_tick(m, p)
        if s is not None and s.symbol == symbol:
            return s
        if s is not None:
            t.on_ack(f"o{c.t}", s.symbol, s.side, s.price)
    return None


def test_echo_plan_times_and_size_follow_the_link():
    (plan,) = echo_plans(["NFLX"], -0.06, 15, now=100.0)
    assert (plan.link.follower, plan.side, plan.exit_side) == ("AMZN", "buy", "sell")
    assert plan.enter_until == 107.0                        # first echo at tick 8
    assert 121.5 < plan.exit_at < 122.0                     # last echo at tick 22
    assert plan.expected == ECHO_DRIFT * 0.30 * 0.06        # 0.63%
    assert plan.worth_it(spread=0.0016, taker=0.0008, margin=1.25)
    assert not plan.worth_it(spread=0.0016, taker=0.0400, margin=1.25)   # house
    assert not plan.worth_it(spread=0.0060, taker=0.0008, margin=1.25)   # wide book
    assert echo_plans(["GOOGL"], 0.06, 15, 0.0) == []       # a follower leads nothing
    assert echo_plans(["NFLX", "AMZN", "AAPL"], 0.02, 15, 0.0) == []     # market-wide


def test_seat_takes_the_follower_against_the_leader_then_leaves_on_the_last_echo():
    t, m, p, c = echo_seat()
    release(t, -0.06)
    t0 = c.t
    s = next_order(t, m, p, c, "AMZN")
    assert (s.side, s.quantity, s.price) == ("buy", 60, m.best_ask("AMZN"))
    assert c.t < t0 + 7.0                                   # before the first echo
    t.on_fill("buy", "AMZN", 60, s.price)
    p.positions["AMZN"] += 60
    assert next_order(t, m, p, c, "AMZN", seconds=20.0) is None or c.t >= t0 + 21.5
    c.t = t0 + 21.8
    out = next_order(t, m, p, c, "AMZN", seconds=0.5)
    assert (out.side, out.quantity) == ("sell", 60)
    assert out.price < m.best_bid("AMZN")                   # crosses: it must fill


def test_seat_sells_the_follower_when_the_leader_jumps():
    t, m, p, c = echo_seat()
    release(t, +0.06)
    s = next_order(t, m, p, c, "AMZN")
    assert (s.side, s.quantity, s.price) == ("sell", 60, m.best_bid("AMZN"))


def test_no_echo_trade_at_house_fees_or_on_a_market_wide_print():
    t, m, p, c = echo_seat(taker=0.04)
    release(t, -0.06)
    assert next_order(t, m, p, c, "AMZN", seconds=8.0) is None

    t, m, p, c = echo_seat()
    release(t, -0.02, affected=("NFLX", "AMZN", "AAA"))
    assert "AMZN" not in t._echoes


def test_no_echo_entry_once_the_echoes_have_begun():
    t, m, p, c = echo_seat()
    release(t, -0.06)
    c.t += 7.5
    assert next_order(t, m, p, c, "AMZN", seconds=5.0) is None


def test_an_unfilled_echo_entry_leaves_nothing_to_exit():
    t, m, p, c = echo_seat()
    release(t, -0.06)
    t0 = c.t
    s = next_order(t, m, p, c, "AMZN")
    t.on_ack("E1", "AMZN", s.side, s.price)
    c.t = t0 + 7.2
    t.on_tick(m, p)
    assert ("E1", "AMZN") in t.take_cancels()
    c.t = t0 + 22.0
    assert next_order(t, m, p, c, "AMZN", seconds=1.0) is None


def test_the_second_venue_runs_no_echo_trade():
    away = RoutedSeat()
    release(away, -0.06)
    assert away._echoes == {}


# ── the broker on a venue that sends no portfolio ───────────────────────────

import asyncio  # noqa: E402

from shared.messages import Leaderboard, PlaceOrder  # noqa: E402
from team import broker as team_broker  # noqa: E402


def board(seat, cash=119_000.0, positions=None):
    row = {"team_id": seat, "cash": cash, "net_worth": cash,
           "positions": positions if positions is not None else {"AAPL": 40}}
    return Leaderboard(traders=[], brokers=[row], exchange_fees=0.0, tick=7)


def waiting_broker():
    b = team_broker.TeamBrokerBot(team_broker.MyBroker(), "ws://test")
    b.state.exchange_prices.update({"AAPL": 100.0})
    return b


def bid(b):
    return PlaceOrder(team_id=team_broker._config.TEAM_ID, symbol="AAPL", side="buy",
                      order_type="limit", price=99.9, quantity=10)


def test_broker_takes_its_portfolio_from_the_leaderboard_when_none_is_sent():
    b = waiting_broker()
    assert b._risk_refusal(bid(b)) == "no portfolio from the venue yet"
    asyncio.run(b._handle(board(team_broker._config.TEAM_ID).model_dump_json()))
    assert b.state.cash == 119_000.0 and b.state.positions == {"AAPL": 40}
    assert b._risk_refusal(bid(b)) is None
    assert b._opening == {"AAPL": 40}                       # held, not traded


def test_broker_keeps_waiting_when_the_board_has_no_row_for_it():
    b = waiting_broker()
    asyncio.run(b._handle(board("someone_else").model_dump_json()))
    assert b._risk_refusal(bid(b)) == "no portfolio from the venue yet"
