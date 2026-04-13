import pandas as pd

from intraday_advisor.smart_money import analyze_smart_money, build_volume_profile, detect_fair_value_gaps, liquidity_sweep, order_flow_proxy


def test_detects_bullish_fair_value_gap():
    df = pd.DataFrame(
        {
            "Open": [10, 11, 14],
            "High": [11, 12, 16],
            "Low": [9, 10, 13],
            "Close": [10.5, 11.5, 15],
            "Volume": [100, 120, 200],
        }
    )
    gaps = detect_fair_value_gaps(df)
    assert gaps[-1].direction == "bullish"
    assert gaps[-1].lower == 11
    assert gaps[-1].upper == 13


def test_liquidity_sweep_detects_bullish_sweep():
    base = [{"Open": 10, "High": 11, "Low": 9, "Close": 10, "Volume": 100} for _ in range(20)]
    base.append({"Open": 9.5, "High": 10.5, "Low": 8.5, "Close": 9.8, "Volume": 300})
    assert liquidity_sweep(pd.DataFrame(base), lookback=20) == "bullish liquidity sweep"


def test_order_flow_proxy_detects_buyers():
    df = pd.DataFrame(
        {
            "Open": [10] * 20,
            "High": [12] * 20,
            "Low": [9] * 20,
            "Close": [11.8] * 20,
            "Volume": [1000] * 20,
        }
    )
    flow, delta = order_flow_proxy(df)
    assert flow == "buyer-dominant order flow"
    assert delta > 0


def test_volume_profile_returns_point_of_control():
    df = pd.DataFrame(
        {
            "Open": [10, 10, 20],
            "High": [11, 11, 21],
            "Low": [9, 9, 19],
            "Close": [10, 10.2, 20],
            "Volume": [1000, 1500, 100],
        }
    )
    profile = build_volume_profile(df, bins=4)
    assert profile.point_of_control is not None
    assert profile.point_of_control < 15


def test_smart_money_report_pairs_fvg_and_vwap():
    df = pd.DataFrame(
        {
            "Open": [10, 11, 14],
            "High": [11, 12, 16],
            "Low": [9, 10, 13],
            "Close": [10.5, 11.5, 15],
            "Volume": [100, 120, 300],
            "VWAP": [10, 11, 14],
            "EMA9": [10, 11, 14],
            "EMA21": [9, 10, 13],
        }
    )
    report = analyze_smart_money(df)
    assert report.fvg_direction == "bullish"
    assert report.vwap_relation == "above VWAP"
