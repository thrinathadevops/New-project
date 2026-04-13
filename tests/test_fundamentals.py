import pandas as pd

from intraday_advisor.fundamentals import apply_fundamental_quality_screen, merge_fundamentals, normalize_screener_export


def test_screener_quality_filter_keeps_only_strong_fundamentals():
    raw = pd.DataFrame(
        {
            "Symbol": ["GOOD", "DEBTY", "SLOW"],
            "Mar Cap Rs.Cr.": ["9,500", "8,000", "7,000"],
            "ROE %": [25, 30, 22],
            "ROCE %": [24, 35, 25],
            "Debt / Eq.": [0.1, 0.6, 0.05],
            "Sales growth %": [25, 30, 10],
            "Prom. Hold. %": [60, 70, 65],
            "Profit growth %": [35, 45, 40],
            "OPM %": [18, 25, 20],
            "EPS Rs.": [25, 30, 28],
        }
    )
    filtered = apply_fundamental_quality_screen(raw)
    assert filtered["Ticker"].tolist() == ["GOOD"]


def test_screener_normalization_handles_common_column_names():
    raw = pd.DataFrame({"NSE Code": ["abc.ns"], "Market Capitalization": ["1,000"], "Return on equity": ["22%"]})
    normalized = normalize_screener_export(raw)
    assert normalized.loc[0, "Ticker"] == "ABC"
    assert normalized.loc[0, "MarketCapCr"] == 1000
    assert normalized.loc[0, "ROE"] == 22


def test_merge_fundamentals_inner_joins_on_ticker():
    technical = pd.DataFrame({"Ticker": ["GOOD", "MISS"], "Close": [100, 200]})
    fundamentals = pd.DataFrame({"Symbol": ["GOOD"], "ROE %": [25]})
    merged = merge_fundamentals(technical, fundamentals)
    assert merged["Ticker"].tolist() == ["GOOD"]
