"""Tests for the index-options decision engine and the live position monitor.

These run fully offline: ``suggest_option_trade`` falls back to its synthetic
option-chain model when no broker creds are set and a spot price is supplied,
so no network is required.
"""

import pytest

from intraday_advisor.data import generate_sample_ohlcv
from intraday_advisor.index_options import (
    DEFAULT_STOP_PCT,
    DEFAULT_TARGET_PCT,
    analyze_index_option,
    scan_indices,
)
from intraday_advisor.position_monitor import evaluate_position


# --------------------------------------------------------------------------- #
# position monitor
# --------------------------------------------------------------------------- #
def test_monitor_flags_target_hit():
    s = evaluate_position(100, 130, "CE", qty=75, underlying_direction="CALL")
    assert s.status == "TARGET_HIT"
    assert s.action == "BOOK PROFIT"
    assert round(s.pnl_pct, 2) == 0.30


def test_monitor_flags_stop_hit_with_alarm():
    s = evaluate_position(100, 78, "CE", qty=75, underlying_direction="CALL")
    assert s.status == "STOP_HIT"
    assert s.is_alarm is True            # drives the loud -20% warning
    assert s.action == "EXIT NOW"
    assert s.pnl_amount == (78 - 100) * 75


def test_monitor_warns_when_underlying_turns_against_call():
    # CE held, but the index now reads bearish -> thesis broken
    s = evaluate_position(100, 95, "CE", qty=75, underlying_direction="PUT")
    assert s.status == "WARNING"
    assert s.action == "CONSIDER EXIT"
    assert not s.is_alarm


def test_monitor_warns_when_nearing_stop():
    # -14% with a -20% stop -> past the 0.6 warn fraction (-12%)
    s = evaluate_position(100, 86, "PE", qty=15, underlying_direction="PUT")
    assert s.status == "WARNING"


def test_monitor_on_track():
    s = evaluate_position(100, 108, "CE", qty=75, underlying_direction="CALL")
    assert s.status == "ON_TRACK"
    assert s.action == "HOLD"


def test_monitor_handles_no_live_premium():
    s = evaluate_position(100, 0, "CE", qty=75, underlying_direction="CALL")
    assert s.status == "NO_DATA"


def test_monitor_respects_custom_thresholds():
    # +18% is a target hit if target is 15%
    s = evaluate_position(100, 118, "CE", qty=50, underlying_direction="CALL", target_pct=0.15)
    assert s.status == "TARGET_HIT"


# --------------------------------------------------------------------------- #
# index option engine
# --------------------------------------------------------------------------- #
def test_analyze_index_option_returns_valid_signal():
    candles = generate_sample_ohlcv(seed=11)  # injected -> no network
    sig = analyze_index_option("NIFTY", candles=candles)
    assert sig.index == "NIFTY"
    assert sig.direction in ("CALL", "PUT", "WAIT")
    assert 0 <= sig.confidence <= 100
    assert sig.option_type in ("CE", "PE", "—")


def test_analyze_index_option_premium_plan_math():
    candles = generate_sample_ohlcv(seed=11)
    sig = analyze_index_option("BANKNIFTY", candles=candles, target_pct=0.25, stop_pct=0.20)
    if sig.entry_premium > 0:  # only when an actionable CALL/PUT was produced
        assert sig.target_premium == round(sig.entry_premium * 1.25, 2)
        assert sig.stop_premium == round(sig.entry_premium * 0.80, 2)
    else:
        assert sig.direction == "WAIT"


def test_analyze_index_option_rejects_unknown_index():
    with pytest.raises(ValueError):
        analyze_index_option("SENSEX")


def test_wait_signal_has_no_option_type_premium():
    candles = generate_sample_ohlcv(seed=11)
    sig = analyze_index_option("NIFTY", candles=candles)
    if sig.direction == "WAIT":
        assert sig.option_type == "—"
        assert sig.entry_premium == 0.0


def test_scan_indices_returns_all_indices_sorted():
    sigs = scan_indices()
    assert len(sigs) == 2
    names = {s.index for s in sigs}
    assert names == {"NIFTY", "BANKNIFTY"}
    # actionable (non-WAIT) trades must sort ahead of WAIT
    directions = [s.direction != "WAIT" for s in sigs]
    assert directions == sorted(directions, reverse=True)


def test_defaults_are_25_and_20_percent():
    assert DEFAULT_TARGET_PCT == 0.25
    assert DEFAULT_STOP_PCT == 0.20
