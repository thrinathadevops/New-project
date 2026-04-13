from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    previous_close = df["Close"].shift(1)
    true_range = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - previous_close).abs(),
            (df["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return pd.DataFrame(
        {
            "MACD": macd_line,
            "MACDSignal": signal_line,
            "MACDHist": macd_line - signal_line,
        },
        index=series.index,
    )


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["Close"].diff()).fillna(0)
    return (direction * df["Volume"]).cumsum()


def vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    cumulative_volume = df["Volume"].cumsum()
    return (typical_price * df["Volume"]).cumsum() / cumulative_volume.replace(0, np.nan)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    enriched = df.copy()
    enriched["SMA20"] = sma(enriched["Close"], 20)
    enriched["SMA50"] = sma(enriched["Close"], 50)
    enriched["SMA200"] = sma(enriched["Close"], 200)
    enriched["EMA9"] = ema(enriched["Close"], 9)
    enriched["EMA21"] = ema(enriched["Close"], 21)
    enriched["EMA12"] = ema(enriched["Close"], 12)
    enriched["EMA26"] = ema(enriched["Close"], 26)
    enriched["ATR14"] = atr(enriched, 14)
    enriched["RSI14"] = rsi(enriched["Close"], 14)
    enriched = enriched.join(macd(enriched["Close"]))
    enriched["OBV"] = obv(enriched)
    enriched["VWAP"] = vwap(enriched)
    enriched["Momentum10"] = enriched["Close"] - enriched["Close"].shift(10)
    enriched["ADTV20"] = enriched["Volume"].rolling(20, min_periods=20).mean()
    enriched["Volatility10"] = enriched["Close"].pct_change().rolling(10, min_periods=10).std()
    enriched["RecentSwingHigh"] = enriched["High"].shift(1).rolling(10, min_periods=3).max()
    enriched["RecentSwingLow"] = enriched["Low"].shift(1).rolling(10, min_periods=3).min()
    return enriched
