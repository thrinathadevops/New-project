"""Tests for the plain-language indicator readings and the trade playbook.

These exist because the readings are the part a beginner will actually act on.
A reading that says "bullish" when the numbers say otherwise is worse than no
reading at all, so every branch is pinned.
"""

from __future__ import annotations

import pytest

from intraday_advisor.readings import (
    GLOSSARY,
    atr_reading,
    build_playbook,
    build_readings,
    chain_reading,
    ema_reading,
    rsi_reading,
    supertrend_reading,
    vwap_reading,
)


# ===================================================================== EMA ==

def test_ema_fast_above_slow_is_bullish():
    reading = ema_reading({"EMA9": 101.0, "EMA21": 100.0})
    assert reading.state == "bullish"
    assert "CALL" in reading.action


def test_ema_fast_below_slow_is_bearish():
    reading = ema_reading({"EMA9": 99.0, "EMA21": 100.0})
    assert reading.state == "bearish"
    assert "PUT" in reading.action


def test_ema_almost_touching_is_neutral_not_a_signal():
    """A 0.01% gap is noise, not a trend. Calling it bullish invites a trade
    that flips on the next bar."""
    reading = ema_reading({"EMA9": 100.01, "EMA21": 100.0})
    assert reading.state == "neutral"
    assert "Waiting costs nothing" in reading.action


def test_ema_absent_returns_nothing():
    assert ema_reading({}) is None


# ==================================================================== VWAP ==

def test_vwap_above_is_bullish():
    reading = vwap_reading({"Close": 101.0, "VWAP": 100.0})
    assert reading.state == "bullish"
    assert "floor" in reading.meaning


def test_vwap_below_is_bearish():
    reading = vwap_reading({"Close": 99.0, "VWAP": 100.0})
    assert reading.state == "bearish"
    assert "ceiling" in reading.meaning


def test_vwap_far_from_price_is_a_caution():
    """Stretched from VWAP means a wide stop and poor reward for the risk."""
    reading = vwap_reading({"Close": 103.0, "VWAP": 100.0})
    assert reading.state == "caution"
    assert "stretched" in reading.action


def test_vwap_unavailable_explains_itself_rather_than_vanishing():
    """The free index feed reports no volume, so VWAP cannot be computed. An
    indicator that silently disappears looks like a bug and teaches nothing."""
    reading = vwap_reading({"Close": 100.0})
    assert reading is not None
    assert reading.value == "unavailable on this feed"
    assert "volume" in reading.meaning
    assert reading.state == "neutral"


# ===================================================================== RSI ==

@pytest.mark.parametrize("rsi,state", [
    (75, "caution"), (25, "caution"), (60, "bullish"), (40, "bearish"), (50, "neutral"),
])
def test_rsi_states(rsi, state):
    assert rsi_reading({"RSI14": rsi}).state == state


def test_overbought_rsi_warns_against_buying_calls():
    reading = rsi_reading({"RSI14": 78})
    assert "poor place to BUY a CALL" in reading.action


# ============================================================== SuperTrend ==

def test_supertrend_directions():
    assert supertrend_reading({"SuperTrend_Dir": 1}).state == "bullish"
    assert supertrend_reading({"SuperTrend_Dir": -1}).state == "bearish"
    assert supertrend_reading({"SuperTrend_Dir": 0}).state == "neutral"
    assert supertrend_reading({}) is None


# ===================================================================== ATR ==

def test_atr_too_quiet_warns_about_costs():
    reading = atr_reading({"ATR14": 0.05, "Close": 100.0})      # 0.05%
    assert reading.state == "caution"
    assert "cost" in reading.action


def test_atr_too_wild_warns_about_noise():
    reading = atr_reading({"ATR14": 0.80, "Close": 100.0})      # 0.8%
    assert reading.state == "caution"
    assert "noise" in reading.action


def test_atr_normal_is_fine():
    assert atr_reading({"ATR14": 0.20, "Close": 100.0}).state == "neutral"


# =================================================================== chain ==

def test_pcr_extremes_lean_the_right_way():
    assert chain_reading({"pcr": 1.6}).state == "bullish"
    assert chain_reading({"pcr": 0.5}).state == "bearish"
    assert chain_reading({"pcr": 1.0}).state == "neutral"


def test_overhead_call_wall_is_flagged():
    reading = chain_reading({"pcr": 1.0, "spot": 23_000, "ce_wall": 23_050})
    assert reading.state == "caution"
    assert "just overhead" in reading.action


def test_chain_absent_returns_nothing():
    assert chain_reading({}) is None


# ================================================================ assembly ==

def test_build_readings_returns_dicts_with_all_fields():
    signal = {
        "features": {"EMA9": 101, "EMA21": 100, "Close": 101, "VWAP": 100,
                     "RSI14": 58, "ATR14": 0.2, "SuperTrend_Dir": 1},
        "pcr": 1.4, "spot": 100, "ce_wall": 110, "pe_wall": 90,
    }
    readings = build_readings(signal)
    assert len(readings) == 6
    for reading in readings:
        assert set(reading) == {"key", "label", "what", "value", "state",
                                "meaning", "action"}
        assert reading["what"] and reading["meaning"] and reading["action"]


# ================================================================ playbook ==

def tradable_signal(**overrides) -> dict:
    base = {
        "direction": "CALL", "index": "NIFTY", "strike": 23_000,
        "option_type": "CE", "entry_premium": 100.0, "target_premium": 125.0,
        "stop_premium": 80.0, "target_pct": 0.25, "stop_pct": 0.20,
        "features": {"VWAP": 22_950.0},
        "plan": {"lots": 2, "risk_amount": 3_100.0,
                 "risk_pct_of_capital": 0.155, "affordable": True,
                 "breakeven": 101.32},
    }
    base.update(overrides)
    return base


def test_playbook_for_wait_says_do_nothing():
    playbook = build_playbook({"direction": "WAIT"})
    assert playbook["tradable"] is False
    assert "No trade" in playbook["headline"]
    assert "cost of waiting is zero" in playbook["why"]


def test_playbook_covers_entry_size_stop_target_time_and_costs():
    playbook = build_playbook(tradable_signal())
    joined = " ".join(playbook["steps"])

    assert playbook["tradable"] is True
    for label in ("ENTRY", "SIZE", "STOP", "TARGET", "TIME", "COSTS"):
        assert label in joined
    # Concrete numbers, not advice nobody can follow or check afterwards.
    assert "100.00" in joined and "125.00" in joined and "21.48" not in joined
    assert "80.00" in joined


def test_playbook_states_the_invalidation_level():
    """The exit rule that is not the stop: your reason for the trade is gone."""
    playbook = build_playbook(tradable_signal())
    joined = " ".join(playbook["steps"])
    assert "INVALIDATION" in joined
    assert "22,950" in joined


def test_playbook_omits_invalidation_without_vwap():
    playbook = build_playbook(tradable_signal(features={}))
    assert "INVALIDATION" not in " ".join(playbook["steps"])


def test_playbook_warns_that_breakeven_is_above_entry():
    playbook = build_playbook(tradable_signal())
    costs_step = [s for s in playbook["steps"] if s.startswith("COSTS")][0]
    assert "101.32" in costs_step
    assert "not at your" in costs_step


def test_playbook_flags_an_unaffordable_setup_as_information():
    playbook = build_playbook(tradable_signal(
        plan={"lots": 0, "risk_amount": 1_592.0, "risk_pct_of_capital": 0.318,
              "affordable": False, "breakeven": 101.32}))
    assert "information, not a trade" in playbook["why"]


# ================================================================ glossary ==

def test_glossary_covers_the_indicators_on_screen():
    terms = " ".join(entry["term"] for entry in GLOSSARY)
    for expected in ("EMA 9", "VWAP", "RSI 14", "SuperTrend", "ATR 14",
                     "Breakeven", "Net R:R"):
        assert expected in terms


def test_every_glossary_entry_is_complete():
    for entry in GLOSSARY:
        assert set(entry) == {"term", "plain", "detail", "use"}
        assert all(entry[key].strip() for key in entry)
