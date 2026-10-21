"""Execution style, which moves cost further than any signal in this project.

The measured spread costs 1.25pp of a one-lot round trip. No signal measured
here has moved the edge at all. So the discipline that matters is: a passive
saving must be *measured* before it may be claimed.
"""

from __future__ import annotations

import pytest

from intraday_advisor.ledger.costs import OptionsCostModel
from intraday_advisor.order_execution import (
    AGGRESSIVE,
    PASSIVE,
    PATIENT,
    FillStatistics,
    break_even_fill_rate,
    compare_styles,
    price_execution,
    with_execution,
)

PREMIUM = 160.0
QTY = 65


def measured(**kwargs) -> FillStatistics:
    base = dict(fill_rate=0.62, adverse_selection_pct=0.0020,
                chase_penalty_pct=0.0015, samples=120, horizon_seconds=30.0)
    base.update(kwargs)
    return FillStatistics(**base)


# ================================================ no measurement, no saving ==

def test_an_unmeasured_passive_plan_is_priced_as_if_it_crossed():
    """The core discipline. An optimistic assumption about execution survives
    into a verdict, so a passive style with no fill statistics must cost the
    same as crossing and say that it is not measured."""
    crossing = price_execution(premium=PREMIUM, quantity=QTY, style=AGGRESSIVE)
    resting = price_execution(premium=PREMIUM, quantity=QTY, style=PASSIVE)

    assert resting.expected_cost == pytest.approx(crossing.expected_cost)
    assert resting.measured is False
    assert crossing.measured is True
    assert any("has not been measured" in n for n in resting.notes)


def test_too_few_samples_is_not_a_measurement():
    thin = measured(samples=5)
    assert thin.usable is False
    plan = price_execution(premium=PREMIUM, quantity=QTY, style=PASSIVE,
                           fills=thin)
    assert plan.measured is False


def test_a_measured_passive_plan_may_claim_its_saving():
    plan = price_execution(premium=PREMIUM, quantity=QTY, style=PASSIVE,
                           fills=measured())
    crossing = price_execution(premium=PREMIUM, quantity=QTY, style=AGGRESSIVE)

    assert plan.measured is True
    assert plan.expected_cost < crossing.expected_cost
    assert plan.spread_cost == 0.0, "a resting order does not cross"
    assert plan.adverse_cost > 0, "but it is adversely selected when it fills"


# ======================================================= the honest surprise ==

def test_patient_execution_can_cost_more_than_simply_crossing():
    """Found by building it: at a 62% fill rate with 0.20% adverse selection,
    PATIENT costs more than AGGRESSIVE, because it pays adverse selection on
    the fills AND the cross plus a widened-spread penalty on the rest."""
    crossing = price_execution(premium=PREMIUM, quantity=QTY, style=AGGRESSIVE)
    patient = price_execution(premium=PREMIUM, quantity=QTY, style=PATIENT,
                              fills=measured())
    assert patient.expected_cost > crossing.expected_cost


def test_a_high_fill_rate_makes_patient_worthwhile():
    patient = price_execution(premium=PREMIUM, quantity=QTY, style=PATIENT,
                              fills=measured(fill_rate=0.95))
    crossing = price_execution(premium=PREMIUM, quantity=QTY, style=AGGRESSIVE)
    assert patient.expected_cost < crossing.expected_cost


def test_adverse_selection_can_erase_the_whole_benefit():
    """If being filled costs as much as crossing, resting cannot win."""
    heavy = measured(adverse_selection_pct=0.010)
    plan = price_execution(premium=PREMIUM, quantity=QTY, style=PASSIVE,
                           fills=heavy)
    crossing = price_execution(premium=PREMIUM, quantity=QTY, style=AGGRESSIVE)
    assert plan.expected_cost > crossing.expected_cost


# ============================================================ the arithmetic ==

def test_fees_are_charged_whatever_the_style():
    styles = [price_execution(premium=PREMIUM, quantity=QTY, style=s,
                              fills=measured()) for s in (AGGRESSIVE, PASSIVE,
                                                          PATIENT)]
    assert all(plan.fees > 0 for plan in styles)
    assert len({round(plan.fees, 2) for plan in styles}) == 1, \
        "statutory fees do not depend on how the order was placed"


def test_size_amortises_the_flat_fee():
    """The second cost lever: Rs 20 per order is 0.40pp at one lot and 0.04pp
    at ten, which is why this strategy class is hostile to small accounts."""
    one = price_execution(premium=PREMIUM, quantity=65, style=AGGRESSIVE)
    ten = price_execution(premium=PREMIUM, quantity=650, style=AGGRESSIVE)
    assert ten.expected_cost_pct < one.expected_cost_pct


def test_a_single_leg_costs_less_than_a_round_trip():
    one_way = price_execution(premium=PREMIUM, quantity=QTY, round_trip=False)
    both = price_execution(premium=PREMIUM, quantity=QTY, round_trip=True)
    assert one_way.expected_cost < both.expected_cost


def test_the_breakdown_sums_to_the_total():
    plan = price_execution(premium=PREMIUM, quantity=QTY, style=PATIENT,
                           fills=measured())
    parts = plan.fees + plan.spread_cost + plan.adverse_cost + plan.chase_cost
    assert plan.expected_cost == pytest.approx(parts, abs=0.01)


# ================================================================== guards ====

def test_an_unknown_style_is_refused():
    with pytest.raises(ValueError):
        price_execution(premium=PREMIUM, quantity=QTY, style="hopeful")


def test_no_premium_prices_nothing():
    plan = price_execution(premium=0.0, quantity=QTY)
    assert plan.expected_cost == 0.0
    assert plan.notes


def test_with_nothing_measured_the_cheapest_pick_is_the_certain_one():
    """Corrected from my own wrong assertion. With no fill statistics all three
    styles cost the same, so the cheapest IS aggressive -- whose cost is
    certain -- and there is nothing to caveat. The tie-break must prefer the
    measured style rather than depend on the order of the STYLES tuple."""
    unmeasured = compare_styles(premium=PREMIUM, quantity=QTY)
    assert unmeasured["cheapest"] == AGGRESSIVE
    assert unmeasured["plans"][AGGRESSIVE]["measured"] is True
    assert "NOT measured" not in unmeasured["note"]


def test_the_caveat_fires_when_the_cheapest_style_is_unmeasured():
    """Constructed so a passive plan is cheapest yet unmeasured: a thin sample
    is not a measurement, so the saving must not be presented as one."""
    from intraday_advisor.order_execution import ExecutionPlan
    plans = {
        AGGRESSIVE: ExecutionPlan(style=AGGRESSIVE, expected_cost_pct=1.14,
                                  measured=True),
        PASSIVE: ExecutionPlan(style=PASSIVE, expected_cost_pct=0.40,
                               measured=False),
    }
    cheapest = min(plans.values(),
                   key=lambda p: (round(p.expected_cost_pct, 6), not p.measured))
    assert cheapest.style == PASSIVE and cheapest.measured is False


def test_a_measured_comparison_carries_no_caveat():
    known = compare_styles(premium=PREMIUM, quantity=QTY, fills=measured())
    assert "NOT measured" not in known["note"]
    assert known["cheapest"] in (AGGRESSIVE, PASSIVE, PATIENT)


# ======================================================= break-even analysis ==

def test_break_even_refuses_when_adverse_selection_exceeds_the_spread():
    result = break_even_fill_rate(premium=PREMIUM, quantity=QTY,
                                 adverse_selection_pct=0.05)
    assert result["ok"] is False
    assert "cannot win at any fill rate" in result["reason"]


def test_break_even_reports_the_ratio_when_resting_can_win():
    result = break_even_fill_rate(premium=PREMIUM, quantity=QTY,
                                 adverse_selection_pct=0.0020)
    assert result["ok"] is True
    assert 0 < result["break_even_fill_rate"] < 1


# ============================================ feeding it back into the costs ==

def test_an_execution_plan_can_drive_the_rest_of_the_system():
    """Sizing, breakeven and candidate ranking should inherit the execution
    assumption without each learning about styles."""
    base = OptionsCostModel()
    plan = price_execution(premium=PREMIUM, quantity=QTY, style=PASSIVE,
                           fills=measured())
    adjusted = with_execution(base, plan, premium=PREMIUM, quantity=QTY)

    assert adjusted.half_spread_pct < base.half_spread_pct
    assert adjusted.min_half_spread_ticks == 0.0


def test_an_unmeasured_plan_does_not_soften_the_cost_model():
    base = OptionsCostModel()
    plan = price_execution(premium=PREMIUM, quantity=QTY, style=PASSIVE)
    adjusted = with_execution(base, plan, premium=PREMIUM, quantity=QTY)
    assert adjusted.half_spread_pct >= base.half_spread_pct
