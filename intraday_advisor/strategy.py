from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from intraday_advisor.price_action import analyze_price_action


@dataclass(frozen=True)
class AnalysisDecision:
    ticker: str
    signal: str
    confidence: int
    setup: str
    reasons: list[str]
    warnings: list[str]


def apply_ema_swing_breakout_strategy(df: pd.DataFrame, swing_window: int = 10) -> pd.DataFrame:
    strategy = df.copy()
    if "RecentSwingHigh" not in strategy:
        strategy["RecentSwingHigh"] = strategy["High"].shift(1).rolling(swing_window, min_periods=3).max()
    if "RecentSwingLow" not in strategy:
        strategy["RecentSwingLow"] = strategy["Low"].shift(1).rolling(swing_window, min_periods=3).min()

    strategy["BullishEMACross"] = (strategy["EMA9"] > strategy["EMA21"]) & (strategy["EMA9"].shift(1) <= strategy["EMA21"].shift(1))
    strategy["BearishEMACross"] = (strategy["EMA9"] < strategy["EMA21"]) & (strategy["EMA9"].shift(1) >= strategy["EMA21"].shift(1))
    strategy["EMABreakoutLevel"] = pd.NA
    strategy["Signal"] = "HOLD"
    strategy["StrategyState"] = "WAIT"

    armed_level: float | None = None
    in_position = False

    for index, row in strategy.iterrows():
        if bool(row["BearishEMACross"]):
            armed_level = None
            if in_position:
                strategy.at[index, "Signal"] = "SELL"
                strategy.at[index, "StrategyState"] = "EMA_EXIT"
                in_position = False
            else:
                strategy.at[index, "StrategyState"] = "WAIT"
            continue

        if bool(row["BullishEMACross"]) and pd.notna(row["RecentSwingHigh"]):
            armed_level = float(row["RecentSwingHigh"])
            strategy.at[index, "EMABreakoutLevel"] = armed_level
            strategy.at[index, "StrategyState"] = "ARMED"

        if armed_level is not None and not in_position:
            strategy.at[index, "EMABreakoutLevel"] = armed_level
            if float(row["Close"]) > armed_level:
                strategy.at[index, "Signal"] = "BUY"
                strategy.at[index, "StrategyState"] = "BREAKOUT_ENTRY"
                in_position = True
                armed_level = None
            elif strategy.at[index, "StrategyState"] == "WAIT":
                strategy.at[index, "StrategyState"] = "ARMED"
        elif in_position and strategy.at[index, "StrategyState"] == "WAIT":
            strategy.at[index, "StrategyState"] = "IN_POSITION"

    return strategy


def ema_swing_breakout_decision(ticker: str, df: pd.DataFrame) -> AnalysisDecision:
    strategy = apply_ema_swing_breakout_strategy(df)
    usable = strategy.dropna(subset=["Close", "EMA9", "EMA21", "RecentSwingHigh", "RecentSwingLow", "ATR14"])
    if usable.empty:
        return AnalysisDecision(ticker, "HOLD", 0, "Insufficient data", [], ["Need EMA9, EMA21, swing levels, and ATR before analysis."])

    last = usable.iloc[-1]
    price_action = analyze_price_action(strategy)
    reasons: list[str] = []
    warnings: list[str] = list(price_action.warnings)

    if last["EMA9"] > last["EMA21"]:
        reasons.append("EMA9 is above EMA21")
    else:
        reasons.append("EMA9 is below EMA21")

    if pd.notna(last.get("EMABreakoutLevel")):
        reasons.append(f"breakout level is {float(last['EMABreakoutLevel']):.2f}")

    reasons.extend(price_action.reasons)

    if last["Close"] > last["VWAP"]:
        reasons.append("price is above VWAP")
    else:
        warnings.append("price is below VWAP confirmation")

    if last["Volume"] > last["ADTV20"]:
        reasons.append("volume is above 20-bar average")
    else:
        warnings.append("volume confirmation is missing")

    if last["RSI14"] > 72:
        warnings.append("RSI is stretched; avoid chasing")
    elif last["RSI14"] < 40:
        warnings.append("RSI momentum is weak")
    else:
        reasons.append("RSI is in a tradable zone")

    if last["Signal"] == "BUY":
        price_action_ok = price_action.trend == "uptrend" and price_action.volume_confirmation == "bullish confirmation"
        confidence = 88 if price_action_ok else 72
        return AnalysisDecision(ticker, "BUY", max(55, confidence - len(warnings) * 5), "EMA9/EMA21 swing-high breakout + price action", reasons, warnings)
    if last["Signal"] == "SELL":
        return AnalysisDecision(ticker, "SELL", 80, "EMA9 crossed below EMA21 exit", ["EMA9 crossed below EMA21"], warnings)
    if last["StrategyState"] == "ARMED":
        return AnalysisDecision(ticker, "HOLD", 60, "EMA crossover armed; waiting for swing-high break", reasons, warnings)
    if last["StrategyState"] == "IN_POSITION":
        return AnalysisDecision(ticker, "HOLD", 70, "In position; hold until EMA9 crosses below EMA21", reasons, warnings)
    return AnalysisDecision(ticker, "HOLD", 45, "Waiting for EMA9/EMA21 crossover", reasons, warnings)


def confluence_decision(ticker: str, df: pd.DataFrame) -> AnalysisDecision:
    usable = df.dropna(subset=["Close", "VWAP", "SMA20", "SMA50", "ATR14", "RSI14", "MACD", "MACDSignal", "ADTV20"])
    if usable.empty:
        return AnalysisDecision(ticker, "HOLD", 0, "Insufficient data", [], ["Need more candles before analysis."])

    last = usable.iloc[-1]
    previous = usable.iloc[-2] if len(usable) > 1 else last
    bullish: list[str] = []
    bearish: list[str] = []
    warnings: list[str] = []

    if last["Close"] > last["VWAP"]:
        bullish.append("price above VWAP")
    else:
        bearish.append("price below VWAP")

    if last["SMA20"] > last["SMA50"]:
        bullish.append("short trend above medium trend")
    else:
        bearish.append("short trend below medium trend")

    if 45 <= last["RSI14"] <= 68:
        bullish.append("RSI in tradable momentum zone")
    elif last["RSI14"] > 72:
        warnings.append("RSI is stretched; avoid chasing.")
    elif last["RSI14"] < 35:
        bearish.append("RSI is weak")

    if last["MACD"] > last["MACDSignal"] and previous["MACD"] <= previous["MACDSignal"]:
        bullish.append("fresh MACD bullish cross")
    elif last["MACD"] > last["MACDSignal"]:
        bullish.append("MACD momentum positive")
    else:
        bearish.append("MACD momentum negative")

    if last["Volume"] > last["ADTV20"]:
        bullish.append("volume above 20-bar average")
    else:
        warnings.append("volume confirmation is missing.")

    atr_pct = last["ATR14"] / last["Close"]
    if atr_pct < 0.004:
        warnings.append("ATR is low; reward may not justify costs.")
    elif atr_pct > 0.035:
        warnings.append("ATR is high; reduce size or skip.")

    if last["Close"] > last["SMA20"] and previous["Close"] <= previous["SMA20"]:
        bullish.append("price crossed above SMA20")
    if last["Close"] < last["SMA20"] and previous["Close"] >= previous["SMA20"]:
        bearish.append("price crossed below SMA20")

    buy_score = len(bullish)
    sell_score = len(bearish)
    if buy_score >= 5 and buy_score > sell_score:
        signal = "BUY"
        setup = "Long confluence"
        confidence = min(95, 45 + buy_score * 8 - len(warnings) * 5)
        reasons = bullish
    elif sell_score >= 4 and sell_score > buy_score:
        signal = "SELL"
        setup = "Short confluence"
        confidence = min(95, 45 + sell_score * 8 - len(warnings) * 5)
        reasons = bearish
    else:
        signal = "HOLD"
        setup = "No clean edge"
        confidence = max(0, 40 + abs(buy_score - sell_score) * 5 - len(warnings) * 5)
        reasons = bullish + bearish

    return AnalysisDecision(ticker, signal, int(confidence), setup, reasons, warnings)
