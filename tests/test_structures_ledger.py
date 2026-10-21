"""Recording a multi-leg position, which the ledger could not do before.

``spreads.py`` could price a vertical exactly and nothing could open one: the
positions table held one option_type, one strike, one entry_premium. So the
structure the evidence favours could be computed and never traded.
"""

from __future__ import annotations

import pytest

from intraday_advisor.ledger.structures import (
    MULTI,
    close_structure,
    evaluate_structure,
    load_open_structures,
    load_structure,
    open_structure,
    structure_pnl,
)

LOT = 65

BEAR_PUT = [
    {"option_type": "PE", "strike": 22_700.0, "action": "BUY",
     "entry_premium": 168.35, "option_token": "51341"},
    {"option_type": "PE", "strike": 22_500.0, "action": "SELL",
     "entry_premium": 88.75, "option_token": "51337"},
]

BULL_PUT_CREDIT = [
    {"option_type": "PE", "strike": 22_400.0, "action": "BUY",
     "entry_premium": 72.00},
    {"option_type": "PE", "strike": 22_500.0, "action": "SELL",
     "entry_premium": 88.75},
]


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "ledger.sqlite")


# ============================================================ the sign rule ==

def test_a_sold_leg_is_recorded_with_a_positive_premium():
    """The sign comes from the action, never from the price. Recording a sold
    leg as a negative premium would double-apply the sign."""
    with pytest.raises(ValueError, match="sign comes from the action"):
        open_structure(index_name="NIFTY", lot_size=LOT, legs=[
            {"option_type": "PE", "strike": 22_500.0, "action": "SELL",
             "entry_premium": -88.75},
        ])


def test_a_debit_structure_has_a_positive_net_premium(db):
    open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT, db_path=db)
    stored = load_open_structures(db_path=db)[0]
    assert stored.net_entry_premium == pytest.approx(168.35 - 88.75)
    assert stored.is_credit is False


def test_a_credit_structure_has_a_negative_net_premium(db):
    open_structure(index_name="NIFTY", lot_size=LOT, legs=BULL_PUT_CREDIT,
                   db_path=db)
    stored = load_open_structures(db_path=db)[0]
    assert stored.net_entry_premium < 0
    assert stored.is_credit is True


def test_the_net_matches_what_spreads_priced(db):
    """The two modules must agree or the screen and the ledger disagree."""
    from intraday_advisor.spreads import premiums_from_chain, vertical
    import pandas as pd

    frame = pd.DataFrame({
        "strike": [22_500.0, 22_700.0],
        "pe_ltp": [88.75, 168.35], "ce_ltp": [317.95, 190.20],
    })
    priced = vertical("NIFTY", "PE", 22_700.0, 22_500.0,
                      premiums_from_chain(frame), LOT)

    open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT, db_path=db)
    stored = load_open_structures(db_path=db)[0]
    assert stored.net_entry_premium * LOT == pytest.approx(priced.net_debit,
                                                           abs=1.0)


# ================================================================ P&L =======

def test_pnl_applies_the_leg_signs(db):
    """A long leg gains when its premium rises; a short leg loses."""
    pid = open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT,
                         db_path=db)
    stored = load_structure(pid, db_path=db)
    result = structure_pnl(stored.legs, LOT, {0: 255.0, 1: 150.0})

    by_hand = (255.0 - 168.35) * LOT - (150.0 - 88.75) * LOT
    assert result["gross_pnl"] == pytest.approx(by_hand)
    assert result["net_pnl"] < result["gross_pnl"], "costs on four orders"


def test_a_hedge_is_not_a_doubled_position(db):
    """If a sign were flipped, both legs would move together and the structure
    would behave like two longs. The short leg must offset."""
    pid = open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT,
                         db_path=db)
    stored = load_structure(pid, db_path=db)

    both_up = structure_pnl(stored.legs, LOT, {0: 200.0, 1: 120.0})["gross_pnl"]
    long_only = (200.0 - 168.35) * LOT
    assert abs(both_up) < abs(long_only), "the short leg must give some back"


def test_a_partially_priced_structure_reports_unknown_not_smaller(db):
    """A two-leg spread valued on one leg is a naked option. Showing that
    number as the spread's would be the most dangerous kind of wrong."""
    pid = open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT,
                         db_path=db)
    stored = load_structure(pid, db_path=db)
    result = evaluate_structure(stored, {0: 255.0})

    assert result["complete"] is False
    assert "not this structure" in result["warning"]


# ============================================================== lifecycle ===

def test_closing_requires_every_leg(db):
    pid = open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT,
                         db_path=db)
    refused = close_structure(pid, {0: 255.0}, db_path=db)
    assert refused["ok"] is False
    assert "refused rather than half-recorded" in refused["error"]
    assert len(load_open_structures(db_path=db)) == 1, "still open"


def test_a_full_close_records_the_net_and_every_leg(db):
    pid = open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT,
                         db_path=db)
    result = close_structure(pid, {0: 255.0, 1: 150.0}, db_path=db)

    assert result["ok"] is True
    assert load_open_structures(db_path=db) == []
    reloaded = load_structure(pid, db_path=db)
    assert reloaded.status == "CLOSED"
    assert all(leg.exit_premium is not None for leg in reloaded.legs)
    assert reloaded.net_pnl == pytest.approx(result["net_pnl"])


def test_closing_twice_is_refused(db):
    pid = open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT,
                         db_path=db)
    close_structure(pid, {0: 255.0, 1: 150.0}, db_path=db)
    assert close_structure(pid, {0: 255.0, 1: 150.0}, db_path=db) is None


def test_an_unknown_structure_is_reported(db):
    assert close_structure("nope", {0: 1.0}, db_path=db) is None
    assert load_structure("nope", db_path=db) is None


# ================================================= backwards compatibility ==

def test_single_leg_positions_are_untouched(db):
    """The whole reason legs went in their own table: the live monitor, the
    grader and every report read `positions`, and must keep working."""
    from intraday_advisor.positions import load_open_positions, open_position

    open_position(index_name="NIFTY", option_type="PE", strike=22_700.0,
                  entry_premium=168.35, lots=1, lot_size=LOT,
                  target_pct=0.25, stop_pct=0.20, db_path=db)
    open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT, db_path=db)

    singles = load_open_positions(db_path=db)
    structures = load_open_structures(db_path=db)

    assert len(structures) == 1
    assert any(p.option_type == "PE" for p in singles), "the single leg survives"
    assert all(leg.exit_premium is None for leg in structures[0].legs)


def test_a_structure_is_marked_multi_in_the_parent_row(db):
    from intraday_advisor.ledger.schema import connect

    pid = open_structure(index_name="NIFTY", lot_size=LOT, legs=BEAR_PUT,
                         db_path=db)
    with connect(db) as conn:
        row = conn.execute(
            "SELECT option_type, target_pct, stop_pct FROM positions "
            "WHERE position_id = ?", (pid,)
        ).fetchone()
    assert row["option_type"] == MULTI
    # A structure's exit comes from its payoff, not a percentage of a net
    # premium that may be negative.
    assert row["target_pct"] == 0.0 and row["stop_pct"] == 0.0


# ==================================================================== guards ==

def test_an_empty_structure_is_refused():
    with pytest.raises(ValueError, match="at least one leg"):
        open_structure(index_name="NIFTY", lot_size=LOT, legs=[])


def test_a_bad_action_or_type_is_refused():
    with pytest.raises(ValueError, match="BUY or SELL"):
        open_structure(index_name="NIFTY", lot_size=LOT, legs=[
            {"option_type": "PE", "strike": 1.0, "action": "HOLD",
             "entry_premium": 10.0}])
    with pytest.raises(ValueError, match="CE or PE"):
        open_structure(index_name="NIFTY", lot_size=LOT, legs=[
            {"option_type": "XX", "strike": 1.0, "action": "BUY",
             "entry_premium": 10.0}])


def test_a_zero_lot_size_is_refused():
    with pytest.raises(ValueError, match="lot_size"):
        open_structure(index_name="NIFTY", lot_size=0, legs=BEAR_PUT)
