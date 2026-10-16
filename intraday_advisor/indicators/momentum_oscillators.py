"""Momentum oscillators missing from the indicator suite.

Adds:
  - Stochastic Oscillator (%K, %D)
  - Stochastic RSI
  - Williams %R
  - CCI (Commodity Channel Index)
  - ADX / DMI (Average Directional Index with +DI / -DI)

All register via the indicator registry so they are computed automatically
inside ``add_indicators``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (equivalent to EMA with alpha = 1/period)."""
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# ---------------------------------------------------------------------------
# Stochastic Oscillator (14, 3, 3)
# ---------------------------------------------------------------------------

@registry.register("stochastic")
def add_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3, smooth_k: int = 3) -> pd.DataFrame:
    """Stochastic Oscillator — measures where price closed relative to the
    high-low range over the last *k_period* bars.

    %K > 80 = overbought, %K < 20 = oversold.
    Crossovers of %K and %D in those zones are the actionable signals.
    """
    enriched = df.copy()
    lowest_low = enriched["Low"].rolling(k_period, min_periods=k_period).min()
    highest_high = enriched["High"].rolling(k_period, min_periods=k_period).max()

    raw_k = 100 * (enriched["Close"] - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    enriched["Stoch_K"] = raw_k.rolling(smooth_k, min_periods=1).mean()
    enriched["Stoch_D"] = enriched["Stoch_K"].rolling(d_period, min_periods=1).mean()
    return enriched


# ---------------------------------------------------------------------------
# Stochastic RSI
# ---------------------------------------------------------------------------

@registry.register("stochastic_rsi")
def add_stochastic_rsi(df: pd.DataFrame, rsi_period: int = 14, stoch_period: int = 14,
                       k_smooth: int = 3, d_smooth: int = 3) -> pd.DataFrame:
    """Stochastic RSI — applies the Stochastic formula to the RSI itself,
    making it more sensitive to momentum shifts than either indicator alone.

    Values 0-100; >80 overbought, <20 oversold.
    """
    enriched = df.copy()
    rsi = enriched.get("RSI14")
    if rsi is None:
        delta = enriched["Close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / rsi_period, adjust=False, min_periods=rsi_period).mean()
        avg_loss = loss.ewm(alpha=1 / rsi_period, adjust=False, min_periods=rsi_period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

    rsi_low = rsi.rolling(stoch_period, min_periods=stoch_period).min()
    rsi_high = rsi.rolling(stoch_period, min_periods=stoch_period).max()
    stoch_rsi = (rsi - rsi_low) / (rsi_high - rsi_low).replace(0, np.nan) * 100
    enriched["StochRSI_K"] = stoch_rsi.rolling(k_smooth, min_periods=1).mean()
    enriched["StochRSI_D"] = enriched["StochRSI_K"].rolling(d_smooth, min_periods=1).mean()
    return enriched


# ---------------------------------------------------------------------------
# Williams %R
# ---------------------------------------------------------------------------

@registry.register("williams_r")
def add_williams_r(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Williams %R — momentum oscillator ranging from -100 to 0.

    Above -20 = overbought, below -80 = oversold.
    Popular among options traders because it leads RSI by 1-2 bars.
    """
    enriched = df.copy()
    highest_high = enriched["High"].rolling(period, min_periods=period).max()
    lowest_low = enriched["Low"].rolling(period, min_periods=period).min()
    enriched["Williams_R"] = -100 * (highest_high - enriched["Close"]) / (highest_high - lowest_low).replace(0, np.nan)
    return enriched


# ---------------------------------------------------------------------------
# CCI (Commodity Channel Index)
# ---------------------------------------------------------------------------

@registry.register("cci")
def add_cci(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """CCI — measures deviation of price from its statistical mean.

    Above +100 = overbought / strong up trend.
    Below -100 = oversold / strong down trend.
    Zero-line crosses are trend-change signals.
    """
    enriched = df.copy()
    typical = (enriched["High"] + enriched["Low"] + enriched["Close"]) / 3
    sma = typical.rolling(period, min_periods=period).mean()
    mean_deviation = typical.rolling(period, min_periods=period).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    enriched["CCI20"] = (typical - sma) / (0.015 * mean_deviation).replace(0, np.nan)
    return enriched


# ---------------------------------------------------------------------------
# ADX / DMI (Average Directional Index)
# ---------------------------------------------------------------------------

@registry.register("adx")
def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ADX with +DI and -DI — measures trend STRENGTH, not direction.

    ADX > 25 = trending market (trend strategies work).
    ADX < 20 = ranging market (mean-reversion or stand aside).
    +DI > -DI = bullish trend; -DI > +DI = bearish trend.
    """
    enriched = df.copy()

    high = enriched["High"]
    low = enriched["Low"]
    close = enriched["Close"]

    # True Range
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    # Directional movement
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=enriched.index, dtype="float64")
    minus_dm = pd.Series(minus_dm, index=enriched.index, dtype="float64")

    # Wilder smoothing
    atr = _wilder_smooth(tr, period)
    plus_di = 100 * _wilder_smooth(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100 * _wilder_smooth(minus_dm, period) / atr.replace(0, np.nan)

    # DX and ADX
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _wilder_smooth(dx, period)

    enriched["Plus_DI"] = plus_di
    enriched["Minus_DI"] = minus_di
    enriched["ADX"] = adx
    return enriched
