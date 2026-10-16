"""Tests for world market sessions, the LLM token gate, and news gathering.

The gate is the point: an LLM reviewer polling overnight costs money and tells
you nothing, because there is no live price to reason about. These pin the
window open exactly when it should be and shut otherwise.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from intraday_advisor.market_calendar import (
    CLOSED,
    LUNCH,
    MARKETS,
    OPEN,
    POST_MARKET,
    PRE_MARKET,
    SUN_THU,
    WEEKEND,
    get_market,
    list_markets,
    llm_should_run,
    session_state,
)


def at(tz: str, year=2026, month=10, day=16, hour=9, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))


# ================================================================ coverage ==

def test_a_broad_set_of_markets_is_defined():
    assert len(MARKETS) >= 30
    for key in ("india", "singapore", "usa", "uk", "japan", "hongkong",
                "australia", "germany", "saudi", "brazil"):
        assert key in MARKETS


def test_every_market_is_internally_consistent():
    for market in MARKETS.values():
        assert market.open_time < market.close_time, market.key
        assert market.timezone and "/" in market.timezone, market.key
        assert market.trading_days, market.key
        if market.lunch_start:
            assert market.lunch_end > market.lunch_start, market.key
            assert market.open_time <= market.lunch_start, market.key
            assert market.lunch_end <= market.close_time, market.key


def test_unknown_market_falls_back_to_the_default():
    assert get_market("atlantis").key == "india"
    assert get_market(None).key == "india"


def test_list_markets_is_dashboard_ready():
    rows = list_markets()
    assert len(rows) == len(MARKETS)
    for row in rows:
        assert row["name"] and row["exchange"] and row["open"] and row["close"]


# ============================================================ session state ==

def test_india_session_through_the_day():
    """2026-10-16 is a Friday."""
    cases = {
        (7, 0): CLOSED,        # long before
        (8, 50): PRE_MARKET,   # inside the 30-minute LLM lead
        (9, 30): OPEN,
        (15, 20): OPEN,
        (16, 0): POST_MARKET,
    }
    for (hour, minute), expected in cases.items():
        state = session_state("india", at("Asia/Kolkata", hour=hour, minute=minute))
        assert state.state == expected, f"{hour}:{minute:02d} -> {state.state}"


def test_weekend_is_detected():
    # 2026-10-17 is a Saturday.
    state = session_state("india", at("Asia/Kolkata", day=17, hour=11))
    assert state.state == WEEKEND
    assert state.llm_active is False


def test_gulf_markets_trade_sunday_to_thursday():
    """A Mon-Fri assumption reports Tadawul closed on its busiest day and open
    on its weekend."""
    assert MARKETS["saudi"].trading_days == SUN_THU

    # Friday is Saudi's weekend.
    friday = session_state("saudi", at("Asia/Riyadh", day=16, hour=11))
    assert friday.state == WEEKEND
    # Sunday is a normal trading day there.
    sunday = session_state("saudi", at("Asia/Riyadh", day=18, hour=11))
    assert sunday.state == OPEN
    assert sunday.llm_active is True


def test_lunch_break_is_detected_in_asian_markets():
    """Price does not move during the break; treating it as OPEN invites
    trading a frozen tape."""
    state = session_state("japan", at("Asia/Tokyo", hour=12, minute=0))
    assert state.state == LUNCH
    # Still inside the window, because the session resumes.
    assert state.llm_active is True


def test_each_market_uses_its_own_timezone():
    """Same instant, different sessions."""
    instant = at("Asia/Kolkata", hour=9, minute=30)
    india = session_state("india", instant)
    usa = session_state("usa", instant)

    assert india.state == OPEN
    assert usa.state in (CLOSED, POST_MARKET, WEEKEND)


# ================================================================ the gate ==

def test_llm_gate_opens_exactly_at_the_lead_time():
    """NSE opens 09:15 with a 30-minute lead, so the gate opens at 08:45."""
    before = session_state("india", at("Asia/Kolkata", hour=8, minute=44))
    after = session_state("india", at("Asia/Kolkata", hour=8, minute=46))

    assert before.llm_active is False
    assert after.llm_active is True
    assert "saves tokens" in before.llm_reason or "starts in" in before.llm_reason


def test_llm_gate_shuts_at_the_close():
    during = session_state("india", at("Asia/Kolkata", hour=15, minute=29))
    after = session_state("india", at("Asia/Kolkata", hour=15, minute=31))

    assert during.llm_active is True
    assert after.llm_active is False
    assert "closed" in after.llm_reason.lower()


def test_llm_gate_is_shut_all_weekend():
    for hour in (3, 9, 12, 18, 23):
        state = session_state("india", at("Asia/Kolkata", day=17, hour=hour))
        assert state.llm_active is False


def test_llm_should_run_helper_matches_session_state():
    moment = at("Asia/Kolkata", hour=10)
    active, reason = llm_should_run("india", moment)
    assert active is True
    assert reason == session_state("india", moment).llm_reason


def test_gate_reports_how_long_until_it_opens():
    state = session_state("india", at("Asia/Kolkata", hour=8, minute=15))
    assert state.llm_opens_in_minutes == 30     # 08:45 - 08:15
    assert state.llm_active is False


def test_next_open_is_reported_for_a_closed_market():
    state = session_state("india", at("Asia/Kolkata", day=17, hour=11))  # Saturday
    assert state.next_open and "unknown" not in state.next_open


# ===================================================================== news ==

def test_headline_dedupe_collapses_the_same_story():
    """The same wire story across a dozen outlets must not read as a dozen
    independent confirmations."""
    from intraday_advisor.news import Headline, _dedupe

    headlines = [
        Headline("Fed holds rates steady as inflation cools", "Reuters", "f"),
        Headline("Fed holds rates steady as inflation cools further", "CNBC", "f"),
        Headline("Oil slips on demand worries", "Bloomberg", "f"),
    ]
    assert len(_dedupe(headlines)) == 2


def test_clean_strips_markup_and_entities():
    from intraday_advisor.news import _clean

    assert _clean("<b>Nifty</b> &amp; Sensex  rise") == "Nifty & Sensex rise"
    assert _clean(None) == ""


def test_dead_feed_returns_empty_rather_than_raising():
    from intraday_advisor.news import fetch_feed

    assert fetch_feed("dead", "http://127.0.0.1:1/nope", timeout=1.0) == []


def test_format_for_prompt_handles_no_headlines():
    from intraday_advisor.news import format_for_prompt

    assert "no headlines" in format_for_prompt({"headlines": []}).lower()


# ================================================================ premarket ==

def test_brief_outside_the_window_makes_no_llm_call(monkeypatch):
    """The whole point of the gate: no tokens spent overnight."""
    from intraday_advisor import premarket

    premarket.clear_cache()

    def explode(*args, **kwargs):
        raise AssertionError("news was gathered outside the market window")

    monkeypatch.setattr("intraday_advisor.news.gather_news", explode)

    brief = premarket.build_brief(
        "india", now=at("Asia/Kolkata", hour=2))     # middle of the night
    assert brief.lean == "UNCLEAR"
    assert "paused" in brief.note.lower()


def test_brief_is_cached_per_trading_day(monkeypatch):
    """A ten-second refresh loop must not re-ask the same question all day."""
    from intraday_advisor import premarket

    premarket.clear_cache()
    calls = {"n": 0}

    def counted(*args, **kwargs):
        calls["n"] += 1
        return {"headlines": [], "count": 0, "coverage": "", "market": "india"}

    monkeypatch.setattr("intraday_advisor.news.gather_news", counted)

    moment = at("Asia/Kolkata", hour=9)
    first = premarket.build_brief("india", now=moment, force=True)
    second = premarket.build_brief("india", now=moment)
    third = premarket.build_brief("india", now=moment)

    assert calls["n"] == 1, "the brief should be built once per day, not per call"
    assert second is first and third is first


def test_brief_notification_is_silent_when_there_is_nothing_to_say():
    from intraday_advisor.premarket import PremarketBrief, brief_notification

    empty = PremarketBrief(market="india", market_name="India",
                           session_date="2026-10-16", built_at="now")
    assert brief_notification(empty) is None


@pytest.mark.parametrize("lean,severity,sticky", [
    ("LEAN_UP", "good", False),
    ("LEAN_DOWN", "warn", True),
    ("UNSTABLE", "danger", True),
])
def test_brief_notification_severity(lean, severity, sticky):
    from intraday_advisor.premarket import PremarketBrief, brief_notification

    brief = PremarketBrief(market="india", market_name="India",
                           session_date="2026-10-16", built_at="now",
                           lean=lean, confidence=60, headline="something")
    note = brief_notification(brief)
    assert note["severity"] == severity
    assert note["sticky"] is sticky
