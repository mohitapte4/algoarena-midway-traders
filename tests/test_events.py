"""Tests for team.events: the edge-vs-fee table and plan price levels."""

from __future__ import annotations

import pytest

from team.events import FADE, RIDE, EventPlan, designed_edge, size_fraction

FEE = 0.003                 # hosted venue taker fee
ROUND_TRIP = 2 * FEE


def test_recap_edge_table():
    # Friday's two events: AAPL +6.2%, market-wide −1.97%, overshoot 1.3.
    ride = designed_edge(RIDE, 0.061958, 1.3)
    fade = designed_edge(FADE, -0.019661, 1.3)
    extra = (1.3 - 1) * 0.019661          # buy at the low, sell at the settle
    assert ride == pytest.approx(0.0415, abs=5e-4) and ride > ROUND_TRIP
    assert fade == pytest.approx(0.0157, abs=5e-4) and fade > ROUND_TRIP
    assert extra < ROUND_TRIP             # 0.59% < 0.60%: not traded


def test_size_fraction_scales_with_edge():
    assert size_fraction(0.042, ROUND_TRIP) == 1.0
    assert size_fraction(ROUND_TRIP, ROUND_TRIP) == 0.0
    assert size_fraction(0.003, ROUND_TRIP) == 0.0
    assert size_fraction(0.009, ROUND_TRIP) == pytest.approx(0.5)


def test_small_move_has_no_edge_after_fees():
    assert size_fraction(designed_edge(RIDE, 0.008, 1.3), ROUND_TRIP) == 0.0


def plan(mode, pct, pre=100.0):
    return EventPlan(symbol="AAPL", mode=mode, pct=pct, pre=pre, t0=0.0,
                     ramp_sec=15.0, overshoot=1.3, target_shares=100)


def test_ride_levels():
    p = plan(RIDE, 0.06)
    assert p.buy_cap == pytest.approx(103.0)
    assert p.target == pytest.approx(100 * (1 + 0.9 * 1.3 * 0.06))
    assert p.entry_end == 7.5 and p.deadline == 45.0
    p.record("buy", 10, 101.0)
    assert p.stop_price == pytest.approx(101.0 * 0.97)
    p.record("sell", 10, 107.0)
    assert p.held == 0 and p.gross_pnl() == pytest.approx(60.0)


def test_fade_levels():
    p = plan(FADE, -0.02)
    assert p.sell_floor == pytest.approx(99.0)
    assert p.rebuy_cap == pytest.approx(98.0)
    assert p.rebuy_start == 15.0
    p.record("sell", 20, 99.5)
    p.record("buy", 15, 97.6)
    assert p.owed == 5
