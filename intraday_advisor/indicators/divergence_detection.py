"""Divergence detection and multi-timeframe confluence.

Adds:
  - RSI Divergence (Regular + Hidden, Bullish + Bearish)
  - MACD Divergence (Regular + Hidden, Bullish + Bearish)
  - Multi-timeframe RSI Confluence (not an indicator column, but a flag)

These are among the highest-probability signals in trading: price makes
a new extreme but the oscillator doesn't confirm it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


# ---------------------------------------------------------------------------
# Swing pivot detection for divergence
# ---------------------------------------------------------------------------

def _swing_highs(series: pd.Series, order: int = 5) -> pd.Series:
    """Identify local maxima: bars where the value is highest in ±order bars."""
    mask = pd.Series(True, index=series.index)
    for i in range(1, order + 1):
        mask &= (series > series.shift(i)) & (series > series.shift(-i))
    return series.where(mask)


def _swing_lows(series: pd.Series, order: int = 5) -> pd.Series:
    """Identify local minima."""
    mask = pd.Series(True, index=series.index)
    for i in range(1, order + 1):
        mask &= (series < series.shift(i)) & (series < series.shift(-i))
    return series.where(mask)


def _detect_divergence(price: pd.Series, oscillator: pd.Series, order: int = 5) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Detect regular and hidden divergences.

    Returns: (regular_bull, regular_bear, hidden_bull, hidden_bear) as boolean Series.

    Regular Bullish: price makes LOWER low, oscillator makes HIGHER low → reversal up
    Regular Bearish: price makes HIGHER high, oscillator makes LOWER high → reversal down
    Hidden Bullish: price makes HIGHER low, oscillator makes LOWER low → continuation up
    Hidden Bearish: price makes LOWER high, oscillator makes HIGHER high → continuation down
    """
    price_highs = _swing_highs(price, order)
    price_lows = _swing_lows(price, order)
    osc_highs = _swing_highs(oscillator, order)
    osc_lows = _swing_lows(oscillator, order)

    n = len(price)
    reg_bull = pd.Series(False, index=price.index)
    reg_bear = pd.Series(False, index=price.index)
    hid_bull = pd.Series(False, index=price.index)
    hid_bear = pd.Series(False, index=price.index)

    # Find pairs of consecutive swing lows/highs
    low_indices = price_lows.dropna().index.tolist()
    high_indices = price_highs.dropna().index.tolist()

    # Check lows for bullish divergences
    for i in range(1, len(low_indices)):
        idx_prev = low_indices[i - 1]
        idx_curr = low_indices[i]

        p_prev = price.loc[idx_prev]
        p_curr = price.loc[idx_curr]
        o_prev = oscillator.loc[idx_prev] if not pd.isna(oscillator.get(idx_prev, np.nan)) else np.nan
        o_curr = oscillator.loc[idx_curr] if not pd.isna(oscillator.get(idx_curr, np.nan)) else np.nan

        if np.isnan(o_prev) or np.isnan(o_curr):
            continue

        # Regular bullish: price lower low, oscillator higher low
        if p_curr < p_prev and o_curr > o_prev:
            reg_bull.loc[idx_curr] = True

        # Hidden bullish: price higher low, oscillator lower low
        if p_curr > p_prev and o_curr < o_prev:
            hid_bull.loc[idx_curr] = True

    # Check highs for bearish divergences
    for i in range(1, len(high_indices)):
        idx_prev = high_indices[i - 1]
        idx_curr = high_indices[i]

        p_prev = price.loc[idx_prev]
        p_curr = price.loc[idx_curr]
        o_prev = oscillator.loc[idx_prev] if not pd.isna(oscillator.get(idx_prev, np.nan)) else np.nan
        o_curr = oscillator.loc[idx_curr] if not pd.isna(oscillator.get(idx_curr, np.nan)) else np.nan

        if np.isnan(o_prev) or np.isnan(o_curr):
            continue

        # Regular bearish: price higher high, oscillator lower high
        if p_curr > p_prev and o_curr < o_prev:
            reg_bear.loc[idx_curr] = True

        # Hidden bearish: price lower high, oscillator higher high
        if p_curr < p_prev and o_curr > o_prev:
            hid_bear.loc[idx_curr] = True

    return reg_bull, reg_bear, hid_bull, hid_bear


# ---------------------------------------------------------------------------
# RSI Divergence
# ---------------------------------------------------------------------------

@registry.register("rsi_divergence")
def add_rsi_divergence(df: pd.DataFrame, order: int = 5) -> pd.DataFrame:
    """RSI Divergence detection — the single most reliable reversal warning.

    Regular bullish divergence: price makes lower low but RSI makes higher low.
    This means selling momentum is WEAKENING even though price is still dropping.
    """
    enriched = df.copy()
    rsi = enriched.get("RSI14")
    if rsi is None:
        return enriched

    close = enriched["Close"]
    reg_bull, reg_bear, hid_bull, hid_bear = _detect_divergence(close, rsi, order)

    enriched["RSI_Div_Bull"] = reg_bull
    enriched["RSI_Div_Bear"] = reg_bear
    enriched["RSI_Div_Hidden_Bull"] = hid_bull
    enriched["RSI_Div_Hidden_Bear"] = hid_bear
    return enriched


# ---------------------------------------------------------------------------
# MACD Divergence
# ---------------------------------------------------------------------------

@registry.register("macd_divergence")
def add_macd_divergence(df: pd.DataFrame, order: int = 5) -> pd.DataFrame:
    """MACD histogram divergence — catches larger-timeframe trend exhaustion.

    Works the same way as RSI divergence but on the MACD histogram,
    which makes it more sensitive to momentum changes in trending markets.
    """
    enriched = df.copy()
    macd_hist = enriched.get("MACDHist")
    if macd_hist is None:
        return enriched

    close = enriched["Close"]
    reg_bull, reg_bear, hid_bull, hid_bear = _detect_divergence(close, macd_hist, order)

    enriched["MACD_Div_Bull"] = reg_bull
    enriched["MACD_Div_Bear"] = reg_bear
    enriched["MACD_Div_Hidden_Bull"] = hid_bull
    enriched["MACD_Div_Hidden_Bear"] = hid_bear
    return enriched
