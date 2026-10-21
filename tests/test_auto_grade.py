"""Grading that happens without being remembered.

The pipeline worked and 151 real signals sat ungraded for days, because grading
was a command somebody had to type after the close. When it finally ran it
produced the most useful measurement in the project. A measurement that depends
on being remembered is a measurement that does not happen.
"""

from __future__ import annotations

import datetime
import zoneinfo

import pytest

from intraday_advisor import auto_grade
from intraday_advisor.auto_grade import (
    GRADE_AFTER,
    describe,
    grade_due,
    is_safe_to_grade,
    sessions_awaiting_grade,
)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def at(hour: int, minute: int = 0, day: int = 21) -> datetime.datetime:
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=IST)


def record(db: str, *, session: str, synthetic: bool = False,
           token: str = "51341") -> None:
    """Write one signal through the REAL recorder.

    The first version of this file hand-wrote INSERT statements and invented
    column names -- there is no ``recorded_at``, it is ``emitted_at``. A test
    that builds its own rows can pass against a schema the application no
    longer has, which is the opposite of what a test is for.
    """
    from intraday_advisor.ledger.recorder import record_signal

    record_signal(
        bar_time=f"{session}T10:00:00+05:30", interval="5m",
        index_name="NIFTY", direction="PUT", option_type="PE",
        strike=22_700.0, lot_size=65, entry_premium=100.0,
        stop_premium=80.0, target_premium=125.0, score=50,
        option_token=token, is_synthetic=synthetic, db_path=db,
    )


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(auto_grade, "_STATE", auto_grade.AutoGradeState())


@pytest.fixture
def db(tmp_path) -> str:
    from intraday_advisor.ledger.schema import init_ledger
    path = str(tmp_path / "ledger.sqlite")
    init_ledger(path)
    return path


# ================================================= when grading is allowed ==

def test_todays_session_is_refused_mid_session():
    """THE correctness question. Grading reads the whole day's premium path, so
    doing it at 13:40 resolves stops against bars that have not happened --
    which would flatter every number in the ledger."""
    safe, reason = is_safe_to_grade("2026-10-21", now=at(13, 40))
    assert safe is False
    assert "unfinished bars" in reason


def test_todays_session_is_allowed_once_the_close_has_settled():
    safe, reason = is_safe_to_grade("2026-10-21", now=at(15, 50))
    assert safe is True
    assert GRADE_AFTER.strftime("%H:%M") in reason


def test_the_boundary_is_inclusive():
    assert is_safe_to_grade("2026-10-21", now=at(15, 45))[0] is True
    assert is_safe_to_grade("2026-10-21", now=at(15, 44))[0] is False


def test_an_earlier_session_is_always_safe():
    """A finished day can be graded at any hour, which is what makes catching
    up after a week away possible at all."""
    safe, reason = is_safe_to_grade("2026-10-16", now=at(9, 30))
    assert safe is True
    assert "complete" in reason


def test_a_future_session_is_refused():
    assert is_safe_to_grade("2026-11-30", now=at(15, 50))[0] is False


# ===================================================== what is still pending ==

def test_an_empty_ledger_has_nothing_pending(db):
    assert sessions_awaiting_grade(db) == []


def test_synthetic_signals_are_never_graded(db):
    """They exist to prove the pipeline. Grading them would put invented trades
    into a real track record."""
    record(db, session="2026-10-16", synthetic=True)
    assert sessions_awaiting_grade(db) == []


def test_a_real_ungraded_signal_shows_up(db):
    record(db, session="2026-10-16")
    assert sessions_awaiting_grade(db) == ["2026-10-16"]


def test_sessions_come_back_oldest_first(db):
    for day in ("2026-10-19", "2026-10-16", "2026-10-20"):
        record(db, session=day, token=f"tok{day}")
    assert sessions_awaiting_grade(db) == ["2026-10-16", "2026-10-19",
                                           "2026-10-20"]


def test_the_catch_up_is_bounded(db):
    """A desk idle for a year must not turn a dashboard start into hundreds of
    provider calls."""
    for i in range(1, 16):
        record(db, session=f"2026-09-{i:02d}", token=f"t{i}")
    assert len(sessions_awaiting_grade(db, limit=10)) == 10


# ============================================================== the runner ===

def test_grading_is_deferred_not_attempted_when_unsafe(db, monkeypatch):
    today = at(13, 40).date().isoformat()
    record(db, session=today)

    called = []
    monkeypatch.setattr(
        "intraday_advisor.ledger.grader.grade_session",
        lambda *a, **k: called.append(a) or {"graded": 0},
    )
    result = grade_due(db, now=at(13, 40))

    assert result["graded"] == []
    assert result["deferred"]
    assert "unfinished bars" in result["deferred"][0]["reason"]
    assert called == [], "the provider must not be called at all"


def test_a_finished_session_is_graded(db, monkeypatch):
    record(db, session="2026-10-16")
    monkeypatch.setattr(
        "intraday_advisor.ledger.grader.grade_session",
        lambda *a, **k: {"graded": 7, "outcomes": {"STOP": 7}},
    )
    result = grade_due(db, now=at(16, 0))

    assert result["graded"] == [{"session": "2026-10-16", "graded": 7,
                                 "outcomes": {"STOP": 7}}]
    assert auto_grade.status()["graded_total"] == 7
    assert auto_grade.status()["last_success"]


def test_a_provider_failure_does_not_raise(db, monkeypatch):
    """This runs on a background thread in a desk application; a broker hiccup
    must never take the screen down."""
    record(db, session="2026-10-16")

    def boom(*args, **kwargs):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr("intraday_advisor.ledger.grader.grade_session", boom)
    result = grade_due(db, now=at(16, 0))

    assert result["errors"]
    assert "broker unreachable" in result["errors"][0]["error"]
    assert auto_grade.status()["last_error"]


def test_one_bad_session_does_not_stop_the_others(db, monkeypatch):
    record(db, session="2026-10-16", token="a")
    record(db, session="2026-10-19", token="b")

    def selective(session_date, **kwargs):
        if session_date == "2026-10-16":
            raise RuntimeError("that one is broken")
        return {"graded": 3}

    monkeypatch.setattr("intraday_advisor.ledger.grader.grade_session",
                        selective)
    result = grade_due(db, now=at(16, 0))
    assert len(result["errors"]) == 1
    assert len(result["graded"]) == 1


def test_disabling_stops_it(db):
    auto_grade.disable()
    assert grade_due(db, now=at(16, 0)) == {"skipped": "disabled"}


def test_it_will_not_run_twice_at_once(db):
    auto_grade._STATE.running = True
    assert grade_due(db, now=at(16, 0)) == {"skipped": "already running"}


def test_the_running_flag_is_always_released(db, monkeypatch):
    """A crash mid-pass must not wedge it permanently."""
    record(db, session="2026-10-16")
    monkeypatch.setattr(
        "intraday_advisor.auto_grade.sessions_awaiting_grade",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        grade_due(db, now=at(16, 0))
    assert auto_grade.status()["running"] is False


# =========================================================== the status line ==

def test_an_empty_ledger_says_nothing_is_waiting(db):
    assert "Nothing waiting" in describe(db, now=at(16, 0))["note"]


def test_the_note_explains_when_it_will_happen(db):
    today = at(13, 0).date().isoformat()
    record(db, session=today)
    note = describe(db, now=at(13, 0))["note"]
    assert GRADE_AFTER.strftime("%H:%M") in note
    assert "automatically" in note


def test_the_note_says_when_something_is_ready(db):
    record(db, session="2026-10-16")
    note = describe(db, now=at(16, 0))["note"]
    assert "ready to grade" in note
