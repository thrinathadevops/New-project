import pandas as pd

from intraday_advisor.data import generate_sample_ohlcv
from intraday_advisor.indicators import add_indicators, atr, rsi


def test_add_indicators_creates_expected_columns():
    df = add_indicators(generate_sample_ohlcv(rows=260))
    for column in ["SMA20", "SMA50", "SMA200", "EMA9", "EMA21", "ATR14", "RSI14", "MACD", "MACDSignal", "OBV", "VWAP", "Momentum10", "RecentSwingHigh", "RecentSwingLow"]:
        assert column in df.columns
    assert df["ATR14"].dropna().gt(0).all()


def test_rsi_is_between_zero_and_hundred():
    values = pd.Series([1, 2, 3, 2, 4, 5, 6, 5, 6, 7, 8, 7, 9, 10, 9, 11, 12, 13, 12, 14])
    output = rsi(values, 14).dropna()
    assert output.between(0, 100).all()


def test_atr_uses_true_range():
    df = pd.DataFrame({"High": [11, 12, 13], "Low": [9, 10, 11], "Close": [10, 11, 12]})
    output = atr(df, 2)
    assert output.dropna().iloc[-1] >= 2
