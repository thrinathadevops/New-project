"""Reading the archive back, which is the half that makes storing it worth it.

Every table the archive writes is read here. The one that matters most is the
agent scorecard: AGENTS.md calls "would the vetoed trades have made money?" the
only question that settles the agent layer, and it was answerable for weeks
with nothing asking it.

One rule runs through all of these: **a session is the unit of observation, not
a trade.** Trades inside one day share that day's regime, so pooling them and
dividing by the square root of the count overstates precision -- an error this
project has already made twice.
"""

from __future__ import annotations

import pytest

from intraday_advisor.calibration import (
    MIN_SAMPLES_PER_HOUR,
    agent_scorecard,
    conviction_calibration,
    measured_hours,
    odds_reliability,
    render_agent,
    render_conviction,
    render_reliability,
    render_spreads,
    spread_shape,
    volatility_history,
)
from intraday_advisor.ledger.schema import ENGINE_VERSION, connect, init_ledger


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "ledger.sqlite")
    init_ledger(path)
    return path


def add_signal(db, *, signal_id, session, stance, pnl, outcome="STOP",
               strike=24500.0):
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO signals (signal_id, dedupe_key, emitted_at, bar_time, "
            "interval, session_date, index_name, direction, option_type, "
            "strike, lot_size, entry_premium, stop_premium, target_premium, "
            "score, is_live, is_synthetic, engine_version, agent_stance) "
            "VALUES (?,?,?,?, '5m', ?, 'NIFTY', 'CALL', 'CE', ?, 65, 194, 155, "
            "242, 50, 1, 0, ?, ?)",
            (signal_id, f"{signal_id}-key", f"{session}T10:00:00+05:30",
             f"{session} 10:00:00+05:30", session, strike, ENGINE_VERSION,
             stance))
        conn.execute(
            "INSERT INTO signal_outcomes (signal_id, outcome, "
            "net_pnl_per_lot, graded_at, grader_version) "
            "VALUES (?, ?, ?, ?, 'v1')",
            (signal_id, outcome, pnl, f"{session}T16:00:00+05:30"))
        conn.commit()


# ================================================================== spreads ==

def test_an_empty_archive_leaves_the_prior_standing(db):
    result = spread_shape(db_path=db)
    assert result["basis"] == "prior"
    assert result["measured_hours"] == []
    assert all(row["source"] == "prior" for row in result["rows"])


def test_an_hour_below_the_threshold_keeps_the_prior(db):
    from intraday_advisor.archive import record_spread_sample

    for i in range(MIN_SAMPLES_PER_HOUR - 1):
        record_spread_sample(db_path=db, index_name="NIFTY",
                             tradingsymbol=f"c{i}", half_spread_pct=0.02,
                             observed_at=f"2026-11-05T10:{i:02d}:00+05:30")
    assert measured_hours(db_path=db) == []
    assert spread_shape(db_path=db)["basis"] == "prior"


def test_an_hour_at_the_threshold_becomes_measured(db):
    from intraday_advisor.archive import record_spread_sample

    for i in range(MIN_SAMPLES_PER_HOUR):
        record_spread_sample(db_path=db, index_name="NIFTY",
                             tradingsymbol=f"c{i}", half_spread_pct=0.02,
                             observed_at=f"2026-11-05T10:{i:02d}:00+05:30")
    assert measured_hours(db_path=db) == [10]
    result = spread_shape(db_path=db)
    assert 10 in result["measured_hours"]
    assert any(row["hour"] == 10 and row["source"] == "measured"
               for row in result["rows"])


def test_the_measured_shape_reaches_the_live_cost_model(db):
    """The whole point of storing them. Without this the samples accumulate and
    change nothing."""
    import datetime as dt

    from intraday_advisor.archive import record_spread_sample
    from intraday_advisor.microstructure import spread_multiplier_at

    before = spread_multiplier_at(dt.time(10, 30), db_path=db)
    assert before["basis"] == "prior"

    for i in range(MIN_SAMPLES_PER_HOUR):
        record_spread_sample(db_path=db, index_name="NIFTY",
                             tradingsymbol=f"c{i}", half_spread_pct=0.05,
                             observed_at=f"2026-11-05T10:{i:02d}:00+05:30")
    after = spread_multiplier_at(dt.time(10, 30), db_path=db)
    assert after["basis"] == "measured"
    assert "measured from your own book" in after["note"]


def test_a_missing_database_leaves_the_cost_model_on_the_prior(tmp_path):
    """The cost model must never depend on the archive being reachable.

    It must also not CREATE it. `init_ledger` makes parent directories, so an
    earlier version of this test left a stray `no/such/` tree in the repository
    root -- a read path with a side effect, called on every verdict.
    """
    import datetime as dt

    from intraday_advisor.microstructure import spread_multiplier_at

    missing = tmp_path / "absent" / "ledger.sqlite"
    result = spread_multiplier_at(dt.time(10, 30), db_path=str(missing))
    assert result["basis"] == "prior"
    assert not missing.parent.exists(), "asking must not build a ledger"


def test_the_spread_report_names_the_threshold(db):
    text = render_spreads(spread_shape(db_path=db))
    assert str(MIN_SAMPLES_PER_HOUR) in text
    assert "prior" in text


# ============================================================= agent layer ==

def test_an_empty_ledger_says_the_agent_has_no_record(db):
    result = agent_scorecard(db_path=db)
    assert result["trades"] == 0
    assert "no record" in result["verdict"]


def test_a_veto_that_blocked_losers_is_credited(db):
    add_signal(db, signal_id="a", session="2026-11-03", stance="VETO",
               pnl=-900.0)
    add_signal(db, signal_id="b", session="2026-11-04", stance="VETO",
               pnl=-800.0, strike=24600.0)
    add_signal(db, signal_id="c", session="2026-11-03", stance="PROCEED",
               pnl=-100.0, strike=24700.0)
    add_signal(db, signal_id="d", session="2026-11-04", stance="PROCEED",
               pnl=-50.0, strike=24800.0)
    result = agent_scorecard(db_path=db)
    assert "earning its place" in result["verdict"]


def test_a_veto_that_blocked_the_winners_is_called_out(db):
    """The failure the measurement exists to catch. If the agent is blocking
    winners that must show up, not be reported as success."""
    add_signal(db, signal_id="a", session="2026-11-03", stance="VETO",
               pnl=800.0, outcome="TARGET")
    add_signal(db, signal_id="b", session="2026-11-04", stance="VETO",
               pnl=600.0, outcome="TARGET", strike=24600.0)
    add_signal(db, signal_id="c", session="2026-11-03", stance="PROCEED",
               pnl=-500.0, strike=24700.0)
    add_signal(db, signal_id="d", session="2026-11-04", stance="PROCEED",
               pnl=-400.0, strike=24800.0)
    result = agent_scorecard(db_path=db)
    assert "blocking the better trades" in result["verdict"]
    assert "thresholds" in result["verdict"]


def test_an_all_vetoed_ledger_refuses_to_score_the_veto(db):
    """This is the live situation: every graded signal was vetoed, so there is
    no approved group and the veto cannot be scored -- only observed."""
    add_signal(db, signal_id="a", session="2026-11-03", stance="VETO",
               pnl=-900.0)
    add_signal(db, signal_id="b", session="2026-11-04", stance="VETO",
               pnl=-800.0, strike=24600.0)
    result = agent_scorecard(db_path=db)
    assert "cannot be scored yet" in result["verdict"]


def test_sessions_are_counted_separately_from_trades(db):
    for i in range(6):
        add_signal(db, signal_id=f"s{i}", session="2026-11-03", stance="VETO",
                   pnl=-500.0, strike=24500.0 + i * 100)
    result = agent_scorecard(db_path=db)
    record = result["records"][0]
    assert record["trades"] == 6
    assert record["sessions"] == 1
    assert any("not independent" in n for n in result["notes"])


def test_a_thin_sample_is_flagged(db):
    add_signal(db, signal_id="a", session="2026-11-03", stance="VETO",
               pnl=-900.0)
    result = agent_scorecard(db_path=db)
    assert any("describes those days" in n for n in result["notes"])


def test_an_unruled_signal_is_labelled_not_dropped(db):
    add_signal(db, signal_id="a", session="2026-11-03", stance=None, pnl=-900.0)
    result = agent_scorecard(db_path=db)
    assert result["records"][0]["stance"] == "UNRULED"


def test_the_agent_report_renders(db):
    add_signal(db, signal_id="a", session="2026-11-03", stance="VETO",
               pnl=-900.0)
    text = render_agent(agent_scorecard(db_path=db))
    assert "AGENT SCORECARD" in text
    assert "VETO" in text


# ============================================================== conviction ==

def _observed(db, session, conviction, regime, move):
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO observations (observed_at, session_date, index_name, "
            "spot, regime, conviction) VALUES (?, ?, 'NIFTY', 24500, ?, ?)",
            (f"{session}T10:{int(conviction) % 60:02d}:00+05:30", session,
             regime, conviction))
        conn.execute(
            "INSERT OR REPLACE INTO daily_record (session_date, built_at, "
            "spot_move_pct) VALUES (?, '2026-11-05T16:00:00+05:30', ?)",
            (session, move))
        conn.commit()


def test_conviction_needs_daily_records_to_be_calibrated(db):
    result = conviction_calibration(db_path=db)
    assert result["observations"] == 0
    assert "cannot be calibrated" in result["note"]


def test_a_correct_directional_call_counts_as_a_hit(db):
    _observed(db, "2026-11-03", 85, "TRENDING_UP", 0.5)
    result = conviction_calibration(db_path=db)
    assert result["buckets"][0]["realised_pct"] == 100.0


def test_a_wrong_directional_call_counts_as_a_miss(db):
    _observed(db, "2026-11-03", 85, "TRENDING_UP", -0.5)
    assert conviction_calibration(db_path=db)["buckets"][0]["realised_pct"] == 0.0


def test_a_down_regime_is_right_when_the_day_falls(db):
    _observed(db, "2026-11-03", 85, "TRENDING_DOWN", -0.5)
    assert conviction_calibration(db_path=db)["buckets"][0]["realised_pct"] == 100.0


def test_a_range_regime_is_excluded_rather_than_counted_wrong(db):
    """RANGE and VOLATILE make no directional claim. Scoring them as misses
    would punish the regime for being honest."""
    _observed(db, "2026-11-03", 85, "RANGE", 0.5)
    result = conviction_calibration(db_path=db)
    assert result["buckets"] == []
    assert any("no directional claim" in n for n in result["notes"])


def test_the_interval_is_reported_so_a_thin_bucket_is_visible(db):
    _observed(db, "2026-11-03", 85, "TRENDING_UP", 0.5)
    bucket = conviction_calibration(db_path=db)["buckets"][0]
    assert bucket["ci_low"] < bucket["realised_pct"]
    assert bucket["ci_high"] >= bucket["realised_pct"]


def test_the_conviction_report_renders(db):
    _observed(db, "2026-11-03", 85, "TRENDING_UP", 0.5)
    assert "CONVICTION CALIBRATION" in render_conviction(
        conviction_calibration(db_path=db))


# ============================================================= reliability ==

def test_unresolved_predictions_are_counted_not_treated_as_misses(db):
    from intraday_advisor.archive import record_prediction

    record_prediction(db_path=db, kind="forward_odds", index_name="NIFTY",
                      predicted=0.47, made_at="2026-11-05T10:00:00+05:30")
    result = odds_reliability(db_path=db)
    assert result["resolved"] == 0
    assert result["pending"] == 1
    assert "none resolved" in result["note"]


def test_a_resolved_prediction_lands_in_its_band(db):
    from intraday_advisor.archive import record_prediction, resolve_prediction

    for i in range(4):
        made = f"2026-11-05T10:{i:02d}:00+05:30"
        record_prediction(db_path=db, kind="forward_odds",
                          index_name=f"IDX{i}", predicted=0.48, made_at=made)
        resolve_prediction(db_path=db, made_at=made, kind="forward_odds",
                           index_name=f"IDX{i}", realised=1.0 if i < 2 else 0.0)
    result = odds_reliability(db_path=db)
    assert result["resolved"] == 4
    band = result["buckets"][0]
    assert band["n"] == 4
    assert band["realised"] == 50.0


def test_the_reliability_report_renders(db):
    assert "RELIABILITY" in render_reliability(odds_reliability(db_path=db))


# ======================================================= volatility history ==

def test_volatility_history_says_when_nothing_is_archived(db):
    result = volatility_history(db_path=db)
    assert result["sessions"] == 0
    assert "ledger_cli archive" in result["note"]


def test_volatility_history_refuses_to_fit_har_on_a_thin_sample(db):
    """HAR-RV carries a monthly term. Fitting it on a handful of days produces
    a number with the shape of a forecast and none of the substance."""
    import numpy as np
    import pandas as pd

    from intraday_advisor.archive import archive_bars

    rng = np.random.default_rng(1)
    rows, stamps = [], []
    price = 24_500.0
    for day in range(5):
        start = (pd.Timestamp("2026-11-02", tz="Asia/Kolkata")
                 + pd.Timedelta(days=day, hours=9, minutes=15))
        for bar in range(75):
            close = price * (1 + rng.normal(0, 0.001))
            rows.append((price, max(price, close) * 1.0004,
                         min(price, close) * 0.9996, close, 1000.0))
            stamps.append(start + pd.Timedelta(minutes=5 * bar))
            price = close
    frame = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"],
                         index=pd.DatetimeIndex(stamps))
    archive_bars(symbols=("^NSEI",), db_path=db,
                 fetch=lambda s, period=None, interval=None: frame)

    result = volatility_history(db_path=db, symbol="^NSEI")
    assert result["sessions"] == 5
    assert "forecast" not in result
    # The model's OWN reason, surfaced rather than replaced by a bare "not
    # fitted". The lags consume the start of the series, so the usable-row
    # count is what binds -- not the session count a caller can see.
    assert "usable rows" in result["note"]
    assert "100 needed" in result["note"]
    assert "Keep archiving" in result["note"]
