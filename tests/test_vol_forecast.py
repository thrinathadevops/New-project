"""HAR-RV: the one prediction problem in this project that works.

Returns are unforecastable here because a predictable return is an arbitrage.
A predictable variance is not -- knowing tomorrow is turbulent says nothing
about direction -- so the persistence survives and can be modelled.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.vol_forecast import (
    MIN_FIT_ROWS,
    MONTHLY_WINDOW,
    compare_to_naive,
    fit_har,
    forecast_next,
    har_features,
    realised_variance_series,
)


def ohlc(n: int = 800, seed: int = 5, clustered: bool = True,
         steps_per_bar: int = 40) -> pd.DataFrame:
    """Daily bars built from a simulated INTRABAR path.

    The first version drew High and Low as independent noise around the close,
    which quietly broke every comparison between variance proxies: Garman-Klass
    earns its efficiency from the *real* relationship between a bar's range and
    its return, and a fixture with no intrabar path cannot express that. It
    reported Garman-Klass as the noisier estimator, which is the reverse of the
    truth on real data.

    So each bar walks ``steps_per_bar`` sub-steps and the high and low are the
    extremes that walk actually reached. Volatility is persistent across bars,
    which is the property HAR exists to model.
    """
    rng = np.random.default_rng(seed)
    vol = np.empty(n)
    vol[0] = 0.01
    for i in range(1, n):
        vol[i] = (0.85 * vol[i - 1] + 0.15 * 0.01
                  + (0.002 * rng.standard_normal() if clustered else 0.0))
        vol[i] = max(0.002, vol[i])

    opens, highs, lows, closes = [], [], [], []
    level = 100.0
    for i in range(n):
        # Per-step volatility, so the bar's total variance is vol[i]^2.
        step = vol[i] / math.sqrt(steps_per_bar)
        path = level * np.exp(np.cumsum(rng.standard_normal(steps_per_bar) * step))
        opens.append(level)
        highs.append(float(path.max()))
        lows.append(float(path.min()))
        closes.append(float(path[-1]))
        level = closes[-1]

    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows, "Close": closes,
        "Volume": rng.integers(1e5, 9e5, size=n),
    }, index=pd.date_range("2022-01-03", periods=n, freq="B"))


# ========================================================== the RV proxy =====

def test_garman_klass_never_returns_zero_on_a_bar_that_moved():
    """The whole reason the proxy changed. A squared close-to-close return is
    zero whenever the close is unchanged, and QLIKE divides by the forecast."""
    frame = ohlc(300)
    gk = realised_variance_series(frame, proxy="garman_klass")
    assert (gk > 0).all()


def test_the_squared_return_proxy_produces_near_zero_days():
    """The defect that faked a 99% QLIKE improvement, stated as the property
    that actually causes it.

    Corrected from my own first attempt, which compared coefficients of
    variation. That measures Garman-Klass's *efficiency*, which comes from
    using the real intrabar range -- and this fixture draws High/Low as
    independent noise, so it cannot express that property and the comparison
    measured nothing. Near-zero frequency is the mechanism behind the fake
    result and it is fixture-independent.

    On real NIFTY dailies: 9.5% of days below 1% of the mean, minimum exactly
    zero, and 86% of the benchmark's QLIKE loss came from 5 days of 364.
    """
    frame = ohlc(600)
    gk = realised_variance_series(frame, proxy="garman_klass")
    sq = realised_variance_series(frame, proxy="squared_return")

    tiny_sq = (sq < sq.mean() * 0.01).mean()
    tiny_gk = (gk < gk.mean() * 0.01).mean()

    # Measured with an intrabar path: 12.7% vs 0.2% here, 9.5% vs 0.0% on real
    # NIFTY dailies. Not exactly zero -- a bar whose path genuinely barely
    # moved has a genuinely tiny variance, and that is information, not noise.
    assert tiny_sq > tiny_gk * 10

    # Now that the fixture has a real range, the efficiency shows up too.
    assert gk.std() / gk.mean() < sq.std() / sq.mean()


def test_an_unknown_proxy_is_refused():
    with pytest.raises(ValueError):
        realised_variance_series(ohlc(100), proxy="vibes")


# ========================================================== no lookahead =====

def test_the_target_is_strictly_the_next_observation():
    series = pd.Series(np.arange(1.0, 101.0))
    frame = har_features(series)
    row = frame.iloc[0]
    assert row["target"] == pytest.approx(row["rv"] + 1)


def test_the_features_never_use_the_target_row():
    """Off-by-one here and the model predicts the present, which would look
    spectacular and be worthless."""
    series = pd.Series(np.arange(1.0, 201.0))
    frame = har_features(series)
    for row in frame.head(20).itertuples():
        assert row.rv_d <= row.rv < row.target
        assert row.rv_m <= row.rv_d, "monthly average lags the daily value"


# ================================================================ fitting ====

def test_a_short_series_is_refused_not_fitted():
    model = fit_har(pd.Series(np.random.default_rng(1).random(40)))
    assert model.fitted is False
    assert any("fitting noise" in n for n in model.notes)
    assert model.rows < MIN_FIT_ROWS


def test_a_clustered_series_fits_with_positive_persistence():
    rv = realised_variance_series(ohlc(700))
    model = fit_har(rv)
    assert model.fitted is True
    # Persistence has to come from somewhere; the three betas cannot all be ~0.
    assert abs(model.beta_daily) + abs(model.beta_weekly) + abs(model.beta_monthly) > 0.1


def test_a_variance_forecast_is_never_negative():
    """An unconstrained linear model will happily predict a negative variance
    on a quiet stretch. Clipping is not cosmetic."""
    model = fit_har(realised_variance_series(ohlc(700)))
    assert model.predict_one(-5.0, -5.0, -5.0) >= 0.0


# =============================================================== forecasting ==

def test_a_forecast_needs_the_monthly_window():
    model = fit_har(realised_variance_series(ohlc(700)))
    short = pd.Series([0.0001] * (MONTHLY_WINDOW - 2))
    assert forecast_next(short, model)["ok"] is False


def test_a_forecast_reports_both_itself_and_the_naive_alternative():
    rv = realised_variance_series(ohlc(700))
    result = forecast_next(rv, fit_har(rv))
    assert result["ok"] is True
    assert result["annualised_pct"] > 0
    assert result["naive_annualised_pct"] > 0


def test_an_unfitted_model_refuses_to_forecast():
    from intraday_advisor.vol_forecast import HarModel
    assert forecast_next(pd.Series([0.001] * 50), HarModel())["ok"] is False


# ================================= the benchmark that decides it earns a place ==

def test_har_beats_the_random_walk_on_a_clustered_series():
    """The benchmark is not zero, it is 'tomorrow looks like today'. Measured
    on 5 years of real dailies with a Garman-Klass proxy: RMSE +13.7% to
    +22.9%, QLIKE +29% to +43%, out of sample, on four instruments."""
    rv = realised_variance_series(ohlc(900))
    result = compare_to_naive(rv)
    assert result["ok"] is True
    assert result["beats_naive"] is True
    assert result["rmse_improvement_pct"] > 0
    assert "beats the random walk" in result["verdict"]


def test_an_unclustered_series_gives_har_nothing_to_find():
    """Control: with no persistence there is nothing to model, and the verdict
    must say so rather than find structure anyway."""
    rng = np.random.default_rng(9)
    n = 900
    close = 100 * np.exp(np.cumsum(rng.standard_normal(n) * 0.01))
    spread = np.abs(rng.standard_normal(n)) * 0.01 * close
    frame = pd.DataFrame({
        "Open": close, "High": close + spread,
        "Low": np.maximum(0.01, close - spread), "Close": close,
    }, index=pd.date_range("2022-01-03", periods=n, freq="B"))

    result = compare_to_naive(realised_variance_series(frame))
    assert result["ok"] is True
    # It may still beat a one-day naive by smoothing, but the gain should be
    # far smaller than on a genuinely persistent series.
    assert result["rmse_improvement_pct"] < 40


def test_the_split_is_chronological():
    rv = realised_variance_series(ohlc(900))
    result = compare_to_naive(rv, train_fraction=0.70)
    frame = har_features(rv)
    assert result["test_rows"] == len(frame) - int(len(frame) * 0.70)


def test_too_little_data_to_split_is_refused():
    assert compare_to_naive(pd.Series(np.random.default_rng(2).random(80)))["ok"] is False
