from intraday_advisor.explainability import decision_guide, theory_info_rows


def test_theory_info_rows_include_box_and_risk():
    rows = theory_info_rows()
    names = {row["Theory"] for row in rows}
    assert "Box Theory" in names
    assert "Risk Plan" in names


def test_decision_guide_returns_buy_watch_for_clean_buy():
    action, reason, option_suggestion = decision_guide(
        {
            "AboveEMA200": True,
            "Signal": "BUY",
            "BoxZone": "bottom buy-only zone",
            "BoxBias": "BUY WATCH",
            "VWAPRelation": "above VWAP",
            "OrderFlow": "buyer-dominant order flow",
            "VolumeConfirmation": "bullish confirmation",
            "Close": 500.0,
        }
    )
    assert action == "BUY WATCH"
    assert "above EMA200" in reason
    assert "CE" in option_suggestion  # a clean BUY WATCH suggests a call option


def test_decision_guide_avoids_middle_box():
    action, reason, option_suggestion = decision_guide(
        {
            "AboveEMA200": True,
            "Signal": "BUY",
            "BoxZone": "middle no-trade zone",
            "VWAPRelation": "above VWAP",
            "OrderFlow": "buyer-dominant order flow",
            "VolumeConfirmation": "bullish confirmation",
        }
    )
    assert action == "WAIT / AVOID"
    assert "box middle" in reason
    assert option_suggestion == "No Options Trade"  # no trade when blocked
