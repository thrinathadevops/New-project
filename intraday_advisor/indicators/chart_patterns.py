"""Classical Geometrical Chart Pattern Recognition.

Detects high-probability structural chart patterns:
  - Double Bottom (W Pattern) & Neckline Breakout
  - Double Top (M Pattern) & Neckline Breakdown
  - Ascending Triangle (Rising lows into horizontal resistance)
  - Descending Triangle (Falling highs into horizontal support)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


def _find_peaks_valleys(high: pd.Series, low: pd.Series, lookback: int = 3):
    """Find local swing peaks and valleys with fixed lookback."""
    is_peak = pd.Series(True, index=high.index)
    is_valley = pd.Series(True, index=low.index)
    for i in range(1, lookback + 1):
        is_peak &= (high > high.shift(i)) & (high > high.shift(-i))
        is_valley &= (low < low.shift(i)) & (low < low.shift(-i))
    return is_peak, is_valley


@registry.register("chart_patterns")
def add_chart_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Identifies Double Tops, Double Bottoms, and Triangle patterns across candles."""
    enriched = df.copy()
    n = len(enriched)
    if n < 15:
        # Not enough bars to form multi-swing chart patterns
        enriched["Pattern_Double_Bottom_W"] = False
        enriched["Pattern_Double_Top_M"] = False
        enriched["Pattern_Ascending_Triangle"] = False
        enriched["Pattern_Descending_Triangle"] = False
        return enriched

    high = enriched["High"]
    low = enriched["Low"]
    close = enriched["Close"]

    double_bottom_w = np.zeros(n, dtype=bool)
    double_top_m = np.zeros(n, dtype=bool)
    ascending_tri = np.zeros(n, dtype=bool)
    descending_tri = np.zeros(n, dtype=bool)

    # Check for recent swings over rolling windows of 20-30 bars
    window = 25
    for i in range(window, n):
        sub_h = high.iloc[i - window : i]
        sub_l = low.iloc[i - window : i]
        cur_c = close.iloc[i]
        cur_prev_c = close.iloc[i - 1]

        # Find the two lowest lows in the window separated by at least 4 bars
        l_sorted = sub_l.sort_values()
        if len(l_sorted) >= 2:
            l1_idx = l_sorted.index[0]
            # find second low at least 4 bars apart
            l2_candidates = [idx for idx in l_sorted.index[1:] if abs(sub_l.index.get_loc(idx) - sub_l.index.get_loc(l1_idx)) >= 4]
            if l2_candidates:
                l2_idx = l2_candidates[0]
                val1 = sub_l.loc[l1_idx]
                val2 = sub_l.loc[l2_idx]
                # Similar level within 0.35%
                if abs(val1 - val2) / min(val1, val2) < 0.0035:
                    # Neckline is the highest high between l1 and l2
                    loc1 = min(sub_l.index.get_loc(l1_idx), sub_l.index.get_loc(l2_idx))
                    loc2 = max(sub_l.index.get_loc(l1_idx), sub_l.index.get_loc(l2_idx))
                    between_high = sub_h.iloc[loc1:loc2].max() if loc2 > loc1 else sub_h.max()
                    # Double bottom triggers if current bar breaks above neckline
                    if cur_c > between_high and cur_prev_c <= between_high * 1.002:
                        double_bottom_w[i] = True

        # Find the two highest highs in the window separated by at least 4 bars (Double Top)
        h_sorted = sub_h.sort_values(ascending=False)
        if len(h_sorted) >= 2:
            h1_idx = h_sorted.index[0]
            h2_candidates = [idx for idx in h_sorted.index[1:] if abs(sub_h.index.get_loc(idx) - sub_h.index.get_loc(h1_idx)) >= 4]
            if h2_candidates:
                h2_idx = h2_candidates[0]
                hval1 = sub_h.loc[h1_idx]
                hval2 = sub_h.loc[h2_idx]
                if abs(hval1 - hval2) / min(hval1, hval2) < 0.0035:
                    loc1 = min(sub_h.index.get_loc(h1_idx), sub_h.index.get_loc(h2_idx))
                    loc2 = max(sub_h.index.get_loc(h1_idx), sub_h.index.get_loc(h2_idx))
                    between_low = sub_l.iloc[loc1:loc2].min() if loc2 > loc1 else sub_l.min()
                    # Double top triggers if current bar breaks below neckline
                    if cur_c < between_low and cur_prev_c >= between_low * 0.998:
                        double_top_m[i] = True

        # Ascending Triangle: Highs are near horizontal ceiling, Lows are making higher lows
        recent_h_max = sub_h.max()
        top_cluster = sub_h[sub_h >= recent_h_max * 0.997]
        # Lows slope
        l_first_half = sub_l.iloc[: window // 2].min()
        l_second_half = sub_l.iloc[window // 2 :].min()
        if len(top_cluster) >= 2 and l_second_half > l_first_half * 1.002:
            if cur_c > recent_h_max:
                ascending_tri[i] = True

        # Descending Triangle: Lows are near horizontal floor, Highs are making lower highs
        recent_l_min = sub_l.min()
        bottom_cluster = sub_l[sub_l <= recent_l_min * 1.003]
        h_first_half = sub_h.iloc[: window // 2].max()
        h_second_half = sub_h.iloc[window // 2 :].max()
        if len(bottom_cluster) >= 2 and h_second_half < h_first_half * 0.998:
            if cur_c < recent_l_min:
                descending_tri[i] = True

    enriched["Pattern_Double_Bottom_W"] = double_bottom_w
    enriched["Pattern_Double_Top_M"] = double_top_m
    enriched["Pattern_Ascending_Triangle"] = ascending_tri
    enriched["Pattern_Descending_Triangle"] = descending_tri

    return enriched
