from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    brokerage_flat: float = 20.0
    brokerage_pct: float = 0.0003
    gst_pct: float = 0.18
    stt_sell_pct: float = 0.00025
    slippage_pct: float = 0.0005


@dataclass(frozen=True)
class RiskConfig:
    capital: float = 20_000.0
    risk_per_trade_pct: float = 0.02
    max_total_open_risk_pct: float = 0.05
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pause_pct: float = 0.10
    atr_stop_multiplier: float = 1.5
    reward_risk_ratio: float = 2.0

