"""Advanced volume analysis indicators.

Adds:
  - VWMA (Volume Weighted Moving Average)
  - Accumulation/Distribution Line
  - Chaikin Money Flow (CMF)
  - Money Flow Index (MFI)
  - Volume Oscillator
  - Volume-Price Trend (VPT)
  - Elder's Force Index

All register via the indicator registry.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from .base import registry


# ---------------------------------------------------------------------------
# VWMA (Volume Weighted Moving Average)
# ---------------------------------------------------------------------------

@registry.register("vwma")
def add_vwma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """VWMA — moving average weighted by volume.

    When VWMA > SMA, smart money is buying (volume concentrates on up-bars).
    When VWMA < SMA, institutions are distributing.
    """
    enriched = df.copy()
    vol = enriched["Volume"].replace(0, np.nan)
    enriched["VWMA20"] = (enriched["Close"] * vol).rolling(period, min_periods=period).sum() / vol.rolling(period, min_periods=period).sum()
    return enriched


# ---------------------------------------------------------------------------
# Accumulation/Distribution Line
# ---------------------------------------------------------------------------

@registry.register("ad_line")
def add_ad_line(df: pd.DataFrame) -> pd.DataFrame:
    """Accumulation/Distribution Line — unlike OBV, weights volume by WHERE
    in the candle's range the close fell.

    Close near high + high volume = accumulation.
    Close near low + high volume = distribution.
    Divergence vs price = reversal warning.
    """
    enriched = df.copy()
    hl_range = (enriched["High"] - enriched["Low"]).replace(0, np.nan)
    clv = ((enriched["Close"] - enriched["Low"]) - (enriched["High"] - enriched["Close"])) / hl_range
    enriched["AD_Line"] = (clv * enriched["Volume"]).fillna(0).cumsum()
    return enriched


# ---------------------------------------------------------------------------
# Chaikin Money Flow (CMF)
# ---------------------------------------------------------------------------

@registry.register("cmf")
def add_cmf(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Chaikin Money Flow — A/D as a bounded oscillator (-1 to +1).

    CMF > 0 = buying pressure dominates.
    CMF < 0 = selling pressure dominates.
    Divergence from price = early warning of trend change.
    """
    enriched = df.copy()
    hl_range = (enriched["High"] - enriched["Low"]).replace(0, np.nan)
    clv = ((enriched["Close"] - enriched["Low"]) - (enriched["High"] - enriched["Close"])) / hl_range
    mf_volume = clv * enriched["Volume"]
    enriched["CMF20"] = mf_volume.rolling(period, min_periods=period).sum() / enriched["Volume"].rolling(period, min_periods=period).sum().replace(0, np.nan)
    return enriched


# ---------------------------------------------------------------------------
# Money Flow Index (MFI)
# ---------------------------------------------------------------------------

@registry.register("mfi")
def add_mfi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Money Flow Index — volume-weighted RSI.

    MFI > 80 = overbought (potential distribution).
    MFI < 20 = oversold (potential accumulation).
    Catches divergences that OBV and standard RSI miss.
    """
    enriched = df.copy()
    typical = (enriched["High"] + enriched["Low"] + enriched["Close"]) / 3
    raw_mf = typical * enriched["Volume"]
    delta = typical.diff()

    positive_mf = raw_mf.where(delta > 0, 0.0)
    negative_mf = raw_mf.where(delta < 0, 0.0)

    pos_sum = positive_mf.rolling(period, min_periods=period).sum()
    neg_sum = negative_mf.rolling(period, min_periods=period).sum().replace(0, np.nan)

    mf_ratio = pos_sum / neg_sum
    enriched["MFI14"] = 100 - (100 / (1 + mf_ratio))
    return enriched


# ---------------------------------------------------------------------------
# Volume Oscillator
# ---------------------------------------------------------------------------

@registry.register("volume_oscillator")
def add_volume_oscillator(df: pd.DataFrame, short: int = 5, long: int = 20) -> pd.DataFrame:
    """Volume Oscillator — percentage difference between short and long volume MAs.

    Positive and rising = volume trend is expanding (supports the price move).
    Negative = volume is contracting (trend weakening).
    """
    enriched = df.copy()
    short_ma = enriched["Volume"].rolling(short, min_periods=short).mean()
    long_ma = enriched["Volume"].rolling(long, min_periods=long).mean()
    enriched["Vol_Osc"] = ((short_ma - long_ma) / long_ma.replace(0, np.nan)) * 100
    return enriched


# ---------------------------------------------------------------------------
# Volume-Price Trend (VPT)
# ---------------------------------------------------------------------------

@registry.register("vpt")
def add_vpt(df: pd.DataFrame) -> pd.DataFrame:
    """Volume-Price Trend — combines price change percentage with volume.

    Better than OBV because it accounts for the SIZE of the price move.
    Rising VPT confirms uptrend; falling VPT confirms downtrend.
    Divergence = trend reversal warning.
    """
    enriched = df.copy()
    pct_change = enriched["Close"].pct_change().fillna(0)
    enriched["VPT"] = (pct_change * enriched["Volume"]).cumsum()
    enriched["VPT_Signal"] = enriched["VPT"].ewm(span=9, adjust=False).mean()
    return enriched


# ---------------------------------------------------------------------------
# Elder's Force Index
# ---------------------------------------------------------------------------

@registry.register("force_index")
def add_force_index(df: pd.DataFrame, period: int = 13) -> pd.DataFrame:
    """Elder's Force Index — price change × volume.

    Positive = buyers dominating.
    Negative = sellers dominating.
    Crossing zero = trend change confirmation.
    """
    enriched = df.copy()
    raw_force = enriched["Close"].diff() * enriched["Volume"]
    enriched["Force_Index"] = raw_force.ewm(span=period, adjust=False, min_periods=period).mean()
    return enriched
