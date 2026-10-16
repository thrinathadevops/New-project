"""Heikin-Ashi Trend Filtering & Reversal Exhaustion System.

Heikin-Ashi cleans market noise from erratic intraday wicks, allowing traders
to ride trends without getting shaken out prematurely.
Signals:
  - Strong Bullish Trend: Consecutive green candles with NO lower wick (flat base)
  - Strong Bearish Trend: Consecutive red candles with NO upper wick (flat top)
  - Reversal Alert: Spinning top / Doji candle after a 3+ candle trending streak
  - Trend Color (+1 = Bullish, -1 = Bearish)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


@registry.register("heikin_ashi")
def add_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates Heikin-Ashi candle series, clean trend states, and exhaustion reversals."""
    enriched = df.copy()
    n = len(enriched)
    if n == 0:
        return enriched

    open_arr = enriched["Open"].to_numpy()
    high_arr = enriched["High"].to_numpy()
    low_arr = enriched["Low"].to_numpy()
    close_arr = enriched["Close"].to_numpy()

    ha_close = (open_arr + high_arr + low_arr + close_arr) / 4.0
    ha_open = np.empty(n, dtype=float)
    ha_high = np.empty(n, dtype=float)
    ha_low = np.empty(n, dtype=float)

    ha_open[0] = (open_arr[0] + close_arr[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    ha_high = np.maximum(high_arr, np.maximum(ha_open, ha_close))
    ha_low = np.minimum(low_arr, np.minimum(ha_open, ha_close))

    enriched["HA_Open"] = ha_open
    enriched["HA_High"] = ha_high
    enriched["HA_Low"] = ha_low
    enriched["HA_Close"] = ha_close

    ha_body = np.abs(ha_close - ha_open)
    ha_range = np.maximum(ha_high - ha_low, 1e-9)
    ha_upper_wick = ha_high - np.maximum(ha_open, ha_close)
    ha_lower_wick = np.minimum(ha_open, ha_close) - ha_low

    ha_is_green = ha_close >= ha_open
    ha_is_red = ha_close < ha_open

    # Flat base (no lower shadow) = Strong Bullish
    # Using small tolerance (< 8% of body or 5% of range)
    ha_flat_base = ha_lower_wick <= np.maximum(ha_body * 0.1, ha_range * 0.05)
    ha_flat_top = ha_upper_wick <= np.maximum(ha_body * 0.1, ha_range * 0.05)

    ha_strong_bull = ha_is_green & ha_flat_base
    ha_strong_bear = ha_is_red & ha_flat_top

    # Doji / Indecision on Heikin-Ashi: small body, wicks on both sides
    ha_doji = (ha_body <= ha_range * 0.25) & (ha_upper_wick > ha_body) & (ha_lower_wick > ha_body)

    # Color: +1 for green, -1 for red
    ha_color = np.where(ha_is_green, 1, -1)

    enriched["HA_Color"] = ha_color
    enriched["HA_Strong_Bull"] = ha_strong_bull
    enriched["HA_Strong_Bear"] = ha_strong_bear
    enriched["HA_Doji_Reversal"] = ha_doji

    # Streak calculation
    streak = np.zeros(n, dtype=int)
    for i in range(n):
        if i == 0:
            streak[i] = ha_color[i]
        else:
            if ha_color[i] == np.sign(streak[i - 1]):
                streak[i] = streak[i - 1] + ha_color[i]
            else:
                streak[i] = ha_color[i]

    enriched["HA_Streak"] = streak
    # Exhaustion: Doji appearing when streak magnitude was >= 3
    prev_streak_mag = np.abs(pd.Series(streak).shift(1).fillna(0))
    enriched["HA_Exhaustion_Alert"] = ha_doji & (prev_streak_mag >= 3)

    return enriched
