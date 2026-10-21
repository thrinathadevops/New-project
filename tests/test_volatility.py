"""Realised volatility, and the comparison the chain cannot make alone.

The estimators are checked against a simulated series whose true volatility is
known, because an estimator that returns a plausible number on real data is
indistinguishable from one that is wrong by a constant factor.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.volatility import (
    BARS_PER_YEAR,
    MIN_BARS,
    RealizedVol,
    implied_move_from_straddle,
    realized_volatility,
    variance_risk_premium,
    volatility_cone,
)


def ohlc(
    bars: int = 400, annual_vol: float = 0.15, bar_size: str = "5m",
    seed: int = 3, ticks_per_bar: int = 40, sessions: int = 1,
) -> pd.DataFrame:
    """Simulated OHLC with a KNOWN annualised volatility.

    A Brownian path is generated inside every bar and its open/high/low/close
    recorded, which is exactly the process Parkinson and Garman-Klass assume.
    That makes the true answer known, so an estimator can be checked rather
    than merely eyeballed.
    """
    rng = np.random.default_rng(seed)
    per_bar = annual_vol / math.sqrt(BARS_PER_YEAR[bar_size])
    step = per_bar / math.sqrt(ticks_per_bar)

    rows, price = [], 22_000.0
    per_session = max(1, bars // sessions)
    stamps = []
    for i in range(bars):
        path = price * np.exp(np.cumsum(rng.normal(0, step, ticks_per_bar)))
        rows.append({
            "Open": price, "High": float(path.max()),
            "Low": float(path.min()), "Close": float(path[-1]),
            "Volume": 1_000,
        })
        price = float(path[-1])
        day = i // per_session
        stamps.append(
            pd.Timestamp("2026-09-01 09:15") + pd.Timedelta(days=day)
            + pd.Timedelta(minutes=5 * (i % per_session))
        )
    return pd.DataFrame(rows, index=pd.DatetimeIndex(stamps))


# ================================================== estimators are correct ==

@pytest.mark.parametrize(
    "method", ["close_to_close", "parkinson", "garman_klass",
               "rogers_satchell"])
def test_each_estimator_recovers_the_volatility_it_was_given(method):
    """A 15% series must read as roughly 15%, not as a plausible-looking
    number that happens to be wrong by a constant."""
    frame = ohlc(bars=1_200, annual_vol=0.15, seed=5)
    result = realized_volatility(frame, method=method, bar_size="5m")

    assert result.usable
    assert result.annualized_pct == pytest.approx(15.0, rel=0.20), (
        f"{method} returned {result.annualized_pct}% for a 15% series"
    )


def test_the_range_estimators_are_more_efficient_than_close_to_close():
    """The documented reason to prefer them: Parkinson ~5x and Garman-Klass
    ~7.4x the efficiency of close-only. Measured here as the spread of the
    estimate across independent samples -- less scatter is the whole point."""
    def scatter(method: str) -> float:
        values = [
            realized_volatility(
                ohlc(bars=120, annual_vol=0.15, seed=s), method=method,
            ).annualized_pct
            for s in range(30)
        ]
        return float(np.std(values))

    close_only = scatter("close_to_close")
    parkinson = scatter("parkinson")
    garman = scatter("garman_klass")

    assert parkinson < close_only, "Parkinson should be steadier"
    assert garman < close_only, "Garman-Klass should be steadier"


def test_annualisation_uses_trading_time_not_calendar_time():
    """options_math prices with days/365. Realised volatility accrues only
    while the market is open, and 252 trading days span one calendar year, so
    the bar count must be built on 252. Using 365 would overstate realised
    volatility by ~20% and make every option look cheap."""
    assert BARS_PER_YEAR["5m"] == 75 * 252       # 375-minute session / 5
    assert BARS_PER_YEAR["1d"] == 252
    assert BARS_PER_YEAR["15m"] == 25 * 252

    ratio = math.sqrt(365 / 252)
    assert ratio == pytest.approx(1.203, abs=0.01), (
        "the error this guards is a ~20% overstatement"
    )


# ========================================================= honest failures ==

def test_too_few_bars_is_refused_rather_than_estimated():
    result = realized_volatility(ohlc(bars=MIN_BARS - 1), method="parkinson")

    assert not result.usable
    assert result.annualized_pct is None
    assert any("measurement rather than a guess" in n for n in result.notes)


def test_a_negative_variance_is_explained_not_square_rooted():
    """Garman-Klass and Rogers-Satchell SUBTRACT terms, so a degenerate sample
    can produce a negative variance. Returning nan -- or worse, zero -- would
    read as 'no volatility' instead of 'bad sample'."""
    # Bars whose close sits far from the open but with no range at all: an
    # impossible shape that drives the subtracted term negative.
    frame = pd.DataFrame({
        "Open": [100.0] * 40, "High": [100.0] * 40,
        "Low": [100.0] * 40, "Close": [120.0] * 40,
    }, index=pd.date_range("2026-09-01 09:15", periods=40, freq="5min"))
    result = realized_volatility(frame, method="garman_klass")

    assert result.annualized_pct is None
    assert any("negative variance" in n for n in result.notes)
    assert any("not a volatility of zero" in n for n in result.notes)


def test_parkinson_cannot_go_negative_and_is_offered_as_the_fallback():
    frame = pd.DataFrame({
        "Open": [100.0] * 40, "High": [100.0] * 40,
        "Low": [100.0] * 40, "Close": [120.0] * 40,
    }, index=pd.date_range("2026-09-01 09:15", periods=40, freq="5min"))
    result = realized_volatility(frame, method="parkinson")

    assert result.annualized_pct == 0.0, "no range means no measured movement"


def test_a_non_positive_price_is_dropped_and_counted():
    frame = ohlc(bars=60)
    frame.iloc[5, frame.columns.get_loc("Low")] = -1.0
    result = realized_volatility(frame, method="parkinson")

    assert any("non-positive price" in n for n in result.notes)
    assert any("bad data, not low volatility" in n for n in result.notes)


def test_missing_columns_are_named():
    result = realized_volatility(
        pd.DataFrame({"Close": [1.0] * 40}), method="parkinson")
    assert result.annualized_pct is None
    assert any("Open/High/Low/Close" in n for n in result.notes)


def test_an_unknown_method_is_refused_loudly():
    with pytest.raises(ValueError, match="unknown method"):
        realized_volatility(ohlc(60), method="my_own_estimator")


def test_an_unknown_bar_size_is_refused_rather_than_defaulted():
    """Silently defaulting the bar count would scale the answer by whatever
    factor the default happened to be."""
    with pytest.raises(ValueError, match="unknown bar_size"):
        realized_volatility(ohlc(60), bar_size="7m")


# ============================================= the overnight gap, excluded ==

def test_an_overnight_gap_is_not_counted_as_a_five_minute_move():
    """The single biggest way to get a nonsense intraday number: annualise a
    whole night of news as if it were one 5-minute bar."""
    frame = ohlc(bars=160, annual_vol=0.15, sessions=4, seed=9)
    # Insert a violent gap at each session boundary.
    closes = frame["Close"].to_numpy()
    dates = pd.DatetimeIndex(frame.index).normalize()
    boundaries = np.where(dates[1:] != dates[:-1])[0] + 1
    for b in boundaries:
        for col in ("Open", "High", "Low", "Close"):
            frame.iloc[b, frame.columns.get_loc(col)] = closes[b] * 1.05

    aware = realized_volatility(frame, method="close_to_close",
                                session_aware=True)
    naive = realized_volatility(frame, method="close_to_close",
                                session_aware=False)

    assert aware.annualized_pct < naive.annualized_pct
    assert any("overnight return" in n for n in aware.notes)


def test_yang_zhang_says_the_overnight_term_is_the_point_of_it():
    frame = ohlc(bars=300, sessions=10, seed=4)
    result = realized_volatility(frame, method="yang_zhang", bar_size="1d",
                                 session_aware=True)
    assert any("overnight term by construction" in n for n in result.notes)


def test_high_frequency_bars_declare_their_noise_bias():
    """Bid-ask bounce inflates realised volatility at 5 minutes, which biases
    any variance premium DOWNWARDS -- conservative, and worth saying."""
    result = realized_volatility(ohlc(400), method="garman_klass",
                                 bar_size="5m")
    assert any("conservative, not flattered" in n for n in result.notes)


# ======================================================================= cone ==

def test_the_cone_detects_an_expansion():
    calm = ohlc(bars=300, annual_vol=0.10, seed=1)
    wild = ohlc(bars=60, annual_vol=0.40, seed=2)
    wild.index = pd.date_range(calm.index[-1] + pd.Timedelta(minutes=5),
                               periods=60, freq="5min")
    frame = pd.concat([calm, wild])

    cone = volatility_cone(frame, windows=(20, 60, 240))
    assert cone["state"] == "expanding"
    assert "widens spreads" in cone["note"]


def test_the_cone_detects_a_contraction_and_warns_about_breakouts():
    wild = ohlc(bars=300, annual_vol=0.40, seed=1)
    calm = ohlc(bars=60, annual_vol=0.08, seed=2)
    calm.index = pd.date_range(wild.index[-1] + pd.Timedelta(minutes=5),
                               periods=60, freq="5min")
    frame = pd.concat([wild, calm])

    cone = volatility_cone(frame, windows=(20, 60, 240))
    assert cone["state"] == "contracting"
    assert "false signals" in cone["note"]


def test_a_short_frame_cannot_produce_a_term_structure():
    cone = volatility_cone(ohlc(30), windows=(20, 60, 240))
    assert "no term structure" in cone["note"]


# ================================================== the variance premium ==

def test_a_positive_premium_names_the_seller_as_the_paid_side():
    """The posture this project has been on the wrong side of all along:
    'Puts were the dear side on both and I was buying them.'"""
    premium = variance_risk_premium(17.1, 12.0, days_to_expiry=6)

    assert premium.premium_points == pytest.approx(5.1)
    assert premium.side_being_paid == "seller"
    assert "of premium to the seller" in premium.reasons[0]


def test_a_negative_premium_names_the_buyer():
    premium = variance_risk_premium(12.0, 17.0, days_to_expiry=6)
    assert premium.side_being_paid == "buyer"
    assert "buyer's favour" in premium.reasons[0]


def test_without_a_measured_cost_it_refuses_to_call_the_premium_an_edge():
    """The step where every option strategy in this project went wrong: a few
    volatility points are routinely smaller than the option's own spread."""
    premium = variance_risk_premium(17.1, 12.0, days_to_expiry=6)

    assert premium.verdict == "premium measured, not assessed"
    assert any("not a recommendation" in r for r in premium.reasons)


def test_a_premium_smaller_than_the_measured_spread_is_rejected():
    premium = variance_risk_premium(
        16.0, 15.0, days_to_expiry=6, round_trip_cost_pct=2.5)

    assert premium.verdict == "premium smaller than costs"
    assert "too small to collect" in premium.reasons[-1]
    assert "+0.111%" in premium.reasons[-1], "cite the precedent"


def test_a_premium_that_clears_costs_is_a_candidate_not_a_trade():
    premium = variance_risk_premium(
        40.0, 12.0, days_to_expiry=6, round_trip_cost_pct=0.05)

    assert premium.verdict == "premium exceeds measured costs"
    assert "candidate to test, not a trade to place" in premium.reasons[-2]
    # The tail warning is not optional on a short-premium position.
    assert "defined-risk structure" in premium.reasons[-1]
    assert "size for the tail" in premium.reasons[-1]


def test_annualised_points_are_converted_before_being_compared_to_a_cost():
    """An annualised volatility gap and a per-trade cost are different
    quantities; comparing them directly is meaningless."""
    premium = variance_risk_premium(17.1, 12.0, round_trip_cost_pct=1.0)

    assert premium.verdict == "premium measured, not assessed"
    assert any("not comparable" in r for r in premium.reasons)


def test_an_unusable_realised_estimate_blocks_the_comparison():
    unusable = realized_volatility(ohlc(bars=5), method="parkinson")
    premium = variance_risk_premium(15.0, unusable)

    assert premium.verdict == "no realised estimate"


def test_a_realised_vol_object_is_accepted_directly():
    estimate = realized_volatility(ohlc(400, annual_vol=0.12),
                                   method="garman_klass")
    premium = variance_risk_premium(18.0, estimate, days_to_expiry=6)

    assert premium.realized_pct == estimate.annualized_pct
    assert premium.side_being_paid == "seller"
    assert premium.evidence["realized"]["method"] == "garman_klass"


def test_a_missing_implied_vol_is_treated_as_information():
    """options_math returns None for an insane IV rather than a confident wrong
    number, and that absence says something about the contract's liquidity."""
    premium = variance_risk_premium(None, 12.0)
    assert premium.verdict == "incomplete"
    assert any("liquidity" in r for r in premium.reasons)


def test_zero_realised_volatility_is_a_data_problem_not_a_calm_market():
    premium = variance_risk_premium(15.0, 0.0)
    assert premium.verdict == "incomplete"
    assert any("data problem" in r for r in premium.reasons)


# ==================================================== the straddle benchmark ==

def test_the_straddle_prices_the_move_the_default_target_assumes():
    """The same correction as taking the target from the option wall instead of
    the default +25%, arrived at from the pricing side."""
    read = implied_move_from_straddle(straddle_premium=320.0, spot=22_700,
                                      days_to_expiry=6)

    assert read["usable"]
    assert read["implied_move_pct"] == pytest.approx(320 / 22_700 * 100, abs=1e-4)
    assert read["approx_implied_vol_pct"] > 0
    assert "leaving a default at +25%" in read["note"]
    assert "0.7979" in read["caveat"], "the approximation is declared"


def test_the_straddle_read_recovers_a_sane_implied_vol():
    """A 6-day NIFTY straddle around 1.4% of spot should imply a mid-teens
    volatility, which is where India VIX has actually been."""
    read = implied_move_from_straddle(320.0, 22_700, 6)
    assert 10 < read["approx_implied_vol_pct"] < 25


def test_nonsense_straddle_inputs_are_refused():
    for args in ((0.0, 22_700, 6), (320.0, 0.0, 6), (320.0, 22_700, 0)):
        assert implied_move_from_straddle(*args)["usable"] is False
