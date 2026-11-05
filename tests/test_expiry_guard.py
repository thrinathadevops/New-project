"""On expiry day a percentage stop measures decay, not direction.

The checklist knew about minutes-to-square-off and nothing about days-to-expiry,
although `days_to_expiry` had been computed for IV and spread pricing all along.
An ATM weekly is almost all extrinsic value, so on 0 DTE that value goes to zero
over a few hours whichever way the index goes -- and a stop set as a percentage
of the premium, which is how this project sets them, then fires on time passing
rather than on the trade being wrong.

The gap was found by reading the `day-trader-skill` pack's event guard ("Tesla
reports this week, so a day trade is out before the close"). The index
equivalent of an earnings date is expiry.
"""

from __future__ import annotations

import pytest

from intraday_advisor.agents.base import TradeBrief, brief_from_snapshot
from intraday_advisor.agents.rules import RuleAgent


def brief(**overrides) -> TradeBrief:
    """A structurally sound trade, so only the expiry rule can object."""
    base = dict(
        index="NIFTY", direction="CALL", score=60, contract="24500 CE",
        entry_premium=194.0, target_premium=242.0, stop_premium=155.0,
        lots=1, net_rr=1.2, cost_drag=0.01, data_is_live=True,
        market_open=True, minutes_to_square_off=120, graded_trades=0,
    )
    base.update(overrides)
    return TradeBrief(**base)


def expiry_concerns(verdict) -> list[str]:
    return [c for c in (verdict.concerns or [])
            if "xpiry" in c or "ecay" in c]


def test_expiry_day_is_vetoed():
    """The strongest objection the checklist can make, and the right one: the
    stop cannot mean what it says when decay alone can trigger it."""
    verdict = RuleAgent().review(brief(days_to_expiry=0.0))
    assert verdict.stance == "VETO"
    assert expiry_concerns(verdict), "the veto must name expiry as the reason"


def test_under_a_day_to_expiry_is_cautioned_not_vetoed():
    verdict = RuleAgent().review(brief(days_to_expiry=0.4))
    assert verdict.stance == "CAUTION"
    assert expiry_concerns(verdict)


def test_a_comfortable_expiry_raises_no_expiry_objection():
    """It may still be cautioned for other reasons -- an unmeasured edge, for
    one -- but not for expiry."""
    verdict = RuleAgent().review(brief(days_to_expiry=3.0))
    assert verdict.stance != "VETO"
    assert not expiry_concerns(verdict)


def test_an_unknown_expiry_stays_silent_rather_than_guessing():
    """A missing field must not invent a veto. The checklist judges only what
    it can see -- the same rule the regime and breadth checks follow."""
    verdict = RuleAgent().review(brief(days_to_expiry=None))
    assert verdict.stance != "VETO"
    assert not expiry_concerns(verdict)


@pytest.mark.parametrize("dte", [0.0, -1.0])
def test_an_already_expired_contract_is_vetoed_too(dte):
    """A negative figure means the expiry has passed. It must not read as
    'plenty of time' by arithmetic accident."""
    assert RuleAgent().review(brief(days_to_expiry=dte)).stance == "VETO"


def test_the_brief_picks_expiry_up_from_the_snapshot():
    """The rule is useless if the field never arrives. This pins the wiring
    from the engine's volatility block through to the agent."""
    signal = {
        "index": "NIFTY", "direction": "CALL", "score": 60, "strike": 24500,
        "option_type": "CE", "entry_premium": 194.0, "target_premium": 242.0,
        "stop_premium": 155.0, "volatility": {"days_to_expiry": 0.0},
        "plan": {"lots": 1, "net_rr": 1.2, "cost_drag": 0.01},
    }
    snapshot = {"mode": "LIVE", "market_open": True,
                "minutes_to_square_off": 120, "settings": {}}
    built = brief_from_snapshot(signal, snapshot, {})
    assert built.days_to_expiry == 0.0
    assert RuleAgent().review(built).stance == "VETO"


def test_a_snapshot_without_volatility_does_not_break_the_brief():
    built = brief_from_snapshot(
        {"index": "NIFTY", "direction": "WAIT"}, {"settings": {}}, {})
    assert built.days_to_expiry is None
