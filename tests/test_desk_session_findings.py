"""Gaps found by actually trading the dashboard on 2026-10-21.

Each of these was visible on screen while operating the desk, and none was
caught by the existing suite. They are grouped by the question a trader was
trying to answer when the gap surfaced.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.ledger.sizing import plan_position
from intraday_advisor.scoring import score_signal


# ============================== "how much am I actually risking?" ===========

def nifty_plan(fraction: float, premium: float = 170.60):
    return plan_position(
        available_capital=20_000, entry_premium=premium,
        stop_premium=round(premium * 0.80, 2),
        target_premium=round(premium * 1.25, 2),
        lot_size=65, max_capital_fraction=fraction,
    )


def test_the_plan_reports_what_the_deploy_cap_actually_leaves():
    """The UNAFFORDABLE panel could only show TOTAL capital, so it read
    "needs Rs 11,089 / you have Rs 20,000" -- which looks like an arithmetic
    error when the deploy cap was the real constraint."""
    plan = nifty_plan(0.05)
    assert plan.affordable is False
    assert plan.available_capital == 20_000
    assert plan.spendable_capital == 1_000
    assert plan.max_capital_fraction == 0.05


def test_a_full_deploy_reports_the_whole_balance_as_spendable():
    plan = nifty_plan(1.0)
    assert plan.affordable is True
    assert plan.spendable_capital == plan.available_capital


def test_one_indivisible_lot_can_cost_more_than_the_deploy_cap_allows():
    """The honest shape of the small-account problem: the cap cannot be
    honoured at all, because a lot does not divide."""
    plan = nifty_plan(0.30)
    assert plan.capital_per_lot > plan.spendable_capital
    assert plan.lots == 0
    assert plan.risk_band == "UNAFFORDABLE"
    assert any("Cannot afford one lot" in w for w in plan.warnings)


# ===================== "why does the alternatives table disagree?" ==========

def chain_fixture() -> dict:
    """A chain shaped like the live one: spot 22,722, put wall at 22,500."""
    strikes = [22_400, 22_500, 22_600, 22_700, 22_800, 22_900, 23_000]
    return {
        "spot": 22_722.0,
        "atm": 22_700,
        "expiry": "27OCT2026",
        "max_pe_wall": 22_500.0,
        "max_ce_wall": 23_000.0,
        "df": pd.DataFrame({
            "strike": [float(s) for s in strikes],
            "ce_ltp": [330.0, 290.0, 250.0, 199.0, 150.0, 110.0, 79.0],
            "pe_ltp": [80.0, 93.0, 125.0, 167.0, 217.0, 277.0, 346.0],
            # The put wall carries far more OI than anything else.
            "ce_oi": [90_000, 2_100_000, 1_500_000, 2_900_000, 6_200_000,
                      7_900_000, 16_000_000],
            "pe_oi": [1_500_000, 9_270_000, 6_000_000, 6_900_000, 6_000_000,
                      4_600_000, 7_200_000],
        }),
    }


def build(fraction: float = 1.0, direction: str = "PUT"):
    from intraday_advisor.candidates import build_candidates
    return build_candidates(
        "NIFTY", chain_fixture(), direction=direction,
        available_capital=20_000, lot_size=65, step=100.0,
        max_capital_fraction=fraction, limit=10,
    )


def test_the_alternatives_table_respects_the_deploy_cap():
    """It did not accept the cap at all, so at a 5% cap the signal card read
    UNAFFORDABLE while the table below it offered eight contracts at 11-20%
    risk. Two answers to the same question on one screen."""
    full = build(1.0)
    capped = build(0.05)

    assert any(c["affordable"] for c in full), "sanity: tradeable at 100%"
    assert not any(c["affordable"] for c in capped), \
        "nothing is affordable on Rs 1,000 when a lot costs Rs 5,000+"


def test_the_cap_cannot_invent_more_lots_than_the_full_account_would():
    for capped, full in zip(build(0.5), build(1.0)):
        assert capped["lots"] <= full["lots"]


# ================== "is this strike fighting the whole chain?" ==============

def test_a_put_strike_on_the_put_wall_is_penalised():
    """Live: spot 22,722 with the biggest put OI on the board at 22,500. The
    22500 PE was offered as a PUT candidate and credited +5 for "Heavy open
    interest - liquid", when buying it bets on the most defended support
    breaking."""
    rows = {(c["strike"], c["option_type"]): c for c in build(direction="PUT")}
    on_wall = rows.get((22_500.0, "PE"))
    above_wall = rows.get((22_700.0, "PE"))
    assert on_wall and above_wall

    assert any("PUT wall" in n for n in on_wall["notes"])
    assert on_wall["score"] < above_wall["score"], \
        "the strike that must break the wall must not outrank one above it"


def test_a_strike_with_room_to_the_wall_says_where_to_book():
    rows = {(c["strike"], c["option_type"]): c for c in build(direction="PUT")}
    above = rows.get((22_700.0, "PE"))
    assert any("Room down to" in n and "book" in n for n in above["notes"])


def test_a_call_strike_above_the_call_wall_is_penalised():
    rows = {(c["strike"], c["option_type"]): c for c in build(direction="CALL")}
    on_wall = rows.get((23_000.0, "CE"))
    if on_wall:
        assert any("CALL wall" in n for n in on_wall["notes"])


# ===================== "which of these setups is strongest?" ===============

STRONG_BEAR = {
    "SuperTrend_Dir": -1, "Adaptive_MACD_Hist": -1, "Bearish_OB": True,
    "Liquidity_Sweep_Bearish": True, "Bearish_FVG": True,
    "ADX": 49, "Minus_DI": 40, "Plus_DI": 10, "PSAR_Dir": -1,
    "Squeeze_Mom": -1,
}


def test_the_score_saturates_so_ranking_needs_the_raw_value():
    """32 components carry 193 points against a baseline of 50, so the 0-100
    score rails once a quarter of the evidence agrees. Measured live: all 30
    ranked stocks scored exactly 0, a 30-way tie."""
    strong = score_signal(dict(STRONG_BEAR), decision_signal="SELL",
                          vwap_relation="below VWAP")
    overwhelming = score_signal(
        {**STRONG_BEAR, "ADX": 60, "Minus_DI": 50, "Plus_DI": 5,
         "Stoch_K": 85, "Stoch_D": 90, "MFI14": 90},
        decision_signal="SELL", vwap_relation="below VWAP",
    )

    assert strong.score == overwhelming.score == 0, "both rail, as expected"
    assert overwhelming.raw_score < strong.raw_score, \
        "the raw value must still separate them"


def test_raw_score_matches_the_clamped_one_when_nothing_saturates():
    neutral = score_signal({})
    assert neutral.score == 50
    assert neutral.raw_score == pytest.approx(50.0)


def test_the_stock_table_carries_the_raw_value():
    from intraday_advisor.stock_screener import StockRow, screen_one

    row = StockRow(symbol="X", price=1.0, change_pct=0.0, score=50,
                   signal="HOLD")
    assert hasattr(row, "raw_score")

    rng = np.random.default_rng(3)
    bars = 300
    close = 1000.0 * np.exp(np.cumsum(rng.normal(-0.0012, 0.004, size=bars)))
    spread = np.abs(rng.normal(0, 0.004, size=bars)) * close
    frame = pd.DataFrame({
        "Open": close, "High": close + spread, "Low": close - spread,
        "Close": close, "Volume": rng.integers(100_000, 900_000, size=bars),
    }, index=pd.date_range("2026-10-21 09:15", periods=bars, freq="5min"))

    scored = screen_one("X", frame, capital=200_000, risk_pct=0.02)
    assert scored.raw_score is not None


# ============================ "is the feed actually alive?" ================

def test_index_subscriptions_count_towards_the_tick_status():
    """The header read "TICKS --" while index ticks were arriving 0.02s old and
    driving the chart and every spot figure, because the badge counted only
    option-contract subscriptions."""
    from intraday_advisor import live_ticks

    before = live_ticks.status()["subscribed"]
    live_ticks.register_subscribed(["26000", "26009"])
    after = live_ticks.status()["subscribed"]
    assert after >= before + 2


def test_registering_nothing_is_harmless():
    from intraday_advisor import live_ticks
    live_ticks.register_subscribed(["26000"])
    before = live_ticks.status()["subscribed"]
    live_ticks.register_subscribed([])
    live_ticks.register_subscribed([None, ""])
    assert live_ticks.status()["subscribed"] == before
