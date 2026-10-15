"""Tests for the position store.

Positions now live in the database rather than Streamlit session state, so they
survive a refresh, a restart and a second browser. The previous dashboard lost
them on every rerun, which made the stop alarm unreliable precisely when it
mattered.
"""

from __future__ import annotations

import pytest

from intraday_advisor.ledger.schema import init_ledger
from intraday_advisor.positions import (
    close_position,
    evaluate_live,
    load_open_positions,
    open_position,
    session_summary,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.sqlite"
    init_ledger(path)
    return path


def a_position(db, **overrides) -> str:
    base = dict(
        index_name="NIFTY", option_type="CE", strike=23_000,
        lot_size=75, lots=1, entry_premium=100.0,
        target_pct=0.25, stop_pct=0.20, db_path=db,
    )
    base.update(overrides)
    return open_position(**base)


def test_open_and_load_round_trip(db):
    position_id = a_position(db)
    positions = load_open_positions(db_path=db)

    assert len(positions) == 1
    position = positions[0]
    assert position.position_id == position_id
    assert position.quantity == 75
    assert position.target_premium == 125.0
    assert position.stop_premium == 80.0
    assert position.deployed == 7_500.0


def test_invalid_inputs_are_refused(db):
    with pytest.raises(ValueError):
        a_position(db, lots=0)
    with pytest.raises(ValueError):
        a_position(db, entry_premium=0.0)


def test_close_computes_net_pnl_after_costs(db):
    position_id = a_position(db)
    result = close_position(position_id, exit_premium=125.0, db_path=db)

    assert result is not None
    assert result["gross_pnl"] == pytest.approx(25.0 * 75)
    assert result["cost"] > 0
    # Net is strictly worse than gross -- exiting costs money too.
    assert result["net_pnl"] < result["gross_pnl"]
    assert load_open_positions(db_path=db) == []


def test_closing_twice_is_refused(db):
    position_id = a_position(db)
    assert close_position(position_id, exit_premium=120.0, db_path=db) is not None
    assert close_position(position_id, exit_premium=120.0, db_path=db) is None


def test_closing_unknown_position_returns_none(db):
    assert close_position("nope", exit_premium=100.0, db_path=db) is None


def test_evaluate_live_flags_stop_with_alarm(db):
    a_position(db)
    position = load_open_positions(db_path=db)[0]
    evaluated = evaluate_live(position, current_premium=75.0,
                              premium_source="live", underlying_direction="CALL")

    assert evaluated.status == "STOP_HIT"
    assert evaluated.is_alarm is True
    assert evaluated.action == "EXIT NOW"
    assert evaluated.net_pnl < 0


def test_evaluate_live_flags_target(db):
    a_position(db)
    position = load_open_positions(db_path=db)[0]
    evaluated = evaluate_live(position, current_premium=130.0,
                              premium_source="live", underlying_direction="CALL")

    assert evaluated.status == "TARGET_HIT"
    assert evaluated.action == "BOOK PROFIT"
    assert evaluated.net_pnl > 0


def test_net_pnl_is_worse_than_gross(db):
    """The old screen showed gross premium movement, which overstates every
    small winner."""
    a_position(db)
    position = load_open_positions(db_path=db)[0]
    evaluated = evaluate_live(position, current_premium=105.0,
                              premium_source="live", underlying_direction="CALL")

    assert evaluated.gross_pnl == pytest.approx(5.0 * 75)
    assert evaluated.net_pnl < evaluated.gross_pnl
    assert evaluated.cost_to_exit > 0


def test_thesis_turning_against_the_option_warns(db):
    """A CE held while the index now reads bearish is the early nudge."""
    a_position(db)
    position = load_open_positions(db_path=db)[0]
    evaluated = evaluate_live(position, current_premium=98.0,
                              premium_source="live", underlying_direction="PUT")

    assert evaluated.status == "WARNING"
    assert evaluated.action == "CONSIDER EXIT"


def test_missing_premium_is_no_data_not_a_guess(db):
    a_position(db)
    position = load_open_positions(db_path=db)[0]
    evaluated = evaluate_live(position, current_premium=None,
                              premium_source="unavailable",
                              underlying_direction="CALL")

    assert evaluated.status == "NO_DATA"
    assert evaluated.net_pnl == 0.0


def test_session_summary_counts_realised_only(db):
    first = a_position(db)
    a_position(db, strike=23_100)

    close_position(first, exit_premium=130.0, db_path=db)
    summary = session_summary(db_path=db)

    assert summary["closed_trades"] == 1
    assert summary["open_trades"] == 1
    assert summary["realised_pnl"] > 0
    assert summary["wins"] == 1
    assert summary["win_rate"] == 1.0


def test_session_summary_on_empty_db(db):
    summary = session_summary(db_path=db)
    assert summary["closed_trades"] == 0
    assert summary["realised_pnl"] == 0.0
    assert summary["win_rate"] is None


def test_positions_survive_reload(db):
    """The whole reason these live in the database."""
    a_position(db, lots=3)
    assert len(load_open_positions(db_path=db)) == 1
    assert load_open_positions(db_path=db)[0].quantity == 225
