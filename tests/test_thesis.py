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

