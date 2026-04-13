import pandas as pd

from intraday_advisor.screening import apply_screen, score_stocks


def test_apply_screen_filters_illiquid_rows():
    df = pd.DataFrame(
        {
            "Close": [100, 40],
            "ADTV20": [600_000, 700_000],
            "MarketCap": [30_000_000_000, 30_000_000_000],
            "ATR14": [2, 1],
            "FloatPct": [30, 30],
        }
    )
    screened = apply_screen(df)
    assert len(screened) == 1
    assert screened.iloc[0]["Close"] == 100


def test_score_stocks_orders_high_score_first():
    df = pd.DataFrame(
        {
            "Close": [120, 100],
            "SMA20": [110, 105],
            "SMA50": [100, 110],
            "ADTV20": [900_000, 600_000],
            "ATR14": [3, 1],
            "Momentum10": [5, -2],
            "RSI14": [55, 80],
        }
    )
    scored = score_stocks(df)
    assert scored.iloc[0]["Close"] == 120

