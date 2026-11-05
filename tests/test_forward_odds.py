"""Odds for one trade, with the costs charged and the reversion preserved.

The idea is borrowed from the day-trader-skill pack's Time Machine. These tests
pin the two things that had to change for it to be usable here: costs are
charged from the real premium, and bars are resampled in contiguous blocks so
that a mean-reverting index does not simulate as a trending one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.forward_odds import (
    BLOCK_BARS,
    MIN_SESSIONS,
    assess,
    forward_odds,
    history_leg,
    render,
    required_hit_rate,
    session_blocks,
)


def make_bars(sessions: int = 40, bars_per_session: int = 75, *,
              drift: float = 0.0, seed: int = 3,
              mean_revert: float = 0.0) -> pd.DataFrame:
    """Synthetic 5-minute sessions with a controllable serial correlation.

    ``mean_revert`` above 0 makes each step lean against the last one, which is
    what these indices actually do (measured lag-1 -0.088 / -0.099).
    """
    rng = np.random.default_rng(seed)
    rows = []
    stamps = []
    price = 22_000.0
    for day in range(sessions):
        start = pd.Timestamp("2026-08-03 09:15", tz="Asia/Kolkata") + pd.Timedelta(days=day)
        step_before = 0.0
        for bar in range(bars_per_session):
            shock = rng.normal(drift, 0.0012)
            step = shock - mean_revert * step_before
            step_before = step
            close = price * (1 + step)
            high = max(price, close) * (1 + abs(rng.normal(0, 0.0004)))
            low = min(price, close) * (1 - abs(rng.normal(0, 0.0004)))
            rows.append((price, high, low, close, 1000.0))
            stamps.append(start + pd.Timedelta(minutes=5 * bar))
            price = close
    frame = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"],
                         index=pd.DatetimeIndex(stamps))
    return frame


# ==================================================================== pool ==

def test_the_pool_keeps_sessions_separate():
    """Blocks must never splice across an overnight gap, or the replay inherits
    a jump an intraday path cannot have."""
    blocks = session_blocks(make_bars(sessions=30))
    assert len(blocks) == 30
    assert all(len(s) >= BLOCK_BARS * 2 for s in blocks)


def test_a_thin_history_returns_no_pool_rather_than_a_thin_one():
    assert session_blocks(make_bars(sessions=5)) == []


def test_an_empty_frame_is_handled():
    assert session_blocks(pd.DataFrame()) == []
    assert session_blocks(None) == []


# ================================================================= replays ==

def test_odds_sum_to_one():
    odds = forward_odds(make_bars(), entry=22_000, stop=21_900, target=22_100,
                        bars_left=40, replays=2000)
    total = odds.target_first + odds.stop_first + odds.neither
    assert odds.replays == 2000
    assert total == pytest.approx(1.0, abs=1e-9)


def test_the_same_trade_twice_gives_the_same_answer():
    """A risk number that moves when you re-ask it cannot be argued with."""
    kwargs = dict(entry=22_000, stop=21_900, target=22_100, bars_left=40,
                  replays=1500)
    bars = make_bars()
    assert (forward_odds(bars, **kwargs).as_dict()
            == forward_odds(bars, **kwargs).as_dict())


def test_a_nearer_target_is_reached_more_often():
    bars = make_bars()
    near = forward_odds(bars, entry=22_000, stop=21_800, target=22_050,
                        bars_left=50, replays=3000)
    far = forward_odds(bars, entry=22_000, stop=21_800, target=22_400,
                       bars_left=50, replays=3000)
    assert near.target_first > far.target_first


def test_a_bar_touching_both_counts_as_the_stop():
    """The conservative convention, kept from the original. With stop and
    target both inside a single bar's range, every replay must resolve to the
    stop -- never the target."""
    odds = forward_odds(make_bars(), entry=22_000, stop=21_999.99,
                        target=22_000.01, bars_left=30, replays=500)
    assert odds.stop_first == 1.0
    assert odds.target_first == 0.0


def test_a_short_is_assessed_the_right_way_round():
    odds = forward_odds(make_bars(), entry=22_000, stop=22_100, target=21_900,
                        bars_left=40, replays=2000)
    assert odds.replays == 2000
    assert odds.target_first > 0


def test_levels_on_the_wrong_side_are_refused_not_guessed():
    bad = forward_odds(make_bars(), entry=22_000, stop=22_100, target=22_200,
                       bars_left=40)
    assert bad.replays == 0
    assert "stop < entry < target" in bad.note


def test_no_bars_left_reports_rather_than_dividing_by_zero():
    none_left = forward_odds(make_bars(), entry=22_000, stop=21_900,
                             target=22_100, bars_left=0)
    assert none_left.replays == 0
    assert "No bars left" in none_left.note


def test_a_thin_history_refuses_to_give_odds():
    thin = forward_odds(make_bars(sessions=4), entry=22_000, stop=21_900,
                        target=22_100, bars_left=40)
    assert thin.replays == 0
    assert str(MIN_SESSIONS) in thin.note


def test_block_resampling_preserves_mean_reversion():
    """The reason this does not resample single bars.

    A mean-reverting series should reach a far breakout target LESS often than
    a memoryless one of the same volatility. With single-bar draws the memory
    is destroyed and both look the same; with blocks the difference survives.
    """
    reverting = make_bars(mean_revert=0.6, seed=11)
    blocks = forward_odds(reverting, entry=22_000, stop=21_600, target=22_400,
                          bars_left=70, replays=4000, block=BLOCK_BARS)
    singles = forward_odds(reverting, entry=22_000, stop=21_600, target=22_400,
                           bars_left=70, replays=4000, block=1)
    assert blocks.target_first < singles.target_first, (
        "independent single-bar draws must overstate the breakout odds on a "
        "mean-reverting series — that is the flaw being corrected"
    )


# ============================================================ cost hurdle ==

def test_a_one_to_one_trade_needs_fifty_percent_before_costs():
    hurdle = required_hit_rate(entry_premium=200.0, stop_premium=180.0,
                               target_premium=220.0, quantity=65)
    assert hurdle["reward_risk"] == pytest.approx(1.0)
    assert hurdle["breakeven_rate_gross"] == pytest.approx(0.5, abs=1e-4)


def test_costs_always_raise_the_bar():
    """This is the whole point of the addition."""
    hurdle = required_hit_rate(entry_premium=200.0, stop_premium=180.0,
                               target_premium=220.0, quantity=65)
    assert hurdle["breakeven_rate_net"] > hurdle["breakeven_rate_gross"]
    assert hurdle["round_trip_cost"] > 0


def test_a_two_to_one_trade_needs_about_a_third_before_costs():
    hurdle = required_hit_rate(entry_premium=200.0, stop_premium=180.0,
                               target_premium=240.0, quantity=65)
    assert hurdle["breakeven_rate_gross"] == pytest.approx(1 / 3, abs=1e-4)


def test_a_cheap_option_is_eaten_by_its_own_spread():
    """At a Rs 12 premium the round trip is a large share of the target, which
    is exactly the trade this project keeps being offered."""
    hurdle = required_hit_rate(entry_premium=12.0, stop_premium=9.0,
                               target_premium=15.0, quantity=65)
    assert hurdle["breakeven_rate_net"] > 0.60


def test_a_target_smaller_than_its_costs_is_called_unwinnable():
    hurdle = required_hit_rate(entry_premium=200.0, stop_premium=199.0,
                               target_premium=200.2, quantity=65)
    assert hurdle["unwinnable"] is True
    assert hurdle["breakeven_rate_net"] == 1.0
    assert "no win rate breaks even" in hurdle["note"]


def test_inverted_levels_are_refused():
    assert "error" in required_hit_rate(
        entry_premium=200.0, stop_premium=220.0, target_premium=180.0,
        quantity=65)


# =============================================================== assessment ==

def test_the_verdict_compares_against_costs_not_against_fifty():
    result = assess(
        make_bars(), entry_premium=200.0, stop_premium=180.0,
        target_premium=220.0, quantity=65, entry=22_000, stop=21_900,
        target=22_100, bars_left=40, replays=2000,
    )
    assert result.odds is not None
    assert result.breakeven_rate_net > 0.5
    expected = round((result.odds.hit_rate - result.breakeven_rate_net) * 100, 1)
    assert result.edge_points == expected
    assert result.verdict


def test_an_unwinnable_trade_is_refused_before_the_odds_are_computed():
    """No point replaying 10,000 sessions for a trade the arithmetic already
    rules out -- and reporting odds on it would read as encouragement."""
    result = assess(
        make_bars(), entry_premium=200.0, stop_premium=199.0,
        target_premium=200.2, quantity=65, entry=22_000, stop=21_990,
        target=22_002, bars_left=40,
    )
    assert result.odds is None
    assert "costs exceed the target" in result.verdict


def test_a_mostly_undecided_trade_says_so():
    """With a wide stop and target most replays end at the close, and then the
    square-off decides the trade rather than the plan."""
    result = assess(
        make_bars(), entry_premium=200.0, stop_premium=100.0,
        target_premium=400.0, quantity=65, entry=22_000, stop=18_000,
        target=26_000, bars_left=20, replays=1000,
    )
    assert result.odds.neither > 0.40
    assert any("still open at the close" in n for n in result.notes)


def test_the_rendered_report_states_the_cost_hurdle():
    result = assess(
        make_bars(), entry_premium=200.0, stop_premium=180.0,
        target_premium=220.0, quantity=65, entry=22_000, stop=21_900,
        target=22_100, bars_left=40, replays=1000,
    )
    text = render(result)
    assert "needs to win" in text
    assert "after costs" in text
    assert "round trip cost" in text


# ================================================================== history ==

def test_the_history_leg_is_one_observation_per_session():
    result = history_leg(make_bars(sessions=35), stop_pct=0.004,
                         target_pct=0.004, bars_left=60)
    assert result["sessions"] == 35
    assert (result["target_first"] + result["stop_first"]
            + result["neither"]) == 35


def test_the_history_leg_refuses_a_thin_sample():
    assert history_leg(make_bars(sessions=4), stop_pct=0.004,
                       target_pct=0.004, bars_left=60)["sessions"] == 0
