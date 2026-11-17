"""The decision brief: one argument per signal, derived from its own evidence.

The inputs were always on the page, scattered over a dozen panels. What these
tests pin is that the headline is *derived* from the checks -- it cannot say
something the rows under it do not -- and that the figure it shows is honest
about being agreement among checks rather than a chance of profit.
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday_advisor.thesis import FAIL, NEUTRAL, PASS, build_thesis, ladder


def signal(**overrides) -> dict:
    base = {
        "index": "NIFTY", "direction": "CALL", "option_type": "CE",
        "strike": 25300.0, "score": 78, "underlying_score": 82,
        "chain_bias": "BULLISH", "pcr": 1.21, "spot": 25291.0, "lot_size": 65,
        "entry_premium": 194.0, "target_premium": 242.5, "stop_premium": 155.2,
        "regime": {"regime": "TRENDING_UP", "bias": "BULLISH", "conviction": 80},
        "volatility": {"days_to_expiry": 4.0},
        "variance_premium": {"premium_points": 0.4},
        "plan": {"affordable": True, "net_rr": 1.2, "cost_drag": 0.012,
                 "breakeven": 196.1, "risk_pct_of_capital": 0.04, "lots": 1},
        "agent": {"stance": "PROCEED", "concerns": []},
        "warnings": [],
    }
    base.update(overrides)
    return base


def brief(**overrides) -> dict:
    return build_thesis(signal(**overrides), event_risk={}, performance={},
                        minutes_to_square_off=180, mode="LIVE")


def state_of(result: dict, key: str) -> str:
    return next(c["state"] for c in result["checks"] if c["key"] == key)


def test_a_clean_setup_reads_as_supported_and_still_warns_it_can_lose():
    result = brief()
    assert result["verdict"] == "EVIDENCE SUPPORTS"
    assert result["tone"] == "good"
    assert "can lose" in result["summary"]
    assert not result["blockers"]


def test_the_figure_is_agreement_among_checks_not_a_probability():
    """The one sentence that must never be dropped from the brief."""
    result = brief()
    passed = sum(1 for c in result["checks"] if c["state"] == PASS)
    assert result["agreement"] == round(passed / len(result["checks"]) * 100)
    assert "not the chance of profit" in result["disclaimer"]
    assert "certain" in result["disclaimer"]


def test_the_headline_cannot_contradict_the_rows():
    """A veto underneath must never sit below a supportive headline."""
    result = brief(agent={"stance": "VETO",
                          "concerns": ["Net R:R below 1."]})
    assert result["verdict"] == "DO NOT TAKE"
    assert "Risk checklist" in result["blockers"]
    assert "Net R:R below 1." in result["summary"]


def test_one_blocker_outweighs_every_supporting_check():
    result = brief(plan={"affordable": True, "net_rr": 0.7, "cost_drag": 0.05,
                         "breakeven": 200.0, "risk_pct_of_capital": 0.04})
    assert result["verdict"] == "DO NOT TAKE"
    assert state_of(result, "trend") == PASS, "the trend still agrees..."
    assert state_of(result, "cost") == FAIL, "...and it still does not matter"


def test_a_breakeven_above_the_target_is_a_blocker():
    result = brief(plan={"affordable": True, "net_rr": 1.1, "cost_drag": 0.2,
                         "breakeven": 250.0, "risk_pct_of_capital": 0.04})
    assert "Costs" in result["blockers"]


def test_a_trade_against_the_trend_is_marked_against_it():
    result = brief(regime={"regime": "TRENDING_DOWN", "bias": "BEARISH",
                           "conviction": 75})
    assert state_of(result, "trend") == FAIL
    assert result["verdict"] != "EVIDENCE SUPPORTS"


def test_a_put_is_judged_the_other_way_round():
    result = brief(direction="PUT", option_type="PE", underlying_score=20,
                   chain_bias="BEARISH",
                   regime={"regime": "TRENDING_DOWN", "bias": "BEARISH",
                           "conviction": 75})
    assert state_of(result, "trend") == PASS
    assert state_of(result, "price") == PASS
    assert state_of(result, "chain") == PASS


def test_dear_options_count_against_a_buyer():
    result = brief(variance_premium={"premium_points": 5.5})
    assert state_of(result, "volatility") == FAIL


def test_expiry_day_blocks_the_trade():
    result = brief(volatility={"days_to_expiry": 0.0})
    assert "Time left" in result["blockers"]


def test_a_results_day_is_named():
    result = build_thesis(signal(), event_risk={"earnings_today": ["RELIANCE"]},
                          performance={}, minutes_to_square_off=180, mode="LIVE")
    check = next(c for c in result["checks"] if c["key"] == "events")
    assert check["state"] == FAIL and "RELIANCE" in check["detail"]


def test_a_losing_record_is_stated_as_a_measurement():
    result = build_thesis(signal(), event_risk={},
                          performance={"trades": 597, "expectancy": -300.0},
                          minutes_to_square_off=180, mode="LIVE")
    check = next(c for c in result["checks"] if c["key"] == "record")
    assert check["state"] == FAIL
    assert "597" in check["detail"] and "300" in check["detail"]


def test_a_thin_record_is_neutral_rather_than_reassuring():
    result = build_thesis(signal(), event_risk={},
                          performance={"trades": 4, "expectancy": 900.0},
                          minutes_to_square_off=180, mode="LIVE")
    assert state_of(result, "record") == NEUTRAL


def test_estimated_data_blocks_everything():
    result = build_thesis(signal(), event_risk={}, performance={},
                          minutes_to_square_off=180, mode="CLOSED")
    assert result["verdict"] == "DO NOT TAKE"
    assert "Data" in result["blockers"]


def test_a_wait_bar_explains_itself_without_inventing_checks():
    result = brief(direction="WAIT", score=21)
    assert result["verdict"] == "NO TRADE"
    assert result["checks"] == []
    assert result["agreement"] is None
    assert "Waiting is a position" in result["summary"]


def test_a_bare_signal_does_not_raise():
    result = build_thesis({"index": "NIFTY", "direction": "CALL"},
                          mode="LIVE")
    assert result["verdict"] in ("WEAK", "MIXED", "DO NOT TAKE")


# ================================================================== ladder ==

def test_the_ladder_centres_on_the_money():
    frame = pd.DataFrame({
        "strike": [float(s) for s in range(24000, 26050, 50)],
        "ce_ltp": 100.0, "pe_ltp": 90.0, "ce_oi": 1.0, "pe_oi": 2.0,
    })
    rows = ladder({"df": frame, "atm": 25000.0}, around=3)
    assert [r["strike"] for r in rows] == [24850.0, 24900.0, 24950.0, 25000.0,
                                           25050.0, 25100.0, 25150.0]
    assert [r["atm"] for r in rows].count(True) == 1


def test_an_empty_chain_gives_an_empty_ladder():
    assert ladder({}) == []
    assert ladder({"df": pd.DataFrame()}) == []


# ============================================== a held contract's own price ==

def test_a_strike_missing_from_the_chain_is_unknown_not_the_atm_price(monkeypatch):
    """Regression. A NIFTY 24650 CE left over from an expired week was priced
    at the ATM contract's Rs 82, which reported a Rs 1,56,776 loss that did not
    exist and fired a STOP HIT alarm on someone else's quote."""
    from intraday_advisor import index_options

    frame = pd.DataFrame({"strike": [25300.0], "ce_ltp": [82.0],
                          "pe_ltp": [75.0]})
    monkeypatch.setattr(index_options, "suggest_option_trade",
                        lambda *a, **k: {"df": frame, "atm_ce_ltp": 82.0,
                                         "data_source": "live"})
    premium, source = index_options.get_option_premium("NIFTY", 24650, "CE")
    assert premium == 0.0
    assert "unknown" in source
    assert index_options.get_option_premium("NIFTY", 25300, "CE")[0] == 82.0



# ================================================= accuracy: delta and quotes ==

from intraday_advisor.thesis import ATM_DELTA, option_delta, quote_leg


def test_an_at_the_money_option_has_a_delta_near_a_half():
    delta, basis = option_delta(spot=25300, strike=25300, premium=194.0,
                                days_to_expiry=4, option_type="CE")
    assert 0.45 <= delta <= 0.60
    assert "own price" in basis


def test_a_far_wing_has_a_much_smaller_delta_than_the_old_assumption():
    """The point of the change. At an assumed 0.5 a cheap wing's target looked
    about three times closer than it is."""
    delta, _ = option_delta(spot=25300, strike=25800, premium=12.0,
                            days_to_expiry=4, option_type="CE")
    assert delta < 0.25
    assert delta < ATM_DELTA / 2


def test_a_put_is_measured_on_its_own_side():
    itm_put, _ = option_delta(spot=25300, strike=25600, premium=330.0,
                              days_to_expiry=4, option_type="PE")
    otm_put, _ = option_delta(spot=25300, strike=25000, premium=40.0,
                              days_to_expiry=4, option_type="PE")
    assert itm_put > otm_put


def test_no_price_or_expiry_falls_back_and_says_so():
    delta, basis = option_delta(spot=25300, strike=25300, premium=None,
                                days_to_expiry=0, option_type="CE")
    assert delta == ATM_DELTA
    assert "assumed" in basis


def test_any_strike_gets_exact_costs_and_its_own_checks():
    """A contract you chose yourself is the one most worth checking; it used
    to be the only one with no analysis behind it."""
    quote = quote_leg(signal(direction="WAIT"), strike=25400.0,
                      option_type="CE", premium=150.0, lots=2,
                      target_pct=0.25, stop_pct=0.20,
                      available_capital=200_000, mode="LIVE")
    plan = quote["plan"]
    assert plan["lots"] == 2 and plan["quantity"] == 130
    assert plan["round_trip_cost"] > 0
    assert plan["breakeven"] > 150.0, "costs must lift the breakeven"
    assert quote["thesis"]["checks"], "the leg is analysed, not waved through"
    assert any("your own decision" in n for n in quote["notes"])


def test_a_leg_against_the_engine_is_called_out():
    quote = quote_leg(signal(), strike=25300.0, option_type="PE",
                      premium=180.0, lots=1, target_pct=0.25, stop_pct=0.20,
                      available_capital=200_000, mode="LIVE")
    assert any("AGAINST" in n for n in quote["notes"])
    assert quote["is_engine_contract"] is False


def test_the_agents_verdict_is_not_borrowed_for_another_contract():
    """A VETO was about the engine's contract. Carrying it onto a different
    strike would report an objection nobody actually made about it."""
    vetoed = signal(agent={"stance": "VETO", "concerns": ["Costs."]})
    other = quote_leg(vetoed, strike=25500.0, option_type="CE", premium=90.0,
                      lots=1, target_pct=0.25, stop_pct=0.20,
                      available_capital=200_000, mode="LIVE")
    same = quote_leg(vetoed, strike=25300.0, option_type="CE", premium=194.0,
                     lots=1, target_pct=0.25, stop_pct=0.20,
                     available_capital=200_000, mode="LIVE")
    assert "Risk checklist" not in other["thesis"]["blockers"]
    assert "Risk checklist" in same["thesis"]["blockers"]


def test_an_order_larger_than_the_capital_is_flagged_not_resized():
    quote = quote_leg(signal(), strike=25300.0, option_type="CE",
                      premium=194.0, lots=10, target_pct=0.25, stop_pct=0.20,
                      available_capital=20_000, mode="LIVE")
    assert quote["plan"]["lots"] == 10, "price what was asked for"
    assert quote["plan"]["affordable"] is False
    assert any("needs" in n for n in quote["notes"])


def test_a_leg_without_a_premium_reports_rather_than_raises():
    quote = quote_leg(signal(), strike=25300.0, option_type="CE", premium=0,
                      lots=1, target_pct=0.25, stop_pct=0.20,
                      available_capital=20_000)
    assert "error" in quote


# ====================================================== a position's expiry ==

def test_an_expired_contract_has_no_quote(monkeypatch):
    from intraday_advisor import index_options

    def forbidden(*a, **k):
        raise AssertionError("an expired contract must not be looked up")

    monkeypatch.setattr(index_options, "suggest_option_trade", forbidden)
    premium, source = index_options.get_option_premium(
        "NIFTY", 24650, "CE", expiry="06NOV2020")
    assert premium == 0.0
    assert "expired" in source


def test_a_position_is_priced_from_its_own_expiry(monkeypatch):
    """The same strike in another week is a different contract."""
    from intraday_advisor import index_options

    seen = {}

    def chain(index, spot=0.0, expiry=None):
        seen["expiry"] = expiry
        return {"df": pd.DataFrame({"strike": [25300.0], "ce_ltp": [82.0],
                                    "pe_ltp": [75.0]}), "data_source": "live"}

    monkeypatch.setattr(index_options, "suggest_option_trade", chain)
    index_options.get_option_premium("NIFTY", 25300, "CE", expiry="24NOV2099")
    assert seen["expiry"] == "24NOV2099"


def test_the_expiry_is_stored_with_the_position(tmp_path):
    from intraday_advisor.positions import load_open_positions, open_position

    db = str(tmp_path / "ledger.sqlite")
    open_position(index_name="NIFTY", option_type="CE", strike=25300,
                  lot_size=65, lots=1, entry_premium=194.0,
                  expiry="24nov2026", db_path=db)
    assert load_open_positions(db_path=db)[0].expiry == "24NOV2026"


def test_the_regimes_own_up_down_spelling_is_understood():
    """Found live on 2026-11-17: the regime reports UP / DOWN, the check
    matched BULLISH / BEARISH, and every trend row read "no clear lean"."""
    up = brief(regime={"regime": "TRENDING_UP", "bias": "UP", "conviction": 52})
    assert state_of(up, "trend") == PASS
    against = brief(regime={"regime": "TRENDING_DOWN", "bias": "DOWN",
                            "conviction": 70})
    assert state_of(against, "trend") == FAIL
    put = brief(direction="PUT", option_type="PE", underlying_score=20,
                regime={"regime": "TRENDING_DOWN", "bias": "DOWN",
                        "conviction": 70})
    assert state_of(put, "trend") == PASS
