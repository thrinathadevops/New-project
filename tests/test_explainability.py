from intraday_advisor.explainability import decision_guide, theory_info_rows


def test_theory_info_rows_include_box_and_risk():
    rows = theory_info_rows()
    names = {row["Theory"] for row in rows}
    assert "Box Theory" in names
    assert "Risk Plan" in names


def test_decision_guide_returns_buy_watch_for_clean_buy():
    action, reason = decision_guide(
        {
            "AboveEMA200": True,
            "Signal": "BUY",
            "BoxZone": "bottom buy-only zone",
            "BoxBias": "BUY WATCH",
            "VWAPRelation": "above VWAP",
            "OrderFlow": "buyer-dominant order flow",
            "VolumeConfirmation": "bullish confirmation",
        }
    )
    assert action == "BUY WATCH"
    assert "above EMA200" in reason


def test_decision_guide_avoids_middle_box():
    action, reason = decision_guide(
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
