"""Multi-leg structures: payoff arithmetic, capital, cost and real edge.

The app could previously express only one structure -- buy a single option --
which is the worst risk shape for a small account and makes you the buyer of a
volatility the chain said was expensive. These tests pin down the arithmetic
that makes the alternatives safe to show a trader.
"""

from __future__ import annotations

import pytest

from intraday_advisor.spreads import (
    Leg,
    assess_edge,
    build_structure,
    iron_condor,
    premiums_from_chain,
    time_is_sufficient,
    vertical,
)

LOT = 65

#: Live NIFTY prices from 2026-10-21, spot ~22,690.
PREMIUMS = {
    (22_400.0, "PE"): 72.00, (22_400.0, "CE"): 390.00,
    (22_500.0, "PE"): 88.75, (22_500.0, "CE"): 317.95,
    (22_600.0, "PE"): 117.65, (22_600.0, "CE"): 249.50,
    (22_700.0, "PE"): 168.35, (22_700.0, "CE"): 190.20,
    (22_900.0, "PE"): 270.85, (22_900.0, "CE"): 103.25,
    (23_000.0, "PE"): 346.40, (23_000.0, "CE"): 79.35,
}


def bear_put():
    return vertical("NIFTY", "PE", 22_700.0, 22_500.0, PREMIUMS, LOT)


def bull_put_credit():
    return vertical("NIFTY", "PE", 22_400.0, 22_500.0, PREMIUMS, LOT)


# ============================================================ payoff shape ==

def test_a_debit_spread_risks_exactly_what_it_cost():
    spread = bear_put()
    assert spread.net_debit > 0
    assert spread.net_credit == 0
    assert spread.max_loss == pytest.approx(spread.net_debit, abs=1.0)


def test_a_debit_spread_caps_the_gain_at_the_short_strike():
    """The point of selling the far strike: you were going to exit there."""
    spread = bear_put()
    width = (22_700.0 - 22_500.0) * LOT
    assert spread.max_profit == pytest.approx(width - spread.net_debit, abs=1.0)


def test_the_payoff_is_flat_beyond_both_strikes():
    spread = bear_put()
    assert spread.payoff_at(22_000) == pytest.approx(spread.payoff_at(21_000))
    assert spread.payoff_at(23_000) == pytest.approx(spread.payoff_at(24_000))


def test_the_breakeven_sits_between_the_strikes():
    spread = bear_put()
    assert len(spread.breakevens) == 1
    assert 22_500.0 < spread.breakevens[0] < 22_700.0
    assert spread.payoff_at(spread.breakevens[0]) == pytest.approx(0, abs=50)


def test_direction_is_derived_from_the_strikes_not_a_flag():
    """A mislabelled position is a whole class of bug this removes."""
    assert "Bear put" in vertical(
        "NIFTY", "PE", 22_700.0, 22_500.0, PREMIUMS, LOT).name
    assert "Bull put" in vertical(
        "NIFTY", "PE", 22_500.0, 22_700.0, PREMIUMS, LOT).name
    assert "Bull call" in vertical(
        "NIFTY", "CE", 22_600.0, 22_900.0, PREMIUMS, LOT).name


# ================================================================= capital ==

def test_a_debit_structure_ties_up_its_debit():
    spread = bear_put()
    assert spread.capital_required == pytest.approx(spread.net_debit)


def test_a_credit_structure_ties_up_margin_not_the_credit():
    """The trap: a Rs 1,000 credit can require Rs 5,000 of margin. Sizing off
    the credit would be sizing off the wrong number by five times."""
    spread = bull_put_credit()
    assert spread.net_credit > 0
    assert spread.capital_required > spread.net_credit * 2
    assert spread.capital_required == pytest.approx(spread.max_loss, abs=1.0)


def test_a_spread_costs_far_less_capital_than_the_naked_option():
    """The measured reason to prefer it: same view, same target, half the
    capital. Naked 22700 PE was Rs 10,943 of a Rs 20,000 account."""
    naked = PREMIUMS[(22_700.0, "PE")] * LOT
    assert bear_put().capital_required < naked * 0.6


# =================================================================== costs ==

def test_costs_scale_with_legs_both_ways():
    """Two legs is FOUR orders round trip, and the half-spread is paid on every
    one of them. This is what makes small structures unviable."""
    spread = bear_put()
    assert spread.orders_round_trip == 4
    assert spread.round_trip_cost > 0

    condor = iron_condor("NIFTY", {
        "long_put": 22_400.0, "short_put": 22_500.0,
        "short_call": 22_900.0, "long_call": 23_000.0,
    }, PREMIUMS, LOT)
    assert condor.orders_round_trip == 8
    assert condor.round_trip_cost > spread.round_trip_cost


def test_a_structure_whose_costs_exceed_its_profit_says_so():
    """One rupee wide, so the cost cannot possibly be covered."""
    legs = [
        Leg("PE", 22_700.0, "BUY", 168.35),
        Leg("PE", 22_699.0, "SELL", 168.30),
    ]
    tiny = build_structure("hair-thin", legs, LOT)
    assert tiny.net_max_profit <= 0
    assert any("exceed" in w for w in tiny.warnings)


def test_an_unpriced_leg_is_refused_not_guessed():
    legs = [Leg("PE", 22_700.0, "BUY", 168.35), Leg("PE", 22_500.0, "SELL", 0.0)]
    result = build_structure("broken", legs, LOT)
    assert result.warnings
    assert result.max_profit == 0


# ============================================== the question that matters ==

def test_reward_risk_is_converted_into_the_win_rate_it_demands():
    """A credit spread at 0.30 reward:risk is not a bad trade -- it is a trade
    that needs a 77% win rate. Reading R:R as the answer is the mistake."""
    credit = bull_put_credit()
    edge = assess_edge(credit, spot=22_690.0, iv=0.152, days_to_expiry=6.0)

    assert edge["ok"] is True
    assert edge["required_win_rate"] > 0.70
    assert 0 < edge["implied_win_rate"] < 1


def test_the_credit_spreads_priced_that_day_were_negative_expectancy():
    """Measured live: three premium-collection structures that all looked
    sensible and all demanded a higher win rate than the market implied."""
    credit = bull_put_credit()
    edge = assess_edge(credit, spot=22_690.0, iv=0.152, days_to_expiry=6.0)
    assert edge["edge_pp"] < 0
    assert edge["verdict"] == "negative expectancy"


def test_the_debit_spread_that_day_was_the_only_one_with_an_edge():
    edge = assess_edge(bear_put(), spot=22_690.0, iv=0.152, days_to_expiry=6.0)
    assert edge["edge_pp"] > 0


def test_a_wider_sigma_lowers_the_odds_of_a_narrow_credit_spread():
    """More volatility means the short strike is likelier to be breached."""
    credit = bull_put_credit()
    calm = assess_edge(credit, spot=22_690.0, iv=0.10, days_to_expiry=6.0)
    wild = assess_edge(credit, spot=22_690.0, iv=0.30, days_to_expiry=6.0)
    assert wild["implied_win_rate"] < calm["implied_win_rate"]


def test_edge_assessment_degrades_cleanly_without_inputs():
    assert assess_edge(bear_put(), spot=0, iv=0.15,
                       days_to_expiry=6.0)["ok"] is False
    assert assess_edge(bear_put(), spot=22_690.0, iv=0,
                       days_to_expiry=6.0)["ok"] is False


# ====================================================== is there time left ==

def test_a_structure_opened_near_square_off_cannot_pay_its_cost():
    """Measured at 14:37 with 36 minutes left: 1.6% of a six-day structure's
    life remained, worth about Rs 123 against Rs 213 to get in and out."""
    verdict = time_is_sufficient(bear_put(), minutes_left=36, days_to_expiry=6.0)
    assert verdict["ok"] is False
    assert verdict["harvestable"] < verdict["cost"]
    assert "fraction of the edge" in verdict["note"]


def test_a_full_session_can_pay_for_the_structure():
    verdict = time_is_sufficient(bear_put(), minutes_left=360, days_to_expiry=1.0)
    assert verdict["ok"] is True
    assert "Worth opening" in verdict["note"]


def test_no_time_left_is_reported_not_divided_by():
    assert time_is_sufficient(bear_put(), 0, 6.0)["ok"] is False
    assert time_is_sufficient(bear_put(), 30, 0)["ok"] is False


# ================================================================== condor ==

def test_an_iron_condor_profits_in_the_middle_and_is_capped_outside():
    condor = iron_condor("NIFTY", {
        "long_put": 22_400.0, "short_put": 22_500.0,
        "short_call": 22_900.0, "long_call": 23_000.0,
    }, PREMIUMS, LOT)

    assert condor.payoff_at(22_700) > 0, "wins while price goes nowhere"
    assert condor.payoff_at(22_000) < 0
    assert condor.payoff_at(23_500) < 0
    assert condor.payoff_at(21_000) == pytest.approx(condor.payoff_at(22_000))
    assert len(condor.breakevens) == 2


def test_premiums_from_chain_skips_unpriced_strikes():
    import pandas as pd
    frame = pd.DataFrame({
        "strike": [22_500.0, 22_600.0],
        "ce_ltp": [317.95, 0.0],
        "pe_ltp": [88.75, 117.65],
    })
    priced = premiums_from_chain(frame)
    assert (22_500.0, "CE") in priced
    assert (22_600.0, "CE") not in priced
    assert (22_600.0, "PE") in priced


# ============================ probability over EVERY profitable region =====

def condor():
    return iron_condor("NIFTY", {
        "long_put": 22_400.0, "short_put": 22_500.0,
        "short_call": 22_900.0, "long_call": 23_000.0,
    }, PREMIUMS, LOT)


def test_a_two_breakeven_structure_profits_between_them_not_beyond_one():
    """The bug this pins down: the first version took breakevens[0] and asked
    which side won, which is right for a vertical and wrong for a condor. On
    the live BANKNIFTY pin it returned the probability of finishing BELOW the
    lower breakeven -- the chance of losing on the downside -- and reported it
    as the win rate, calling a negative-expectancy trade an edge."""
    edge = assess_edge(condor(), spot=22_690.0, iv=0.152, days_to_expiry=6.0)

    assert edge["ok"] is True
    assert len(edge["breakevens"]) == 2
    assert len(edge["profitable_regions"]) == 1
    region = edge["profitable_regions"][0]
    assert region["from"] == pytest.approx(edge["breakevens"][0])
    assert region["to"] == pytest.approx(edge["breakevens"][1])


def test_the_condor_win_rate_is_the_mass_between_the_breakevens():
    import math
    spot, iv, days = 22_690.0, 0.152, 6.0
    edge = assess_edge(condor(), spot=spot, iv=iv, days_to_expiry=days)
    sigma = spot * iv * math.sqrt(days / 365.0)

    lo, hi = edge["breakevens"]
    def cdf(x):
        return 0.5 * (1 + math.erf(((x - spot) / sigma) / math.sqrt(2)))
    assert edge["implied_win_rate"] == pytest.approx(cdf(hi) - cdf(lo), abs=0.01)


def test_a_narrow_condor_in_a_wide_market_is_negative_expectancy():
    """A 480-point profit band against a 442-point one-sigma move cannot win
    often enough to pay a 64% required rate."""
    edge = assess_edge(condor(), spot=22_690.0, iv=0.152, days_to_expiry=6.0)
    assert edge["implied_win_rate"] < edge["required_win_rate"]
    assert edge["verdict"] == "negative expectancy"


def test_a_vertical_still_reports_a_single_region():
    edge = assess_edge(bear_put(), spot=22_690.0, iv=0.152, days_to_expiry=6.0)
    assert len(edge["profitable_regions"]) == 1
    assert edge["profitable_regions"][0]["from"] is None, "unbounded downside"
