"""Tests for the Phase-1 correctness layer: closed-candle boundary, the unified
scoring engine, the live risk gate, and the feed abstraction.

These deliberately avoid importing ``realtime_server`` (it requires the
``websockets`` package and calls ``sys.exit`` if it is missing). The logic under
test lives in the library modules, which is exactly the point of the refactor.
"""

import pandas as pd
import pytest

from intraday_advisor.data import (
    candle_age,
    drop_forming_candle,
    last_closed_row,
    parse_interval,
)
from intraday_advisor.feeds import AngelOneFeed, SyntheticFeed, resolve_feed
from intraday_advisor.risk import LiveRiskState
from intraday_advisor.config import RiskConfig
from intraday_advisor.scoring import ScoreWeights, score_signal


def _intraday_df(times, price=100.0):
    idx = pd.to_datetime(times)
    n = len(idx)
    return pd.DataFrame(
        {"Open": [price] * n, "High": [price + 1] * n, "Low": [price - 1] * n,
         "Close": [price] * n, "Volume": [1000] * n},
        index=idx,
    )


# --------------------------------------------------------------------------- #
# parse_interval
# --------------------------------------------------------------------------- #
def test_parse_interval_yahoo_and_angel_styles():
    assert parse_interval("5m") == pd.Timedelta("5min")
    assert parse_interval("15m") == pd.Timedelta("15min")
    assert parse_interval("1h") == pd.Timedelta("60min")
    assert parse_interval("1d") == pd.Timedelta("1D")
    assert parse_interval("FIVE_MINUTE") == pd.Timedelta("5min")
    assert parse_interval("ONE_HOUR") == pd.Timedelta("60min")


def test_parse_interval_unknown_returns_none():
    assert parse_interval("bogus") is None
    assert parse_interval("") is None


# --------------------------------------------------------------------------- #
# drop_forming_candle  (the anti-repaint fix)
# --------------------------------------------------------------------------- #
def test_drop_forming_candle_removes_in_progress_bar():
    df = _intraday_df(["2025-01-02 09:15", "2025-01-02 09:20", "2025-01-02 09:25"])
    # now is inside the 09:25-09:30 window -> last bar still forming
    closed = drop_forming_candle(df, "5m", now="2025-01-02 09:28")
    assert len(closed) == 2
    assert closed.index[-1] == pd.Timestamp("2025-01-02 09:20")


def test_drop_forming_candle_keeps_completed_bar():
    df = _intraday_df(["2025-01-02 09:15", "2025-01-02 09:20", "2025-01-02 09:25"])
    # now is past 09:30 -> the 09:25 bar has fully closed
    closed = drop_forming_candle(df, "5m", now="2025-01-02 09:31")
    assert len(closed) == 3
    assert closed.index[-1] == pd.Timestamp("2025-01-02 09:25")


def test_drop_forming_candle_unknown_interval_is_safe_noop():
    df = _intraday_df(["2025-01-02 09:15", "2025-01-02 09:20"])
    assert len(drop_forming_candle(df, "bogus", now="2025-01-02 09:21")) == 2


def test_drop_forming_candle_handles_tz_aware_index():
    idx = pd.to_datetime(["2025-01-02 09:15", "2025-01-02 09:20", "2025-01-02 09:25"]).tz_localize("Asia/Kolkata")
    df = pd.DataFrame({"Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 1}, index=idx)
    closed = drop_forming_candle(df, "5m", now="2025-01-02 09:28")  # naive now, localized internally
    assert len(closed) == 2


def test_last_closed_row_returns_completed_bar():
    df = _intraday_df(["2025-01-02 09:15", "2025-01-02 09:20", "2025-01-02 09:25"])
    row = last_closed_row(df, "5m", now="2025-01-02 09:28")
    assert row.name == pd.Timestamp("2025-01-02 09:20")


def test_candle_age_is_positive_for_past_bar():
    df = _intraday_df(["2025-01-02 09:15", "2025-01-02 09:20"])
    age = candle_age(df, now="2025-01-02 09:30")
    assert age == pd.Timedelta("10min")


# --------------------------------------------------------------------------- #
# scoring engine
# --------------------------------------------------------------------------- #
def test_score_all_bullish_features_triggers_buy():
    row = {"Bullish_OB": True, "Liquidity_Sweep_Bullish": True, "Bullish_FVG": True,
           "SuperTrend_Dir": 1, "Adaptive_MACD_Hist": 0.5}
    result = score_signal(row, decision_signal="BUY", vwap_relation="above VWAP")
    # 50 + 15 + 15 + 10 + 10 + 5 + 10 + 5 = 100
    assert result.score == 100
    assert result.signal_type == "BUY"
    assert "order_block" in result.components


def test_score_all_bearish_features_triggers_sell():
    row = {"Bearish_OB": True, "Liquidity_Sweep_Bearish": True, "Bearish_FVG": True,
           "SuperTrend_Dir": -1, "Adaptive_MACD_Hist": -0.5}
    result = score_signal(row, decision_signal="SELL", vwap_relation="below VWAP")
    assert result.score == 0
    assert result.signal_type == "SELL"


def test_score_neutral_row_holds():
    result = score_signal({})
    assert result.score == 50
    assert result.signal_type == "HOLD"


def test_score_respects_custom_weights():
    row = {"Bullish_OB": True}
    result = score_signal(row, weights=ScoreWeights(order_block=40.0))
    assert result.score == 90
    assert result.components["order_block"] == 40.0


def test_score_weights_load_defaults_when_no_file(tmp_path):
    assert ScoreWeights.load(str(tmp_path / "missing.json")) == ScoreWeights()


def test_score_weights_load_reads_file(tmp_path):
    p = tmp_path / "w.json"
    p.write_text('{"order_block": 99, "unknown_key": 1}', encoding="utf-8")
    w = ScoreWeights.load(str(p))
    assert w.order_block == 99.0
    assert w.fvg == ScoreWeights().fvg  # untouched keys keep defaults


# --------------------------------------------------------------------------- #
# live risk gate
# --------------------------------------------------------------------------- #
def test_risk_gate_caps_concurrent_open_risk():
    # capital 20000 * 5% budget = 1000 rupees of concurrent open risk
    state = LiveRiskState(config=RiskConfig(capital=20_000.0))
    state.begin_cycle()
    assert state.can_admit(400)
    state.admit(400)
    assert state.can_admit(500)        # 400 + 500 = 900 <= 1000
    assert not state.can_admit(700)    # 400 + 700 = 1100 > 1000


def test_risk_gate_halts_on_daily_loss_limit():
    state = LiveRiskState(config=RiskConfig(capital=20_000.0))  # daily limit 3% = 600
    state.update_equity(19_300)  # realized -700, beyond -600
    assert state.halted
    assert not state.can_admit(1)
    assert "daily loss" in state.status()["halt_reason"]


def test_risk_gate_force_halt_blocks_everything():
    state = LiveRiskState()
    state.force_halt = True
    assert state.halted
    assert not state.can_admit(0)


def test_risk_gate_begin_cycle_resets_open_risk():
    state = LiveRiskState(config=RiskConfig(capital=20_000.0))
    state.admit(500)
    assert state.open_risk == 500
    state.begin_cycle()
    assert state.open_risk == 0


# --------------------------------------------------------------------------- #
# feeds
# --------------------------------------------------------------------------- #
def test_synthetic_feed_is_flagged_synthetic():
    feed = SyntheticFeed()
    assert feed.is_synthetic is True
    df = feed.get_candles("RELIANCE", "5m")
    assert not df.empty
    assert {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns)


def test_angelone_feed_is_not_synthetic_and_lazy():
    # Constructing must not trigger a login (no network in tests).
    feed = AngelOneFeed()
    assert feed.is_synthetic is False


def test_resolve_feed_factory():
    assert isinstance(resolve_feed("synthetic"), SyntheticFeed)
    assert isinstance(resolve_feed("angelone"), AngelOneFeed)
    with pytest.raises(ValueError):
        resolve_feed("nonsense")
