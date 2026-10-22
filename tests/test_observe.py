"""Sampling market conditions, so a good day can be explained.

Three graded sessions in, the only positive one was unexplainable: nothing
stored the regime, where volatility sat, or how breadth behaved. A trade is a
biased slice of a session -- it says nothing about the hours when nothing was
taken, and those are most of the day.
"""

from __future__ import annotations

import pytest

from intraday_advisor.ledger.schema import connect, init_ledger
from intraday_advisor.observe import (
    compare_sessions,
    observation_rows,
    record_observations,
    session_shape,
)


def snapshot(spot: float = 22_700.0, score: int = 47, **overrides) -> dict:
    signal = {
        "index": "NIFTY", "spot": spot, "direction": "PUT", "score": score,
        "underlying_score": 14, "chain_bias": "NEUTRAL",
        "agreement": "price-only", "pcr": 0.93, "max_pain": 22_950.0,
        "agent": {"stance": "VETO"},
        "volatility": {"atm_ce_iv": 14.8, "atm_pe_iv": 15.2, "skew": 0.4},
        "regime": {
            "regime": "TRENDING_DOWN", "conviction": 82,
            "volatility": {"vix": 15.9},
            "breadth": {"pct": 0.5},
            "levels": {"range_position": 0.054},
        },
    }
    signal.update(overrides)
    return {"signals": [signal]}


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "ledger.sqlite")
    init_ledger(path)
    return path


# =============================================================== flattening ==

def test_every_condition_worth_comparing_is_captured():
    """The fields exist because the question "what was different about the day
    that worked" could not be answered without them."""
    row = observation_rows(snapshot())[0]
    for field in ("regime", "conviction", "vix", "breadth_pct",
                  "range_position", "atm_pe_iv", "iv_skew", "agent_stance",
                  "pcr", "max_pain", "score", "spot"):
        assert row[field] is not None, field
    assert row["regime"] == "TRENDING_DOWN"
    assert row["agent_stance"] == "VETO"


def test_a_missing_regime_does_not_break_the_sample(db):
    """Observing must never break trading: a snapshot taken before the regime
    refresh has landed is still worth recording for its price."""
    rows = observation_rows({"signals": [{"index": "NIFTY", "spot": 22_700.0}]})
    assert rows[0]["spot"] == 22_700.0
    assert rows[0]["regime"] is None
    assert record_observations(
        {"signals": [{"index": "NIFTY", "spot": 22_700.0}]}, db_path=db) == 1


def test_a_nan_becomes_none_not_nan(db):
    """NaN in a numeric column breaks JSON and every downstream mean."""
    import math
    rows = observation_rows(
        {"signals": [{"index": "NIFTY", "spot": float("nan"), "pcr": math.nan}]}
    )
    assert rows[0]["spot"] is None
    assert rows[0]["pcr"] is None


def test_an_empty_snapshot_writes_nothing(db):
    assert record_observations({"signals": []}, db_path=db) == 0
    assert record_observations({}, db_path=db) == 0


# ================================================================ recording ==

def test_a_sample_is_stored_and_readable(db):
    assert record_observations(snapshot(), db_path=db) == 1
    with connect(db) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM observations")]
    assert len(rows) == 1
    assert rows[0]["index_name"] == "NIFTY"


def test_two_samples_in_the_same_second_are_one_observation(db):
    """A desk that retries a cycle must not double-count the market."""
    record_observations(snapshot(), db_path=db)
    second = record_observations(snapshot(spot=22_705.0), db_path=db)
    with connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert second == 0
    assert total == 1


def test_recording_never_raises_on_a_bad_database(tmp_path):
    bad = str(tmp_path / "nope" / "deep" / "x.sqlite")
    assert record_observations(snapshot(), db_path=bad) in (0, 1)


# ============================================================ day shape =====

def test_an_unsampled_session_says_so(db):
    result = session_shape("2026-01-01", db_path=db)
    assert result["samples"] == 0
    assert "No observations" in result["note"]


def test_the_shape_describes_the_day(db):
    import datetime
    import zoneinfo
    from intraday_advisor.ledger.recorder import now_ist

    today = now_ist().date().isoformat()
    # Three samples with a drifting spot, written directly so the timestamps
    # differ (the primary key collapses same-second samples).
    with connect(db) as conn:
        for i, spot in enumerate((22_700.0, 22_650.0, 22_600.0)):
            conn.execute(
                "INSERT INTO observations (observed_at, session_date, "
                "index_name, spot, regime, conviction, vix, breadth_pct, "
                "iv_skew, direction, agent_stance, score) VALUES "
                "(?, ?, 'NIFTY', ?, 'TRENDING_DOWN', 82, 15.9, 0.5, 0.4, "
                "'PUT', 'VETO', 47)",
                (f"{today}T10:{i:02d}:00+05:30", today, spot),
            )
        conn.commit()

    shape = session_shape(today, db_path=db)
    nifty = shape["per_index"]["NIFTY"]
    assert shape["samples"] == 3
    assert nifty["spot_open"] == 22_700.0
    assert nifty["spot_close"] == 22_600.0
    assert nifty["spot_move_pct"] < 0, "a falling day must read as falling"
    assert nifty["spot_range_pct"] > 0
    assert nifty["regimes_seen"] == {"TRENDING_DOWN": 3}
    assert nifty["stances"] == {"VETO": 3}


def test_sessions_can_be_compared_side_by_side(db):
    with connect(db) as conn:
        for day, vix in (("2026-10-21", 15.9), ("2026-10-22", 12.4)):
            for i in range(3):
                conn.execute(
                    "INSERT INTO observations (observed_at, session_date, "
                    "index_name, spot, vix, score, conviction) VALUES "
                    "(?, ?, 'NIFTY', 22700, ?, 47, 80)",
                    (f"{day}T10:{i:02d}:00+05:30", day, vix),
                )
        conn.commit()

    frame = compare_sessions(db_path=db)
    assert len(frame) == 2
    assert frame.loc["2026-10-22", "mean_vix"] < frame.loc["2026-10-21", "mean_vix"]


def test_comparison_of_an_empty_ledger_is_empty(db):
    assert compare_sessions(db_path=db).empty
