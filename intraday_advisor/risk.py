from __future__ import annotations

from dataclasses import dataclass

from intraday_advisor.config import RiskConfig


@dataclass(frozen=True)
class TradePlan:
    ticker: str
    direction: str
    entry: float
    stop: float
    target: float
    shares: int
    risk_amount: float
    reward_amount: float
    risk_per_share: float
    notes: str


def calculate_position_size(
    capital: float,
    entry: float,
    stop: float,
    risk_per_trade_pct: float = 0.02,
    max_affordable: bool = True,
) -> int:
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return 0
    risk_budget = capital * risk_per_trade_pct
    shares = int(risk_budget // risk_per_share)
    if max_affordable:
        shares = min(shares, int(capital // entry))
    return max(shares, 0)


def build_trade_plan(
    ticker: str,
    direction: str,
    entry: float,
    atr14: float,
    config: RiskConfig = RiskConfig(),
) -> TradePlan:
    if atr14 <= 0:
        raise ValueError("ATR must be positive to build a risk plan")
    direction_clean = direction.upper()
    stop_distance = config.atr_stop_multiplier * atr14
    if direction_clean == "BUY":
        stop = entry - stop_distance
        target = entry + (stop_distance * config.reward_risk_ratio)
    elif direction_clean == "SELL":
        stop = entry + stop_distance
        target = entry - (stop_distance * config.reward_risk_ratio)
    else:
        raise ValueError("direction must be BUY or SELL")

    shares = calculate_position_size(config.capital, entry, stop, config.risk_per_trade_pct)
    risk_amount = shares * abs(entry - stop)
    reward_amount = shares * abs(target - entry)
    return TradePlan(
        ticker=ticker,
        direction=direction_clean,
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        shares=shares,
        risk_amount=round(risk_amount, 2),
        reward_amount=round(reward_amount, 2),
        risk_per_share=round(abs(entry - stop), 2),
        notes=f"ATR stop {config.atr_stop_multiplier}x, target {config.reward_risk_ratio}R",
    )


def build_trade_plan_from_stop(
    ticker: str,
    direction: str,
    entry: float,
    stop: float,
    config: RiskConfig = RiskConfig(),
) -> TradePlan:
    direction_clean = direction.upper()
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        raise ValueError("Stop must create positive risk per share")
    if direction_clean == "BUY":
        target = entry + risk_per_share * config.reward_risk_ratio
    elif direction_clean == "SELL":
        target = entry - risk_per_share * config.reward_risk_ratio
    else:
        raise ValueError("direction must be BUY or SELL")

    shares = calculate_position_size(config.capital, entry, stop, config.risk_per_trade_pct)
    risk_amount = shares * risk_per_share
    reward_amount = shares * abs(target - entry)
    return TradePlan(
        ticker=ticker,
        direction=direction_clean,
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        shares=shares,
        risk_amount=round(risk_amount, 2),
        reward_amount=round(reward_amount, 2),
        risk_per_share=round(risk_per_share, 2),
        notes=f"Watchlist plan: swing/ATR stop, target {config.reward_risk_ratio}R; invalidate if EMA9 crosses below EMA21",
    )


def can_open_trade(
    current_open_risk: float,
    new_trade_risk: float,
    capital: float,
    max_total_open_risk_pct: float = 0.05,
) -> bool:
    return (current_open_risk + new_trade_risk) <= capital * max_total_open_risk_pct


def should_halt_trading(
    daily_pnl: float,
    peak_equity: float,
    current_equity: float,
    config: RiskConfig = RiskConfig(),
) -> bool:
    daily_loss_hit = daily_pnl <= -(config.capital * config.daily_loss_limit_pct)
    drawdown_hit = current_equity <= peak_equity * (1 - config.max_drawdown_pause_pct)
    return daily_loss_hit or drawdown_hit
