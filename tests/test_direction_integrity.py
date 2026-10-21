"""Two defects found live on 2026-10-21, with the market falling hard.

Both were in the same family: a direction asserted where none was earned, and
a direction ignored where it mattered. Neither existing test caught them, so
these pin the behaviour down.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.scoring import (
    ScoreWeights,
    SignalScore,
    score_option_signal,
)
from intraday_advisor.stock_screener import screen_one


# ============================================ 1. max pain is not a direction ==

def test_a_neutral_pcr_stays_neutral_however_far_max_pain_sits():
    """THE live defect, at the rule itself.

    Old behaviour: ``if bias == "NEUTRAL": bias = "CALL"`` whenever spot sat
    below max pain. NIFTY's PCR was 0.95 -- squarely mid-range -- and the chain
    still reported CALL.
    """
    from intraday_advisor.nse_option_chain import decide_chain_bias

    bias, reasons = decide_chain_bias(pcr=0.95, spot=22825.0,
                                      max_pain=22950.0, step=50.0)
    assert bias == "NEUTRAL"
    assert any("no directional opinion" in r for r in reasons)


def test_the_bias_rule_has_no_standing_bullish_tilt():
    """Below max pain used to create a CALL while above it never created a
    PUT, so the chain leaned bullish by construction."""
    from intraday_advisor.nse_option_chain import decide_chain_bias

    below = decide_chain_bias(0.95, 22825.0, 22950.0, 50.0)[0]
    above = decide_chain_bias(0.95, 23075.0, 22950.0, 50.0)[0]
    assert below == above == "NEUTRAL"


def test_pcr_still_decides_the_direction():
    from intraday_advisor.nse_option_chain import decide_chain_bias

    assert decide_chain_bias(1.45, 100.0, 100.0, 1.0)[0] == "CALL"
    assert decide_chain_bias(0.60, 100.0, 100.0, 1.0)[0] == "PUT"
    assert decide_chain_bias(1.00, 100.0, 100.0, 1.0)[0] == "NEUTRAL"


def test_max_pain_distance_is_still_reported_to_the_operator():
    """Removed from the decision, not from the screen -- it is real
    information about where writers are positioned."""
    from intraday_advisor.nse_option_chain import decide_chain_bias

    _, reasons = decide_chain_bias(0.95, 22825.0, 22950.0, 50.0)
    assert any("Max Pain" in r for r in reasons)
    assert any("not a direction call" in r for r in reasons)


def bearish(score: int = 0) -> SignalScore:
    return SignalScore(score, "SELL", [], ["every indicator bearish"], {})


def bullish(score: int = 100) -> SignalScore:
    return SignalScore(score, "BUY", ["every indicator bullish"], [], {})


def test_a_neutral_chain_cannot_veto_a_bearish_price_read():
    """The live failure: PCR 0.95 (neutral) was promoted to CALL purely because
    spot sat below max pain, which then conflicted with a 0/100 bearish price
    read built from fourteen indicators -- and the PUT was vetoed."""
    result = score_option_signal(
        bearish(), chain_bias="NEUTRAL", pcr=0.95,
        spot=22825.0, max_pain=22950.0,
    )
    assert result.direction == "PUT"
    assert result.agreement == "price-only"
    assert "chain_conflict" not in result.components


def test_max_pain_only_nudges_the_score_never_the_direction():
    near = score_option_signal(bearish(), chain_bias="NEUTRAL",
                               spot=22825.0, max_pain=22830.0)
    against = score_option_signal(bearish(), chain_bias="NEUTRAL",
                                  spot=22825.0, max_pain=23100.0)
    assert near.direction == against.direction == "PUT"
    assert against.components.get("max_pain_pull", 0) < 0
    assert against.score < near.score


def test_the_max_pain_nudge_is_symmetric():
    """The old rule created a CALL from below-max-pain but never a PUT from
    above it, which gave the chain a standing bullish tilt."""
    w = ScoreWeights()
    call_helped = score_option_signal(
        bullish(), chain_bias="NEUTRAL", spot=100.0, max_pain=102.0, weights=w,
    ).components.get("max_pain_pull", 0)
    put_helped = score_option_signal(
        bearish(), chain_bias="NEUTRAL", spot=100.0, max_pain=98.0, weights=w,
    ).components.get("max_pain_pull", 0)

    assert call_helped > 0 and put_helped > 0
    assert call_helped == pytest.approx(put_helped)


def test_max_pain_within_noise_is_ignored():
    result = score_option_signal(bearish(), chain_bias="NEUTRAL",
                                 spot=22825.0, max_pain=22826.0)
    assert "max_pain_pull" not in result.components


def test_a_real_chain_conflict_still_vetoes():
    """The confluence rule itself was never the problem -- only the evidence
    being fed into it. A chain that genuinely reads CALL must still veto."""
    result = score_option_signal(bearish(), chain_bias="CALL", pcr=1.45)
    assert result.direction == "WAIT"
    assert result.agreement == "conflict"


def test_max_pain_cannot_rescue_a_wait():
    """It runs after the direction is settled, so it must not resurrect one."""
    result = score_option_signal(
        bearish(), chain_bias="CALL", spot=100.0, max_pain=104.0,
    )
    assert result.direction == "WAIT"
    assert "max_pain_pull" not in result.components


# ================================================= 2. shorts are not longs ==

def candles(bars: int = 300, drift: float = 0.0008) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    returns = rng.normal(drift, 0.004, size=bars)
    close = 1000.0 * np.exp(np.cumsum(returns))
    spread = np.abs(rng.normal(0, 0.004, size=bars)) * close
    index = pd.date_range("2026-10-21 09:15", periods=bars, freq="5min")
    return pd.DataFrame({
        "Open": close * (1 + rng.normal(0, 0.0005, size=bars)),
        "High": close + spread, "Low": close - spread, "Close": close,
        "Volume": rng.integers(100_000, 900_000, size=bars),
    }, index=index)


def a_sell_row():
    """A falling series, which is what 29 of 30 ranked names were that day."""
    row = screen_one("TEST", candles(drift=-0.0012), capital=200_000,
                     risk_pct=0.02)
    if row.signal != "SELL":
        pytest.skip("fixture did not produce a SELL read")
    return row


def test_a_sell_plan_brackets_the_entry_the_short_way():
    """The live defect: every row got a long bracket, so a SELL was told its
    target was ABOVE the entry and its stop BELOW -- exactly backwards."""
    row = a_sell_row()
    assert row.side == "SHORT"
    assert row.target < row.entry < row.stop


def test_a_buy_plan_is_still_a_long():
    row = screen_one("TEST", candles(drift=0.0012), capital=200_000,
                     risk_pct=0.02)
    if row.signal != "BUY":
        pytest.skip("fixture did not produce a BUY read")
    assert row.side == "LONG"
    assert row.stop < row.entry < row.target


def test_a_short_breaks_even_below_its_entry():
    """A short has to FALL past its costs, not rise past them."""
    row = a_sell_row()
    assert row.breakeven < row.entry


def test_a_short_is_sized_on_the_stop_distance_not_its_sign():
    row = a_sell_row()
    assert row.shares > 0
    assert row.risk_amount > 0
    textbook = row.shares * (row.stop - row.entry)
    assert row.risk_amount > textbook, "costs widen the loss"


def test_a_short_reward_risk_uses_absolute_distances():
    row = a_sell_row()
    assert row.reward_amount >= 0
    assert row.net_rr >= 0


def test_a_short_row_warns_that_it_needs_margin():
    row = a_sell_row()
    assert any("SHORT" in w for w in row.warnings)
