"""VWAP Standard Deviation Bands, Dynamic Bounce & Mean Reversion.

Brian Shannon and institutional desks use VWAP standard deviation bands as the
true statistical measure of whether price is "cheap" or "expensive" intraday.
Calculates:
  - Upper Bands: +1σ, +2σ, +3σ
  - Lower Bands: -1σ, -2σ, -3σ
  - VWAP Bounce (Bullish rejection off VWAP in an uptrend)
  - VWAP Rejection (Bearish rejection off VWAP in a downtrend)
  - Mean-Reversion Signals at ±2σ and ±3σ extremes
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


@registry.register("vwap_bands")
def add_vwap_bands(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates VWAP Multi-Deviation Bands (+1σ, +2σ, +3σ) and Rejection/Mean-Reversion signals."""
    enriched = df.copy()
    if len(enriched) == 0:
        return enriched

    high = enriched["High"]
    low = enriched["Low"]
    close = enriched["Close"]
    volume = enriched["Volume"]
    typical = (high + low + close) / 3.0

    # Ensure VWAP is present, or compute
    if "VWAP" in enriched.columns and enriched["VWAP"].notna().any():
        v = enriched["VWAP"]
    else:
        cum_vol = volume.cumsum()
        v = (typical * volume).cumsum() / cum_vol.replace(0, np.nan)
        enriched["VWAP"] = v

    # Standard deviation of typical price from VWAP:
    # variance = sum(volume * (typical - VWAP)^2) / sum(volume)
    diff_sq = (typical - v) ** 2
    cum_diff_sq_vol = (diff_sq * volume).cumsum()
    cum_vol = volume.cumsum().replace(0, np.nan)
    variance = cum_diff_sq_vol / cum_vol
    std_dev = np.sqrt(variance.clip(lower=0)).fillna(0)

    # If std_dev is nearly zero (e.g. constant price or 0 volume), fallback to 0.5% of VWAP
    std_dev = std_dev.where(std_dev > 0.0001, v * 0.005)

    enriched["VWAP_Std"] = std_dev
    enriched["VWAP_Upper_1"] = v + 1.0 * std_dev
    enriched["VWAP_Upper_2"] = v + 2.0 * std_dev
    enriched["VWAP_Upper_3"] = v + 3.0 * std_dev
    enriched["VWAP_Lower_1"] = v - 1.0 * std_dev
    enriched["VWAP_Lower_2"] = v - 2.0 * std_dev
    enriched["VWAP_Lower_3"] = v - 3.0 * std_dev

    # Signals:
    # 1. VWAP Bounce Bullish: low reaches near VWAP, price was above VWAP, closed green above VWAP
    near_vwap_low = (low <= v * 1.002) & (low >= v * 0.995)
    closed_green = close > enriched["Open"]
    enriched["VWAP_Bounce_Bull"] = near_vwap_low & closed_green & (close > v)

    # 2. VWAP Rejection Bearish: high reaches near VWAP, price was below VWAP, closed red below VWAP
    near_vwap_high = (high >= v * 0.998) & (high <= v * 1.005)
    closed_red = close < enriched["Open"]
    enriched["VWAP_Rejection_Bear"] = near_vwap_high & closed_red & (close < v)

    # 3. Overextension / Mean-Reversion Zones (statistical extremes)
    enriched["VWAP_Overextended_Bull"] = high >= enriched["VWAP_Upper_2"]
    enriched["VWAP_Overextended_Bear"] = low <= enriched["VWAP_Lower_2"]
    enriched["VWAP_Extreme_Bull"] = high >= enriched["VWAP_Upper_3"]
    enriched["VWAP_Extreme_Bear"] = low <= enriched["VWAP_Lower_3"]

    return enriched
