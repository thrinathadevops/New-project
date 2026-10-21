"""Measuring whether a signal predicts anything, and whether it beats costs.

These tests exist because the answer to "how do I make a profit" turned out to
be arithmetic rather than opinion: the edge available at five-minute
granularity was 0.1% and the cheapest round trip was 0.19%.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.validation import (
    COST_INDEX_PCT,
    MIN_SAMPLE,
    autocorrelation_note,
    evaluate_rule,
    score_forward_returns,
    split_sample,
)


def series(n: int = 600, drift: float = 0.0, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 22_000 * np.exp(np.cumsum(rng.normal(drift, 0.0008, size=n)))
    return pd.DataFrame(
        {"Close": close},
        index=pd.date_range("2026-09-01 09:15", periods=n, freq="5min"),
    )


# ============================================================== no lookahead ==

def test_the_forward_return_looks_forward_only():
    frame = series(50)
    study = score_forward_returns(frame, [50] * 50, horizons=(6,))
    expected = (frame["Close"].iloc[6] / frame["Close"].iloc[0] - 1) * 100
    assert study["fwd6"].iloc[0] == pytest.approx(expected)


def test_the_last_bars_have_no_forward_return():
    study = score_forward_returns(series(50), [50] * 50, horizons=(6,))
    assert study["fwd6"].tail(6).isna().all()


def test_the_split_is_chronological_never_shuffled():
    """Shuffling a time series puts a bar's own neighbours in both halves, so
    every rule looks predictive. The split has to be by time."""
    study = score_forward_returns(series(100), list(range(100)), horizons=(6,))
    train, test = split_sample(study, 0.70)

    assert len(train) == 70 and len(test) == 30
    assert train.index.max() <= test.index.min()
    assert list(train["score"]) == sorted(train["score"]), "order preserved"


def test_an_absurd_split_is_refused():
    study = score_forward_returns(series(100), [50] * 100, horizons=(6,))
    with pytest.raises(ValueError):
        split_sample(study, 0.99)


# ========================================================= the costs gate ====

def _constant_edge_study(edge_pct: float, n: int = 400) -> pd.DataFrame:
    """A synthetic study where the high band reliably earns ``edge_pct``."""
    scores = ([95] * (n // 2)) + ([5] * (n - n // 2))
    forward = ([edge_pct] * (n // 2)) + ([-edge_pct] * (n - n // 2))
    return pd.DataFrame({
        "close": [22_000.0] * n, "score": scores, "fwd12": forward,
    }, index=pd.date_range("2026-09-01 09:15", periods=n, freq="5min"))


def test_an_edge_below_every_cost_tier_is_reported_as_losing():
    """Corrected from my own wrong assertion: I first wrote this expecting
    0.10% to lose on everything, forgetting futures cost only 0.03%. An edge
    that loses to EVERY instrument has to be below the cheapest of them."""
    study = _constant_edge_study(0.02)
    result = evaluate_rule(study, horizon=12, buy_below=9, sell_above=90,
                           fade=False)

    assert result.mean_pct == pytest.approx(0.02)
    assert "smaller than costs" in result.verdict
    assert not any(v["profitable"] for v in result.net_by_instrument.values())


def test_the_measured_banknifty_edge_clears_futures_but_not_options():
    """THE finding, at the number actually measured out-of-sample:
    +0.111% per 24-bar hold on BANKNIFTY. It is real (t=+3.87) and it is
    *tradeable* -- but only on the cheapest instrument. On a one-lot option it
    loses 0.39% a go, which is what I lost money doing."""
    study = _constant_edge_study(0.1111)
    result = evaluate_rule(study, horizon=12, buy_below=9, sell_above=90,
                           fade=False)
    nets = result.net_by_instrument

    assert nets["option_1lot"]["profitable"] is False
    assert nets["option_1lot"]["net_pct"] == pytest.approx(-0.3889, abs=1e-3)
    assert nets["equity"]["profitable"] is False
    assert nets["futures"]["profitable"] is True
    assert "futures" in result.verdict


def test_a_perfectly_consistent_edge_is_not_called_noise():
    """Zero dispersion with a positive mean is maximal significance. An earlier
    guard divided by a zero standard error and returned t=0, reporting a
    flawless signal as indistinguishable from zero."""
    import math
    result = evaluate_rule(_constant_edge_study(0.60), horizon=12,
                           buy_below=9, sell_above=90)
    assert math.isinf(result.t_stat) and result.t_stat > 0
    assert any("zero dispersion" in n for n in result.notes)


def test_a_big_enough_edge_clears_costs_and_says_where():
    study = _constant_edge_study(0.60)
    result = evaluate_rule(study, horizon=12, buy_below=9, sell_above=90,
                           fade=False)
    assert "tradeable on" in result.verdict
    assert result.net_by_instrument["equity"]["profitable"] is True
    assert result.net_by_instrument["futures"]["profitable"] is True


def test_options_are_the_hardest_instrument_to_clear():
    """A half-spread of Rs 1 on a Rs 160 premium is 0.6%, and at delta 0.5 the
    index must move twice that far to stand still."""
    assert COST_INDEX_PCT["option_1lot"] > COST_INDEX_PCT["equity"]
    assert COST_INDEX_PCT["equity"] > COST_INDEX_PCT["futures"]


def test_an_edge_between_the_cost_tiers_is_tradeable_only_on_the_cheap_one():
    study = _constant_edge_study(0.10)
    result = evaluate_rule(study, horizon=12, buy_below=9, sell_above=90)
    nets = result.net_by_instrument
    assert nets["futures"]["profitable"] is True    # 0.10 - 0.03
    assert nets["equity"]["profitable"] is False    # 0.10 - 0.19
    assert nets["option_1lot"]["profitable"] is False


# =================================================== statistical honesty =====

def test_noise_is_called_noise():
    rng = np.random.default_rng(5)
    n = 400
    study = pd.DataFrame({
        "close": [22_000.0] * n,
        "score": rng.integers(0, 101, size=n),
        "fwd12": rng.normal(0, 0.25, size=n),
    }, index=pd.date_range("2026-09-01 09:15", periods=n, freq="5min"))

    result = evaluate_rule(study, horizon=12, buy_below=9, sell_above=90)
    assert result.verdict == "no measurable edge"
    assert abs(result.t_stat) < 2


def test_a_small_sample_is_refused_not_reported():
    study = _constant_edge_study(0.5, n=20)
    result = evaluate_rule(study, horizon=12, buy_below=9, sell_above=90)
    assert result.verdict == "insufficient sample"
    assert result.observations < MIN_SAMPLE
    assert result.win_rate == 0.0, "no win rate is published below the minimum"


def test_the_win_rate_carries_a_confidence_interval():
    study = _constant_edge_study(0.10, n=100)
    result = evaluate_rule(study, horizon=12, buy_below=9, sell_above=90)
    assert result.win_rate_low <= result.win_rate <= result.win_rate_high
    assert result.win_rate_high - result.win_rate_low > 0


def test_fading_is_the_mirror_of_following():
    study = _constant_edge_study(0.60)
    follow = evaluate_rule(study, horizon=12, buy_below=9, sell_above=90,
                           fade=False)
    fade = evaluate_rule(study, horizon=12, buy_below=9, sell_above=90,
                         fade=True)
    assert fade.mean_pct == pytest.approx(-follow.mean_pct)


def test_a_missing_horizon_is_reported_not_crashed():
    study = _constant_edge_study(0.10)
    result = evaluate_rule(study, horizon=99, buy_below=9, sell_above=90)
    assert result.verdict == "no data"


# ================================================== why momentum underperforms ==

def test_a_mean_reverting_series_is_identified():
    """The diagnosis behind the inverted BANKNIFTY score: measured lag-1
    autocorrelation of -0.089 means strength is followed by weakness, so every
    trend-following component is fighting the series."""
    rng = np.random.default_rng(3)
    n = 800
    shocks = rng.normal(0, 1, size=n)
    # Alternating shocks produce strong negative autocorrelation.
    reverting = np.cumsum(shocks - 0.8 * np.roll(shocks, 1))
    frame = pd.DataFrame(
        {"Close": 22_000 + reverting},
        index=pd.date_range("2026-09-01 09:15", periods=n, freq="5min"),
    )
    note = autocorrelation_note(frame)
    assert note["autocorrelation"] < -0.03
    assert note["character"] == "mean-reverting"
    assert "contrarian" in note["meaning"]


def test_a_trending_series_is_identified():
    rng = np.random.default_rng(4)
    n = 800
    steps = rng.normal(0, 1, size=n)
    trending = np.cumsum(steps + 0.9 * np.roll(steps, 1))
    frame = pd.DataFrame(
        {"Close": 22_000 + trending},
        index=pd.date_range("2026-09-01 09:15", periods=n, freq="5min"),
    )
    assert autocorrelation_note(frame)["character"] == "trending"


def test_a_random_walk_is_called_neither():
    note = autocorrelation_note(series(900, seed=21))
    assert note["character"] == "neither"
    assert "random walk" in note["meaning"]
