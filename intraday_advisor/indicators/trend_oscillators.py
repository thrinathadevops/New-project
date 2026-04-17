import pandas as pd
import numpy as np
from .base import registry

@registry.register("adaptive_macd")
def add_adaptive_macd(df: pd.DataFrame) -> pd.DataFrame:
    """Adaptive MACD: Uses volatility to scale the moving averages dynamically."""
    enriched = df.copy()
    
    # Calculate regular MACD fast/slow
    if "EMA12" not in enriched:
        enriched["EMA12"] = enriched["Close"].ewm(span=12, adjust=False).mean()
    if "EMA26" not in enriched:
        enriched["EMA26"] = enriched["Close"].ewm(span=26, adjust=False).mean()
        
    macd_line = enriched["EMA12"] - enriched["EMA26"]
    
    # Adapt using ATR as multiplier for signal line smoothing
    atr = enriched["ATR14"] if "ATR14" in enriched else df["High"] - df["Low"]
    volatility_ratio = atr / atr.rolling(20).mean()
    volatility_ratio = volatility_ratio.fillna(1).clip(0.5, 2.0)
    
    # Simple dynamic span approximation for signal
    signal_line = macd_line.ewm(span=9, adjust=False).mean() * volatility_ratio
    
    enriched["Adaptive_MACD"] = macd_line
    enriched["Adaptive_MACD_Signal"] = signal_line
    enriched["Adaptive_MACD_Hist"] = macd_line - signal_line
    
    return enriched


@registry.register("supertrend")
def add_supertrend(df: pd.DataFrame) -> pd.DataFrame:
    """SuperTrend Indicator based on ATR and factor 3."""
    enriched = df.copy()
    period = 10
    multiplier = 3.0
    
    hl2 = (enriched["High"] + enriched["Low"]) / 2
    atr = enriched["ATR14"] if "ATR14" in enriched else df["High"] - df["Low"]
    
    basic_ub = hl2 + (multiplier * atr)
    basic_lb = hl2 - (multiplier * atr)
    
    # We use vectorised logic instead of iterrows for performance
    # Initializing SuperTrend arrays
    final_ub = basic_ub.copy()
    final_lb = basic_lb.copy()
    supertrend = pd.Series(index=enriched.index, dtype='float64')
    st_direction = pd.Series(1, index=enriched.index, dtype='int64')
    
    for i in range(1, len(enriched)):
        if enriched["Close"].iloc[i-1] <= final_ub.iloc[i-1]:
            final_ub.iloc[i] = min(basic_ub.iloc[i], final_ub.iloc[i-1])
            
        if enriched["Close"].iloc[i-1] >= final_lb.iloc[i-1]:
            final_lb.iloc[i] = max(basic_lb.iloc[i], final_lb.iloc[i-1])
            
        if enriched["Close"].iloc[i] > final_ub.iloc[i-1]:
            st_direction.iloc[i] = 1
        elif enriched["Close"].iloc[i] < final_lb.iloc[i-1]:
            st_direction.iloc[i] = -1
        else:
            st_direction.iloc[i] = st_direction.iloc[i-1]
            
        if st_direction.iloc[i] == 1:
            supertrend.iloc[i] = final_lb.iloc[i]
        else:
            supertrend.iloc[i] = final_ub.iloc[i]
            
    enriched["SuperTrend"] = supertrend
    enriched["SuperTrend_Dir"] = st_direction
    return enriched
