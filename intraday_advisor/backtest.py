from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from intraday_advisor.config import CostModel, RiskConfig
from intraday_advisor.risk import calculate_position_size


@dataclass(frozen=True)
class BacktestResult:
    trades: pd.DataFrame
    equity_curve: pd.DataFrame
    metrics: dict[str, float]


def estimate_cost(notional: float, is_sell: bool, costs: CostModel = CostModel()) -> float:
    brokerage = min(costs.brokerage_flat, notional * costs.brokerage_pct)
    gst = brokerage * costs.gst_pct
    stt = notional * costs.stt_sell_pct if is_sell else 0.0
    slippage = notional * costs.slippage_pct
    return brokerage + gst + stt + slippage


def run_backtest(
    df: pd.DataFrame,
    risk: RiskConfig = RiskConfig(),
    costs: CostModel = CostModel(),
) -> BacktestResult:
    data = df.dropna(subset=["ATR14", "Signal"]).copy()
    cash = risk.capital
    shares = 0
    entry = 0.0
    stop = 0.0
    target = 0.0
    trades: list[dict] = []
    equity: list[dict] = []

    for timestamp, row in data.iterrows():
        price = float(row["Close"])
        atr14 = float(row["ATR14"])
        signal = row["Signal"]

        if shares:
            exit_reason = None
            if price <= stop:
                exit_reason = "STOP"
            elif price >= target:
                exit_reason = "TARGET"
            elif signal == "SELL":
                exit_reason = "SIGNAL"

            if exit_reason:
                notional = shares * price
                cost = estimate_cost(notional, is_sell=True, costs=costs)
                pnl = shares * (price - entry) - cost
                cash += notional - cost
                trades.append(
                    {
                        "ExitTime": timestamp,
                        "Exit": price,
                        "Shares": shares,
                        "PnL": pnl,
                        "Reason": exit_reason,
                    }
                )
                shares = 0

        if shares == 0 and signal == "BUY" and atr14 > 0:
            stop = price - risk.atr_stop_multiplier * atr14
            target = price + risk.reward_risk_ratio * (price - stop)
            qty = calculate_position_size(cash, price, stop, risk.risk_per_trade_pct)
            if qty > 0:
                notional = qty * price
                cost = estimate_cost(notional, is_sell=False, costs=costs)
                if cash >= notional + cost:
                    cash -= notional + cost
                    shares = qty
                    entry = price
                    trades.append(
                        {
                            "EntryTime": timestamp,
                            "Entry": entry,
                            "Stop": stop,
                            "Target": target,
                            "Shares": qty,
                            "EntryCost": cost,
                        }
                    )

        equity_value = cash + shares * price
        equity.append({"Time": timestamp, "Equity": equity_value})

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity).set_index("Time") if equity else pd.DataFrame(columns=["Equity"])
    return BacktestResult(trades=trades_df, equity_curve=equity_df, metrics=compute_metrics(equity_df, trades_df))


def compute_metrics(equity_curve: pd.DataFrame, trades: pd.DataFrame) -> dict[str, float]:
    if equity_curve.empty:
        return {"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "win_rate_pct": 0.0, "profit_factor": 0.0}
    returns = equity_curve["Equity"].pct_change().dropna()
    total_return = (equity_curve["Equity"].iloc[-1] / equity_curve["Equity"].iloc[0] - 1) * 100
    sharpe = 0.0 if returns.std() == 0 or returns.empty else (returns.mean() / returns.std()) * np.sqrt(252)
    running_max = equity_curve["Equity"].cummax()
    drawdown = (equity_curve["Equity"] / running_max - 1) * 100
    closed = trades.dropna(subset=["PnL"]) if "PnL" in trades else pd.DataFrame()
    wins = closed[closed["PnL"] > 0]
    losses = closed[closed["PnL"] < 0]
    win_rate = 0.0 if closed.empty else len(wins) / len(closed) * 100
    gross_profit = wins["PnL"].sum() if not wins.empty else 0.0
    gross_loss = abs(losses["PnL"].sum()) if not losses.empty else 0.0
    profit_factor = 0.0 if gross_loss == 0 else gross_profit / gross_loss
    return {
        "total_return_pct": round(float(total_return), 2),
        "sharpe": round(float(sharpe), 2),
        "max_drawdown_pct": round(float(drawdown.min()), 2),
        "win_rate_pct": round(float(win_rate), 2),
        "profit_factor": round(float(profit_factor), 2),
    }

