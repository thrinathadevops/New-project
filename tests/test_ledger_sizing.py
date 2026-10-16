"""Tests for options position sizing when capital is whatever is available.

The behaviour under test is not "pick a size" but "tell the truth about the
size you are forced into". With one indivisible lot and a -20% premium stop,
the risk percentage is whatever it is -- these tests prove it is reported
accurately rather than quietly recomputed into something comfortable.
"""

from __future__ import annotations

import pytest

from intraday_advisor.ledger.sizing import (
    FALLBACK_LOT_SIZES,
    LOT_SIZES,
    describe_plan,
    lot_size_for,
    min_capital_for_risk_pct,
    plan_position,
    suggest_capital_fraction,
)


def plan(**overrides):
    base = {
        "available_capital": 100_000.0,
        "entry_premium": 100.0,
        "stop_premium": 80.0,
        "target_premium": 125.0,
        "lot_size": 75,
    }
    base.update(overrides)
    return plan_position(**base)


# ------------------------------------------------------------------ lot sizes

def test_lot_size_prefers_the_broker_over_any_hardcoded_table(monkeypatch):
    """The broker's instrument master is the only source that is current by
    construction. Checked live on 2026-10-16, every hardcoded value in this
    repo was wrong (NIFTY 75 vs 65, BANKNIFTY 35 vs 30) -- a 13-15% error in
    capital per lot, risk and P&L.
    """
    import intraday_advisor.ledger.sizing as sizing

    monkeypatch.setattr(sizing, "lot_size_from_broker", lambda index: 30)
    assert sizing.lot_size_for("BANKNIFTY") == 30


def test_lot_size_falls_back_when_the_broker_is_unreachable(monkeypatch):
    import intraday_advisor.ledger.sizing as sizing

    monkeypatch.setattr(sizing, "lot_size_from_broker", lambda index: None)
    assert sizing.lot_size_for("NIFTY") == FALLBACK_LOT_SIZES["NIFTY"]


def test_an_explicit_override_beats_everything(monkeypatch):
    import intraday_advisor.ledger.sizing as sizing

    monkeypatch.setattr(sizing, "lot_size_from_broker", lambda index: 30)
    assert sizing.lot_size_for("BANKNIFTY", {"BANKNIFTY": 15}) == 15


def test_unknown_index_refuses_to_guess():
    with pytest.raises(ValueError, match="Unknown index"):
        lot_size_for("RELIANCE")


def test_lot_size_override_is_honoured():
    assert lot_size_for("NIFTY", {"NIFTY": 50}) == 50


# --------------------------------------------------------------- lot counting

def test_lots_floor_to_whole_contracts():
    """1.9 lots' worth of capital buys one lot. Fractions do not exist."""
    result = plan(available_capital=14_000.0)   # one lot costs 7,500
    assert result.lots == 1
    assert result.quantity == 75
    assert result.leftover_capital == pytest.approx(6_500.0)


def test_exact_capital_buys_exactly_one_lot():
    """The user's stated case: a lot costs 5k, I have 5k, I invest 5k."""
    result = plan(available_capital=5_000.0, entry_premium=100.0, lot_size=50)
    assert result.lots == 1
    assert result.deployed_capital == pytest.approx(5_000.0)
    assert result.capital_utilisation == pytest.approx(1.0)


def test_insufficient_capital_is_flagged_not_rounded_up():
    result = plan(available_capital=5_000.0)    # one lot needs 7,500
    assert result.lots == 0
    assert result.affordable is False
    assert result.risk_band == "UNAFFORDABLE"
    assert any("Cannot afford one lot" in w for w in result.warnings)


def test_unaffordable_plan_still_prices_one_notional_lot():
    """The UI must be able to show what the trade *would* cost."""
    result = plan(available_capital=1_000.0)
    assert result.risk_amount > 0
    assert result.capital_per_lot == pytest.approx(7_500.0)


def test_max_capital_fraction_caps_deployment():
    """The single most effective control available when the minimum position is
    one whole lot."""
    full = plan(available_capital=30_000.0)
    capped = plan(available_capital=30_000.0, max_capital_fraction=0.25)

    assert full.lots == 4
    assert capped.lots == 1


def test_max_lots_caps_size():
    assert plan(available_capital=100_000.0, max_lots=2).lots == 2


# ----------------------------------------------------------- risk honesty

def test_risk_is_net_of_costs_not_textbook_distance():
    """Textbook risk is (entry - stop) * qty = Rs 1,500. Real risk is worse,
    because you pay costs on the way out of a loser too."""
    result = plan(available_capital=7_500.0)
    textbook = (100.0 - 80.0) * 75

    assert result.risk_amount > textbook
    assert result.risk_amount < textbook * 1.15     # but not absurdly so


def test_full_capital_in_one_lot_reports_severe_risk():
    """The user's actual plan. A -20% stop on 100% of capital is -20% of the
    account, and the band must say so."""
    result = plan(available_capital=7_500.0)

    assert result.risk_pct_of_capital > 0.20
    assert result.risk_band == "SEVERE"
    assert any("SEVERE" in w for w in result.warnings)


def test_more_capital_does_not_reduce_risk_percentage():
    """The arithmetic small accounts cannot escape.

    At 100% deployment a -20% premium stop is a -20% account loss regardless of
    account size, because extra capital buys extra lots rather than safety.
    100x the capital, same risk percentage.
    """
    small = plan(available_capital=7_500.0)       # 1 lot
    large = plan(available_capital=750_000.0)     # 100 lots

    assert small.lots == 1
    assert large.lots == 100
    assert large.risk_pct_of_capital == pytest.approx(
        small.risk_pct_of_capital, rel=0.05
    )
    assert small.risk_band == large.risk_band == "SEVERE"


def test_deploying_less_is_the_only_lever():
    """Risk scales with the fraction deployed, not with capital held.

    Not exactly proportional, because lots are whole numbers: 25% of Rs 1 lakh
    buys 3 lots where 100% buys 13, so the ratio is 3/13 rather than 1/4. The
    lot grid is itself a reason small accounts cannot fine-tune risk.
    """
    full = plan(available_capital=100_000.0, max_capital_fraction=1.0)
    reduced = plan(available_capital=100_000.0, max_capital_fraction=0.30)

    assert full.lots == 13
    assert reduced.lots == 4
    assert full.risk_pct_of_capital > 0.20
    assert reduced.risk_pct_of_capital == pytest.approx(
        full.risk_pct_of_capital * 4 / 13, rel=0.02
    )
    assert reduced.risk_band == "CAUTION"


def test_losses_to_halve_is_reported():
    """Three consecutive stop-outs at ~21% each take the account below half."""
    assert plan(available_capital=7_500.0).losses_to_halve == 3
    assert plan(available_capital=100_000.0, max_capital_fraction=0.05).losses_to_halve > 20


def test_risk_bands_step_with_deployment():
    # 0.08 => exactly one lot of Rs 7,500, so ~1.5% of a Rs 1 lakh account.
    assert plan(available_capital=100_000.0, max_capital_fraction=0.08).risk_band == "SAFE"
    # 0.30 => 4 lots, ~6.2%.
    assert plan(available_capital=100_000.0, max_capital_fraction=0.30).risk_band == "CAUTION"
    assert plan(available_capital=100_000.0, max_capital_fraction=1.0).risk_band == "SEVERE"
    # Too little deployable capital for even one lot.
    assert plan(available_capital=100_000.0, max_capital_fraction=0.01).risk_band == "UNAFFORDABLE"


def test_single_lot_risk_warning_is_actionable():
    """At one lot there is nothing to reduce, so the advice must be about
    capital or contract choice, not 'buy fewer'."""
    result = plan(available_capital=7_500.0)
    joined = " ".join(result.warnings)

    assert "cannot be reduced" in joined
    assert "capital" in joined


def test_multi_lot_risk_warning_suggests_fewer_lots():
    result = plan(available_capital=100_000.0)
    assert any("Drop to" in w for w in result.warnings)


def test_suggest_capital_fraction_inverts_the_stop():
    """To risk 5% with a 20% stop, deploy 25% of the account."""
    assert suggest_capital_fraction(0.05, 0.20) == pytest.approx(0.25)
    assert suggest_capital_fraction(0.02, 0.20) == pytest.approx(0.10)
    # Cannot deploy more than everything.
    assert suggest_capital_fraction(0.50, 0.20) == 1.0


def test_min_capital_for_risk_pct_states_the_real_requirement():
    """One NIFTY lot at Rs 100 with a 20% stop needs ~Rs 7.5 lakh to risk 2%."""
    needed = min_capital_for_risk_pct(
        entry_premium=100.0, lot_size=75, target_risk_pct=0.02, stop_pct=0.20
    )
    assert needed == pytest.approx(75_000.0)


# -------------------------------------------------------- cost-reality checks

def test_cheap_contract_at_small_size_triggers_cost_drag_warning():
    """Rs 2 premium, one lot: flat Rs 20-per-order brokerage dominates and
    round-trip cost exceeds the entire target move."""
    result = plan_position(
        available_capital=150.0,     # exactly one lot at Rs 2 x 75
        entry_premium=2.0, stop_premium=1.6, target_premium=2.5,
        lot_size=75,
    )
    assert result.lots == 1
    assert result.cost_drag > 0.30
    assert any("Costs eat" in w for w in result.warnings)


def test_flat_brokerage_amortises_at_larger_size():
    """The same Rs 2 contract is far less toxic at 66 lots: brokerage is per
    order, not per lot. Cheap options are a small-account problem specifically.
    """
    one_lot = plan_position(
        available_capital=150.0,
        entry_premium=2.0, stop_premium=1.6, target_premium=2.5, lot_size=75,
    )
    many_lots = plan_position(
        available_capital=10_000.0,
        entry_premium=2.0, stop_premium=1.6, target_premium=2.5, lot_size=75,
    )

    assert many_lots.lots > 60
    assert many_lots.cost_drag < one_lot.cost_drag / 4


def test_unprofitable_by_construction_is_called_out():
    """Target below breakeven: this trade cannot make money even if it works."""
    result = plan_position(
        available_capital=10_000.0,
        entry_premium=2.0, stop_premium=1.8, target_premium=2.1,
        lot_size=75,
    )
    assert result.breakeven > result.target_premium
    assert any("cannot make money after costs" in w for w in result.warnings)


def test_net_rr_below_one_is_warned():
    result = plan_position(
        available_capital=100_000.0,
        entry_premium=100.0, stop_premium=80.0, target_premium=110.0,
        lot_size=75,
    )
    assert result.net_rr < 1.0
    assert any("Net reward:risk" in w for w in result.warnings)


def test_healthy_sized_rich_contract_has_no_warnings():
    """A liquid Rs 100 contract, sized so one stop-out costs ~2%, is clean."""
    result = plan(available_capital=750_000.0, max_capital_fraction=0.10)

    assert result.warnings == []
    assert result.net_rr > 1.0
    assert result.risk_band == "SAFE"
    assert result.lots == 10


# ------------------------------------------------------------------ guards

def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        plan(lot_size=0)
    with pytest.raises(ValueError):
        plan(entry_premium=0.0)


def test_zero_capital_does_not_divide_by_zero():
    result = plan(available_capital=0.0)
    assert result.affordable is False
    assert result.risk_pct_of_capital == 0.0


def test_describe_plan_leads_with_risk():
    text = describe_plan(plan(available_capital=7_500.0))
    assert "Risk if stopped" in text
    assert "SEVERE" in text
    assert "Consecutive stop-outs to halve the account" in text
