"""Hull Moving Average (HMA) and Chandelier Trailing Exit System.

Alan Hull's HMA provides zero-lag smoothing, eliminating lag while maintaining
a smooth curve.
Chuck LeBeau's Chandelier Exit ratchets trailing stops based on volatility,
protecting accumulated profits while letting winners run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


def _wma(series: pd.Series, period: int) -> pd.Series:
    """Weighted Moving Average with linear weights 1, 2, ..., period."""
    weights = np.arange(1, period + 1)
    return series.rolling(period, min_periods=period).apply(
        lambda s: np.dot(s, weights) / weights.sum(), raw=True
    )


def _hma(series: pd.Series, period: int) -> pd.Series:
    """Hull Moving Average: WMA(2*WMA(n/2) - WMA(n), sqrt(n))."""
    half_period = max(1, period // 2)
    sqrt_period = max(1, int(np.round(np.sqrt(period))))
    wma_half = _wma(series, half_period)
    wma_full = _wma(series, period)
    diff = 2.0 * wma_half - wma_full
    return _wma(diff, sqrt_period)


@registry.register("trend_filters")
def add_trend_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates Hull Moving Average (HMA 9 & 21) and Chandelier Trailing Exit."""
    enriched = df.copy()
    if len(enriched) == 0:
        return enriched

    close = enriched["Close"]
    high = enriched["High"]
    low = enriched["Low"]

    # 1. Hull Moving Average (9 and 21)
    hma9 = _hma(close, 9)
    hma21 = _hma(close, 21)

    enriched["HMA9"] = hma9
    enriched["HMA21"] = hma21

    # Slope of HMA9: +1 = Bullish (rising), -1 = Bearish (falling)
    hma9_slope = np.sign(hma9.diff()).fillna(0)
    enriched["HMA_Slope"] = hma9_slope
    enriched["HMA_Bullish"] = hma9_slope > 0
    enriched["HMA_Bearish"] = hma9_slope < 0

    # HMA crossovers
    hma_cross_bull = (hma9 > hma21) & (hma9.shift(1) <= hma21.shift(1))
    hma_cross_bear = (hma9 < hma21) & (hma9.shift(1) >= hma21.shift(1))
    enriched["HMA_Cross_Bull"] = hma_cross_bull
    enriched["HMA_Cross_Bear"] = hma_cross_bear

    # 2. Chandelier Exit (22 period lookback, 3x ATR)
    if "ATR14" in enriched.columns and enriched["ATR14"].notna().any():
        atr_val = enriched["ATR14"]
    else:
        # Fallback ATR 14
        tr = np.maximum(high - low, np.maximum((high - close.shift(1)).abs(), (low - close.shift(1)).abs()))
        atr_val = tr.rolling(14, min_periods=1).mean()

    lookback = min(22, max(2, len(enriched)))
    highest_high = high.rolling(lookback, min_periods=lookback).max()
    lowest_low = low.rolling(lookback, min_periods=lookback).min()

    chandelier_long = highest_high - 3.0 * atr_val
    chandelier_short = lowest_low + 3.0 * atr_val

    enriched["Chandelier_Long"] = chandelier_long
    enriched["Chandelier_Short"] = chandelier_short

    # Chandelier state: +1 if price above long stop, -1 if below short stop
    chandelier_bull = close > chandelier_long
    chandelier_bear = close < chandelier_short

    enriched["Chandelier_Bullish"] = chandelier_bull
    enriched["Chandelier_Bearish"] = chandelier_bear

    return enriched
