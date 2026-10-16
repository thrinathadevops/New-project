"""Advanced Classic Candlestick Formations & Reversals.

Adds high-reliability classic Japanese candlestick patterns:
  - Hammer & Inverted Hammer (bottom rejections)
  - Shooting Star & Hanging Man (top rejections)
  - Bullish & Bearish Marubozu (strong conviction directional candles)
  - Piercing Line & Dark Cloud Cover (potent 2-candle counter-trend thrusts)
  - Inside Bar & Inside Bar Breakout (the classic Mother-Child volatility compression)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


@registry.register("advanced_candlesticks")
def add_advanced_candlesticks(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates classic Japanese candlestick formations with trend context."""
    enriched = df.copy()
    n = len(enriched)
    if n < 2:
        return enriched

    open_p = enriched["Open"]
    high = enriched["High"]
    low = enriched["Low"]
    close = enriched["Close"]

    body = (close - open_p).abs()
    rng = (high - low).replace(0, 1e-9)
    upper_wick = high - np.maximum(open_p, close)
    lower_wick = np.minimum(open_p, close) - low

    is_green = close > open_p
    is_red = close < open_p

    # Recent swing context (5-bar lookback)
    swing_low_5 = low.shift(1).rolling(5, min_periods=2).min()
    swing_high_5 = high.shift(1).rolling(5, min_periods=2).max()

    # 1. Hammer:
    # Small body at the upper end of the bar, lower wick >= 2x body, upper wick <= 0.2x body
    # Occurs at or near a swing low
    is_hammer = (
        (lower_wick >= 2.0 * body)
        & (upper_wick <= 0.25 * body)
        & (body >= 0.1 * rng)
        & (low <= swing_low_5 * 1.002)
    )

    # 2. Inverted Hammer:
    # Small body at lower end, upper wick >= 2x body, lower wick <= 0.25x body, at swing low
    is_inverted_hammer = (
        (upper_wick >= 2.0 * body)
        & (lower_wick <= 0.25 * body)
        & (body >= 0.1 * rng)
        & (low <= swing_low_5 * 1.002)
    )

    # 3. Shooting Star:
    # Bearish pin bar at swing high: upper wick >= 2x body, lower wick <= 0.25x body
    is_shooting_star = (
        (upper_wick >= 2.0 * body)
        & (lower_wick <= 0.25 * body)
        & (body >= 0.1 * rng)
        & (high >= swing_high_5 * 0.998)
    )

    # 4. Hanging Man:
    # Small body at top, lower wick >= 2x body, at swing high (warning of top exhaustion)
    is_hanging_man = (
        (lower_wick >= 2.0 * body)
        & (upper_wick <= 0.25 * body)
        & (body >= 0.1 * rng)
        & (high >= swing_high_5 * 0.998)
    )

    # 5. Marubozu (Bullish & Bearish):
    # Body comprises >= 85% of the total range (conviction candle, no indecision)
    is_bull_marubozu = is_green & (body >= 0.85 * rng)
    is_bear_marubozu = is_red & (body >= 0.85 * rng)

    # 6. Piercing Line:
    # 2-candle pattern: Prior is red, current is green, opens below prior low and closes > 50% into prior red body
    prev_open = open_p.shift(1)
    prev_close = close.shift(1)
    prev_low = low.shift(1)
    prev_high = high.shift(1)
    prev_red = prev_close < prev_open
    prev_green = prev_close > prev_open

    is_piercing_line = (
        prev_red
        & is_green
        & (open_p < prev_low)
        & (close > (prev_open + prev_close) / 2.0)
        & (close < prev_open)
    )

    # 7. Dark Cloud Cover:
    # 2-candle pattern: Prior is green, current is red, opens above prior high and closes > 50% into prior green body
    is_dark_cloud = (
        prev_green
        & is_red
        & (open_p > prev_high)
        & (close < (prev_open + prev_close) / 2.0)
        & (close > prev_close)
    )

    # 8. Inside Bar & Breakout (Mother-Child Bar):
    # Current bar is inside prior bar
    is_inside_bar = (high < prev_high) & (low > prev_low)

    # Inside Bar Breakout: Prior bar was Inside Bar, current bar breaks and closes above/below Mother Bar
    # fillna on an object-dtype boolean series triggers a deprecated
    # downcast; cast first so the series stays boolean.
    prev_inside = is_inside_bar.shift(1).astype('boolean').fillna(False).astype(bool)
    mother_high = high.shift(2)
    mother_low = low.shift(2)
    inside_breakout_bull = prev_inside & (close > mother_high)
    inside_breakout_bear = prev_inside & (close < mother_low)

    enriched["Candle_Hammer"] = is_hammer
    enriched["Candle_Inverted_Hammer"] = is_inverted_hammer
    enriched["Candle_Shooting_Star"] = is_shooting_star
    enriched["Candle_Hanging_Man"] = is_hanging_man
    enriched["Candle_Marubozu_Bull"] = is_bull_marubozu
    enriched["Candle_Marubozu_Bear"] = is_bear_marubozu
    enriched["Candle_Piercing_Line"] = is_piercing_line
    enriched["Candle_Dark_Cloud"] = is_dark_cloud
    enriched["Candle_Inside_Bar"] = is_inside_bar
    enriched["Candle_Inside_Breakout_Bull"] = inside_breakout_bull
    enriched["Candle_Inside_Breakout_Bear"] = inside_breakout_bear

    return enriched
