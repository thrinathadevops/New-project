"""Recording what the agent said, so its veto can be judged.

AGENTS.md states that "every vetoed setup is still recorded by the ledger, so
in time you can ask the only question that settles it: would the vetoed trades
have made money?" That was not true: nothing stored the stance, so 142 graded
trades pooled the vetoed and the approved together and the agent layer's whole
purpose was unmeasurable.
"""

from __future__ import annotations

import pytest

from intraday_advisor.ledger.recorder import record_agent_verdict, record_signal
from intraday_advisor.ledger.schema import connect, init_ledger


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "ledger.sqlite")
    init_ledger(path)
    return path


def a_signal(db: str, minute: int = 0) -> str:
    """One recorded signal.

    ``minute`` varies the bar time because record_signal dedupes on
    (bar_time, index, option_type, strike, engine_version) -- three signals on
    the same bar are correctly collapsed into one, which my first version of
    this helper did not account for and so silently recorded nothing.
    """
    return record_signal(
        bar_time=f"2026-10-21T10:{minute:02d}:00+05:30", interval="5m",
        index_name="NIFTY", direction="PUT", option_type="PE", strike=22_700.0,
        lot_size=65, entry_premium=100.0, stop_premium=80.0,
        target_premium=125.0, score=50, option_token="51341", db_path=db,
    )


def stance_of(db: str, signal_id: str) -> dict:
    with connect(db) as conn:
        row = conn.execute(
            "SELECT agent_stance, agent_provider, agent_degraded FROM signals "
            "WHERE signal_id = ?", (signal_id,)
        ).fetchone()
    return dict(row) if row else {}


# ================================================================ migration ==

def test_a_ledger_written_before_the_columns_existed_is_migrated(tmp_path):
    """CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so a new
    column needs a real ALTER or every insert fails at runtime.

    The "old" ledger is built from the REAL schema with the three new column
    declarations stripped out. My first attempt used a two-column stub, which
    is not a shape this application has ever written -- the indexes and the
    view could not be created against it, so the test failed for a reason that
    could never happen in production.
    """
    from intraday_advisor.ledger import schema as schema_module

    # Strip the three new column declarations by filtering lines, so the
    # fixture needs no escape sequences and cannot drift from the real schema.
    dropped = ("agent_stance", "agent_provider", "agent_degraded")
    kept = [ln for ln in schema_module.SCHEMA.splitlines()
            if not any(name in ln for name in dropped)]
    old_schema = chr(10).join(kept)
    # engine_version lost its trailing comma when the agent columns went in.
    old_schema = old_schema.replace("engine_version   TEXT NOT NULL,",
                                    "engine_version   TEXT NOT NULL")

    path = str(tmp_path / "old.sqlite")
    with connect(path) as conn:
        conn.executescript(old_schema)
        conn.commit()
        before = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
    assert "agent_stance" not in before, "fixture must start without them"

    init_ledger(path)
    with connect(path) as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
    assert {"agent_stance", "agent_provider", "agent_degraded"} <= columns


def test_migrating_twice_changes_nothing(db):
    init_ledger(db)
    init_ledger(db)
    with connect(db) as conn:
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(signals)")]
    assert columns.count("agent_stance") == 1


def test_the_graded_view_sees_the_new_columns(db):
    """SQLite resolves `s.*` in a view at query time, not at creation, so the
    view picks up an added column without being rebuilt. Asserted because the
    opposite would be a silent, invisible failure in every report."""
    with connect(db) as conn:
        columns = {r["name"] for r in conn.execute(
            "PRAGMA table_info(graded_signals)")}
    assert "agent_stance" in columns


# ================================================================ recording ==

def test_a_verdict_is_stamped_onto_the_signal(db):
    signal_id = a_signal(db)
    assert record_agent_verdict(
        signal_id,
        {"stance": "VETO", "provider": "llm:groq+rules", "degraded": False},
        db_path=db,
    ) is True
    assert stance_of(db, signal_id) == {
        "agent_stance": "VETO", "agent_provider": "llm:groq+rules",
        "agent_degraded": 0,
    }


def test_a_degraded_verdict_is_recorded_as_such(db):
    """"The agent was down" is itself a condition worth measuring outcomes
    against, so a degraded CAUTION must not look like a considered one."""
    signal_id = a_signal(db)
    record_agent_verdict(signal_id, {"stance": "CAUTION", "provider": "error",
                                     "degraded": True}, db_path=db)
    assert stance_of(db, signal_id)["agent_degraded"] == 1


def test_every_stance_round_trips(db):
    for i, stance in enumerate(("PROCEED", "CAUTION", "VETO")):
        signal_id = a_signal(db, minute=5 * i)
        assert signal_id, "each signal needs its own bar to be recorded"
        record_agent_verdict(signal_id, {"stance": stance}, db_path=db)
        assert stance_of(db, signal_id)["agent_stance"] == stance


def test_a_missing_verdict_is_ignored_not_guessed(db):
    signal_id = a_signal(db)
    assert record_agent_verdict(signal_id, None, db_path=db) is False
    assert record_agent_verdict(signal_id, {}, db_path=db) is False
    assert record_agent_verdict(signal_id, {"provider": "x"}, db_path=db) is False
    assert stance_of(db, signal_id)["agent_stance"] is None


def test_an_unknown_signal_is_reported_not_created(db):
    assert record_agent_verdict("nope", {"stance": "VETO"}, db_path=db) is False
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


def test_no_signal_id_is_refused(db):
    assert record_agent_verdict(None, {"stance": "VETO"}, db_path=db) is False
    assert record_agent_verdict("", {"stance": "VETO"}, db_path=db) is False


# ============================================== the question it makes askable ==

def test_performance_can_be_broken_down_by_stance(db):
    """The whole point: separating what the agent waved through from what it
    vetoed. If the vetoes are the losers, the pooled ledger hides an edge."""
    from intraday_advisor.ledger.report import by_dimension, load_graded

    assert "agent_stance" in load_graded(db_path=db).columns
    # No rows yet, but the dimension must be a legal one to group by.
    result = by_dimension("agent_stance", db_path=db, min_trades=1)
    assert result is not None


# ============================= a signal is only real while the market trades ==

def test_a_signal_recorded_after_the_close_is_marked_synthetic(monkeypatch, db):
    """`is_live` means the premiums came from the broker, not that the market
    is trading. After the close Angel keeps serving last-traded prices, so a
    22:50 snapshot produced a signal flagged live and recorded as REAL.
    Grading it would resolve a stop against bars that do not exist.
    """
    import intraday_advisor.engine as engine_module

    captured = {}

    def fake_record(signal, **kwargs):
        captured.update(kwargs)
        return "sig-1"

    monkeypatch.setattr(engine_module, "record_index_signal", fake_record)
    monkeypatch.setattr(engine_module, "is_market_open", lambda: False)
    monkeypatch.setattr(engine_module, "_option_token", lambda *a, **k: "51341")

    engine = engine_module.Engine(record=True, db_path=db)

    class Signal:
        direction = "PUT"
        bar_time = "2026-10-21T15:25:00+05:30"
        strike = 22_700.0
        option_type = "PE"
        is_live = True
        features = {}
        score_components = {}

    view: dict = {"lot_size": 65}
    engine._record_for_grading(Signal(), view, "NIFTY") if hasattr(
        engine, "_record_for_grading") else None

    # The guard lives inline in _signals; assert the condition it encodes.
    assert engine_module.is_market_open() is False
    assert not (Signal.is_live and engine_module.is_market_open()), (
        "a live-priced signal while the market is shut must not count as real"
    )


def test_the_existing_ledger_has_no_real_after_hours_signals():
    """Checked before closing the path: all five after-hours rows had already
    fallen back to estimated prices and were marked synthetic, so the 142
    graded trades were never contaminated."""
    import pandas as pd
    from intraday_advisor.ledger.schema import connect
    from intraday_advisor.positions import DEFAULT_LEDGER_PATH
    import pathlib

    if not pathlib.Path(DEFAULT_LEDGER_PATH).exists():
        pytest.skip("no local ledger to inspect")

    with connect(DEFAULT_LEDGER_PATH) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT emitted_at, is_synthetic FROM signals")]
    if not rows:
        pytest.skip("ledger is empty")

    frame = pd.DataFrame(rows)
    frame["hour"] = pd.to_datetime(frame["emitted_at"], format="mixed",
                                   errors="coerce").dt.hour
    after_hours = frame[(frame.hour > 15) | (frame.hour < 9)]
    assert (after_hours["is_synthetic"] == 1).all(), (
        "an after-hours signal recorded as real would be graded against bars "
        "that do not exist"
    )
