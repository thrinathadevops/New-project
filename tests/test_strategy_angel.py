import os

import pandas as pd
import pytest

from intraday_advisor.angel_one import AngelCredentials, AngelOneClient
from intraday_advisor.config import RiskConfig
from intraday_advisor.data import generate_sample_ohlcv
from intraday_advisor.indicators import add_indicators
from intraday_advisor.risk import build_trade_plan
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy, confluence_decision, ema_swing_breakout_decision


def test_confluence_decision_returns_explainable_signal():
    df = add_indicators(generate_sample_ohlcv(rows=260))
    decision = confluence_decision("RELIANCE", df)
    assert decision.signal in {"BUY", "SELL", "HOLD"}
    assert 0 <= decision.confidence <= 95
    assert decision.setup


def test_ema_swing_breakout_waits_for_swing_high_break():
    df = pd.DataFrame(
        {
            "High": [10, 11, 12, 13, 12],
            "Low": [8, 9, 10, 11, 10],
            "Close": [9, 10, 11, 13, 11],
            "Volume": [1000, 1000, 1000, 1500, 1000],
            "EMA9": [8, 9, 11, 12, 9],
            "EMA21": [10, 10, 10, 10, 10],
            "RecentSwingHigh": [12, 12, 12, 12, 12],
            "RecentSwingLow": [8, 8, 8, 8, 8],
            "ATR14": [1, 1, 1, 1, 1],
            "RSI14": [50, 50, 55, 58, 45],
            "VWAP": [9, 9.5, 10.5, 11, 11],
            "ADTV20": [1000, 1000, 1000, 1000, 1000],
        }
    )
    output = apply_ema_swing_breakout_strategy(df)
    assert output.loc[2, "Signal"] == "HOLD"
    assert output.loc[2, "StrategyState"] == "ARMED"
    assert output.loc[3, "Signal"] == "BUY"
    assert output.loc[4, "Signal"] == "SELL"


def test_ema_swing_decision_reports_latest_buy():
    df = pd.DataFrame(
        {
            "High": [10, 11, 12, 13],
            "Low": [8, 9, 10, 11],
            "Close": [9, 10, 11, 13],
            "Volume": [1000, 1000, 1000, 1500],
            "EMA9": [8, 9, 11, 12],
            "EMA21": [10, 10, 10, 10],
            "RecentSwingHigh": [12, 12, 12, 12],
            "RecentSwingLow": [8, 8, 8, 8],
            "ATR14": [1, 1, 1, 1],
            "RSI14": [50, 50, 55, 58],
            "VWAP": [9, 9.5, 10.5, 11],
            "ADTV20": [1000, 1000, 1000, 1000],
        }
    )
    decision = ema_swing_breakout_decision("TEST", df)
    assert decision.signal == "BUY"
    assert "swing-high breakout" in decision.setup


def test_angel_live_order_is_blocked_without_enable_flag(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "NO")
    client = AngelOneClient(AngelCredentials("api", "client", "pin", "secret"))
    plan = build_trade_plan("RELIANCE", "BUY", 100, 2, RiskConfig(capital=20_000))
    with pytest.raises(PermissionError):
        client.place_intraday_order(plan, symbol_token="2885", live_confirmed=True)


def test_angel_credentials_require_env(monkeypatch):
    for key in ["ANGEL_ONE_API_KEY", "ANGEL_ONE_CLIENT_CODE", "ANGEL_ONE_PIN", "ANGEL_ONE_TOTP_SECRET"]:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError):
        AngelCredentials.from_env()


def test_angel_symbol_search_prefers_exact_equity_match():
    class FakeSmartApi:
        def searchScrip(self, exchange, query):
            return {
                "status": True,
                "data": [
                    {"tradingsymbol": "RELIANCE-BL", "symboltoken": "999"},
                    {"tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885"},
                ],
            }

    client = AngelOneClient(AngelCredentials("api", "client", "pin", "secret"))
    client.smart_api = FakeSmartApi()
    assert client.search_symbol_token("RELIANCE") == "2885"
