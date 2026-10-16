"""Extended candlestick pattern recognition.

The existing ``price_action.candlestick_psychology`` only detects single-bar
patterns (doji, pin bar, engulfing). This module adds the multi-bar patterns
that Instagram/mentor strategies rely on:

  - Morning Star / Evening Star (3-candle reversal)
  - Three White Soldiers / Three Black Crows (3-candle continuation)
  - Bullish / Bearish Harami (Inside Bar)
  - Tweezer Top / Tweezer Bottom (double rejection)
  - Three Inside Up / Three Inside Down (Harami + confirmation)

All patterns are boolean columns added to the DataFrame.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


def _body(df: pd.DataFrame) -> pd.Series:
    """Signed body size (positive = bullish, negative = bearish)."""
    return df["Close"] - df["Open"]


def _abs_body(df: pd.DataFrame) -> pd.Series:
    return (df["Close"] - df["Open"]).abs()


def _range(df: pd.DataFrame) -> pd.Series:
    return (df["High"] - df["Low"]).replace(0, np.nan)


def _is_bullish(df: pd.DataFrame) -> pd.Series:
    return df["Close"] > df["Open"]


def _is_bearish(df: pd.DataFrame) -> pd.Series:
    return df["Close"] < df["Open"]


def _is_doji(df: pd.DataFrame, threshold: float = 0.1) -> pd.Series:
    return _abs_body(df) <= _range(df) * threshold


@registry.register("candlestick_patterns")
def add_candlestick_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Detect multi-bar candlestick patterns."""
    enriched = df.copy()

    body = _body(enriched)
    abs_body = _abs_body(enriched)
    rng = _range(enriched)
    bullish = _is_bullish(enriched)
    bearish = _is_bearish(enriched)
    doji = _is_doji(enriched)

    avg_body = abs_body.rolling(10, min_periods=3).mean()

    # -----------------------------------------------------------------------
    # Morning Star (3-bar bullish reversal)
    # Bar 1: large bearish
    # Bar 2: small body (doji or spinning top) that gaps down
    # Bar 3: large bullish that closes into Bar 1's body
    # -----------------------------------------------------------------------
    bar1_bearish = bearish.shift(2) & (abs_body.shift(2) > avg_body.shift(2))
    bar2_small = abs_body.shift(1) < abs_body.shift(2) * 0.4
    bar3_bullish = bullish & (abs_body > avg_body * 0.6)
    bar3_closes_into_bar1 = enriched["Close"] > (enriched["Open"].shift(2) + enriched["Close"].shift(2)) / 2
    enriched["Morning_Star"] = bar1_bearish & bar2_small & bar3_bullish & bar3_closes_into_bar1

    # -----------------------------------------------------------------------
    # Evening Star (3-bar bearish reversal)
    # -----------------------------------------------------------------------
    bar1_bullish = bullish.shift(2) & (abs_body.shift(2) > avg_body.shift(2))
    bar2_small_ev = abs_body.shift(1) < abs_body.shift(2) * 0.4
    bar3_bearish = bearish & (abs_body > avg_body * 0.6)
    bar3_closes_into_bar1_ev = enriched["Close"] < (enriched["Open"].shift(2) + enriched["Close"].shift(2)) / 2
    enriched["Evening_Star"] = bar1_bullish & bar2_small_ev & bar3_bearish & bar3_closes_into_bar1_ev

    # -----------------------------------------------------------------------
    # Three White Soldiers (3 consecutive large bullish candles)
    # Each opens within the previous body and closes higher
    # -----------------------------------------------------------------------
    soldiers_cond = (
        bullish & bullish.shift(1) & bullish.shift(2)
        & (enriched["Close"] > enriched["Close"].shift(1))
        & (enriched["Close"].shift(1) > enriched["Close"].shift(2))
        & (enriched["Open"] > enriched["Open"].shift(1))
        & (enriched["Open"].shift(1) > enriched["Open"].shift(2))
        & (abs_body > avg_body * 0.5)
        & (abs_body.shift(1) > avg_body.shift(1) * 0.5)
    )
    enriched["Three_White_Soldiers"] = soldiers_cond

    # -----------------------------------------------------------------------
    # Three Black Crows (3 consecutive large bearish candles)
    # -----------------------------------------------------------------------
    crows_cond = (
        bearish & bearish.shift(1) & bearish.shift(2)
        & (enriched["Close"] < enriched["Close"].shift(1))
        & (enriched["Close"].shift(1) < enriched["Close"].shift(2))
        & (enriched["Open"] < enriched["Open"].shift(1))
        & (enriched["Open"].shift(1) < enriched["Open"].shift(2))
        & (abs_body > avg_body * 0.5)
        & (abs_body.shift(1) > avg_body.shift(1) * 0.5)
    )
    enriched["Three_Black_Crows"] = crows_cond

    # -----------------------------------------------------------------------
    # Bullish Harami (Inside Bar — small bar inside previous large bearish)
    # -----------------------------------------------------------------------
    prev_large_bear = bearish.shift(1) & (abs_body.shift(1) > avg_body.shift(1))
    current_inside = (
        (enriched["High"] < enriched["High"].shift(1))
        & (enriched["Low"] > enriched["Low"].shift(1))
    )
    enriched["Bullish_Harami"] = prev_large_bear & current_inside & bullish

    # -----------------------------------------------------------------------
    # Bearish Harami
    # -----------------------------------------------------------------------
    prev_large_bull = bullish.shift(1) & (abs_body.shift(1) > avg_body.shift(1))
    enriched["Bearish_Harami"] = prev_large_bull & current_inside & bearish

    # -----------------------------------------------------------------------
    # Tweezer Bottom (two candles with nearly identical lows)
    # -----------------------------------------------------------------------
    low_tolerance = rng * 0.05
    same_low = (enriched["Low"] - enriched["Low"].shift(1)).abs() <= low_tolerance
    enriched["Tweezer_Bottom"] = bearish.shift(1) & bullish & same_low

    # -----------------------------------------------------------------------
    # Tweezer Top (two candles with nearly identical highs)
    # -----------------------------------------------------------------------
    same_high = (enriched["High"] - enriched["High"].shift(1)).abs() <= low_tolerance
    enriched["Tweezer_Top"] = bullish.shift(1) & bearish & same_high

    # -----------------------------------------------------------------------
    # Three Inside Up (Bullish Harami + bullish confirmation bar)
    # -----------------------------------------------------------------------
    enriched["Three_Inside_Up"] = (
        enriched["Bullish_Harami"].shift(1)
        & bullish
        & (enriched["Close"] > enriched["High"].shift(1))
    )

    # -----------------------------------------------------------------------
    # Three Inside Down (Bearish Harami + bearish confirmation bar)
    # -----------------------------------------------------------------------
    enriched["Three_Inside_Down"] = (
        enriched["Bearish_Harami"].shift(1)
        & bearish
        & (enriched["Close"] < enriched["Low"].shift(1))
    )

    return enriched
