from intraday_advisor.config import RiskConfig
from intraday_advisor.risk import build_trade_plan, calculate_position_size, can_open_trade, should_halt_trading


def test_position_size_limits_by_risk_budget_and_cash():
    assert calculate_position_size(20_000, entry=100, stop=98, risk_per_trade_pct=0.02) == 200
    assert calculate_position_size(20_000, entry=500, stop=490, risk_per_trade_pct=0.02) == 40


def test_trade_plan_calculates_target_and_risk():
    plan = build_trade_plan("INFY", "BUY", 100, 2, RiskConfig(capital=20_000, risk_per_trade_pct=0.02))
    assert plan.stop == 97
    assert plan.target == 106
    assert plan.shares == 133
    assert plan.risk_amount == 399


def test_risk_circuit_breakers():
    assert can_open_trade(200, 300, 20_000, 0.05)
    assert not can_open_trade(800, 300, 20_000, 0.05)
    assert should_halt_trading(-700, 22_000, 21_500, RiskConfig(capital=20_000, daily_loss_limit_pct=0.03))

