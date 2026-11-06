"""A stop above net breakeven is a locked gain, not a risk.

The bug these lock down
-----------------------
``plan_position`` computed ``risk_amount = abs(net_pnl_at_stop)``. The ``abs()``
is right whenever the stop loses money and exactly wrong when it does not --
and ``positions.breakeven_stop`` plus the Protect button exist specifically to
produce stops that do not.

Measured on a 160 entry with a 161.12 net breakeven, 65-lot, Rs 500,000:

    stop 128  ->  risk 20.59%   SEVERE      correct
    stop 165  ->  risk  2.41%   SAFE        WRONG: that stop realises a gain
    stop 170  ->  risk  5.52%   CAUTION     WRONG, and worse
    stop 200  ->  risk 24.17%   SEVERE      WRONG, and worst of all

**The better the protective stop, the larger the risk it reported.** At stop
170 the dashboard said "CAUTION: one stop-out costs 5.5% of your capital; 13 in
a row halves the account" and then advised dropping to 38 lots -- when 13 such
stop-outs in a row would have grown the account by 5.5% at a time.

So the one feature built to protect a winning trade made that trade look
progressively more dangerous as it became safer, and told the operator to cut
it. That is the most expensive shape a bug can take here: it argues against the
thing that works.
"""

from __future__ import annotations

import pytest

from intraday_advisor.ledger.sizing import describe_plan, plan_position

CAPITAL = 500_000.0
ENTRY = 160.0
TARGET = 220.0
LOT = 65


def plan(stop: float, capital: float = CAPITAL):
    return plan_position(
        available_capital=capital, entry_premium=ENTRY, stop_premium=stop,
        target_premium=TARGET, lot_size=LOT,
    )


# ======================================================= the loss still works ==

def test_a_real_stop_still_reports_a_real_loss():
    """The fix must not blunt the warning that matters."""
    p = plan(128.0)

    assert p.stop_protects_gain is False
    assert p.risk_amount > 0
    assert p.locked_gain == 0.0
    assert p.risk_band == "SEVERE"
    assert p.net_rr > 0


def test_a_stop_between_entry_and_breakeven_is_still_a_loss():
    """The boundary is NET BREAKEVEN, not the entry. A stop above entry but
    below breakeven still gives up the round trip, and calling that protected
    would be the same error in the opposite direction."""
    p = plan(160.0)                     # entry itself; breakeven is ~161.12

    assert p.breakeven > 160.0, "fixture assumption: breakeven is above entry"
    assert p.stop_protects_gain is False
    assert p.risk_amount > 0, "exiting at entry still costs the round trip"
    assert p.risk_band in ("SAFE", "CAUTION", "SEVERE")


# ==================================================== the protected stop case ==

@pytest.mark.parametrize("stop", [165.0, 170.0, 200.0])
def test_a_stop_above_breakeven_carries_no_planned_loss(stop):
    p = plan(stop)

    assert stop > p.breakeven, "fixture assumption"
    assert p.stop_protects_gain is True
    assert p.risk_amount == 0.0
    assert p.risk_pct_of_capital == 0.0
    assert p.locked_gain > 0
    assert p.risk_band == "PROTECTED"


def test_the_locked_gain_grows_with_the_stop_and_the_risk_does_not():
    """The exact inversion that was wrong: a better stop must not read worse."""
    improving = [plan(s) for s in (165.0, 170.0, 200.0)]

    gains = [p.locked_gain for p in improving]
    assert gains == sorted(gains), "a higher protective stop locks in more"
    assert all(p.risk_amount == 0.0 for p in improving)
    assert all(p.risk_band == "PROTECTED" for p in improving)


def test_no_account_halving_warning_on_a_position_that_cannot_lose():
    p = plan(170.0)

    assert p.losses_to_halve == 0
    joined = " ".join(p.warnings)
    assert "halves the account" not in joined
    assert "Drop to" not in joined, "never advise cutting a risk-free position"


def test_the_protected_warning_states_the_gain():
    p = plan(170.0)
    joined = " ".join(p.warnings)

    assert "PROTECTED" in joined
    assert "no planned loss" in joined


def test_protected_is_not_sold_as_risk_free():
    """The opposite error to the one fixed. A stop only pays its stated price
    if it FILLS there, and this app alarms rather than placing a broker order.
    On 2026-10-21 a stop at Rs 164.60 filled at Rs 158.50 because nobody was
    watching -- the stop was right and absence defeated it."""
    joined = " ".join(plan(170.0).warnings)

    assert "not the same as risk-free" in joined
    assert "gap" in joined.lower()
    assert "broker" in joined.lower()


# ============================================================ what is rendered ==

def test_the_summary_does_not_print_a_zero_reward_to_risk_ratio():
    """0.00 : 1 would read as the worst trade on the screen rather than the
    safest. Reward per rupee risked is undefined when nothing is risked."""
    text = describe_plan(plan(170.0))

    assert "0.00 : 1" not in text
    assert "n/a" in text
    assert "Gain if stopped" in text
    assert "PROTECTED" in text


def test_the_summary_still_prints_a_ratio_for_a_real_stop():
    text = describe_plan(plan(128.0))

    assert "Net reward:risk" in text
    assert ": 1" in text
    assert "Risk if stopped" in text


def test_the_plan_serialises_the_new_fields_for_the_dashboard():
    """app.js branches on stop_protects_gain to pick what the risk cells mean,
    so both fields must survive as_dict()."""
    data = plan(170.0).as_dict()

    assert data["stop_protects_gain"] is True
    assert data["locked_gain"] > 0
    assert data["risk_band"] == "PROTECTED"
    # net_rr stays a float, because the dashboard calls .toFixed() on it and a
    # null would throw rather than render.
    assert isinstance(data["net_rr"], float)


def test_an_unaffordable_plan_is_still_unaffordable_not_protected():
    """Band precedence: affordability is the first question."""
    p = plan(170.0, capital=100.0)

    assert p.affordable is False
    assert p.risk_band == "UNAFFORDABLE"
