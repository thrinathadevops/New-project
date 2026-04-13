from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class PriceActionReport:
    trend: str
    support: float | None
    resistance: float | None
    candle_pattern: str
    candle_bias: str
    volume_confirmation: str
    breakout_state: str
    entry_zone: float | None
    stop_zone: float | None
    reasons: list[str]
    warnings: list[str]


def _swing_highs(df: pd.DataFrame, lookback: int = 2) -> pd.Series:
    highs = df["High"]
    mask = pd.Series(True, index=df.index)
    for offset in range(1, lookback + 1):
        mask &= (highs > highs.shift(offset)) & (highs > highs.shift(-offset))
    return highs.where(mask)


def _swing_lows(df: pd.DataFrame, lookback: int = 2) -> pd.Series:
    lows = df["Low"]
    mask = pd.Series(True, index=df.index)
    for offset in range(1, lookback + 1):
        mask &= (lows < lows.shift(offset)) & (lows < lows.shift(-offset))
    return lows.where(mask)


def identify_trend_structure(df: pd.DataFrame, lookback: int = 2) -> str:
    highs = _swing_highs(df, lookback).dropna().tail(2)
    lows = _swing_lows(df, lookback).dropna().tail(2)
    if len(highs) < 2 or len(lows) < 2:
        return "sideways/unclear"
    if highs.iloc[-1] > highs.iloc[-2] and lows.iloc[-1] > lows.iloc[-2]:
        return "uptrend"
    if highs.iloc[-1] < highs.iloc[-2] and lows.iloc[-1] < lows.iloc[-2]:
        return "downtrend"
    return "sideways/unclear"


def support_resistance_zones(df: pd.DataFrame, window: int = 20) -> tuple[float | None, float | None]:
    recent = df.tail(window)
    if recent.empty:
        return None, None
    return float(recent["Low"].min()), float(recent["High"].max())


def candlestick_psychology(df: pd.DataFrame) -> tuple[str, str]:
    if len(df) < 2:
        return "unknown", "not enough candles"
    prev = df.iloc[-2]
    last = df.iloc[-1]
    body = abs(last["Close"] - last["Open"])
    candle_range = max(last["High"] - last["Low"], 1e-9)
    upper_wick = last["High"] - max(last["Open"], last["Close"])
    lower_wick = min(last["Open"], last["Close"]) - last["Low"]
    bullish = last["Close"] > last["Open"]
    bearish = last["Close"] < last["Open"]

    if body <= candle_range * 0.12:
        return "doji", "indecision between buyers and sellers"
    if lower_wick >= body * 2 and upper_wick <= body and bullish:
        return "bullish pin bar", "sellers pushed down, buyers absorbed and won"
    if upper_wick >= body * 2 and lower_wick <= body and bearish:
        return "bearish pin bar", "buyers pushed up, sellers rejected the move"
    if bullish and prev["Close"] < prev["Open"] and last["Close"] > prev["Open"] and last["Open"] < prev["Close"]:
        return "bullish engulfing", "buyers took control from sellers"
    if bearish and prev["Close"] > prev["Open"] and last["Close"] < prev["Open"] and last["Open"] > prev["Close"]:
        return "bearish engulfing", "sellers took control from buyers"
    if bullish and upper_wick > body:
        return "bullish candle with upper rejection", "buyers won but showed weakness near the high"
    if bearish and lower_wick > body:
        return "bearish candle with lower rejection", "sellers won but buyers defended the low"
    if bullish:
        return "bullish candle", "buyers in control"
    if bearish:
        return "bearish candle", "sellers in control"
    return "neutral candle", "balanced candle"


def volume_confirmation(df: pd.DataFrame, volume_window: int = 20) -> str:
    if len(df) < volume_window + 1:
        return "insufficient volume history"
    last = df.iloc[-1]
    previous = df.iloc[-2]
    avg_volume = df["Volume"].tail(volume_window).mean()
    price_up = last["Close"] > previous["Close"]
    price_down = last["Close"] < previous["Close"]
    volume_up = last["Volume"] > avg_volume
    volume_down = last["Volume"] < avg_volume

    if price_up and volume_up:
        return "bullish confirmation"
    if price_up and volume_down:
        return "weak breakout risk"
    if price_down and volume_up:
        return "bearish pressure"
    if price_down and volume_down:
        return "weak selling pressure"
    return "neutral volume"


def breakout_retest_state(df: pd.DataFrame, resistance: float | None, support: float | None, tolerance_pct: float = 0.002) -> tuple[str, float | None]:
    if len(df) < 2 or resistance is None or support is None:
        return "not enough levels", None
    last = df.iloc[-1]
    prev = df.iloc[-2]
    tolerance = resistance * tolerance_pct
    broke_resistance = prev["Close"] <= resistance and last["Close"] > resistance
    retested_resistance = last["Low"] <= resistance + tolerance and last["Close"] > resistance
    broke_support = prev["Close"] >= support and last["Close"] < support

    if broke_resistance:
        return "resistance breakout", resistance
    if retested_resistance:
        return "break and retest holding", resistance
    if broke_support:
        return "support breakdown", support
    return "inside range", None


def analyze_price_action(df: pd.DataFrame) -> PriceActionReport:
    usable = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if usable.empty:
        return PriceActionReport("unknown", None, None, "unknown", "not enough candles", "unknown", "unknown", None, None, [], ["No OHLCV data."])

    trend = identify_trend_structure(usable)
    support, resistance = support_resistance_zones(usable.iloc[:-1], 20) if len(usable) > 1 else support_resistance_zones(usable, 20)
    pattern, bias = candlestick_psychology(usable)
    volume = volume_confirmation(usable)
    breakout, level = breakout_retest_state(usable, resistance, support)
    last = usable.iloc[-1]
    reasons: list[str] = [f"trend structure: {trend}", f"candle: {pattern} ({bias})", f"volume: {volume}", f"breakout: {breakout}"]
    warnings: list[str] = []

    if trend != "uptrend":
        warnings.append("trend is not a clean higher-high/higher-low uptrend")
    if volume in {"weak breakout risk", "insufficient volume history"}:
        warnings.append("breakout lacks strong volume confirmation")
    if "bearish" in pattern:
        warnings.append("latest candle shows seller control or rejection")
    if breakout == "inside range":
        warnings.append("price has not broken a key range yet")

    entry_zone = float(level) if level is not None else None
    stop_zone = None
    if support is not None:
        stop_zone = float(min(support, last["Low"]))
    return PriceActionReport(trend, support, resistance, pattern, bias, volume, breakout, entry_zone, stop_zone, reasons, warnings)

