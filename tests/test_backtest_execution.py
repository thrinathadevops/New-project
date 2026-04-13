from intraday_advisor.backtest import estimate_cost, run_backtest
from intraday_advisor.config import CostModel, RiskConfig
from intraday_advisor.data import generate_sample_ohlcv
from intraday_advisor.execution import PaperBroker
from intraday_advisor.indicators import add_indicators
from intraday_advisor.risk import build_trade_plan
from intraday_advisor.signals import moving_average_pullback_signals


def test_estimate_cost_includes_sell_side_stt():
    buy_cost = estimate_cost(10_000, is_sell=False, costs=CostModel())
    sell_cost = estimate_cost(10_000, is_sell=True, costs=CostModel())
    assert sell_cost > buy_cost


def test_backtest_returns_metrics():
    df = moving_average_pullback_signals(add_indicators(generate_sample_ohlcv(rows=260)))
    result = run_backtest(df, RiskConfig(capital=20_000))
    assert "sharpe" in result.metrics
    assert "max_drawdown_pct" in result.metrics


def test_paper_broker_accepts_valid_plan():
    broker = PaperBroker()
    plan = build_trade_plan("RELIANCE", "BUY", 100, 2, RiskConfig(capital=20_000))
    order = broker.place_order(plan)
    assert order.status == "ACCEPTED"
    assert broker.order_log()[0]["ticker"] == "RELIANCE"

