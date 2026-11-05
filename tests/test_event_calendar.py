"""Scheduled events the index cares about, and the honesty about what is known.

NIFTY is cap-weighted, so the day one heavyweight reports is a day the index can
gap on one name. The checklist learned about expiry and still knew nothing about
results days or RBI policy.

The hard part is not fetching dates, it is not overclaiming: Yahoo reports Indian
results in a US timezone, its "next" date goes stale, and the policy list is
static. Each of those is pinned below, because a calendar that quietly reports
"nothing scheduled" when it has simply run out is worse than no calendar.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from intraday_advisor.event_calendar import (
    NEAR_DAYS,
    Event,
    describe,
    events_for,
    policy_dates,
    policy_horizon,
)


# ===================================================================== near ==

def test_an_event_today_is_near():
    assert Event("earnings", "X results", "2026-11-05", 0).near


def test_the_day_either_side_is_near():
    """Deliberate: the date carries a day of uncertainty because Yahoo stamps
    Indian results in a US timezone."""
    assert Event("earnings", "X", "2026-11-06", 1).near
    assert Event("earnings", "X", "2026-11-04", -1).near


def test_further_out_is_not_near():
    assert not Event("earnings", "X", "2026-11-12", 7).near


# ================================================================= earnings ==

def test_a_result_today_is_reported_and_named():
    risk = events_for("2026-11-05",
                      earnings={"BHARTIARTL.NS": "2026-11-05"})
    assert risk.has_event
    assert risk.earnings_today == ["BHARTIARTL"]
    assert "BHARTIARTL results today" in risk.note


def test_a_result_tomorrow_is_flagged_but_not_as_today():
    risk = events_for("2026-11-05", earnings={"RELIANCE.NS": "2026-11-06"})
    assert risk.has_event
    assert risk.earnings_today == []
    assert risk.events[0].days_away == 1


def test_a_result_weeks_away_is_not_an_event_for_this_session():
    """This is what makes a stale Yahoo date harmless: it never enters the
    window rather than needing to be detected."""
    risk = events_for("2026-11-05", earnings={"TCS.NS": "2026-10-08"})
    assert not risk.has_event
    assert "Nothing scheduled" in risk.note


def test_an_unparseable_date_is_skipped_not_guessed():
    risk = events_for("2026-11-05", earnings={"X.NS": "not a date"})
    assert not risk.has_event


def test_several_events_are_sorted_by_closeness():
    risk = events_for("2026-11-05", earnings={
        "A.NS": "2026-11-06", "B.NS": "2026-11-05"})
    assert [e.days_away for e in risk.events] == [0, 1]


# =================================================================== policy ==

def test_a_policy_day_is_detected():
    policy = policy_dates()
    assert policy, "the static list must not be empty"
    target = policy[0].isoformat()
    risk = events_for(target, earnings={})
    assert risk.policy_today
    assert any(e.kind == "policy" for e in risk.events)


def test_a_normal_day_is_not_a_policy_day():
    risk = events_for("2026-11-05", earnings={})
    assert not risk.policy_today


def test_the_policy_dates_are_sorted_and_parseable():
    parsed = policy_dates()
    assert parsed == sorted(parsed)
    assert all(isinstance(d, date) for d in parsed)


def test_a_session_past_the_policy_list_reports_the_blind_spot():
    """The failure mode that makes a calendar dangerous. Running out of dates
    must read as "not checked", never as "nothing scheduled"."""
    beyond = (date.fromisoformat(policy_horizon()) + timedelta(days=30))
    risk = events_for(beyond.isoformat(), earnings={})
    assert risk.stale_policy_list
    assert "NOT being checked" in risk.note
    assert "RBI_MPC_DATES" in risk.note


def test_the_stale_warning_survives_into_the_description():
    beyond = (date.fromisoformat(policy_horizon()) + timedelta(days=30))
    text = describe(events_for(beyond.isoformat(), earnings={}))
    assert "!!" in text
    assert "NOT being checked" in text


def test_the_policy_list_reaches_past_today():
    """A guard on the guard: if this fails the list needs updating, and the
    suite should say so before a live session does."""
    from intraday_advisor.ledger.recorder import now_ist

    assert policy_horizon() >= now_ist().date().isoformat(), (
        "RBI_MPC_DATES has run out — update it, or policy days stop being "
        "checked entirely"
    )


# ================================================================ bad input ==

def test_an_unreadable_session_date_is_reported_not_raised():
    risk = events_for("the fifth of November", earnings={})
    assert not risk.has_event
    assert "Unreadable" in risk.note


def test_the_description_of_a_quiet_day_says_so():
    assert "nothing within a day" in describe(events_for("2026-11-05",
                                                         earnings={}))


def test_the_payload_is_serialisable():
    import json

    payload = events_for("2026-11-05",
                         earnings={"BHARTIARTL.NS": "2026-11-05"}).as_dict()
    assert json.loads(json.dumps(payload))["earnings_today"] == ["BHARTIARTL"]


# ============================================================== the checklist ==

def test_a_results_day_cautions_rather_than_vetoes():
    """A scheduled event is elevated two-way risk, not a structurally unsound
    trade -- and a bought option is not always on the wrong side of one."""
    from intraday_advisor.agents.base import TradeBrief
    from intraday_advisor.agents.rules import RuleAgent

    brief = TradeBrief(
        index="NIFTY", direction="CALL", score=60, contract="24500 CE",
        entry_premium=194.0, target_premium=242.0, stop_premium=155.0,
        lots=1, net_rr=1.2, cost_drag=0.01, data_is_live=True,
        market_open=True, minutes_to_square_off=120, days_to_expiry=4.0,
        events_today=["BHARTIARTL"],
    )
    verdict = RuleAgent().review(brief)
    assert verdict.stance == "CAUTION"
    assert any("BHARTIARTL" in c for c in verdict.concerns)


def test_a_policy_day_cautions_and_names_itself():
    from intraday_advisor.agents.base import TradeBrief
    from intraday_advisor.agents.rules import RuleAgent

    brief = TradeBrief(
        index="NIFTY", direction="CALL", score=60, contract="24500 CE",
        entry_premium=194.0, target_premium=242.0, stop_premium=155.0,
        lots=1, net_rr=1.2, cost_drag=0.01, data_is_live=True,
        market_open=True, minutes_to_square_off=120, days_to_expiry=4.0,
        policy_today=True,
    )
    verdict = RuleAgent().review(brief)
    assert verdict.stance == "CAUTION"
    assert any("RBI policy" in c for c in verdict.concerns)


def test_a_stale_calendar_is_surfaced_to_the_trader_not_hidden():
    from intraday_advisor.agents.base import TradeBrief
    from intraday_advisor.agents.rules import RuleAgent

    brief = TradeBrief(
        index="NIFTY", direction="CALL", score=60, contract="24500 CE",
        entry_premium=194.0, target_premium=242.0, stop_premium=155.0,
        lots=1, net_rr=1.2, cost_drag=0.01, data_is_live=True,
        market_open=True, minutes_to_square_off=120, days_to_expiry=4.0,
        event_calendar_stale=True,
    )
    verdict = RuleAgent().review(brief)
    assert any("blind spot" in c for c in verdict.concerns)


def test_a_quiet_calendar_raises_no_event_objection():
    from intraday_advisor.agents.base import TradeBrief
    from intraday_advisor.agents.rules import RuleAgent

    brief = TradeBrief(
        index="NIFTY", direction="CALL", score=60, contract="24500 CE",
        entry_premium=194.0, target_premium=242.0, stop_premium=155.0,
        lots=1, net_rr=1.2, cost_drag=0.01, data_is_live=True,
        market_open=True, minutes_to_square_off=120, days_to_expiry=4.0,
    )
    concerns = RuleAgent().review(brief).concerns or []
    assert not any("reports today" in c or "RBI policy" in c for c in concerns)
