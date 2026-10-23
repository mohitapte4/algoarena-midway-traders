"""
Tests for team.books: the seat's own books against the venue's.

The venue's own accounting is the oracle: engine Portfolio objects for the
arithmetic, and a real ExchangeServer for the message stream a seat sees.

Run with:  PYTHONPATH=engine:. python -m pytest tests/test_books.py -v
"""

import random

import pytest

from exchange.server import Portfolio
from team.books import Books, VenueState

TAKER = 0.0015
MAKER = 0.0010
SYMBOLS = ("AAA", "BBB", "CCC")


def random_fills(rng: random.Random, n: int):
    """Buys, sells, shorts, covers and flips, as taker or maker, some of
    them against a teammate (no rebate)."""
    for _ in range(n):
        side = rng.choice(("buy", "sell"))
        qty = rng.choice((1, 3, 7, 10, 25, 60))
        price = round(rng.uniform(20, 200), 2)
        notional = price * qty
        taker = rng.random() < 0.4
        teammate = rng.random() < 0.2
        fee = round(notional * TAKER, 8) if taker else 0.0
        rebate = 0.0 if taker or teammate else round(notional * MAKER, 8)
        yield side, rng.choice(SYMBOLS), qty, price, fee, rebate


def apply_to_portfolio(p: Portfolio, side, symbol, qty, price, fee, rebate):
    if side == "buy":
        p.apply_buy(symbol, price, qty, fee, rebate)
    else:
        p.apply_sell(symbol, price, qty, fee, rebate)


def venue_of(p: Portfolio) -> VenueState:
    return VenueState(
        source="oracle", cash=p.cash,
        positions={s: q for s, q in p.positions.items() if q},
        realized=p.realized_pnl, fees=p.total_fees_paid,
        rebates=p.total_rebates_earned, carry=p.total_carry_paid,
        net_worth=None, avg_cost=dict(p.avg_cost))


def assert_books_match(books: Books, p: Portfolio) -> None:
    assert books.cash == pytest.approx(p.cash, abs=1e-6)
    assert books.positions == {s: q for s, q in p.positions.items() if q}
    assert books.avg_cost.keys() == books.positions.keys()
    for s in books.positions:
        assert books.avg_cost[s] == pytest.approx(p.avg_cost[s], abs=1e-6)
    assert books.realized == pytest.approx(p.realized_pnl, abs=1e-6)
    assert books.fees == pytest.approx(p.total_fees_paid, abs=1e-6)
    assert books.rebates == pytest.approx(p.total_rebates_earned, abs=1e-6)


@pytest.mark.parametrize("seed", range(8))
def test_fills_match_the_venue_portfolio(seed):
    rng = random.Random(seed)
    venue = Portfolio(team_id="me", role="trader", level=1)
    books = Books.seed(venue_of(venue))
    for fill in random_fills(rng, 400):
        apply_to_portfolio(venue, *fill)
        books.apply_fill(*fill)
    assert_books_match(books, venue)
    assert books.fills == 400
    assert books.entry_known


@pytest.mark.parametrize("seed", range(4))
def test_books_seeded_mid_session_keep_matching(seed):
    rng = random.Random(100 + seed)
    venue = Portfolio(team_id="me", role="trader", level=1)
    for fill in random_fills(rng, 150):
        apply_to_portfolio(venue, *fill)
    books = Books.seed(venue_of(venue))
    for fill in random_fills(rng, 150):
        apply_to_portfolio(venue, *fill)
        books.apply_fill(*fill)
    assert_books_match(books, venue)


def test_flip_reopens_at_the_fill_price():
    venue = Portfolio(team_id="me", role="trader", level=1)
    books = Books.seed(venue_of(venue))
    for fill in (("buy", "AAA", 10, 50.0, 0.0, 0.5),
                 ("sell", "AAA", 25, 60.0, 2.25, 0.0)):
        apply_to_portfolio(venue, *fill)
        books.apply_fill(*fill)
    assert books.positions == {"AAA": -15}
    assert books.avg_cost == {"AAA": 60.0}
    assert books.realized == pytest.approx(100.0)
    assert_books_match(books, venue)


def test_ipo_and_dividend_match_the_venue():
    venue = Portfolio(team_id="me", role="trader", level=1)
    books = Books.seed(venue_of(venue))
    books.apply_fill("sell", "AAA", 30, 40.0, 1.8, 0.0)
    venue.apply_sell("AAA", 40.0, 30, 1.8, 0.0)
    # server.py _price_ipo and _pay_dividend, by hand
    venue.cash -= 50 * 12.0
    venue.positions["NEW"] = 50
    venue.avg_cost["NEW"] = 12.0
    venue.cash += venue.positions["AAA"] * 0.75
    books.apply_ipo("NEW", 50, 12.0)
    books.apply_dividend("AAA", 0.75)
    assert_books_match(books, venue)


def test_a_fill_on_an_unpriced_position_loses_the_entry():
    seeded = VenueState(source="portfolio update", cash=1000.0,
                        positions={"AAA": 10, "BBB": 5}, realized=0.0,
                        fees=0.0, rebates=0.0, carry=0.0, net_worth=None)
    books = Books.seed(seeded)
    assert books.unpriced == {"AAA", "BBB"}

    board = VenueState(source="leaderboard t5", cash=1000.0,
                       positions={"AAA": 10, "BBB": 5}, realized=0.0,
                       fees=0.0, rebates=0.0, carry=0.0, net_worth=None,
                       avg_cost={"AAA": 20.0, "BBB": 30.0}, tick=5)
    books.apply_fill("sell", "BBB", 5, 31.0, 0.0, 0.0)
    assert books.learn_entries(board) == ["AAA"]
    assert books.avg_cost["AAA"] == 20.0
    assert not books.entry_known
