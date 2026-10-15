"""Tests for recording, batch grading and reporting.

The behaviours that matter most here are the ones that stop the ledger from
flattering itself: dedupe (so one bar is one observation), synthetic exclusion
(so demo data never enters a statistic), and the refusal to draw conclusions
from small samples.
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday_advisor.ledger import (
    can_calibrate,
    data_quality,
    expectancy_report,
    grade_pending,
    init_ledger,
    ledger_stats,
    load_graded,
    pending_signals,
    record_cost_fill,
    record_signal,
    score_band_report,
    score_rank_correlation,
    summary,
    wilson_interval,
)
from intraday_advisor.ledger.recorder import extract_features, fitted_cost_model
from intraday_advisor.ledger.schema import connect

LOT = 75


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.sqlite"
    init_ledger(path)
    return path


def a_signal(db, **overrides) -> str | None:
    base = dict(
        bar_time="2026-01-05 10:00:00",
        interval="5m",
        index_name="NIFTY",
        direction="CALL",
        option_type="CE",
        strike=23_000,
        lot_size=LOT,
        entry_premium=100.0,
        stop_premium=80.0,
        target_premium=125.0,
        score=72,
        stop_pct=0.20,
        target_pct=0.25,
        option_token="12345",
        db_path=db,
    )
    base.update(overrides)
    return record_signal(**base)


# ------------------------------------------------------------------ recording

def test_signal_is_recorded_and_retrievable(db):
    signal_id = a_signal(db)
    assert signal_id

    stats = ledger_stats(db)
    assert stats["total_signals"] == 1
    assert stats["real_signals"] == 1
    assert stats["ungraded"] == 1
    assert stats["sessions"] == 1


def test_same_bar_and_contract_is_deduped(db):
    """A dashboard refreshing every 8 seconds must not write 450 rows for one
    5-minute bar."""
    first = a_signal(db)
    second = a_signal(db)
    third = a_signal(db, score=99)       # re-scored on fresher chain data

    assert first is not None
    assert second is None
    assert third is None
    assert ledger_stats(db)["total_signals"] == 1


def test_first_observation_wins(db):
    """The first score is the one you could have acted on."""
    a_signal(db, score=61)
    a_signal(db, score=95)

    with connect(db) as conn:
        scores = [r[0] for r in conn.execute("SELECT score FROM signals")]
    assert scores == [61]


def test_different_strike_is_a_different_signal(db):
    a_signal(db, strike=23_000)
    a_signal(db, strike=23_050)
    assert ledger_stats(db)["total_signals"] == 2


def test_different_bar_is_a_different_signal(db):
    a_signal(db, bar_time="2026-01-05 10:00:00")
    a_signal(db, bar_time="2026-01-05 10:05:00")
    assert ledger_stats(db)["total_signals"] == 2


def test_wait_signal_is_refused(db):
    with pytest.raises(ValueError, match="CALL/PUT"):
        a_signal(db, direction="WAIT")


def test_zero_premium_is_refused(db):
    with pytest.raises(ValueError, match="entry_premium"):
        a_signal(db, entry_premium=0.0)


def test_synthetic_signals_are_marked_and_excluded(db):
    a_signal(db, is_synthetic=True)

    stats = ledger_stats(db)
    assert stats["total_signals"] == 1
    assert stats["real_signals"] == 0
    assert stats["synthetic_signals"] == 1
    # And invisible to the reporting view.
    assert load_graded(db_path=db, resolved_only=False).empty


def test_session_date_is_derived_from_bar_time(db):
    a_signal(db, bar_time="2026-03-11 14:25:00")
    with connect(db) as conn:
        row = conn.execute("SELECT session_date FROM signals").fetchone()
    assert row[0] == "2026-03-11"


def test_features_and_components_round_trip_as_json(db):
    a_signal(
        db,
        features={"RSI14": 61.5, "Bullish_OB": True},
        score_components={"order_block": 15.0, "vwap": 5.0},
    )
    with connect(db) as conn:
        row = conn.execute("SELECT features, score_components FROM signals").fetchone()
    assert '"RSI14": 61.5' in row[0]
    assert '"order_block": 15.0' in row[1]


def test_extract_features_picks_the_known_subset():
    row = {"RSI14": 61.5, "Bullish_OB": True, "SomethingElse": 9, "Close": 100.0}
    features = extract_features(row)

    assert features["RSI14"] == 61.5
    assert features["Bullish_OB"] is True
    assert features["Close"] == 100.0
    assert "SomethingElse" not in features


# -------------------------------------------------------------- batch grading

def path_after(signal: dict, high: float, low: float, close: float) -> pd.DataFrame:
    """Two premium bars positioned after this signal's own bar_time.

    Relative to the signal rather than at fixed timestamps: the grader only
    looks at bars strictly after ``bar_time``, so a fixture pinned to one clock
    time would leave every later signal with no bars and grade as NOT_FILLED.
    """
    entry = float(signal["entry_premium"])
    start = pd.Timestamp(signal["bar_time"])
    index = pd.to_datetime([
        start + pd.Timedelta(minutes=5),
        start + pd.Timedelta(minutes=10),
    ])
    return pd.DataFrame(
        {
            "Open": [entry, entry],
            "High": [entry + 1, high],
            "Low": [entry - 1, low],
            "Close": [entry, close],
            "Volume": [10, 10],
        },
        index=index,
    )


def winning_path(signal: dict) -> pd.DataFrame:
    return path_after(signal, high=130.0, low=99.0, close=128.0)


def losing_path(signal: dict) -> pd.DataFrame:
    return path_after(signal, high=104.0, low=70.0, close=72.0)


def test_grade_pending_persists_outcomes(db):
    a_signal(db)
    result = grade_pending(
        winning_path, db_path=db
    )

    assert result["graded"] == 1
    assert result["by_outcome"]["TARGET"] == 1
    assert ledger_stats(db)["graded"] == 1
    # No longer pending.
    assert pending_signals(db_path=db) == []


def test_grade_pending_survives_a_failing_provider(db):
    """One unfetchable contract must not abort the whole batch."""
    a_signal(db, bar_time="2026-01-05 10:00:00", strike=23_000)
    a_signal(db, bar_time="2026-01-05 10:05:00", strike=23_100)

    def flaky(signal):
        if signal["strike"] == 23_000:
            raise RuntimeError("token lookup failed")
        return winning_path(signal)

    result = grade_pending(flaky, db_path=db)

    assert result["graded"] == 2
    assert result["by_outcome"]["NO_DATA"] == 1
    assert result["by_outcome"]["TARGET"] == 1


def test_missing_bars_grade_as_no_data_not_dropped(db):
    a_signal(db)
    result = grade_pending(lambda s: None, db_path=db)

    assert result["by_outcome"]["NO_DATA"] == 1
    # Still visible in data quality, and excluded from performance.
    assert data_quality(db_path=db)["ungradeable"] == 1
    assert expectancy_report(db_path=db)["trades"] == 0


def test_regrade_overwrites_without_touching_signals(db):
    a_signal(db)
    grade_pending(winning_path, db_path=db)
    assert expectancy_report(db_path=db)["trades"] == 1

    # Regrade against a path that stops out instead.
    grade_pending(
        losing_path, db_path=db, regrade=True
    )
    report = expectancy_report(db_path=db)

    assert report["trades"] == 1
    assert report["wins"] == 0
    assert ledger_stats(db)["total_signals"] == 1


def test_synthetic_signals_are_not_graded_by_default(db):
    a_signal(db, is_synthetic=True)
    result = grade_pending(winning_path, db_path=db)
    assert result["graded"] == 0


# ----------------------------------------------------------------- reporting

def seed_outcomes(db, wins: int, losses: int) -> None:
    """Record and grade a mix of winners and losers."""
    minute = 0
    for index in range(wins + losses):
        bar = f"2026-01-{5 + index // 20:02d} {10 + minute // 60:02d}:{minute % 60:02d}:00"
        minute += 5
        a_signal(db, bar_time=bar, strike=23_000 + index * 50,
                 score=80 if index < wins else 55)
    won = {"TARGET": 0}

    def provider(signal):
        if signal["score"] == 80:
            won["TARGET"] += 1
            return winning_path(signal)
        return losing_path(signal)

    grade_pending(provider, db_path=db)


def test_expectancy_report_computes_net_performance(db):
    seed_outcomes(db, wins=6, losses=4)
    report = expectancy_report(db_path=db)

    assert report["trades"] == 10
    assert report["wins"] == 6
    assert report["win_rate"] == 0.6
    assert report["expectancy"] is not None
    assert report["profit_factor"] > 1


def test_small_samples_refuse_to_conclude(db):
    seed_outcomes(db, wins=6, losses=4)
    verdict = expectancy_report(db_path=db)["verdict"]

    assert "too few" in verdict
    assert "30+" in verdict


def test_win_rate_carries_a_confidence_interval(db):
    seed_outcomes(db, wins=6, losses=4)
    report = expectancy_report(db_path=db)

    # A 60% win rate over 10 trades is consistent with anything from ~31% to ~83%.
    assert report["win_rate_low"] < 0.35
    assert report["win_rate_high"] > 0.80


def test_wilson_interval_narrows_with_sample_size():
    narrow = wilson_interval(600, 1000)
    wide = wilson_interval(6, 10)

    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0]) * 5
    # Always inside [0, 1], including at the extremes.
    assert wilson_interval(0, 5)[0] == 0.0
    assert wilson_interval(5, 5)[1] == 1.0
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_score_band_report_splits_by_score(db):
    seed_outcomes(db, wins=6, losses=4)
    bands = score_band_report(db_path=db)

    assert not bands.empty
    assert set(bands["trades"]) == {6, 4}
    # The high band was seeded as the winning one.
    high = bands[bands["band"] == "80-89"].iloc[0]
    assert high["win_rate"] == 1.0


def test_score_correlation_detects_a_working_score(db):
    seed_outcomes(db, wins=10, losses=10)
    correlation = score_rank_correlation(db_path=db)

    assert correlation["spearman"] > 0.5
    assert "Positive" in correlation["interpretation"]


def test_score_correlation_needs_a_minimum_sample(db):
    seed_outcomes(db, wins=2, losses=2)
    assert score_rank_correlation(db_path=db)["spearman"] is None


def test_calibration_is_refused_on_thin_data(db):
    seed_outcomes(db, wins=6, losses=4)
    gate = can_calibrate(db_path=db)

    assert gate["ready"] is False
    assert gate["trades_needed"] > 100
    assert "fit noise" in gate["reason"]


def test_data_quality_flags_fill_slippage(db):
    """Signals quoting Rs 100 that fill at Rs 108 mean the screen lies."""
    a_signal(db)
    grade_pending(
        lambda s: pd.DataFrame(
            {"Open": [100.0, 108.0], "High": [101.0, 140.0],
             "Low": [99.0, 107.0], "Close": [100.0, 138.0], "Volume": [10, 10]},
            index=pd.to_datetime(["2026-01-05 10:00", "2026-01-05 10:05"]),
        ),
        db_path=db,
    )
    quality = data_quality(db_path=db)

    assert quality["median_fill_slippage"] == 8.0
    assert any("cannot get" in f for f in quality["flags"])


def test_summary_renders_without_data(db):
    text = summary(db_path=db)
    assert "LEDGER" in text
    assert "Nothing resolved yet" in text


def test_summary_renders_with_data(db):
    seed_outcomes(db, wins=6, losses=4)
    text = summary(db_path=db)

    assert "Win rate" in text
    assert "Expectancy" in text
    assert "VERDICT" in text


# --------------------------------------------------------- cost calibration

def test_record_cost_fill_fits_and_stores_the_spread(db):
    implied = record_cost_fill(
        quantity=LOT, entry_premium=100.0, exit_premium=110.0,
        actual_total_cost=200.0, index_name="NIFTY", db_path=db,
    )
    assert implied > 0

    with connect(db) as conn:
        row = conn.execute("SELECT quantity, implied_half_spread_pct FROM cost_fills").fetchone()
    assert row[0] == LOT
    assert row[1] == pytest.approx(implied)


def test_fitted_cost_model_needs_three_fills(db):
    default_spread = fitted_cost_model(db).half_spread_pct

    for _ in range(2):
        record_cost_fill(quantity=LOT, entry_premium=100.0, exit_premium=110.0,
                         actual_total_cost=300.0, db_path=db)
    assert fitted_cost_model(db).half_spread_pct == default_spread

    record_cost_fill(quantity=LOT, entry_premium=100.0, exit_premium=110.0,
                     actual_total_cost=300.0, db_path=db)
    assert fitted_cost_model(db).half_spread_pct != default_spread
