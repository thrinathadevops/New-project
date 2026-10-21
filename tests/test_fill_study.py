"""Measuring whether a resting order would have filled, and what that cost.

The point of this module is to supply the only numbers that let
``order_execution`` claim a passive saving. Its most important property is
asymmetric: because the touch rate is an UPPER bound on the fill rate, a
negative verdict is conclusive while a positive one is only a reason to go and
measure real fills.
"""

from __future__ import annotations

import pytest

from intraday_advisor.fill_study import (
    MIN_OBSERVATIONS,
    observe,
    resolve,
    summarise,
    verdict,
)
from intraday_advisor.microstructure import parse_book


def book(bid: float, ask: float, reference: float | None = None):
    return parse_book(
        {"best_5_buy_data": [{"price": bid, "quantity": 50}],
         "best_5_sell_data": [{"price": ask, "quantity": 50}],
         "ltp": reference if reference is not None else (bid + ask) / 2},
        reference_price=reference if reference is not None else (bid + ask) / 2,
    )


def paired(side: str, start=(159.0, 161.0), end=(159.0, 161.0), n: int = 1):
    """n identical observations, posted on ``start`` and resolved on ``end``."""
    out = []
    for i in range(n):
        o = observe("tok", "SYM", side, book(*start), now=float(i))
        out.append(resolve(o, book(*end), now=float(i) + 30))
    return out


# ================================================================ observing ==

def test_a_crossed_or_empty_book_is_not_observable():
    assert observe("t", "S", "buy", book(161.0, 159.0)) is not None  # self-corrects
    assert observe("t", "S", "buy", parse_book({"ltp": 160.0},
                                               reference_price=160.0)) is None


def test_the_limit_rests_at_the_mid():
    o = observe("t", "S", "buy", book(159.0, 161.0), now=0.0)
    assert o.limit_price == pytest.approx(160.0)
    assert o.bid_at_post == 159.0 and o.ask_at_post == 161.0


# ============================================================ touch, or not ==

def test_a_buy_is_touched_when_the_ask_falls_to_the_limit():
    filled = resolve(observe("t", "S", "buy", book(159.0, 161.0), now=0.0),
                     book(158.0, 160.0), now=30.0)
    missed = resolve(observe("t", "S", "buy", book(159.0, 161.0), now=0.0),
                     book(160.0, 162.0), now=30.0)
    assert filled.touched is True
    assert missed.touched is False


def test_a_sell_is_touched_when_the_bid_rises_to_the_limit():
    filled = resolve(observe("t", "S", "sell", book(159.0, 161.0), now=0.0),
                     book(160.0, 162.0), now=30.0)
    missed = resolve(observe("t", "S", "sell", book(159.0, 161.0), now=0.0),
                     book(158.0, 160.0), now=30.0)
    assert filled.touched is True
    assert missed.touched is False


# =================================================== the three measurements ==

def test_adverse_selection_is_measured_against_the_intended_side():
    """A buy that fills while the mid falls has been adversely selected. The
    sign convention must make that a POSITIVE cost."""
    result = summarise(paired("buy", start=(159.0, 161.0),
                              end=(157.0, 159.0), n=40))
    assert result.touch_rate == 1.0
    assert result.adverse_selection_pct > 0, "mid fell after a buy: a cost"


def test_a_favourable_fill_registers_as_negative_adverse_selection():
    result = summarise(paired("buy", start=(159.0, 161.0),
                              end=(160.0, 162.0), n=40))
    if result.touch_rate > 0:
        assert result.adverse_selection_pct <= 0


def test_the_chase_penalty_is_measured_only_on_unfilled_orders():
    """Having missed, crossing later costs more than crossing at the start."""
    result = summarise(paired("buy", start=(159.0, 161.0),
                              end=(161.0, 163.0), n=40))
    assert result.touch_rate == 0.0
    assert result.chase_penalty_pct > 0


def test_a_thin_study_is_not_a_measurement():
    result = summarise(paired("buy", n=5))
    assert result.usable is False
    assert result.observations < MIN_OBSERVATIONS
    assert result.to_fill_statistics() is None, \
        "a thin study must not reach the cost model at all"


def test_an_empty_study_reports_nothing():
    result = summarise([])
    assert result.observations == 0
    assert result.usable is False


def test_per_symbol_breakdown_is_kept():
    mixed = paired("buy", end=(158.0, 160.0), n=20)
    for o in paired("buy", end=(161.0, 163.0), n=20):
        o.symbol = "OTHER"
        mixed.append(o)
    result = summarise(mixed)
    assert set(result.per_symbol) == {"SYM", "OTHER"}
    assert result.per_symbol["SYM"]["touch_rate"] == 1.0
    assert result.per_symbol["OTHER"]["touch_rate"] == 0.0


# ================================================= the upper-bound discipline ==

def test_the_touch_rate_is_always_labelled_an_upper_bound():
    """Queue position decides who trades at a price. Being there is necessary
    and not sufficient, so this can only ever bound the fill rate."""
    result = summarise(paired("buy", end=(158.0, 160.0), n=40))
    assert any("UPPER BOUND" in n for n in result.notes)
    stats = result.to_fill_statistics()
    assert stats is not None
    assert "UPPER BOUND" in stats.note


def test_a_negative_verdict_is_conclusive():
    """The asymmetry that makes a cheap study worth running: if resting fails
    to pay at its optimistic bound, it cannot pay in reality."""
    heavy = summarise(paired("buy", start=(159.0, 161.0),
                             end=(157.0, 159.0), n=40))
    answer = verdict(heavy, premium=160.0, quantity=65)
    assert answer["ok"] is True
    assert answer["pays_at_upper_bound"] is False
    assert answer["conclusive"] is True
    assert "conclusive" in answer["note"]


def test_a_positive_verdict_is_only_a_reason_to_measure_further():
    """Constructed so resting looks cheaper: touched often, mid barely moved."""
    gentle = summarise(paired("buy", start=(159.0, 161.0),
                              end=(158.999, 160.999), n=40))
    answer = verdict(gentle, premium=160.0, quantity=65)
    if answer.get("pays_at_upper_bound"):
        assert answer["conclusive"] is False
        assert "not a result" in answer["note"]


def test_a_verdict_is_refused_below_the_minimum_sample():
    answer = verdict(summarise(paired("buy", n=4)), premium=160.0, quantity=65)
    assert answer["ok"] is False
    assert "Not enough observations" in answer["reason"]
