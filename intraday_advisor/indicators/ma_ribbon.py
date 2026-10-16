"""Moving Average Ribbon indicator.

A fan of 8 EMAs (8, 13, 21, 34, 55, 89, 144, 200):
  - All fanning out and in order = strong trend
  - Converging / tangling = trend weakening, reversal coming
  - Ribbon flip (short EMAs cross long ones) = trend change

Also computes ribbon state (fanned/converging/crossed) and distance
metric for trend strength.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


MA_RIBBON_PERIODS = (8, 13, 21, 34, 55, 89, 144, 200)


@registry.register("ma_ribbon")
def add_ma_ribbon(df: pd.DataFrame) -> pd.DataFrame:
    """Moving Average Ribbon — 8 EMAs for trend strength and direction.

    When all 8 are stacked in order (shortest on top for uptrend), the
    trend is strong and pullbacks are buyable. When they tangle, stand aside.
    """
    enriched = df.copy()
    close = enriched["Close"]

    ribbon_cols = []
    for period in MA_RIBBON_PERIODS:
        col = f"Ribbon_EMA{period}"
        enriched[col] = close.ewm(span=period, adjust=False, min_periods=period).mean()
        ribbon_cols.append(col)

    # Check if ribbon is in perfect bullish order (short > long)
    bullish_order = pd.Series(True, index=enriched.index)
    bearish_order = pd.Series(True, index=enriched.index)
    for i in range(len(ribbon_cols) - 1):
        bullish_order &= enriched[ribbon_cols[i]] > enriched[ribbon_cols[i + 1]]
        bearish_order &= enriched[ribbon_cols[i]] < enriched[ribbon_cols[i + 1]]

    enriched["Ribbon_Bullish"] = bullish_order
    enriched["Ribbon_Bearish"] = bearish_order

    # Ribbon width: distance between fastest and slowest EMA as % of price
    fastest = enriched[ribbon_cols[0]]
    slowest = enriched[ribbon_cols[-1]]
    enriched["Ribbon_Width"] = (fastest - slowest) / close.replace(0, np.nan) * 100

    # Ribbon state
    conditions = [
        bullish_order,
        bearish_order,
    ]
    choices = ["bullish_fanned", "bearish_fanned"]
    enriched["Ribbon_State"] = np.select(conditions, choices, default="converging")

    return enriched
