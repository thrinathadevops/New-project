"""Volatility band indicators.

Adds:
  - Bollinger Bands (20, 2)
  - Keltner Channels (20, 1.5 ATR)
  - TTM Squeeze (Bollinger inside Keltner = consolidation about to break)
  - Donchian Channels (20)
  - Parabolic SAR

All register via the indicator registry.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

@registry.register("bollinger_bands")
def add_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands — SMA ± 2 standard deviations.

    Band squeeze (narrow bands) predicts an imminent large move.
    Price riding the upper band = strong uptrend ("band walk").
    Price touching lower band in uptrend = potential dip-buy.
    %B measures where price is within the bands (0 = lower, 1 = upper).
    Bandwidth measures the squeeze quantitatively.
    """
    enriched = df.copy()
    sma = enriched["Close"].rolling(period, min_periods=period).mean()
    std = enriched["Close"].rolling(period, min_periods=period).std()

    enriched["BB_Upper"] = sma + (std_dev * std)
    enriched["BB_Middle"] = sma
    enriched["BB_Lower"] = sma - (std_dev * std)
    enriched["BB_PctB"] = (enriched["Close"] - enriched["BB_Lower"]) / (enriched["BB_Upper"] - enriched["BB_Lower"]).replace(0, np.nan)
    enriched["BB_Width"] = (enriched["BB_Upper"] - enriched["BB_Lower"]) / enriched["BB_Middle"].replace(0, np.nan)
    return enriched


# ---------------------------------------------------------------------------
# Keltner Channels
# ---------------------------------------------------------------------------

@registry.register("keltner_channels")
def add_keltner_channels(df: pd.DataFrame, ema_period: int = 20, atr_mult: float = 1.5) -> pd.DataFrame:
    """Keltner Channels — EMA ± ATR multiplier.

    Used with Bollinger to detect the TTM Squeeze.
    Breakout above upper channel = strong bullish momentum.
    """
    enriched = df.copy()
    ema = enriched["Close"].ewm(span=ema_period, adjust=False, min_periods=ema_period).mean()
    atr = enriched.get("ATR14")
    if atr is None:
        prev_close = enriched["Close"].shift(1)
        tr = pd.concat([
            enriched["High"] - enriched["Low"],
            (enriched["High"] - prev_close).abs(),
            (enriched["Low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    enriched["KC_Upper"] = ema + (atr_mult * atr)
    enriched["KC_Middle"] = ema
    enriched["KC_Lower"] = ema - (atr_mult * atr)
    return enriched


# ---------------------------------------------------------------------------
# TTM Squeeze
# ---------------------------------------------------------------------------

@registry.register("ttm_squeeze")
def add_ttm_squeeze(df: pd.DataFrame) -> pd.DataFrame:
    """TTM Squeeze — detects when Bollinger Bands are INSIDE Keltner Channels.

    Squeeze ON (True) = low volatility consolidation, breakout imminent.
    Squeeze OFF (False) = volatility has expanded, trend is running.
    Momentum histogram shows direction of the breakout.
    """
    enriched = df.copy()

    # Ensure dependencies exist
    if "BB_Upper" not in enriched:
        enriched = add_bollinger_bands(enriched)
    if "KC_Upper" not in enriched:
        enriched = add_keltner_channels(enriched)

    # Squeeze is ON when Bollinger is inside Keltner
    enriched["Squeeze_On"] = (enriched["BB_Lower"] > enriched["KC_Lower"]) & (enriched["BB_Upper"] < enriched["KC_Upper"])

    # Momentum: linear regression value of (close - midline of Donchian+SMA)
    highest = enriched["High"].rolling(20, min_periods=20).max()
    lowest = enriched["Low"].rolling(20, min_periods=20).min()
    mid_donchian = (highest + lowest) / 2
    mid_bb = enriched["BB_Middle"]
    enriched["Squeeze_Mom"] = enriched["Close"] - (mid_donchian + mid_bb) / 2

    return enriched


# ---------------------------------------------------------------------------
# Donchian Channels
# ---------------------------------------------------------------------------

@registry.register("donchian_channels")
def add_donchian_channels(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Donchian Channels — highest high / lowest low of the last N bars.

    The original turtle-trading breakout system.
    Break above upper = buy, break below lower = sell.
    Middle line serves as a trailing stop reference.
    """
    enriched = df.copy()
    enriched["Donchian_Upper"] = enriched["High"].rolling(period, min_periods=period).max()
    enriched["Donchian_Lower"] = enriched["Low"].rolling(period, min_periods=period).min()
    enriched["Donchian_Middle"] = (enriched["Donchian_Upper"] + enriched["Donchian_Lower"]) / 2
    return enriched


# ---------------------------------------------------------------------------
# Parabolic SAR
# ---------------------------------------------------------------------------

@registry.register("parabolic_sar")
def add_parabolic_sar(df: pd.DataFrame, af_start: float = 0.02, af_step: float = 0.02,
                      af_max: float = 0.20) -> pd.DataFrame:
    """Parabolic SAR — trailing stop that flips direction.

    Dots below price = uptrend, use as trailing stop for longs.
    Dots above price = downtrend, use as trailing stop for shorts.
    A flip is a trend-change signal.
    """
    enriched = df.copy()
    high = enriched["High"].values
    low = enriched["Low"].values
    close = enriched["Close"].values
    n = len(df)

    psar = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)  # 1 = up, -1 = down
    af = af_start
    ep = low[0]  # extreme point

    psar[0] = high[0]
    direction[0] = -1

    for i in range(1, n):
        if direction[i - 1] == 1:  # was uptrend
            psar[i] = psar[i - 1] + af * (ep - psar[i - 1])
            psar[i] = min(psar[i], low[i - 1])
            if i >= 2:
                psar[i] = min(psar[i], low[i - 2])

            if low[i] < psar[i]:  # flip to downtrend
                direction[i] = -1
                psar[i] = ep
                af = af_start
                ep = low[i]
            else:
                direction[i] = 1
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else:  # was downtrend
            psar[i] = psar[i - 1] + af * (ep - psar[i - 1])
            psar[i] = max(psar[i], high[i - 1])
            if i >= 2:
                psar[i] = max(psar[i], high[i - 2])

            if high[i] > psar[i]:  # flip to uptrend
                direction[i] = 1
                psar[i] = ep
                af = af_start
                ep = high[i]
            else:
                direction[i] = -1
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)

    enriched["PSAR"] = psar
    enriched["PSAR_Dir"] = direction  # 1 = bullish, -1 = bearish
    return enriched
