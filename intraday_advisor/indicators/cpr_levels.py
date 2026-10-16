"""Central Pivot Range (CPR) and Daily Floor Pivot Indicators.

Massively popular and reliable in Indian intraday trading (Nifty, Bank Nifty, and F&O stocks).
Identifies:
  - Central Pivot Range: Pivot (P), Bottom Central (BC), Top Central (TC)
  - CPR Width: Narrow CPR (Trending day anticipated), Wide CPR (Rangebound / Sideways day)
  - Virgin CPR (VCPR): Unmitigated CPR levels that act as major price magnets
  - Support & Resistance Floor Pivots (R1, R2, R3, S1, S2, S3)
  - Price relationship to CPR (Above TC = Bullish, Below BC = Bearish, Inside CPR = Trap / Chop)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


@registry.register("cpr_levels")
def add_cpr_levels(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates Central Pivot Range (CPR) and Floor Pivots based on daily aggregated candles.
    
    If intraday data is supplied, aggregates to daily bars to find previous day High, Low, Close.
    If already daily or index lacks datetime, uses rolling 1-day or previous bar.
    """
    enriched = df.copy()
    n = len(enriched)
    if n == 0:
        return enriched

    # Default fallback values based on rolling 24 bars (~2 hours on 5m) if not datetime
    has_datetime = isinstance(enriched.index, pd.DatetimeIndex)

    if has_datetime and len(enriched.index.normalize().unique()) > 1:
        # Group by trading date
        daily = enriched.groupby(enriched.index.date).agg(
            High=("High", "max"),
            Low=("Low", "min"),
            Close=("Close", "last"),
        )
        # Shift 1 to get PREVIOUS day's H, L, C
        prev_daily = daily.shift(1)
        
        # Merge back on date
        date_series = pd.Series(enriched.index.date, index=enriched.index)
        prev_h = date_series.map(prev_daily["High"]).astype(float)
        prev_l = date_series.map(prev_daily["Low"]).astype(float)
        prev_c = date_series.map(prev_daily["Close"]).astype(float)
    else:
        # Rolling approximation: lookback 30 bars (~1 day of 5m/15m)
        lookback = min(30, max(2, n // 2))
        prev_h = enriched["High"].shift(1).rolling(lookback, min_periods=1).max()
        prev_l = enriched["Low"].shift(1).rolling(lookback, min_periods=1).min()
        prev_c = enriched["Close"].shift(1)

    # 1. Pivot calculations
    pivot = (prev_h + prev_l + prev_c) / 3.0
    bc = (prev_h + prev_l) / 2.0
    tc = (pivot - bc) + pivot

    # TC is conventionally top, BC is bottom, but mathematically TC can be < BC
    cpr_tc = np.maximum(tc, bc)
    cpr_bc = np.minimum(tc, bc)

    # 2. Support and Resistance Floor Pivots
    r1 = 2 * pivot - prev_l
    s1 = 2 * pivot - prev_h
    r2 = pivot + (prev_h - prev_l)
    s2 = pivot - (prev_h - prev_l)
    r3 = prev_h + 2 * (pivot - prev_l)
    s3 = prev_l - 2 * (prev_h - pivot)

    # Assign columns
    enriched["CPR_Pivot"] = pivot
    enriched["CPR_TC"] = cpr_tc
    enriched["CPR_BC"] = cpr_bc
    enriched["CPR_R1"] = r1
    enriched["CPR_S1"] = s1
    enriched["CPR_R2"] = r2
    enriched["CPR_S2"] = s2
    enriched["CPR_R3"] = r3
    enriched["CPR_S3"] = s3

    # 3. CPR Width and Classification
    # CPR Width = (TC - BC) / Pivot
    cpr_width = (cpr_tc - cpr_bc).abs()
    cpr_width_pct = (cpr_width / pivot.replace(0, np.nan)) * 100.0
    enriched["CPR_Width_Pct"] = cpr_width_pct

    # Narrow CPR < 0.25% indicates breakout/trending day; Wide CPR > 0.50% indicates sideways/choppy day
    enriched["CPR_Narrow"] = cpr_width_pct < 0.25
    enriched["CPR_Wide"] = cpr_width_pct > 0.50

    # 4. Price Relationship to CPR
    close = enriched["Close"]
    enriched["CPR_Above_TC"] = close > cpr_tc
    enriched["CPR_Below_BC"] = close < cpr_bc
    enriched["CPR_Inside"] = (close >= cpr_bc) & (close <= cpr_tc)

    # 5. Virgin CPR (VCPR) check
    # A CPR is Virgin if today's high < CPR_BC (stayed below entire day) or today's low > CPR_TC (stayed above)
    low = enriched["Low"]
    high = enriched["High"]
    touched_cpr = (low <= cpr_tc) & (high >= cpr_bc)
    enriched["CPR_Tested"] = touched_cpr
    # If not tested throughout recent history, it acts as a magnet
    enriched["CPR_Virgin"] = ~touched_cpr

    return enriched
