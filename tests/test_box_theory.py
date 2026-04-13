import pandas as pd

from intraday_advisor.box_theory import analyze_box_theory, box_zone, john_wick_signal


def _frame(last_candle):
    rows = [
        ("2026-04-09 09:15", 100, 110, 90, 105, 1000),
        ("2026-04-10 09:15", *last_candle, 1500),
    ]
    return pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"]).assign(Date=lambda df: pd.to_datetime(df["Date"])).set_index("Date")


def test_box_zone_marks_middle_as_no_trade():
    zone, middle = box_zone(price=100, previous_high=110, previous_low=90)
    assert zone == "middle no-trade zone"
    assert middle == 100


def test_bullish_john_wick_at_bottom_creates_buy_watch():
    df = _frame((94, 96, 88, 95))
    report = analyze_box_theory(df)
    assert report.zone == "bottom buy-only zone"
    assert report.wick_signal == "bullish john wick"
    assert report.bias == "BUY WATCH"


def test_bearish_john_wick_at_top_creates_sell_watch():
    df = _frame((106, 112, 104, 105))
    report = analyze_box_theory(df)
    assert report.zone == "top sell-only zone"
    assert report.wick_signal == "bearish john wick"
    assert report.bias == "SELL WATCH"


def test_john_wick_signal_returns_none_without_rejection():
    row = pd.Series({"Open": 100, "High": 103, "Low": 99, "Close": 102})
    assert john_wick_signal(row) == "none"
