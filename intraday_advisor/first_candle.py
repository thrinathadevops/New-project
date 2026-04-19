"""
First Candle Rule Analysis
The opening 5-minute candle (9:30-9:35 AM) contains more information than all others combined.
Sets the day's key levels and guides consistent winning trades with FVG + Engulfing confirmation.

Key Concept:
1. Opening candle (9:30-9:35 AM) on 5-min chart → HIGH/LOW are key levels
2. Watch for price to BREAK these levels and CREATE GAP (Fair Value Gap)
3. Gap must be between candle wicks (not just closure)
4. Wait for ENGULFING candle after gap forms
5. Enter on engulfing with 3:1 Risk:Reward ratio
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass(frozen=True)
class FirstCandleAnalysis:
    """Results from first candle rule analysis"""
    opening_high: float
    opening_low: float
    opening_close: float
    opening_volume: int
    opening_trend: str  # "BULLISH", "BEARISH", "NEUTRAL"
    
    # Gap formation tracking
    has_fvg: bool
    fvg_type: str  # "BULLISH", "BEARISH", "NONE"
    fvg_high: Optional[float]
    fvg_low: Optional[float]
    fvg_formation_level: Optional[float]
    
    # Confirmation signals
    has_engulfing: bool
    engulfing_type: str  # "BULLISH", "BEARISH", "NONE"
    engulfing_bar_index: Optional[int]
    
    # Trading signals
    signal: str  # "BUY", "SELL", "WAIT", "NONE"
    confidence: int  # 0-100
    target_high: Optional[float]
    target_low: Optional[float]
    entry_price: Optional[float]
    stop_level: Optional[float]
    
    # Risk:Reward
    risk_distance: Optional[float]
    reward_distance: Optional[float]
    rr_ratio: Optional[float]


def extract_opening_candle(df: pd.DataFrame, open_hour: int = 9, open_minute: int = 15) -> Optional[dict]:
    """
    Extract the opening 5-minute candle (e.g. 9:15-9:20 for NSE).
    Assumes df index contains time information or has a 'Time' column.
    
    Returns: Dict with OHLCV of opening candle or None if not found
    """
    if df.empty:
        return None
    
    # Try to find candles during the specified opening window
    # This assumes data has time information
    try:
        # If index is datetime
        if hasattr(df.index, 'hour') and hasattr(df.index, 'minute'):
            opening_mask = (df.index.hour == open_hour) & (df.index.minute >= open_minute) & (df.index.minute <= open_minute + 5)
            opening_candles = df[opening_mask]
        else:
            # Fallback: use first candle in dataframe (assuming it's ordered by time)
            # In real implementation, would need actual time data
            opening_candles = df.head(1)
        
        if opening_candles.empty:
            # Use first candle as fallback
            opening_candles = df.head(1)
        
        candle = opening_candles.iloc[0]
        
        return {
            "high": float(candle.get("High", candle.get("high", 0))),
            "low": float(candle.get("Low", candle.get("low", 0))),
            "open": float(candle.get("Open", candle.get("open", 0))),
            "close": float(candle.get("Close", candle.get("close", 0))),
            "volume": int(candle.get("Volume", candle.get("volume", 0))),
        }
    except Exception as e:
        return None


def calculate_opening_trend(opening_candle: dict) -> str:
    """
    Determine if opening candle is bullish or bearish.
    Bullish: close > open
    Bearish: close < open
    Neutral: close ≈ open
    """
    if opening_candle["close"] > opening_candle["open"] * 1.001:
        return "BULLISH"
    elif opening_candle["close"] < opening_candle["open"] * 0.999:
        return "BEARISH"
    else:
        return "NEUTRAL"


def detect_fair_value_gap(df: pd.DataFrame, opening_candle: dict) -> tuple[bool, str, Optional[float], Optional[float], Optional[float]]:
    """
    Detect Fair Value Gap (FVG) formation when price breaks opening levels.
    
    FVG occurs when price creates a gap between candle wicks:
    - Bullish FVG: Price breaks above opening high with wicks not overlapping low
    - Bearish FVG: Price breaks below opening low with wicks not overlapping high
    
    Returns: (has_fvg, fvg_type, fvg_high, fvg_low, formation_level)
    """
    if df.empty or len(df) < 2:
        return False, "NONE", None, None, None
    
    opening_high = opening_candle["high"]
    opening_low = opening_candle["low"]
    
    # Skip opening candle, check rest of day
    df_after_opening = df.iloc[1:]
    
    has_fvg = False
    fvg_type = "NONE"
    fvg_high = None
    fvg_low = None
    formation_level = None
    
    # Look for bullish FVG (break above opening high)
    bullish_check = df_after_opening[df_after_opening["High"] > opening_high]
    if not bullish_check.empty:
        # Check if there's a gap (low of breakout candle doesn't overlap with opening high)
        first_breakout = bullish_check.iloc[0]
        if float(first_breakout["Low"]) > opening_high:  # Gap formed!
            has_fvg = True
            fvg_type = "BULLISH"
            fvg_high = float(first_breakout["High"])
            fvg_low = opening_high  # Gap starts at opening high
            formation_level = opening_high
    
    # Look for bearish FVG (break below opening low)
    bearish_check = df_after_opening[df_after_opening["Low"] < opening_low]
    if not bearish_check.empty and not has_fvg:  # Don't double-count
        # Check if there's a gap (high of breakout candle doesn't overlap with opening low)
        first_breakout = bearish_check.iloc[0]
        if float(first_breakout["High"]) < opening_low:  # Gap formed!
            has_fvg = True
            fvg_type = "BEARISH"
            fvg_high = opening_low  # Gap starts at opening low
            fvg_low = float(first_breakout["Low"])
            formation_level = opening_low
    
    return has_fvg, fvg_type, fvg_high, fvg_low, formation_level


def detect_engulfing_candle(df: pd.DataFrame, fvg_type: str, fvg_formation_index: int = -1) -> tuple[bool, str, Optional[int]]:
    """
    After FVG forms, wait for engulfing candle confirmation.
    
    Engulfing candle:
    - Bullish: Close > Previous High AND Open < Previous Close
    - Bearish: Close < Previous Low AND Open > Previous Close
    
    Returns: (has_engulfing, engulfing_type, bar_index)
    """
    if df.empty or len(df) < 2:
        return False, "NONE", None
    
    # Look at recent candles for engulfing pattern
    # Check last 3-5 candles for engulfing confirmation
    recent_candles = df.tail(5)
    
    for i in range(1, len(recent_candles)):
        current = recent_candles.iloc[i]
        previous = recent_candles.iloc[i - 1]
        
        curr_open = float(current.get("Open", current.get("open", 0)))
        curr_close = float(current.get("Close", current.get("close", 0)))
        curr_low = float(current.get("Low", current.get("low", 0)))
        curr_high = float(current.get("High", current.get("high", 0)))
        
        prev_open = float(previous.get("Open", previous.get("open", 0)))
        prev_close = float(previous.get("Close", previous.get("close", 0)))
        prev_low = float(previous.get("Low", previous.get("low", 0)))
        prev_high = float(previous.get("High", previous.get("high", 0)))
        
        # Bullish engulfing for bullish FVG
        if fvg_type == "BULLISH":
            if curr_close > prev_high and curr_open < prev_close:
                return True, "BULLISH", i
        
        # Bearish engulfing for bearish FVG
        elif fvg_type == "BEARISH":
            if curr_close < prev_low and curr_open > prev_close:
                return True, "BEARISH", i
    
    return False, "NONE", None


def calculate_3_to_1_targets(
    entry_price: float,
    stop_loss: float,
    signal_type: str
) -> tuple[float, float, float]:
    """
    Calculate 3:1 Risk:Reward targets.
    
    Risk distance = entry - stop
    Reward = risk * 3
    Target = entry + (reward if BUY) or entry - (reward if SELL)
    
    Returns: (target_price, risk_distance, reward_distance)
    """
    risk_distance = abs(entry_price - stop_loss)
    reward_distance = risk_distance * 3
    
    if signal_type == "BUY":
        target = entry_price + reward_distance
    else:  # SELL
        target = entry_price - reward_distance
    
    return target, risk_distance, reward_distance


def analyze_first_candle(df: pd.DataFrame, open_hour: int = 9, open_minute: int = 15) -> FirstCandleAnalysis:
    """
    Complete first candle rule analysis.
    
    Process:
    1. Extract opening candle for the market
    2. Determine opening trend
    3. Track for Fair Value Gap formation
    4. Confirm with engulfing candle
    5. Set 3:1 RRR targets
    
    Returns: FirstCandleAnalysis with trade signal or WAIT
    """
    
    # Step 1: Extract opening candle
    opening_candle = extract_opening_candle(df, open_hour, open_minute)
    if opening_candle is None:
        return FirstCandleAnalysis(
            opening_high=0, opening_low=0, opening_close=0, opening_volume=0,
            opening_trend="NEUTRAL",
            has_fvg=False, fvg_type="NONE", fvg_high=None, fvg_low=None, fvg_formation_level=None,
            has_engulfing=False, engulfing_type="NONE", engulfing_bar_index=None,
            signal="NONE", confidence=0,
            target_high=None, target_low=None, entry_price=None, stop_level=None,
            risk_distance=None, reward_distance=None, rr_ratio=None
        )
    
    # Step 2: Determine opening trend
    opening_trend = calculate_opening_trend(opening_candle)
    
    # Step 3: Detect FVG formation
    has_fvg, fvg_type, fvg_high, fvg_low, fvg_formation_level = detect_fair_value_gap(df, opening_candle)
    
    # Step 4: Detect engulfing confirmation
    has_engulfing, engulfing_type, engulfing_bar_index = detect_engulfing_candle(df, fvg_type)
    
    # Step 5: Generate trading signal
    signal = "NONE"
    confidence = 0
    entry_price = None
    stop_level = None
    target_high = None
    target_low = None
    risk_distance = None
    reward_distance = None
    rr_ratio = None
    
    # Signal logic:
    # Bullish: Opening trend bullish + Bullish FVG + Bullish engulfing
    # Bearish: Opening trend bearish + Bearish FVG + Bearish engulfing
    
    if has_fvg and has_engulfing and fvg_type == engulfing_type:
        if fvg_type == "BULLISH":
            signal = "BUY"
            entry_price = fvg_formation_level  # Enter at gap level
            stop_level = opening_candle["low"]  # Stop below opening low
            confidence = 85 if opening_trend == "BULLISH" else 75
            
            # Calculate 3:1 targets
            if entry_price and stop_level:
                target_high, risk_distance, reward_distance = calculate_3_to_1_targets(
                    entry_price, stop_level, "BUY"
                )
                target_low = None
                rr_ratio = 3.0
        
        elif fvg_type == "BEARISH":
            signal = "SELL"
            entry_price = fvg_formation_level  # Enter at gap level
            stop_level = opening_candle["high"]  # Stop above opening high
            confidence = 85 if opening_trend == "BEARISH" else 75
            
            # Calculate 3:1 targets
            if entry_price and stop_level:
                target_low, risk_distance, reward_distance = calculate_3_to_1_targets(
                    entry_price, stop_level, "SELL"
                )
                target_high = None
                rr_ratio = 3.0
    
    elif has_fvg and not has_engulfing:
        signal = "WAIT"  # Waiting for engulfing confirmation
        confidence = 50
    
    elif not has_fvg:
        signal = "WAIT"  # Waiting for gap formation
        confidence = 25
    
    return FirstCandleAnalysis(
        opening_high=opening_candle["high"],
        opening_low=opening_candle["low"],
        opening_close=opening_candle["close"],
        opening_volume=opening_candle["volume"],
        opening_trend=opening_trend,
        has_fvg=has_fvg,
        fvg_type=fvg_type,
        fvg_high=fvg_high,
        fvg_low=fvg_low,
        fvg_formation_level=fvg_formation_level,
        has_engulfing=has_engulfing,
        engulfing_type=engulfing_type,
        engulfing_bar_index=engulfing_bar_index,
        signal=signal,
        confidence=confidence,
        target_high=target_high,
        target_low=target_low,
        entry_price=entry_price,
        stop_level=stop_level,
        risk_distance=risk_distance,
        reward_distance=reward_distance,
        rr_ratio=rr_ratio
    )


def format_first_candle_summary(analysis: FirstCandleAnalysis) -> dict:
    """Format analysis results for display in dashboard"""
    return {
        "OpeningHigh": f"₹{analysis.opening_high:.2f}",
        "OpeningLow": f"₹{analysis.opening_low:.2f}",
        "OpeningClose": f"₹{analysis.opening_close:.2f}",
        "OpeningTrend": analysis.opening_trend,
        "FVGFormed": "Yes" if analysis.has_fvg else "No",
        "FVGType": analysis.fvg_type,
        "FVGLevel": f"₹{analysis.fvg_formation_level:.2f}" if analysis.fvg_formation_level else "N/A",
        "EngulfingConfirmed": "Yes" if analysis.has_engulfing else "No",
        "EngulfingType": analysis.engulfing_type,
        "Signal": analysis.signal,
        "Confidence": f"{analysis.confidence}%",
        "EntryPrice": f"₹{analysis.entry_price:.2f}" if analysis.entry_price else "N/A",
        "StopLoss": f"₹{analysis.stop_level:.2f}" if analysis.stop_level else "N/A",
        "Target": f"₹{analysis.target_high:.2f}" if analysis.target_high else f"₹{analysis.target_low:.2f}",
        "RiskPerShare": f"₹{analysis.risk_distance:.2f}" if analysis.risk_distance else "N/A",
        "RewardPerShare": f"₹{analysis.reward_distance:.2f}" if analysis.reward_distance else "N/A",
        "RiskRewardRatio": f"{analysis.rr_ratio:.1f}:1" if analysis.rr_ratio else "N/A"
    }
