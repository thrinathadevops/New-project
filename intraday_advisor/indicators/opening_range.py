"""Opening Range Breakout (ORB) and Initial Balance (IB) Indicator.

The most widely traded intraday setup in Indian and global markets.
Detects:
  - 5-minute ORB (Fast scalp range)
  - 15-minute ORB (Standard institutional intraday range)
  - 30-minute ORB (Conservative range)
  - Initial Balance (IB: 60-minute institutional auction balance)
  - Bullish / Bearish Breakouts with volume confirmation
  - ORB Retest confirmation (pullback to broken level with rejection)
  - ORB Trap / Fakeout alerts (breakout that immediately fails back into range)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


@registry.register("opening_range")
def add_opening_range(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates Opening Ranges (5m, 15m, 30m, 60m IB) and Breakout/Trap signals."""
    enriched = df.copy()
    n = len(enriched)
    if n == 0:
        return enriched

    high = enriched["High"]
    low = enriched["Low"]
    close = enriched["Close"]
    volume = enriched["Volume"]

    # Rolling 20 volume for relative volume confirmation
    avg_vol = volume.rolling(20, min_periods=1).mean().replace(0, 1.0)
    vol_surge = (volume / avg_vol) >= 1.25

    has_datetime = isinstance(enriched.index, pd.DatetimeIndex)

    if has_datetime and len(enriched.index.normalize().unique()) > 1:
        # Group by day and compute the opening range high/low based on the first few candles
        # In a 5m frame: candle 0 = 5m, candle 0-2 = 15m, candle 0-5 = 30m, candle 0-11 = 60m
        groups = enriched.groupby(enriched.index.date)
        
        # Calculate for each day:
        # 1st candle (5m)
        orb_5_h = groups["High"].transform(lambda s: s.iloc[0] if len(s) > 0 else np.nan)
        orb_5_l = groups["Low"].transform(lambda s: s.iloc[0] if len(s) > 0 else np.nan)
        
        # First 3 candles (approx 15m on 5m, or 1 candle on 15m)
        orb_15_h = groups["High"].transform(lambda s: s.iloc[:3].max() if len(s) >= 1 else np.nan)
        orb_15_l = groups["Low"].transform(lambda s: s.iloc[:3].min() if len(s) >= 1 else np.nan)
        
        # First 6 candles (approx 30m on 5m)
        orb_30_h = groups["High"].transform(lambda s: s.iloc[:6].max() if len(s) >= 1 else np.nan)
        orb_30_l = groups["Low"].transform(lambda s: s.iloc[:6].min() if len(s) >= 1 else np.nan)
        
        # Initial Balance (first 12 candles on 5m = 60 mins)
        ib_h = groups["High"].transform(lambda s: s.iloc[:12].max() if len(s) >= 1 else np.nan)
        ib_l = groups["Low"].transform(lambda s: s.iloc[:12].min() if len(s) >= 1 else np.nan)
    else:
        # Non-datetime fallback: use first few bars of the series
        orb_5_h = pd.Series(high.iloc[0], index=enriched.index)
        orb_5_l = pd.Series(low.iloc[0], index=enriched.index)
        orb_15_h = pd.Series(high.iloc[: min(3, n)].max(), index=enriched.index)
        orb_15_l = pd.Series(low.iloc[: min(3, n)].min(), index=enriched.index)
        orb_30_h = pd.Series(high.iloc[: min(6, n)].max(), index=enriched.index)
        orb_30_l = pd.Series(low.iloc[: min(6, n)].min(), index=enriched.index)
        ib_h = pd.Series(high.iloc[: min(12, n)].max(), index=enriched.index)
        ib_l = pd.Series(low.iloc[: min(12, n)].min(), index=enriched.index)

    enriched["ORB_5_High"] = orb_5_h
    enriched["ORB_5_Low"] = orb_5_l
    enriched["ORB_15_High"] = orb_15_h
    enriched["ORB_15_Low"] = orb_15_l
    enriched["ORB_30_High"] = orb_30_h
    enriched["ORB_30_Low"] = orb_30_l
    enriched["IB_High"] = ib_h
    enriched["IB_Low"] = ib_l

    # 15m ORB is the benchmark:
    prev_close = close.shift(1)
    
    # Breakouts: current candle closes above ORB_15_High with volume
    orb_breakout_bull = (close > orb_15_h) & (prev_close <= orb_15_h) & vol_surge
    orb_breakdown_bear = (close < orb_15_l) & (prev_close >= orb_15_l) & vol_surge

    # Retest: price broke out earlier, current candle tests ORB boundary and wicks off
    # Bullish retest: low touches near ORB_15_High (within 0.15%) and close stays above it
    tested_orb_h = ((low <= orb_15_h * 1.0015) & (low >= orb_15_h * 0.9975))
    orb_retest_bull = tested_orb_h & (close > orb_15_h) & (prev_close > orb_15_h)

    tested_orb_l = ((high >= orb_15_l * 0.9985) & (high <= orb_15_l * 1.0025))
    orb_retest_bear = tested_orb_l & (close < orb_15_l) & (prev_close < orb_15_l)

    # Fakeout / Trap: price spiked above ORB High, but closed back below ORB High
    orb_trap_bull = (high > orb_15_h) & (close < orb_15_h) & (prev_close > orb_15_h * 0.998)
    orb_trap_bear = (low < orb_15_l) & (close > orb_15_l) & (prev_close < orb_15_l * 1.002)

    enriched["ORB_Breakout_Bull"] = orb_breakout_bull
    enriched["ORB_Breakdown_Bear"] = orb_breakdown_bear
    enriched["ORB_Retest_Bull"] = orb_retest_bull
    enriched["ORB_Retest_Bear"] = orb_retest_bear
    enriched["ORB_Trap_Bull"] = orb_trap_bull
    enriched["ORB_Trap_Bear"] = orb_trap_bear

    return enriched
