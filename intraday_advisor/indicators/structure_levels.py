"""Advanced price structure level indicators.

Adds:
  - Ichimoku Cloud (Kumo) — complete trading system
  - Fibonacci Retracement Levels — auto-detected swing-based
  - Pivot Points (Standard + Camarilla)

All register via the indicator registry.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


# ---------------------------------------------------------------------------
# Ichimoku Cloud
# ---------------------------------------------------------------------------

@registry.register("ichimoku")
def add_ichimoku(df: pd.DataFrame, tenkan: int = 9, kijun: int = 26,
                 senkou_b: int = 52, displacement: int = 26) -> pd.DataFrame:
    """Ichimoku Kinko Hyo — a complete trading system in one indicator.

    Components:
      - Tenkan-sen (conversion): 9-period midpoint — fast signal line
      - Kijun-sen (base): 26-period midpoint — slow signal line
      - Senkou Span A (leading A): midpoint of Tenkan+Kijun, displaced 26 ahead
      - Senkou Span B (leading B): 52-period midpoint, displaced 26 ahead
      - Chikou Span (lagging): close displaced 26 bars BACK

    Signals:
      - Price above cloud = bullish; below = bearish; inside = ranging
      - Tenkan crossing Kijun above cloud = strong buy
      - Cloud twist (Span A crosses Span B) = trend change incoming
      - Thick cloud = strong support/resistance; thin = weak
    """
    enriched = df.copy()
    high = enriched["High"]
    low = enriched["Low"]
    close = enriched["Close"]

    # Tenkan-sen (Conversion Line)
    tenkan_high = high.rolling(tenkan, min_periods=tenkan).max()
    tenkan_low = low.rolling(tenkan, min_periods=tenkan).min()
    enriched["Ichimoku_Tenkan"] = (tenkan_high + tenkan_low) / 2

    # Kijun-sen (Base Line)
    kijun_high = high.rolling(kijun, min_periods=kijun).max()
    kijun_low = low.rolling(kijun, min_periods=kijun).min()
    enriched["Ichimoku_Kijun"] = (kijun_high + kijun_low) / 2

    # Senkou Span A (Leading Span A) — displaced forward
    enriched["Ichimoku_SpanA"] = ((enriched["Ichimoku_Tenkan"] + enriched["Ichimoku_Kijun"]) / 2).shift(displacement)

    # Senkou Span B (Leading Span B) — displaced forward
    senkou_b_high = high.rolling(senkou_b, min_periods=senkou_b).max()
    senkou_b_low = low.rolling(senkou_b, min_periods=senkou_b).min()
    enriched["Ichimoku_SpanB"] = ((senkou_b_high + senkou_b_low) / 2).shift(displacement)

    # Chikou Span (Lagging) — displaced backward
    enriched["Ichimoku_Chikou"] = close.shift(-displacement)

    # Cloud colour: green when Span A > Span B, red otherwise
    enriched["Ichimoku_Cloud_Green"] = enriched["Ichimoku_SpanA"] > enriched["Ichimoku_SpanB"]

    # Price position relative to cloud
    cloud_top = enriched[["Ichimoku_SpanA", "Ichimoku_SpanB"]].max(axis=1)
    cloud_bottom = enriched[["Ichimoku_SpanA", "Ichimoku_SpanB"]].min(axis=1)
    enriched["Ichimoku_Above_Cloud"] = close > cloud_top
    enriched["Ichimoku_Below_Cloud"] = close < cloud_bottom
    enriched["Ichimoku_In_Cloud"] = ~enriched["Ichimoku_Above_Cloud"] & ~enriched["Ichimoku_Below_Cloud"]

    # TK Cross signal
    enriched["Ichimoku_TK_Cross_Bull"] = (
        (enriched["Ichimoku_Tenkan"] > enriched["Ichimoku_Kijun"]) &
        (enriched["Ichimoku_Tenkan"].shift(1) <= enriched["Ichimoku_Kijun"].shift(1))
    )
    enriched["Ichimoku_TK_Cross_Bear"] = (
        (enriched["Ichimoku_Tenkan"] < enriched["Ichimoku_Kijun"]) &
        (enriched["Ichimoku_Tenkan"].shift(1) >= enriched["Ichimoku_Kijun"].shift(1))
    )

    return enriched


# ---------------------------------------------------------------------------
# Fibonacci Retracement Levels
# ---------------------------------------------------------------------------

@registry.register("fibonacci_levels")
def add_fibonacci_levels(df: pd.DataFrame, lookback: int = 50) -> pd.DataFrame:
    """Auto-detected Fibonacci retracement levels based on recent swing high/low.

    Standard levels: 23.6%, 38.2%, 50%, 61.8%, 78.6%
    In an uptrend: these are dip-buy zones.
    In a downtrend: these are rally-sell zones.
    The 61.8% (golden ratio) is the most watched level.
    """
    enriched = df.copy()
    recent = enriched.tail(lookback)

    if len(recent) < 5:
        for level in ["Fib_0", "Fib_236", "Fib_382", "Fib_500", "Fib_618", "Fib_786", "Fib_1"]:
            enriched[level] = np.nan
        enriched["Fib_Trend"] = "unknown"
        return enriched

    swing_high = float(recent["High"].max())
    swing_low = float(recent["Low"].min())
    high_idx = recent["High"].idxmax()
    low_idx = recent["Low"].idxmin()

    diff = swing_high - swing_low
    if diff == 0:
        for level in ["Fib_0", "Fib_236", "Fib_382", "Fib_500", "Fib_618", "Fib_786", "Fib_1"]:
            enriched[level] = swing_high
        enriched["Fib_Trend"] = "flat"
        return enriched

    # Determine direction: if high came after low, we're in uptrend (retrace down)
    if high_idx > low_idx:
        # Uptrend retracement — levels measured from top down
        enriched["Fib_0"] = swing_high
        enriched["Fib_236"] = swing_high - 0.236 * diff
        enriched["Fib_382"] = swing_high - 0.382 * diff
        enriched["Fib_500"] = swing_high - 0.500 * diff
        enriched["Fib_618"] = swing_high - 0.618 * diff
        enriched["Fib_786"] = swing_high - 0.786 * diff
        enriched["Fib_1"] = swing_low
        enriched["Fib_Trend"] = "uptrend_retracement"
    else:
        # Downtrend retracement — levels measured from bottom up
        enriched["Fib_0"] = swing_low
        enriched["Fib_236"] = swing_low + 0.236 * diff
        enriched["Fib_382"] = swing_low + 0.382 * diff
        enriched["Fib_500"] = swing_low + 0.500 * diff
        enriched["Fib_618"] = swing_low + 0.618 * diff
        enriched["Fib_786"] = swing_low + 0.786 * diff
        enriched["Fib_1"] = swing_high
        enriched["Fib_Trend"] = "downtrend_retracement"

    return enriched


# ---------------------------------------------------------------------------
# Pivot Points (Standard + Camarilla)
# ---------------------------------------------------------------------------

@registry.register("pivot_points")
def add_pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """Standard and Camarilla Pivot Points from previous day's OHLC.

    Standard pivots: PP, S1/S2/S3, R1/R2/R3.
    Camarilla pivots: narrower, better for intraday range trading.

    S3/R3 are breakout levels; S1/R1 are the first bounce levels.
    Professional day traders watch these as the primary support/resistance.
    """
    enriched = df.copy()

    # Group by date to get previous day's OHLC
    enriched.index = pd.to_datetime(enriched.index)
    daily = enriched.groupby(enriched.index.date).agg(
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
    )

    if len(daily) < 2:
        for col in ["Pivot_PP", "Pivot_R1", "Pivot_R2", "Pivot_R3",
                     "Pivot_S1", "Pivot_S2", "Pivot_S3",
                     "Cam_R1", "Cam_R2", "Cam_R3", "Cam_R4",
                     "Cam_S1", "Cam_S2", "Cam_S3", "Cam_S4"]:
            enriched[col] = np.nan
        return enriched

    # Calculate pivots for each day based on PREVIOUS day's data
    pivot_data = {}
    dates = daily.index.tolist()
    for i in range(1, len(dates)):
        prev = daily.loc[dates[i - 1]]
        h, l, c = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
        hl_range = h - l

        # Standard pivots
        pp = (h + l + c) / 3
        pivot_data[dates[i]] = {
            "Pivot_PP": pp,
            "Pivot_R1": 2 * pp - l,
            "Pivot_R2": pp + hl_range,
            "Pivot_R3": h + 2 * (pp - l),
            "Pivot_S1": 2 * pp - h,
            "Pivot_S2": pp - hl_range,
            "Pivot_S3": l - 2 * (h - pp),
            # Camarilla pivots
            "Cam_R1": c + hl_range * 1.1 / 12,
            "Cam_R2": c + hl_range * 1.1 / 6,
            "Cam_R3": c + hl_range * 1.1 / 4,
            "Cam_R4": c + hl_range * 1.1 / 2,
            "Cam_S1": c - hl_range * 1.1 / 12,
            "Cam_S2": c - hl_range * 1.1 / 6,
            "Cam_S3": c - hl_range * 1.1 / 4,
            "Cam_S4": c - hl_range * 1.1 / 2,
        }

    # Map pivots to each bar by date
    bar_dates = enriched.index.date
    for col in ["Pivot_PP", "Pivot_R1", "Pivot_R2", "Pivot_R3",
                 "Pivot_S1", "Pivot_S2", "Pivot_S3",
                 "Cam_R1", "Cam_R2", "Cam_R3", "Cam_R4",
                 "Cam_S1", "Cam_S2", "Cam_S3", "Cam_S4"]:
        enriched[col] = pd.Series(
            [pivot_data.get(d, {}).get(col, np.nan) for d in bar_dates],
            index=enriched.index,
            dtype="float64",
        )

    return enriched
