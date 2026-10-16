"""Intraday Gap Analysis, Gap Fill Probability, and Gap & Go / Fade Strategy.

Almost every session on the NSE opens with an overnight gap influenced by
global markets and GIFT Nifty.
Features:
  - Gap Classification: Full Gap Up/Down, Partial Gap Up/Down, Flat Open
  - Gap Size in points and percentage
  - Gap Fill Monitor: Real-time detection of whether the overnight gap has closed
  - Gap & Go Strategy: Momentum continuation when gap holds opening levels
  - Gap Fade Strategy: Mean-reversion back towards previous day's close
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


@registry.register("gap_analysis")
def add_gap_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates Gap Classification, Gap Fill status, and Gap & Go / Fade setups."""
    enriched = df.copy()
    n = len(enriched)
    if n == 0:
        return enriched

    has_datetime = isinstance(enriched.index, pd.DatetimeIndex)

    if has_datetime and len(enriched.index.normalize().unique()) > 1:
        groups = enriched.groupby(enriched.index.date)
        daily = groups.agg(
            Open=("Open", "first"),
            High=("High", "max"),
            Low=("Low", "min"),
            Close=("Close", "last"),
        )
        prev_daily = daily.shift(1)

        date_series = pd.Series(enriched.index.date, index=enriched.index)
        prev_close = date_series.map(prev_daily["Close"]).astype(float)
        prev_high = date_series.map(prev_daily["High"]).astype(float)
        prev_low = date_series.map(prev_daily["Low"]).astype(float)
        day_open = groups["Open"].transform("first")
    else:
        # Rolling fallback: lookback ~30 bars
        lookback = min(30, max(2, n // 2))
        prev_close = enriched["Close"].shift(1)
        prev_high = enriched["High"].shift(1).rolling(lookback, min_periods=1).max()
        prev_low = enriched["Low"].shift(1).rolling(lookback, min_periods=1).min()
        day_open = enriched["Open"].iloc[0]

    gap_pts = day_open - prev_close
    gap_pct = (gap_pts / prev_close.replace(0, np.nan)) * 100.0

    # Gap Types
    full_gap_up = day_open > prev_high
    full_gap_down = day_open < prev_low
    partial_gap_up = (day_open > prev_close) & ~full_gap_up
    partial_gap_down = (day_open < prev_close) & ~full_gap_down
    flat_open = gap_pct.abs() < 0.10

    enriched["Gap_Points"] = gap_pts
    enriched["Gap_Pct"] = gap_pct
    enriched["Gap_Full_Up"] = full_gap_up
    enriched["Gap_Full_Down"] = full_gap_down
    enriched["Gap_Partial_Up"] = partial_gap_up
    enriched["Gap_Partial_Down"] = partial_gap_down

    # Gap Filled: Has current price touched or crossed prev_close?
    # If gap was up, filled when Low <= prev_close
    # If gap was down, filled when High >= prev_close
    low = enriched["Low"]
    high = enriched["High"]
    gap_up_filled = (gap_pts > 0) & (low <= prev_close)
    gap_down_filled = (gap_pts < 0) & (high >= prev_close)
    enriched["Gap_Filled"] = gap_up_filled | gap_down_filled

    # Gap & Go: Gap exists, holding above day_open with bullish price action
    close = enriched["Close"]
    gap_and_go_bull = (gap_pct >= 0.3) & (close > day_open) & (low > prev_high * 0.998)
    gap_and_go_bear = (gap_pct <= -0.3) & (close < day_open) & (high < prev_low * 1.002)

    # Gap Fade: Gap exists, but price failed to hold and is reverting towards prev_close
    gap_fade_bear = (gap_pct >= 0.3) & (close < day_open) & ~gap_up_filled
    gap_fade_bull = (gap_pct <= -0.3) & (close > day_open) & ~gap_down_filled

    enriched["Gap_And_Go_Bull"] = gap_and_go_bull
    enriched["Gap_And_Go_Bear"] = gap_and_go_bear
    enriched["Gap_Fade_Bear"] = gap_fade_bear
    enriched["Gap_Fade_Bull"] = gap_fade_bull

    return enriched
