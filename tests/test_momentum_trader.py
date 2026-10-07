"""Tests for team.momentum_trader and team.risk (no network)."""

from __future__ import annotations

import logging

import pytest

from team.momentum_trader import (
    PENDING_TIMEOUT_SEC, STARTER_MAX_ATTEMPTS, STARTER_RETRY_SEC, MomentumTrader,
)
from team.risk import RiskLimits

logging.disable(logging.CRITICAL)


# ── fakes with the same read surface on_tick is documented against ─────────

class FakeMarket:
    def __init__(self, books: dict[str, list[float]], half_spread: float = 0.05) -> None:
        self.books = books              # symbol -> mid history, oldest first
        self.half = half_spread

    def symbols(self) -> list[str]:
        return list(self.books)

    def prices(self, s: str) -> list[float]:
        return list(self.books[s])

    def mid_price(self, s: str) -> float | None:
        return self.books[s][-1] if self.books.get(s) else None

    def best_bid(self, s: str) -> float | None:
        mid = self.mid_price(s)
        return None if mid is None else mid - self.half

    def best_ask(self, s: str) -> float | None:
        mid = self.mid_price(s)
        return None if mid is None else mid + self.half


class FakePortfolio:
    def __init__(self, cash: float = 300_000.0, positions: dict | None = None,
                 net_worth: float | None = None) -> None:
        self.cash = cash
        self.positions = dict(positions if positions is not None else {"ZZZ": 6})
        self.nw = net_worth if net_worth is not None else cash

    def net_worth(self, _market) -> float:
        return self.nw

    def can_buy(self, s: str, qty: int, px: float) -> bool:
        return self.cash >= px * qty * 1.001

    def can_sell(self, s: str, qty: int) -> bool:
        return self.positions.get(s, 0) >= qty


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


def flat(n: int = 25, px: float = 100.0) -> list[float]:
    return [px] * n


def trend(n: int = 25, px: float = 100.0, step: float = 0.001) -> list[float]:
    return [px * (1 + step) ** i for i in range(n)]


def make(books, portfolio=None, limits=None):
    clock = Clock()
    return MomentumTrader(limits=limits, clock=clock), FakeMarket(books), \
        portfolio or FakePortfolio(), clock


def filled_trader(books, portfolio=None, limits=None):
    """A trader whose starter order has already filled."""
    t, m, p, c = make(books, portfolio, limits)
    s = t.on_tick(m, p)
    t.on_fill("buy", s.symbol, s.quantity, s.price)
    c.t += 1
    return t, m, p, c


# ── RiskLimits ──────────────────────────────────────────────────────────────

def test_halted_only_after_loss_limit():
    r = RiskLimits()
    assert not r.halted(99_100, 100_000)
    assert r.halted(98_900, 100_000)
    assert not r.halted(50_000, None)


def test_buy_quantity_binding_limit():
    r = RiskLimits()
    assert r.buy_quantity(5, 100.0, 0, 0, 300_000) == (5, "")
    assert r.buy_quantity(5, 100.0, 0, 0, 10_000) == (2, "order size")        # $200 cap
    assert r.buy_quantity(5, 100.0, 14_950, 0, 300_000)[1] == "concentration"  # $15k cap
    assert r.buy_quantity(5, 100.0, 0, 59_900, 300_000)[1] == "gross exposure"  # $60k cap


def test_stop_hit():
    r = RiskLimits()
    assert r.stop_hit(98.5, 100.0)
    assert not r.stop_hit(98.6, 100.0)
    assert not r.stop_hit(None, 100.0)


# ── pre-open and start-up ───────────────────────────────────────────────────

def test_no_orders_during_preopen():
    t, m, p, _ = make({"AAA": flat()})
    t.on_event("SESSION_PREOPEN", "pre-open", {})
    assert t.on_tick(m, p) is None
    t.on_event("AUCTION_RESULT", "opening cross", {})
    assert t.on_tick(m, p) is not None


def test_only_the_starter_trades_before_real_portfolio_state():
    # Before the first fill the exchange has not sent the portfolio, so the
    # local copy is the placeholder: the starter may go, the signal may not.
    placeholder = FakePortfolio(cash=100_000.0, positions={}, net_worth=100_000.0)
    t, m, _, c = make({"AAA": flat(), "UP": trend()})
    s = t.on_tick(m, placeholder)
    assert (s.symbol, s.side, s.quantity) == ("AAA", "buy", 5)
    t.on_fill("buy", s.symbol, s.quantity, s.price)
    c.t += 1
    assert t.on_tick(m, placeholder) is None          # no crossover on placeholder
    assert t._start_net_worth is None
    real = FakePortfolio(positions={"AAA": 5})
    assert t.on_tick(m, real).symbol == "UP"          # real state: signal trades
    assert t._start_net_worth == real.nw


# ── starter order ───────────────────────────────────────────────────────────

def test_starter_is_small_marketable_buy():
    t, m, p, _ = make({"BBB": flat(), "AAA": flat()})
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity, s.price) == ("AAA", "buy", 5, m.best_ask("AAA"))


def test_starter_latches_on_fill_not_on_send():
    t, m, p, c = make({"AAA": flat(), "BBB": flat()})
    first = t.on_tick(m, p)
    c.t += 1
    assert t.on_tick(m, p) is None                    # waiting, no duplicate
    c.t += STARTER_RETRY_SEC
    retry = t.on_tick(m, p)
    assert retry is not None and retry.side == "buy"  # re-sent: never filled
    t.on_fill("buy", retry.symbol, retry.quantity, retry.price)
    c.t += STARTER_RETRY_SEC
    assert t.on_tick(m, p) is None                    # filled: starter done
    assert first.quantity == retry.quantity == 5


def test_starter_gives_up_after_max_attempts():
    t, m, p, c = make({"AAA": flat()})
    sent = 0
    for _ in range(STARTER_MAX_ATTEMPTS + 2):
        if t.on_tick(m, p) is not None:
            sent += 1
        c.t += STARTER_RETRY_SEC
    assert sent == STARTER_MAX_ATTEMPTS


# ── crossover signal ────────────────────────────────────────────────────────

def test_flat_tape_sits_out():
    t, m, p, _ = filled_trader({"AAA": flat()})
    assert t.on_tick(m, p) is None


def test_uptrend_buys_at_the_ask():
    t, m, p, _ = filled_trader({"AAA": flat(), "UP": trend()})
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.price) == ("UP", "buy", m.best_ask("UP"))


def test_downtrend_sells_only_shares_held():
    t, m, p, _ = filled_trader({"AAA": flat(), "DN": trend(step=-0.001)},
                               FakePortfolio(positions={"DN": 3}))
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("DN", "sell", 3)
    t2, m2, p2, _ = filled_trader({"AAA": flat(), "DN": trend(step=-0.001)},
                                  FakePortfolio(positions={"ZZZ": 6}))
    assert t2.on_tick(m2, p2) is None                 # nothing held: long-only


def test_one_outstanding_order_per_symbol():
    t, m, p, c = filled_trader({"AAA": flat(), "UP": trend()})
    assert t.on_tick(m, p).symbol == "UP"
    c.t += 1
    assert t.on_tick(m, p) is None                    # UP still pending
    c.t += PENDING_TIMEOUT_SEC
    assert t.on_tick(m, p).symbol == "UP"             # expired: may trade again


# ── risk limits inside the trader ───────────────────────────────────────────

def test_concentration_blocks_buy():
    # 160 shares at ~$102 is ~$16.3k, above 5% of $300k
    t, m, p, _ = filled_trader({"AAA": flat(), "UP": trend()},
                               FakePortfolio(positions={"UP": 160}))
    assert t.on_tick(m, p) is None


def test_gross_exposure_blocks_buy():
    # $70k held is above 20% of $300k: the starter sells instead of buying,
    # and the crossover buy on UP stays blocked after that fill.
    t, m, p, c = make({"AAA": flat(), "BIG": flat(px=1_000.0), "UP": trend()},
                      FakePortfolio(positions={"BIG": 70}))
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("BIG", "sell", 5)
    t.on_fill("sell", "BIG", 5, s.price)
    p.positions["BIG"] = 65                           # still $65k > $60k
    c.t += 1
    assert t.on_tick(m, p) is None


def test_starter_sells_held_shares_when_halted():
    p = FakePortfolio(positions={"AAA": 6})
    t, m, _, _ = make({"AAA": flat()}, p)
    t._start_net_worth = p.nw / 0.98                   # already down 2%
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("AAA", "sell", 5)


def test_session_loss_limit_stops_buys_but_allows_sells():
    p = FakePortfolio(positions={"DN": 10})
    t, m, _, c = filled_trader({"AAA": flat(), "UP": trend(), "DN": trend(step=-0.001)}, p)
    p.nw *= 0.98                                      # down 2% since the open
    s = t.on_tick(m, p)
    assert (s.symbol, s.side) == ("DN", "sell")       # exit allowed, UP buy blocked
    c.t += 1
    assert t.on_tick(m, p) is None                    # UP still blocked


def test_stop_loss_sells_bought_shares():
    t, m, p, c = filled_trader({"AAA": flat()})
    p.positions["AAA"] = p.positions.get("AAA", 0) + 5
    m.books["AAA"] = flat(px=98.0)                    # bid 97.95, > 1.5% below ~100.05
    s = t.on_tick(m, p)
    assert (s.symbol, s.side, s.quantity) == ("AAA", "sell", 5)


@pytest.mark.parametrize("held", [0, 2, 5, 9])
def test_sell_never_exceeds_holdings(held):
    t, m, p, _ = filled_trader({"AAA": flat(), "DN": trend(step=-0.002)},
                               FakePortfolio(positions={"DN": held, "ZZZ": 6}))
    s = t.on_tick(m, p)
    if held == 0:
        assert s is None
    else:
        assert s.side == "sell" and s.quantity == min(5, held)
