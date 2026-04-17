import pandas as pd
import numpy as np
from .base import registry

@registry.register("fvg")
def add_fvg(df: pd.DataFrame) -> pd.DataFrame:
    """Fair Value Gaps (Bullish & Bearish)"""
    enriched = df.copy()
    
    # Bullish FVG: Low[n] > High[n-2]
    bull_fvg = enriched["Low"] > enriched["High"].shift(2)
    enriched["Bullish_FVG"] = bull_fvg
    enriched["Bullish_FVG_Lower"] = np.where(bull_fvg, enriched["High"].shift(2), np.nan)
    enriched["Bullish_FVG_Upper"] = np.where(bull_fvg, enriched["Low"], np.nan)

    # Bearish FVG: High[n] < Low[n-2]
    bear_fvg = enriched["High"] < enriched["Low"].shift(2)
    enriched["Bearish_FVG"] = bear_fvg
    enriched["Bearish_FVG_Lower"] = np.where(bear_fvg, enriched["High"], np.nan)
    enriched["Bearish_FVG_Upper"] = np.where(bear_fvg, enriched["Low"].shift(2), np.nan)

    return enriched


@registry.register("order_blocks")
def add_order_blocks(df: pd.DataFrame) -> pd.DataFrame:
    """Order Blocks Detector. 
    Bullish OB: Last down candle before a strong up move breaking structure.
    Bearish OB: Last up candle before a strong down move breaking structure.
    Simplified mathematically by looking at range and momentum shifts.
    """
    enriched = df.copy()
    
    # Identify down/up candles
    is_down = enriched["Close"] < enriched["Open"]
    is_up = enriched["Close"] > enriched["Open"]
    
    # Strong move detection
    strong_up = (enriched["Close"] - enriched["Open"]) > (enriched["ATR14"] * 1.5)
    strong_down = (enriched["Open"] - enriched["Close"]) > (enriched["ATR14"] * 1.5)
    
    enriched["Bullish_OB"] = is_down.shift(1) & strong_up
    enriched["Bearish_OB"] = is_up.shift(1) & strong_down
    
    return enriched


@registry.register("liquidity_sweeps")
def add_liquidity_sweeps(df: pd.DataFrame) -> pd.DataFrame:
    """Liquidity Sweeps: Wick extends past a recent swing level but closes within range"""
    enriched = df.copy()
    
    prior_highs = enriched["RecentSwingHigh"]
    prior_lows = enriched["RecentSwingLow"]
    
    # Bullish Sweep: Low < Prev Low, but Close > Prev Low
    bullish_sweep = (enriched["Low"] < prior_lows) & (enriched["Close"] > prior_lows)
    
    # Bearish Sweep: High > Prev High, but Close < Prev High
    bearish_sweep = (enriched["High"] > prior_highs) & (enriched["Close"] < prior_highs)
    
    enriched["Liquidity_Sweep_Bullish"] = bullish_sweep
    enriched["Liquidity_Sweep_Bearish"] = bearish_sweep

    return enriched
