"""Volume Spread Analysis (VSA) and Wyckoff Institutional Order Flow.

Tom Williams' VSA and Richard Wyckoff's methodology analyze the relationship
between the Price Spread (High - Low), Close position within the bar, and Volume.
Detects:
  - Stopping Volume (Absorption of panic sellers by smart money)
  - Buying Climax (Smart money unloading into retail breakout buyers)
  - No Demand Bar (Lack of institutional participation on up-moves)
  - No Supply Bar (Supply exhausted on pullbacks — launchpad for rallies)
  - Effort vs. Result Divergence (Huge volume with small price progress = churn)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


@registry.register("vsa_wyckoff")
def add_vsa_wyckoff(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Calculates Volume Spread Analysis metrics and Wyckoff strength/weakness signals."""
    enriched = df.copy()
    n = len(enriched)
    if n == 0:
        return enriched

    high = enriched["High"]
    low = enriched["Low"]
    close = enriched["Close"]
    volume = enriched["Volume"]

    spread = (high - low).replace(0, 1e-9)
    avg_spread = spread.rolling(window, min_periods=3).mean().replace(0, 1e-9)
    avg_vol = volume.rolling(window, min_periods=3).mean().replace(0, 1.0)

    vol_ratio = volume / avg_vol
    spread_ratio = spread / avg_spread
    close_pos = (close - low) / spread

    prev_vol = volume.shift(1)
    prev_vol_2 = volume.shift(2)
    prev_close = close.shift(1)

    enriched["VSA_Spread_Ratio"] = spread_ratio
    enriched["VSA_Volume_Ratio"] = vol_ratio
    enriched["VSA_Close_Position"] = close_pos

    # 1. Stopping Volume (Bullish):
    # Down-bar on ultra-high volume (> 1.8x), but close finishes in upper 45% of the bar
    stopping_vol_bull = (
        (close < prev_close)
        & (vol_ratio >= 1.8)
        & (close_pos >= 0.45)
    )

    # 2. Buying Climax / Stopping Volume (Bearish):
    # Up-bar on ultra-high volume (> 1.8x), but close finishes in lower 45% of the bar
    stopping_vol_bear = (
        (close > prev_close)
        & (vol_ratio >= 1.8)
        & (close_pos <= 0.55)
    )

    # 3. No Demand Bar:
    # Up-bar on narrow/normal spread with volume lower than the prior 2 bars and below average
    no_demand = (
        (close > prev_close)
        & (volume < prev_vol)
        & (volume < prev_vol_2)
        & (vol_ratio <= 0.85)
        & (spread_ratio <= 1.0)
    )

    # 4. No Supply Bar:
    # Down-bar on narrow/normal spread with volume lower than the prior 2 bars and below average
    no_supply = (
        (close < prev_close)
        & (volume < prev_vol)
        & (volume < prev_vol_2)
        & (vol_ratio <= 0.85)
        & (spread_ratio <= 1.0)
    )

    # 5. Effort vs Result (Bearish):
    # Ultra-high volume (> 1.7x) trying to push up, but spread is narrow (< 0.8x)
    effort_vs_result_bear = (
        (close >= prev_close)
        & (vol_ratio >= 1.7)
        & (spread_ratio <= 0.85)
    )

    # 6. Effort vs Result (Bullish):
    # Ultra-high volume (> 1.7x) trying to push down, but spread is narrow (< 0.8x)
    effort_vs_result_bull = (
        (close <= prev_close)
        & (vol_ratio >= 1.7)
        & (spread_ratio <= 0.85)
    )

    enriched["VSA_Stopping_Volume_Bull"] = stopping_vol_bull
    enriched["VSA_Stopping_Volume_Bear"] = stopping_vol_bear
    enriched["VSA_No_Demand"] = no_demand
    enriched["VSA_No_Supply"] = no_supply
    enriched["VSA_Effort_Vs_Result_Bear"] = effort_vs_result_bear
    enriched["VSA_Effort_Vs_Result_Bull"] = effort_vs_result_bull

    return enriched
