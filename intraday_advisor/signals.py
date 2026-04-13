from __future__ import annotations

import pandas as pd


def moving_average_pullback_signals(df: pd.DataFrame) -> pd.DataFrame:
    signals = df.copy()
    above_sma20 = signals["Close"] > signals["SMA20"]
    crossed_up = above_sma20 & (~above_sma20.shift(1, fill_value=False))
    high_volume = signals["Volume"] > signals["ADTV20"]
    rsi_recovered = signals["RSI14"].between(45, 70)
    macd_positive = signals["MACD"] > signals["MACDSignal"]

    below_sma20 = signals["Close"] < signals["SMA20"]
    crossed_down = below_sma20 & (~below_sma20.shift(1, fill_value=False))
    rsi_weak = signals["RSI14"].between(30, 55)
    macd_negative = signals["MACD"] < signals["MACDSignal"]

    signals["Signal"] = "HOLD"
    signals.loc[crossed_up & high_volume & rsi_recovered & macd_positive, "Signal"] = "BUY"
    signals.loc[crossed_down & high_volume & rsi_weak & macd_negative, "Signal"] = "SELL"
    return signals


def latest_signal(df: pd.DataFrame) -> str:
    usable = df.dropna(subset=["ATR14", "SMA20", "RSI14", "MACD", "MACDSignal"])
    if usable.empty:
        return "HOLD"
    return str(usable.iloc[-1].get("Signal", "HOLD"))
