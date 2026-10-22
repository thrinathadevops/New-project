"""Session-over-session comparison: is it an edge, or was one day lucky?

Three graded sessions gave -1,222 / -1,262 / +478 per signal. The pooled
per-trade interval read "entirely below zero" and was overstating precision,
because trades inside a day share the day's regime and are not independent
draws. Treating each SESSION as one observation, the interval straddles zero.
Same lesson as the overlapping-window correction.
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday_advisor.sessions import (
    MIN_SESSIONS,
    SessionSeries,
    render,
    session_series,
)


def seed(db: str, rows: list[tuple[str, float, str]]) -> None:
    """Write graded signals: (session_date, net_per_lot, outcome)."""
    from intraday_advisor.ledger.recorder import record_signal
    from intraday_advisor.ledger.grader import GradedOutcome, save_outcome
    from intraday_advisor.ledger.schema import init_ledger

    init_ledger(db)
    for i, (session, net, outcome) in enumerate(rows):
        signal_id = record_signal(
            bar_time=f"{session}T10:{i % 60:02d}:00+05:30", interval="5m",
            index_name="NIFTY", direction="PUT", option_type="PE",
            strike=22_700.0 + i, lot_size=65, entry_premium=100.0,
            stop_premium=80.0, target_premium=125.0, score=50,
            option_token=f"t{i}", db_path=db,
        )
        assert signal_id, "each row needs a distinct bar/strike"
        save_outcome(
            GradedOutcome(
                signal_id=signal_id, outcome=outcome,
                gross_pnl_per_lot=net, net_pnl_per_lot=net,
                net_pnl_pct=net / 6500.0,
            ),
            db_path=db,
        )


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "ledger.sqlite")


# ================================================= the unit of observation ==

def test_each_session_counts_once_not_each_trade(db):
    """A day with 100 trades and a day with 5 are ONE observation each.
    Pooling and dividing by sqrt(trades) overstates precision badly."""
    seed(db, [("2026-10-20", -1000.0, "STOP")] * 5
             + [("2026-10-21", 1000.0, "TARGET")] * 100)
    series = session_series(db_path=db)

    assert series.sessions == 2
    # Mean of the two SESSION means is zero, not +905 (the pooled trade mean).
    assert series.mean_per_signal == pytest.approx(0.0, abs=1.0)


def test_a_single_session_concludes_nothing(db):
    seed(db, [("2026-10-22", 500.0, "TARGET")] * 40)
    series = session_series(db_path=db)
    assert series.sessions == 1
    assert "nothing can be concluded" in series.verdict


def test_an_empty_ledger_is_reported_not_crashed(db):
    from intraday_advisor.ledger.schema import init_ledger
    init_ledger(db)
    series = session_series(db_path=db)
    assert series.sessions == 0
    assert series.notes


# ============================================ spread versus the effect ======

def test_a_dominant_spread_is_called_out(db):
    """The measured case: a spread of Rs 993 against a mean of Rs 668."""
    seed(db, [("2026-10-16", -1222.0, "STOP")] * 10
             + [("2026-10-21", -1262.0, "STOP")] * 10
             + [("2026-10-22", 478.0, "TARGET")] * 10)
    series = session_series(db_path=db)

    assert series.sd_between_sessions > abs(series.mean_per_signal)
    assert any("dominates the effect" in n for n in series.notes)


def test_the_interval_straddles_zero_on_the_real_shape(db):
    """Three sessions, one positive: the pooled per-trade interval said
    'entirely below zero'; the per-session one does not."""
    seed(db, [("2026-10-16", -1222.0, "STOP")] * 10
             + [("2026-10-21", -1262.0, "STOP")] * 10
             + [("2026-10-22", 478.0, "TARGET")] * 10)
    series = session_series(db_path=db)

    assert series.ci_low < 0 < series.ci_high
    assert "too few to call" in series.verdict


def test_consistency_is_what_earns_a_verdict(db):
    """Five sessions all positive, with a small spread, is what a direction
    needs -- not one good day averaged with bad ones."""
    rows = []
    for day, value in enumerate((300.0, 340.0, 290.0, 320.0, 310.0), start=1):
        rows += [(f"2026-10-{day:02d}", value, "TARGET")] * 8
    seed(db, rows)
    series = session_series(db_path=db)

    assert series.sessions >= MIN_SESSIONS
    assert series.positive_sessions == series.sessions
    assert series.ci_low > 0
    assert series.verdict == "consistently positive across sessions"


def test_consistently_negative_is_also_reportable(db):
    rows = []
    for day, value in enumerate((-300.0, -340.0, -290.0, -320.0, -310.0), start=1):
        rows += [(f"2026-11-{day:02d}", value, "STOP")] * 8
    seed(db, rows)
    series = session_series(db_path=db)
    assert series.ci_high < 0
    assert series.verdict == "consistently negative across sessions"


# ======================================== how long until it means anything ===

def test_sessions_needed_is_reported_from_the_observed_spread(db):
    """Answers "how long until this means anything" from the data rather than
    from a rule of thumb. On the real three sessions it came to about 34."""
    seed(db, [("2026-10-16", -1222.0, "STOP")] * 10
             + [("2026-10-21", -1262.0, "STOP")] * 10
             + [("2026-10-22", 478.0, "TARGET")] * 10)
    series = session_series(db_path=db)

    assert series.sessions_needed is not None
    assert series.sessions_needed > series.sessions


def test_a_tighter_spread_needs_fewer_sessions(db, tmp_path):
    noisy = str(tmp_path / "noisy.sqlite")
    calm = str(tmp_path / "calm.sqlite")
    seed(noisy, [("2026-10-01", -1500.0, "STOP")] * 5
                + [("2026-10-02", 1500.0, "TARGET")] * 5
                + [("2026-10-03", -300.0, "STOP")] * 5)
    seed(calm, [("2026-10-01", -300.0, "STOP")] * 5
               + [("2026-10-02", -320.0, "STOP")] * 5
               + [("2026-10-03", -310.0, "STOP")] * 5)

    assert (session_series(db_path=calm).sessions_needed
            < session_series(db_path=noisy).sessions_needed)


# ==================================================================== output ==

def test_the_report_shows_every_session(db):
    seed(db, [("2026-10-16", -1222.0, "STOP")] * 4
             + [("2026-10-22", 478.0, "TARGET")] * 4)
    text = render(session_series(db_path=db))

    assert "2026-10-16" in text and "2026-10-22" in text
    assert "ONE observation" in text
    assert "VERDICT" in text


def test_an_empty_report_says_so():
    assert "No graded sessions" in render(SessionSeries())
