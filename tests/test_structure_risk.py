"""Undefined risk, and the structures that were missing.

The bug these lock down
-----------------------
``AGENTS.md`` states the invariant: *"Short legs only ever inside defined risk,
never naked."* Nothing enforced or even detected it. A naked short straddle
priced through ``build_structure`` reported:

    max_loss         Rs  15,145      while payoff_at(40000) = -Rs 1,089,530
    capital_required Rs  15,145      a fabricated margin, derived from it
    net_rr                  1.0      reading as a balanced trade
    warnings               NONE

Two separate causes, and the second was hidden by the first:

1. The payoff extremes were probed at the kinks plus **five times the strike
   span**. A straddle's legs share a strike, so the span was zero and the probe
   reached five *points* either side of 23,000. For a defined-risk structure
   that is harmless -- the payoff is flat beyond the outermost kink, so any
   distance gives the same answer -- which is exactly why nobody noticed.
2. Nothing computed the asymptotic slope, which is the only thing that
   separates "flat beyond the wing" from "falling forever".

Fixing the loss side then exposed the mirror defect: a **long** straddle
reported ``max_profit`` of **-Rs 15,470** -- a negative maximum profit on the
one structure with unlimited upside -- and would have been warned as "cannot
make money at this size".
"""

from __future__ import annotations

import pytest

from intraday_advisor.spreads import (
    STRESS_MOVE_FRACTION,
    Leg,
    broken_wing_butterfly,
    build_structure,
    butterfly,
    iron_butterfly,
    iron_condor,
    open_wings,
    ratio_spread,
    straddle,
    strangle,
    undefined_risk_sides,
    vertical,
)

LOT = 65
PREMIUMS = {
    (22_600, "PE"): 30.0, (22_800, "PE"): 60.0, (23_000, "PE"): 118.0,
    (23_200, "PE"): 240.0,
    (22_800, "CE"): 250.0, (23_000, "CE"): 120.0, (23_200, "CE"): 62.0,
    (23_400, "CE"): 31.0,
}


# ======================================================== detecting the wings ==

def test_a_naked_short_straddle_is_undefined_on_both_sides():
    legs = [Leg("CE", 23_000, "SELL", 120.0), Leg("PE", 23_000, "SELL", 118.0)]
    assert undefined_risk_sides(legs) == ["upside", "downside"]


def test_a_vertical_is_defined_on_both_sides():
    legs = [Leg("PE", 23_000, "BUY", 118.0), Leg("PE", 22_800, "SELL", 60.0)]
    assert undefined_risk_sides(legs) == []


def test_a_naked_short_call_is_undefined_upside_only():
    legs = [Leg("CE", 23_000, "SELL", 120.0)]
    wings = open_wings(legs)
    assert wings["loss_sides"] == ["upside"]
    assert wings["profit_sides"] == []


def test_a_long_straddle_has_unbounded_profit_and_defined_risk():
    legs = [Leg("CE", 23_000, "BUY", 120.0), Leg("PE", 23_000, "BUY", 118.0)]
    wings = open_wings(legs)
    assert wings["loss_sides"] == []
    assert wings["profit_sides"] == ["upside", "downside"]


def test_a_ratio_spread_is_net_short_at_the_wing():
    legs = [Leg("CE", 23_000, "BUY", 120.0, 1),
            Leg("CE", 23_200, "SELL", 62.0, 2)]
    assert undefined_risk_sides(legs) == ["upside"]


# =================================================== the naked short, repriced ==

def test_a_naked_short_straddle_no_longer_hides_its_loss():
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="SELL")

    assert s.risk_is_defined is False
    assert set(s.undefined_sides) == {"upside", "downside"}
    # The old figure was Rs 15,145 -- the premium collected. The stress loss at
    # a 10% move is an order of magnitude larger.
    assert s.max_loss > 100_000
    assert s.stress_move_pct == pytest.approx(STRESS_MOVE_FRACTION * 100, abs=0.1)


def test_the_stress_loss_is_labelled_as_not_a_worst_case():
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="SELL")
    joined = " ".join(s.warnings)

    assert "UNDEFINED RISK" in joined
    assert "NOT a worst case" in joined
    assert "keeps growing" in joined


def test_the_agents_rule_is_quoted_at_the_operator():
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="SELL")
    joined = " ".join(s.warnings)

    assert "never naked" in joined
    assert "Buy a wing" in joined


def test_margin_is_refused_rather_than_fabricated():
    """Deriving margin from a maximum loss that does not exist quoted
    Rs 15,145 for a structure whose real SPAN margin runs to lakhs -- small
    enough to look affordable, which is the worst direction to be wrong in."""
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="SELL")

    assert s.capital_required == 0.0
    assert any("SPAN+ELM" in w for w in s.warnings)
    assert any("margin API" in w for w in s.warnings)


def test_no_reward_to_risk_is_quoted_without_a_risk():
    """A short straddle previously read "net R:R 1.0"."""
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="SELL")

    assert s.net_rr == 0.0
    assert any("undefined rather than large" in n for n in s.notes)


def test_the_payoff_still_tells_the_truth_at_any_spot():
    """The payoff function was always right; only the summary was wrong."""
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="SELL")

    assert s.payoff_at(40_000) < -1_000_000
    assert s.payoff_at(23_000) > 0


# ================================================ the mirror: unbounded profit ==

def test_a_long_straddle_does_not_report_a_negative_maximum_profit():
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="BUY")

    assert s.max_profit > 0, "the one structure with unlimited upside"
    assert s.risk_is_defined is True
    assert set(s.unbounded_profit_sides) == {"upside", "downside"}


def test_a_long_straddle_is_not_warned_as_unable_to_make_money():
    """Both cost warnings compare against a MAXIMUM profit, so neither applies
    where the profit has no maximum."""
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="BUY")
    joined = " ".join(s.warnings)

    assert "cannot make money" not in joined
    assert any("no maximum profit to compare" in n for n in s.notes)


def test_the_unbounded_profit_is_declared_as_understating_the_ratio():
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="BUY")

    assert any("understates this structure" in n for n in s.notes)


def test_a_long_straddle_states_the_move_it_needs():
    s = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="BUY")

    assert any("Breakeven needs a move" in n for n in s.notes)
    assert len(s.breakevens) == 2, "one either side of the strike"


# ========================================================= the new structures ==

def test_an_iron_butterfly_caps_both_tails():
    """The structure to reach for whenever a short straddle is tempting."""
    s = iron_butterfly("NIFTY", 23_000, 22_600, 23_400, PREMIUMS, LOT)

    assert s.risk_is_defined is True
    assert s.undefined_sides == []
    assert s.max_loss > 0
    assert s.capital_required > 0, "a defined-risk credit structure has margin"
    assert s.orders_round_trip == 8


def test_the_iron_butterfly_is_the_short_straddle_made_sizeable():
    naked = straddle("NIFTY", 23_000, PREMIUMS, LOT, action="SELL")
    capped = iron_butterfly("NIFTY", 23_000, 22_600, 23_400, PREMIUMS, LOT)

    assert naked.risk_is_defined is False
    assert capped.risk_is_defined is True
    # Smaller credit, in exchange for a known tail.
    assert capped.net_credit < naked.net_credit
    assert capped.max_loss < naked.max_loss


def test_a_butterfly_peaks_at_its_body_strike():
    s = butterfly("NIFTY", "CE", 22_800, 23_000, 23_200, PREMIUMS, LOT)

    assert s.risk_is_defined is True
    assert s.payoff_at(23_000) > s.payoff_at(22_800)
    assert s.payoff_at(23_000) > s.payoff_at(23_200)
    assert any("23000" in n or "Best case" in n for n in s.notes)


def test_a_butterfly_sells_two_bodies_against_two_wings():
    s = butterfly("NIFTY", "CE", 22_800, 23_000, 23_200, PREMIUMS, LOT)
    body = [leg for leg in s.legs if leg.strike == 23_000][0]

    assert body.lots == 2
    assert body.is_short


def test_a_strangle_needs_the_put_below_the_call():
    s = strangle("NIFTY", 23_200, 22_800, PREMIUMS, LOT, action="SELL")

    assert s.legs == []
    assert any("below the call strike" in w for w in s.warnings)


def test_a_short_strangle_is_undefined_and_names_its_dead_zone():
    s = strangle("NIFTY", 22_800, 23_200, PREMIUMS, LOT, action="SELL")

    assert s.risk_is_defined is False
    assert any("dead zone is 22800 to 23200" in n for n in s.notes)


def test_a_ratio_spread_is_not_sold_as_free():
    s = ratio_spread("NIFTY", "CE", 23_000, 23_200, PREMIUMS, LOT)

    assert s.risk_is_defined is False
    assert "upside" in s.undefined_sides
    if s.net_credit > 0:
        assert any("It is not free" in n for n in s.notes)


def test_a_broken_wing_butterfly_keeps_risk_defined():
    """The whole difference from a ratio spread."""
    s = broken_wing_butterfly("NIFTY", "CE", 22_800, 23_000, 23_400,
                              PREMIUMS, LOT)

    assert s.risk_is_defined is True
    assert "broken-wing" in s.name
    assert any("wider one" in n for n in s.notes)


def test_equal_wings_are_called_out_as_an_ordinary_butterfly():
    s = broken_wing_butterfly("NIFTY", "CE", 22_800, 23_000, 23_200,
                              PREMIUMS, LOT)

    assert any("ordinary butterfly" in n for n in s.notes)


def test_invalid_butterfly_strikes_are_refused():
    for lower, body, upper in ((23_200, 23_000, 22_800), (23_000, 23_000, 23_200)):
        s = butterfly("NIFTY", "CE", lower, body, upper, PREMIUMS, LOT)
        assert s.legs == []
        assert s.warnings


def test_invalid_iron_butterfly_strikes_are_refused():
    s = iron_butterfly("NIFTY", 23_000, 23_400, 22_600, PREMIUMS, LOT)
    assert s.legs == []
    assert s.warnings


# ================================================= the old behaviour preserved ==

def test_a_vertical_is_unchanged_by_all_of_this():
    s = vertical("NIFTY", "PE", 23_000, 22_800, PREMIUMS, LOT)

    assert s.risk_is_defined is True
    assert s.unbounded_profit_sides == []
    assert s.net_rr > 0
    assert s.capital_required > 0
    assert s.stress_move_pct == 0.0, "no stress figure needed when both ends cap"


def test_an_iron_condor_is_unchanged():
    s = iron_condor(
        "NIFTY",
        {"long_put": 22_600, "short_put": 22_800,
         "short_call": 23_200, "long_call": 23_400},
        PREMIUMS, LOT,
    )

    assert s.risk_is_defined is True
    assert s.net_rr >= 0
    assert s.capital_required > 0


def test_a_defined_structure_probes_exactly_because_the_payoff_is_flat():
    """Beyond the outermost kink a defined structure's payoff does not move, so
    the probe distance cannot change the answer. This is why the span collapse
    was harmless for every structure that existed before."""
    s = vertical("NIFTY", "PE", 23_000, 22_800, PREMIUMS, LOT)

    far = s.payoff_at(10_000)
    further = s.payoff_at(1_000)
    assert far == pytest.approx(further, abs=0.01)


def test_a_legless_structure_is_refused():
    s = build_structure("nothing", [], LOT)
    assert s.warnings


def test_a_zero_priced_leg_is_refused():
    s = build_structure(
        "stale", [Leg("CE", 23_000, "BUY", 0.0)], LOT)
    assert any("no price" in w for w in s.warnings)


# ================================= a missing strike is normal, not exceptional ==
#
# Every builder indexed `premiums[...]` directly, so a strike the chain did not
# carry raised KeyError out of the middle of pricing. A live chain returns a
# window around the money and the wings of a condor fall outside it routinely,
# so this is an ordinary input that belongs in a warning rather than a
# traceback.

import pytest as _pytest  # noqa: E402

from intraday_advisor.spreads import missing_premiums  # noqa: E402


def test_missing_premiums_names_what_the_chain_lacks():
    absent = missing_premiums(PREMIUMS, [(23_000, "CE"), (99_999, "CE")])
    assert absent == [(99_999, "CE")]


def test_a_zero_premium_counts_as_missing():
    """0.5 is the synthetic floor this project emits when a feed is
    unavailable; a zero is not a tradeable price either."""
    assert missing_premiums({(1, "CE"): 0.0}, [(1, "CE")]) == [(1, "CE")]


@_pytest.mark.parametrize("build", [
    lambda: straddle("NIFTY", 99_999, {}, LOT),
    lambda: strangle("NIFTY", 99_998, 99_999, {}, LOT),
    lambda: vertical("NIFTY", "PE", 99_999, 99_998, {}, LOT),
    lambda: butterfly("NIFTY", "CE", 1, 2, 3, {}, LOT),
    lambda: iron_butterfly("NIFTY", 2, 1, 3, {}, LOT),
    lambda: ratio_spread("NIFTY", "CE", 1, 2, {}, LOT),
    lambda: iron_condor("NIFTY", {"long_put": 1, "short_put": 2,
                                  "short_call": 3, "long_call": 4}, {}, LOT),
])
def test_no_builder_raises_on_an_unpriced_strike(build):
    structure = build()

    assert structure.legs == []
    assert structure.warnings
    assert "Not priced" in structure.warnings[0]
    assert structure.capital_required == 0.0


def test_the_refusal_names_the_absent_strikes():
    s = iron_condor("NIFTY", {"long_put": 22_600, "short_put": 22_800,
                              "short_call": 23_200, "long_call": 99_999},
                    PREMIUMS, LOT)
    assert s.legs == []
    assert "99999 CE" in s.warnings[0]
    assert "22600 PE" not in s.warnings[0], "only the MISSING ones"


def test_a_fully_priced_structure_is_unaffected_by_the_guard():
    s = iron_condor("NIFTY", {"long_put": 22_600, "short_put": 22_800,
                              "short_call": 23_200, "long_call": 23_400},
                    PREMIUMS, LOT)
    assert len(s.legs) == 4
    assert not any("Not priced" in w for w in s.warnings)
