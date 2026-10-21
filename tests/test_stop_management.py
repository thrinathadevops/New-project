"""Managing the stop on a position that is already open.

Written while running the desk on 2026-10-21: the trade went +10%, the recorded
stop was still 20% below entry, and there was no way to protect the gain. A
position could be opened and closed, never adjusted.
"""

from __future__ import annotations

import pytest

from intraday_advisor.positions import (
    adjust_stop,
    breakeven_stop,
    close_position,
    evaluate_live,
    load_open_positions,
    open_position,
)


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "ledger.sqlite")


@pytest.fixture
def held(db) -> str:
    """One NIFTY put: entry 162.75, 1 lot of 65, stop 20% below."""
    return open_position(
        index_name="NIFTY", option_type="PE", strike=22_700.0,
        entry_premium=162.75, lots=1, lot_size=65,
        target_pct=0.55, stop_pct=0.20, option_token="51341", db_path=db,
    )


def only(db: str):
    return load_open_positions(db_path=db)[0]


# ================================================================ tightening ==

def test_a_stop_can_be_tightened(db, held):
    before = only(db).stop_premium
    result = adjust_stop(held, 150.0, db_path=db)

    assert result["ok"] is True
    assert result["old_stop"] == pytest.approx(before)
    assert only(db).stop_premium == pytest.approx(150.0)


def test_loosening_a_stop_is_refused_not_warned(db, held):
    """Widening a stop to avoid being taken out turns a planned small loss into
    an unplanned large one, and it always feels justified at the time. The
    ledger will not help you do it."""
    result = adjust_stop(held, 100.0, db_path=db)

    assert result["ok"] is False
    assert "only be tightened" in result["error"]
    assert only(db).stop_premium == pytest.approx(130.20), "unchanged"


def test_the_stop_survives_a_reload(db, held):
    adjust_stop(held, 155.0, db_path=db)
    assert only(db).stop_premium == pytest.approx(155.0)


def test_a_nonsense_stop_is_refused(db, held):
    assert adjust_stop(held, 0.0, db_path=db)["ok"] is False
    assert adjust_stop(held, -5.0, db_path=db)["ok"] is False
    assert adjust_stop(held, 162.75 * 4, db_path=db)["ok"] is False


def test_an_unknown_position_is_reported(db):
    assert adjust_stop("nope", 10.0, db_path=db)["ok"] is False


def test_a_closed_position_cannot_be_adjusted(db, held):
    close_position(held, exit_premium=170.0, db_path=db)
    result = adjust_stop(held, 165.0, db_path=db)
    assert result["ok"] is False
    assert "not OPEN" in result["error"]


# ================================================================= breakeven ==

def test_breakeven_sits_above_the_entry_not_at_it(db, held):
    """Breakeven is entry PLUS the round trip. A stop at the entry still loses
    you the costs -- Rs 122 on one lot of this trade."""
    result = breakeven_stop(held, db_path=db)

    assert result["ok"] is True
    assert result["new_stop"] > 162.75
    assert result["locked_in"] > 0


def test_the_stop_fraction_goes_negative_above_the_entry(db, held):
    """stop_premium is derived everywhere as entry * (1 - stop_pct), so a stop
    above entry IS a negative fraction. An earlier `max(0.0, ...)` clamp here
    silently pinned the stop to the entry and left the costs unprotected."""
    breakeven_stop(held, db_path=db)
    position = only(db)

    assert position.stop_pct < 0
    assert position.stop_premium > position.entry_premium


def test_the_monitor_honours_a_breakeven_stop(db, held):
    """The whole point: once set, a dip back through breakeven must fire the
    alarm instead of riding all the way to the original -20%."""
    breakeven_stop(held, db_path=db)
    level = only(db).stop_premium

    above = evaluate_live(only(db), current_premium=level + 6,
                          premium_source="test", underlying_direction="PUT")
    below = evaluate_live(only(db), current_premium=level - 1,
                          premium_source="test", underlying_direction="PUT")

    assert above.status == "ON_TRACK"
    assert below.status == "STOP_HIT"
    assert below.net_pnl > -100, "a breakeven stop should land near flat"


def test_breakeven_cannot_be_used_to_loosen_a_tighter_stop(db, held):
    """If the stop has already been pulled ABOVE breakeven, falling back to
    breakeven would be a loosening, and must be refused like any other."""
    breakeven = breakeven_stop(held, db_path=db)["new_stop"]
    adjust_stop(held, breakeven + 10, db_path=db)

    result = breakeven_stop(held, db_path=db)
    assert result["ok"] is False
    assert only(db).stop_premium == pytest.approx(breakeven + 10)


def test_breakeven_on_an_unknown_position_is_reported(db):
    assert breakeven_stop("nope", db_path=db)["ok"] is False
