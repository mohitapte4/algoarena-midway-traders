"""Tests for team.mean_reversion_trader (no network)."""

from __future__ import annotations

import logging

from team.mean_reversion_trader import MeanReversionTrader
from tests.test_momentum_trader import Clock, FakeMarket, FakePortfolio, flat

logging.disable(logging.CRITICAL)


def dip(px: float = 100.0, drop: float = 0.02) -> list[float]:
    """Flat, then one sharp drop: about 4 standard deviations below the mean."""
    return [px] * 19 + [px * (1 - drop)]


def filled_trader(books, portfolio=None):
    """A trader whose starter order (on AAA) has already filled."""
    clock = Clock()
    t = MeanReversionTrader(clock=clock)
    m, p = FakeMarket(books), portfolio or FakePortfolio()
    s = t.on_tick(m, p)
    t.on_fill("buy", s.symbol, s.quantity, s.price)
    clock.t += 1
    return t, m, p, clock


def test_flat_tape_sits_out():
    t, m, p, _ = filled_trader({"AAA": flat()})
    assert t.on_tick(m, p) is None


def test_sharp_dip_buys_at_the_ask():
    t, m, p, _ = filled_trader({"AAA": flat(), "DIP": dip()})
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity, s.price) == ("DIP", "buy", 5, m.best_ask("DIP"))


def test_dip_too_small_to_cover_costs_is_ignored():
    # z is just as extreme, but a 0.2% move does not pay for a round trip.
    t, m, p, _ = filled_trader({"AAA": flat(), "DIP": dip(drop=0.002)})
    assert t.on_tick(m, p) is None


def test_ordinary_noise_is_ignored():
    noisy = [99.0, 101.0] * 10                        # last mid is 1 sd below
    t, m, p, _ = filled_trader({"AAA": flat(), "NOISY": noisy})
    assert t.on_tick(m, p) is None


def test_does_not_chase_a_rally():
    # The mirror of a dip: momentum would buy this, mean reversion must not.
    t, m, p, _ = filled_trader({"AAA": flat(), "UP": dip(drop=-0.02)})
    assert t.on_tick(m, p) is None


def test_sells_shares_it_bought_once_back_at_the_mean():
    p = FakePortfolio(positions={"ZZZ": 6})
    t, m, _, c = filled_trader({"AAA": flat(), "DIP": dip()}, p)
    buy = t.on_tick(m, p)
    t.on_fill("buy", "DIP", buy.quantity, buy.price)
    p.positions["DIP"] = buy.quantity
    c.t += 1
    m.books["DIP"] = [100.0] * 18 + [98.0, 100.5]     # recovered above the mean
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("DIP", "sell", 5)


def test_never_sells_holdings_it_did_not_buy():
    # 50 RISEN shares came from elsewhere (an IPO grant, another session):
    # a recovery is not a reason to dump them.
    p = FakePortfolio(positions={"RISEN": 50})
    t, m, _, _ = filled_trader({"AAA": flat(), "RISEN": dip(drop=-0.02)}, p)
    assert t.on_tick(m, p) is None


def test_loss_limit_stops_dip_buying():
    t, m, p, _ = filled_trader({"AAA": flat(), "DIP": dip()})
    p.nw *= 0.98                                      # down 2% since the open
    assert t.on_tick(m, p) is None


def test_stop_loss_still_applies():
    p = FakePortfolio(positions={"ZZZ": 6})
    t, m, _, c = filled_trader({"AAA": flat(), "DIP": dip()}, p)
    buy = t.on_tick(m, p)
    t.on_fill("buy", "DIP", buy.quantity, buy.price)
    p.positions["DIP"] = buy.quantity
    c.t += 1
    m.books["DIP"] = dip(drop=0.05)                   # kept falling: ~3% below entry
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("DIP", "sell", 5)
