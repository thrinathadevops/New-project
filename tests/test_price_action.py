import pandas as pd

from intraday_advisor.price_action import analyze_price_action, candlestick_psychology, identify_trend_structure, volume_confirmation


def test_trend_structure_identifies_uptrend():
    df = pd.DataFrame(
        {
            "High": [10, 12, 11, 14, 13, 16, 15],
            "Low": [8, 9, 8.5, 10, 9.5, 12, 11],
            "Open": [9, 10, 10, 12, 12, 14, 14],
            "Close": [9.5, 11, 10, 13, 12, 15, 14],
            "Volume": [100] * 7,
        }
    )
    assert identify_trend_structure(df, lookback=1) == "uptrend"


def test_candlestick_psychology_detects_bullish_engulfing():
    df = pd.DataFrame(
        {
            "Open": [10, 8.5],
            "High": [10.5, 11.5],
            "Low": [8, 8],
            "Close": [9, 11],
            "Volume": [100, 200],
        }
    )
    pattern, bias = candlestick_psychology(df)
    assert pattern == "bullish engulfing"
    assert "buyers" in bias


def test_volume_confirmation_detects_bullish_move():
    df = pd.DataFrame(
        {
            "Open": [10] * 21,
            "High": [11] * 21,
            "Low": [9] * 21,
            "Close": [10] * 20 + [12],
            "Volume": [100] * 20 + [500],
        }
    )
    assert volume_confirmation(df) == "bullish confirmation"


def test_analyze_price_action_reports_breakout():
    df = pd.DataFrame(
        {
            "Open": [10, 10, 11, 12, 13],
            "High": [11, 12, 13, 14, 16],
            "Low": [9, 9.5, 10, 11, 12],
            "Close": [10.5, 11, 12, 13, 15],
            "Volume": [100, 100, 100, 100, 500],
        }
    )
    report = analyze_price_action(df)
    assert report.breakout_state == "resistance breakout"
    assert report.resistance == 14
