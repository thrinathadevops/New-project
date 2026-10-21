"""Forecasting volatility -- the one prediction problem here that is tractable.

Why volatility and not direction
--------------------------------
Everything this project has tried to predict so far has been **direction**, and
the measurements said it cannot be done at these horizons: lag-1 autocorrelation
of returns is about -0.09 intraday, 41 of 50 Nifty names are indistinguishable
from random walks on daily bars, and the one apparently significant edge
evaporated when the overlapping-window statistic was corrected (i.i.d. t +3.90
-> HAC t +1.83).

Volatility is a different object. Returns are close to unforecastable *because*
markets are competitive -- a predictable return is an arbitrage. A predictable
variance is not: knowing tomorrow will be turbulent does not tell you which way,
so there is nothing to arbitrage away, and the persistence survives. That
persistence is one of the most replicated facts in financial econometrics.

HAR-RV
------
Corsi (2009). Regress next period's realised variance on three averages of the
recent past -- daily, weekly, monthly:

    RV[t+1] = b0 + b_d*RV_d[t] + b_w*RV_w[t] + b_m*RV_m[t] + e

The three horizons stand in for three groups of participants acting on three
time scales, and the cascade between them is what produces the long memory. It
is a linear regression on three columns, it has no hyperparameters to tune, and
it has repeatedly matched or beaten GARCH-family models that are far more
elaborate.

**The benchmark that decides whether it earns its place** is not zero -- it is
the random walk, ``RV[t+1] = RV[t]``. A volatility model that cannot beat
"tomorrow looks like today" is worth nothing, and many published ones do not.
:func:`compare_to_naive` applies that test on a holdout, and the comparison uses
QLIKE as well as RMSE because squared error on a variance is dominated by its
largest observations.

What a forecast is and is not worth here
-----------------------------------------
A volatility forecast does not produce a trade by itself. It becomes useful in
two places:

* **against implied** -- ``volatility.variance_risk_premium`` compares realised
  to implied; a *forecast* of realised is the honest input to that comparison,
  since what matters is the volatility that will occur over the holding period,
  not the one that already has;
* **in sizing** -- a stop placed at a fixed percentage is tighter in a
  turbulent week than a calm one without anybody deciding that.

Neither makes money on its own, and this module claims nothing about direction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: Lookback windows, in bars, for the three HAR components.
DAILY_WINDOW = 1
WEEKLY_WINDOW = 5
MONTHLY_WINDOW = 22

#: Fewer rows than this and the regression is fitting noise.
MIN_FIT_ROWS = 100


@dataclass
class HarModel:
    """Fitted HAR-RV coefficients and what they cost to obtain."""

    intercept: float = 0.0
    beta_daily: float = 0.0
    beta_weekly: float = 0.0
    beta_monthly: float = 0.0
    rows: int = 0
    r_squared: float = 0.0
    fitted: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

    def predict_one(self, daily: float, weekly: float, monthly: float) -> float:
        """Next-period realised VARIANCE. Clipped at zero: a variance cannot
        be negative, and an unconstrained linear model will happily produce one
        on a quiet stretch."""
        value = (self.intercept + self.beta_daily * daily
                 + self.beta_weekly * weekly + self.beta_monthly * monthly)
        return max(0.0, float(value))


def har_features(variance: "pd.Series") -> pd.DataFrame:
    """The three HAR regressors, aligned so none of them sees the future.

    Each row's features use information available **up to and including** that
    row, and the target is the NEXT row. The shift is the whole correctness
    question here: get it wrong by one and the model predicts the present.
    """
    series = pd.Series(variance).astype(float).dropna()
    frame = pd.DataFrame({"rv": series})
    frame["rv_d"] = series.rolling(DAILY_WINDOW).mean()
    frame["rv_w"] = series.rolling(WEEKLY_WINDOW).mean()
    frame["rv_m"] = series.rolling(MONTHLY_WINDOW).mean()
    # The target is strictly the next observation.
    frame["target"] = series.shift(-1)
    return frame.dropna()


def fit_har(variance: "pd.Series") -> HarModel:
    """Ordinary least squares on the three HAR columns.

    Plain ``lstsq`` rather than a regression library: three regressors and an
    intercept need no machinery, and a dependency here would be a dependency in
    the hot path of a desk application.
    """
    frame = har_features(variance)
    model = HarModel(rows=len(frame))
    if len(frame) < MIN_FIT_ROWS:
        model.notes.append(
            f"{len(frame)} usable rows is below the {MIN_FIT_ROWS} needed; "
            f"a four-parameter fit on fewer is fitting noise."
        )
        return model

    design = np.column_stack([
        np.ones(len(frame)), frame["rv_d"], frame["rv_w"], frame["rv_m"],
    ])
    target = frame["target"].to_numpy(float)
    try:
        coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
    except np.linalg.LinAlgError as exc:
        model.notes.append(f"Regression failed: {exc}")
        return model

    model.intercept = float(coefficients[0])
    model.beta_daily = float(coefficients[1])
    model.beta_weekly = float(coefficients[2])
    model.beta_monthly = float(coefficients[3])
    model.fitted = True

    predicted = design @ coefficients
    residual = float(np.sum((target - predicted) ** 2))
    total = float(np.sum((target - target.mean()) ** 2))
    model.r_squared = round(1.0 - residual / total, 4) if total > 0 else 0.0
    model.notes.append(
        f"Fitted on {len(frame)} rows, R-squared {model.r_squared:.3f}. "
        f"R-squared on a variance is flattered by its own persistence -- the "
        f"test that matters is compare_to_naive()."
    )
    return model


def forecast_next(variance: "pd.Series", model: HarModel) -> dict:
    """One-step-ahead forecast from the most recent available history."""
    series = pd.Series(variance).astype(float).dropna()
    if not model.fitted:
        return {"ok": False, "reason": "Model is not fitted."}
    if len(series) < MONTHLY_WINDOW:
        return {"ok": False,
                "reason": f"Need {MONTHLY_WINDOW} observations, have {len(series)}."}

    daily = float(series.iloc[-DAILY_WINDOW:].mean())
    weekly = float(series.iloc[-WEEKLY_WINDOW:].mean())
    monthly = float(series.iloc[-MONTHLY_WINDOW:].mean())
    variance_hat = model.predict_one(daily, weekly, monthly)
    naive = daily
    return {
        "ok": True,
        "variance": round(variance_hat, 10),
        "annualised_pct": round(math.sqrt(max(0.0, variance_hat) * 252) * 100, 2),
        "naive_annualised_pct": round(math.sqrt(max(0.0, naive) * 252) * 100, 2),
        "inputs": {"daily": daily, "weekly": weekly, "monthly": monthly},
    }


def _qlike(actual: np.ndarray, predicted: np.ndarray) -> float:
    """QLIKE loss, the standard robust criterion for variance forecasts.

    Squared error on a variance is dominated by the handful of most volatile
    observations, so a model can look good by predicting crises and badly by
    being wrong about calm. QLIKE penalises proportional error instead, which
    is what a position size actually cares about.
    """
    safe = np.clip(predicted, 1e-12, None)
    actual = np.clip(actual, 0.0, None)
    return float(np.mean(actual / safe - np.log(actual / safe + 1e-12) - 1.0))


def compare_to_naive(
    variance: "pd.Series", *, train_fraction: float = 0.70,
) -> dict:
    """Does HAR beat "tomorrow looks like today" on data it never saw?

    The random walk is the benchmark a volatility model has to clear, and
    plenty of published models do not. The split is chronological.
    """
    frame = har_features(variance)
    if len(frame) < MIN_FIT_ROWS * 2:
        return {"ok": False,
                "reason": f"{len(frame)} rows is too few to split and test."}

    cut = int(len(frame) * train_fraction)
    train, test = frame.iloc[:cut], frame.iloc[cut:]

    model = fit_har(pd.Series(
        list(train["rv"]) + [train["target"].iloc[-1]],
        index=list(train.index) + [test.index[0]],
    ))
    if not model.fitted:
        return {"ok": False, "reason": "; ".join(model.notes) or "fit failed"}

    predicted = np.array([
        model.predict_one(row.rv_d, row.rv_w, row.rv_m)
        for row in test.itertuples()
    ])
    naive = test["rv_d"].to_numpy(float)       # tomorrow = today
    actual = test["target"].to_numpy(float)

    har_rmse = float(np.sqrt(np.mean((actual - predicted) ** 2)))
    naive_rmse = float(np.sqrt(np.mean((actual - naive) ** 2)))
    har_qlike = _qlike(actual, predicted)
    naive_qlike = _qlike(actual, naive)

    beats_rmse = har_rmse < naive_rmse
    beats_qlike = har_qlike < naive_qlike
    return {
        "ok": True,
        "test_rows": len(test),
        "har_rmse": har_rmse, "naive_rmse": naive_rmse,
        "rmse_improvement_pct": round((1 - har_rmse / naive_rmse) * 100, 2)
        if naive_rmse else 0.0,
        "har_qlike": round(har_qlike, 6), "naive_qlike": round(naive_qlike, 6),
        "qlike_improvement_pct": round((1 - har_qlike / naive_qlike) * 100, 2)
        if naive_qlike else 0.0,
        "beats_naive": beats_rmse and beats_qlike,
        "model": model.as_dict(),
        "verdict": (
            "beats the random walk on both criteria" if beats_rmse and beats_qlike
            else "beats it on one criterion only -- not convincing"
            if beats_rmse or beats_qlike
            else "does not beat the random walk; it is not worth running"
        ),
    }


def realised_variance_series(
    frame: pd.DataFrame, *, window: int = 1, proxy: str = "garman_klass",
) -> "pd.Series":
    """Per-bar realised variance from OHLC, for feeding the model.

    ``proxy`` is ``"garman_klass"`` (default) or ``"squared_return"``.

    **Why not the squared close-to-close return**, which is where this started:
    it is unbiased but catastrophically noisy as a per-day variance estimate --
    roughly 200% relative standard error, because one observation is being asked
    to estimate a second moment. Measured on 5 years of NIFTY dailies, 9.5% of
    days came out below 1% of the mean and the minimum was **exactly zero**.

    That noise is not a cosmetic problem, it silently fakes a result. QLIKE
    divides actual by predicted, so a naive forecast of "tomorrow equals today"
    built on near-zero days explodes: **86% of the benchmark's total loss came
    from 5 days out of 364**, which this module first reported as HAR beating
    the random walk by 99%. It was not beating anything -- the benchmark was
    being punished for its own noise.

    Garman-Klass uses the whole bar (open, high, low, close) and is about seven
    times more efficient, so it never returns zero on a day that actually moved.
    It carries a small downward bias from ignoring overnight gaps and discrete
    sampling; a bias the regression can absorb into its coefficients is a far
    better problem than variance the regression cannot.
    """
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    close = frame["Close"].astype(float)

    if proxy == "squared_return":
        squared = np.log(close / close.shift(1)) ** 2
    elif proxy == "garman_klass":
        open_ = frame["Open"].astype(float)
        hl = np.log(high / low) ** 2
        co = np.log(close / open_) ** 2
        squared = 0.5 * hl - (2.0 * math.log(2.0) - 1.0) * co
        # Garman-Klass subtracts a term, so a handful of degenerate bars can
        # go negative. Those are bad bars, not negative variance.
        squared = squared.where(squared > 0)
    else:
        raise ValueError(
            f"proxy must be 'garman_klass' or 'squared_return', not {proxy!r}"
        )

    if window > 1:
        squared = squared.rolling(window).mean()
    return squared.dropna()
