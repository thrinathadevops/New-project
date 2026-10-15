"""Backtesting that does not flatter itself.

What was wrong before
---------------------
The previous implementation decided exits like this::

    price = float(row["Close"])
    if price <= stop:
        exit_reason = "STOP"

Only ``Close`` was ever compared to the stop. ``High`` and ``Low`` were never
read. On a 5-minute bar, price can spike straight through your stop and close
back above it, and that run recorded **no loss at all**. Every intrabar
stop-out was invisible, so the equity curve was systematically too good, and
the error grew with the width of the stop. It also entered at the signal bar's
own close (a price unobtainable once you know the signal exists), never took
shorts, and could not say which of stop/target came first inside one bar.

What this does instead
----------------------
Exits are resolved by :func:`intraday_advisor.fills.resolve_bar_exit` -- the
very same function the live grader calls, not a parallel copy of it: stop tested
against ``Low``, target against ``High``, same-bar conflicts resolved as the
stop, and gaps filled at the open rather than at your level. Entry is the next
bar's open.

Sharing one implementation between backtest and live grading is the point. Two
implementations drift, and the backtest always drifts optimistic.

Why there is no full historical options backtest
------------------------------------------------
There cannot be one, and claiming otherwise would be the dishonest part.

The index-option strategy needs the **option chain** -- PCR, max pain, OI walls
-- as it stood at each decision point. Angel One can return historical option
*candles* for a known contract token, but historical *chain snapshots* are not
retrievable after the fact: open interest at 10:35 last Tuesday is simply gone.
Reconstructing it would mean inventing the main input and then calibrating on
the invention.

So the honest split is:

* :func:`run_backtest` -- historical, for the **underlying** price strategy on
  index or equity candles. Real data, real intrabar fills, no chain.
* :func:`backtest_from_ledger` -- replays the signals the system actually
  emitted, graded against real premium paths, through sequential capital so
  compounding and the indivisible-lot constraint are modelled. This is a
  **forward test**: it only covers days the recorder was running, and it grows
  one session at a time. That is the price of needing chain data.
* :func:`walk_forward_splits` -- out-of-sample partitioning, so weights are
  never fitted and evaluated on the same rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
import pandas as pd

from intraday_advisor.config import CostModel, RiskConfig
from intraday_advisor.fills import BarExit, resolve_bar_exit
from intraday_advisor.risk import calculate_position_size


@dataclass(frozen=True)
class BacktestResult:
    trades: pd.DataFrame
    equity_curve: pd.DataFrame
    metrics: dict[str, float]


def estimate_cost(notional: float, is_sell: bool, costs: CostModel = CostModel()) -> float:
    """Equity-segment round-leg cost. For options use
    :mod:`intraday_advisor.ledger.costs`, whose structure is entirely different.
    """
    brokerage = min(costs.brokerage_flat, notional * costs.brokerage_pct)
    gst = brokerage * costs.gst_pct
    stt = notional * costs.stt_sell_pct if is_sell else 0.0
    slippage = notional * costs.slippage_pct
    return brokerage + gst + stt + slippage


def run_backtest(
    df: pd.DataFrame,
    risk: RiskConfig = RiskConfig(),
    costs: CostModel = CostModel(),
    *,
    next_bar_entry: bool = True,
    close_only_exits: bool = False,
) -> BacktestResult:
    """Backtest a long-only ``Signal`` column with honest intrabar fills.

    ``df`` needs ``Open/High/Low/Close``, ``ATR14`` and ``Signal``.

    ``next_bar_entry=True`` (the default) enters at the bar *after* the signal,
    at its open. ``close_only_exits=False`` (the default) tests stops against
    ``Low`` and targets against ``High``.

    Both flags exist so the **old broken behaviour can be reproduced and
    measured** -- setting ``next_bar_entry=False, close_only_exits=True``
    restores exactly what this module used to do. Neither is realistic; see
    :func:`compare_fill_assumptions`.
    """
    required = {"Open", "High", "Low", "Close", "ATR14", "Signal"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Backtest input missing columns: {sorted(missing)}")

    def exit_for(bar: pd.Series, *, stop: float, target: float) -> BarExit:
        """Resolve an exit, honestly or in the old Close-only way."""
        if not close_only_exits:
            return resolve_bar_exit(bar, stop=stop, target=target)
        # The original bug, reproduced on request: only Close is compared, so
        # an intrabar spike through the stop that closes back above it is
        # invisible and records no loss.
        close = float(bar["Close"])
        if close <= stop:
            return BarExit(True, "STOP", close)
        if close >= target:
            return BarExit(True, "TARGET", close)
        return BarExit(False)

    data = df.dropna(subset=["ATR14", "Signal"]).copy()
    cash = risk.capital
    shares = 0
    entry = stop = target = 0.0
    entry_time = None
    conflicts = 0
    pending_entry = False          # a BUY fired last bar; fill at this open

    trades: list[dict] = []
    equity: list[dict] = []

    for timestamp, row in data.iterrows():
        price = float(row["Close"])
        bar_open = float(row["Open"])
        atr14 = float(row["ATR14"])
        signal = row["Signal"]

        # --- 1. exit an open position, intrabar ---
        if shares:
            outcome = exit_for(row, stop=stop, target=target)
            exit_price = None
            reason = None
            if outcome.hit:
                exit_price, reason = float(outcome.price), outcome.reason
                if outcome.conflict:
                    conflicts += 1
            elif signal == "SELL":
                # Strategy exit: earliest actionable price is this bar's close.
                exit_price, reason = price, "SIGNAL"

            if exit_price is not None:
                notional = shares * exit_price
                cost = estimate_cost(notional, is_sell=True, costs=costs)
                trades.append({
                    "EntryTime": entry_time,
                    "Entry": entry,
                    "ExitTime": timestamp,
                    "Exit": exit_price,
                    "Shares": shares,
                    "PnL": shares * (exit_price - entry) - cost,
                    "Reason": reason,
                    "SameBarConflict": bool(outcome.conflict),
                })
                cash += notional - cost
                shares = 0

        # --- 2. fill an entry armed on the previous bar, at this open ---
        if pending_entry and shares == 0:
            pending_entry = False
            fill = bar_open
            if fill > 0 and atr14 > 0:
                fill_stop = fill - risk.atr_stop_multiplier * atr14
                fill_target = fill + risk.reward_risk_ratio * (fill - fill_stop)
                qty = calculate_position_size(cash, fill, fill_stop, risk.risk_per_trade_pct)
                if qty > 0:
                    notional = qty * fill
                    cost = estimate_cost(notional, is_sell=False, costs=costs)
                    if cash >= notional + cost:
                        cash -= notional + cost
                        shares, entry = qty, fill
                        stop, target = fill_stop, fill_target
                        entry_time = timestamp
                        # A position entered at this open can be stopped out
                        # during this very bar.
                        same_bar = exit_for(row, stop=stop, target=target)
                        if same_bar.hit:
                            exit_price = float(same_bar.price)
                            exit_notional = shares * exit_price
                            exit_cost = estimate_cost(exit_notional, is_sell=True, costs=costs)
                            if same_bar.conflict:
                                conflicts += 1
                            trades.append({
                                "EntryTime": entry_time,
                                "Entry": entry,
                                "ExitTime": timestamp,
                                "Exit": exit_price,
                                "Shares": shares,
                                "PnL": shares * (exit_price - entry) - exit_cost,
                                "Reason": same_bar.reason,
                                "SameBarConflict": bool(same_bar.conflict),
                            })
                            cash += exit_notional - exit_cost
                            shares = 0

        # --- 3. arm a new entry ---
        if shares == 0 and signal == "BUY" and atr14 > 0:
            if next_bar_entry:
                pending_entry = True
            else:
                stop = price - risk.atr_stop_multiplier * atr14
                target = price + risk.reward_risk_ratio * (price - stop)
                qty = calculate_position_size(cash, price, stop, risk.risk_per_trade_pct)
                if qty > 0:
                    notional = qty * price
                    cost = estimate_cost(notional, is_sell=False, costs=costs)
                    if cash >= notional + cost:
                        cash -= notional + cost
                        shares, entry, entry_time = qty, price, timestamp

        equity.append({"Time": timestamp, "Equity": cash + shares * price})

    # Mark any still-open position to the final close, so the curve is complete.
    trades_df = pd.DataFrame(trades)
    equity_df = (
        pd.DataFrame(equity).set_index("Time") if equity
        else pd.DataFrame(columns=["Equity"])
    )
    metrics = compute_metrics(equity_df, trades_df)
    metrics["same_bar_conflicts"] = float(conflicts)
    metrics["open_at_end"] = float(1 if shares else 0)
    return BacktestResult(trades=trades_df, equity_curve=equity_df, metrics=metrics)


def compute_metrics(equity_curve: pd.DataFrame, trades: pd.DataFrame) -> dict[str, float]:
    """Performance statistics for an equity curve and its trade list."""
    empty = {
        "total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0, "profit_factor": 0.0, "trades": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "expectancy": 0.0,
    }
    if equity_curve.empty:
        return empty

    returns = equity_curve["Equity"].pct_change().dropna()
    total_return = (equity_curve["Equity"].iloc[-1] / equity_curve["Equity"].iloc[0] - 1) * 100
    sharpe = (
        0.0 if returns.std() == 0 or returns.empty
        else (returns.mean() / returns.std()) * np.sqrt(252)
    )
    running_max = equity_curve["Equity"].cummax()
    drawdown = (equity_curve["Equity"] / running_max - 1) * 100

    closed = trades.dropna(subset=["PnL"]) if "PnL" in trades else pd.DataFrame()
    wins = closed[closed["PnL"] > 0] if not closed.empty else pd.DataFrame()
    losses = closed[closed["PnL"] <= 0] if not closed.empty else pd.DataFrame()
    gross_profit = float(wins["PnL"].sum()) if not wins.empty else 0.0
    gross_loss = abs(float(losses["PnL"].sum())) if not losses.empty else 0.0

    return {
        "total_return_pct": round(float(total_return), 2),
        "sharpe": round(float(sharpe), 2),
        "max_drawdown_pct": round(float(drawdown.min()), 2),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if len(closed) else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0.0,
        "trades": float(len(closed)),
        "avg_win": round(float(wins["PnL"].mean()), 2) if not wins.empty else 0.0,
        "avg_loss": round(float(losses["PnL"].mean()), 2) if not losses.empty else 0.0,
        "expectancy": round(float(closed["PnL"].mean()), 2) if len(closed) else 0.0,
    }


def compare_fill_assumptions(
    df: pd.DataFrame,
    risk: RiskConfig = RiskConfig(),
    costs: CostModel = CostModel(),
) -> dict:
    """Quantify how much the old Close-only convention was inflating results.

    Runs the same data under realistic fills and under the previous
    same-bar-close entry, and reports the gap. Worth running once on your own
    data: the difference is the size of the lie you were previously trading on.
    """
    honest = run_backtest(df, risk, costs,
                          next_bar_entry=True, close_only_exits=False)
    optimistic = run_backtest(df, risk, costs,
                              next_bar_entry=False, close_only_exits=True)
    return {
        "honest": honest.metrics,
        "old_behaviour": optimistic.metrics,
        "return_overstatement_pct": round(
            optimistic.metrics["total_return_pct"] - honest.metrics["total_return_pct"], 2
        ),
        "win_rate_overstatement_pct": round(
            optimistic.metrics["win_rate_pct"] - honest.metrics["win_rate_pct"], 2
        ),
    }


# --------------------------------------------------------------------------- #
# Ledger replay (the valid options "backtest")
# --------------------------------------------------------------------------- #

def backtest_from_ledger(
    *,
    db_path: str | Path | None = None,
    starting_capital: float = 20_000.0,
    max_capital_fraction: float = 1.0,
    one_position_at_a_time: bool = True,
    min_score: int = 0,
    **filters,
) -> BacktestResult:
    """Replay graded ledger signals through sequential capital.

    Unlike a statistics table, this models the two things that actually decide
    whether an account grows:

    * **indivisible lots** -- when capital cannot buy one lot, the trade is
      skipped, and those skips are counted. A small account misses signals that
      a per-lot expectancy figure happily includes.
    * **sequence** -- losses shrink the capital available to the next trade.
      Expectancy per lot says nothing about the order they arrive in.

    ``one_position_at_a_time`` reflects how a single small account really
    trades: a signal arriving while a position is open is skipped, not stacked.
    """
    from intraday_advisor.ledger.report import load_graded
    from intraday_advisor.ledger.schema import DEFAULT_LEDGER_PATH
    from intraday_advisor.ledger.sizing import plan_position

    frame = load_graded(db_path=db_path or DEFAULT_LEDGER_PATH, **filters)
    if frame.empty:
        return BacktestResult(pd.DataFrame(), pd.DataFrame(columns=["Equity"]),
                              {**compute_metrics(pd.DataFrame(), pd.DataFrame()),
                               "skipped_unaffordable": 0.0, "skipped_busy": 0.0,
                               "skipped_low_score": 0.0})

    frame = frame.sort_values("bar_time")
    capital = float(starting_capital)
    trades: list[dict] = []
    equity: list[dict] = [{"Time": frame["bar_time"].iloc[0], "Equity": capital}]
    skipped = {"unaffordable": 0, "busy": 0, "low_score": 0}
    busy_until: str | None = None

    for _, row in frame.iterrows():
        bar_time = str(row["bar_time"])
        if int(row.get("score") or 0) < min_score:
            skipped["low_score"] += 1
            continue
        if one_position_at_a_time and busy_until and bar_time < busy_until:
            skipped["busy"] += 1
            continue

        fill = row.get("fill_premium")
        if fill is None or float(fill or 0) <= 0:
            continue

        plan = plan_position(
            available_capital=capital,
            entry_premium=float(fill),
            stop_premium=float(row["stop_premium"]),
            target_premium=float(row["target_premium"]),
            lot_size=int(row["lot_size"]),
            max_capital_fraction=max_capital_fraction,
        )
        if not plan.affordable:
            skipped["unaffordable"] += 1
            continue

        # net_pnl_per_lot was graded for ONE lot; scale to the lots taken.
        pnl = float(row["net_pnl_per_lot"] or 0) * plan.lots
        capital += pnl
        busy_until = str(row.get("exit_time") or bar_time)

        trades.append({
            "EntryTime": bar_time,
            "Entry": float(fill),
            "ExitTime": row.get("exit_time"),
            "Exit": row.get("exit_premium"),
            "Shares": plan.quantity,
            "Lots": plan.lots,
            "Score": int(row.get("score") or 0),
            "PnL": pnl,
            "Reason": row.get("outcome"),
            "RiskPct": plan.risk_pct_of_capital,
        })
        equity.append({"Time": bar_time, "Equity": capital})

        if capital <= 0:
            # Account blown. Stop rather than trading imaginary money.
            break

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity).set_index("Time")
    metrics = compute_metrics(equity_df, trades_df)
    metrics.update({
        "skipped_unaffordable": float(skipped["unaffordable"]),
        "skipped_busy": float(skipped["busy"]),
        "skipped_low_score": float(skipped["low_score"]),
        "starting_capital": float(starting_capital),
        "ending_capital": round(float(capital), 2),
        "ruined": float(1 if capital <= 0 else 0),
    })
    return BacktestResult(trades_df, equity_df, metrics)


# --------------------------------------------------------------------------- #
# Walk-forward
# --------------------------------------------------------------------------- #

def walk_forward_splits(
    items: Sequence,
    *,
    n_splits: int = 4,
    min_train: int = 50,
) -> list[tuple[Sequence, Sequence]]:
    """Expanding-window train/test splits, in time order.

    Each split trains on everything before a cut point and tests on the block
    after it, so a parameter is never evaluated on data used to choose it. This
    is the only defence against the failure mode where weights fitted on the
    whole history look excellent and then lose money live.

    Returns ``[(train, test), ...]``; empty if there is not enough data, which
    is itself the answer about whether calibration is premature.
    """
    total = len(items)
    if total < min_train + n_splits:
        return []

    test_size = (total - min_train) // n_splits
    if test_size < 1:
        return []

    splits = []
    for index in range(n_splits):
        train_end = min_train + index * test_size
        test_end = train_end + test_size if index < n_splits - 1 else total
        splits.append((items[:train_end], items[train_end:test_end]))
    return splits


def walk_forward_report(
    evaluate: Callable[[Sequence, Sequence], dict],
    items: Sequence,
    *,
    n_splits: int = 4,
    min_train: int = 50,
) -> pd.DataFrame:
    """Run ``evaluate(train, test)`` over each split and tabulate the results.

    ``evaluate`` fits on ``train`` and returns out-of-sample metrics for
    ``test``. Consistency across folds matters far more than the best fold: one
    good fold out of four is noise.
    """
    splits = walk_forward_splits(items, n_splits=n_splits, min_train=min_train)
    if not splits:
        return pd.DataFrame()

    rows = []
    for index, (train, test) in enumerate(splits, start=1):
        result = dict(evaluate(train, test))
        result.update({"fold": index, "train_n": len(train), "test_n": len(test)})
        rows.append(result)
    return pd.DataFrame(rows)


def ledger_walk_forward(
    *,
    db_path: str | Path | None = None,
    n_splits: int = 4,
    min_train: int = 50,
    **filters,
) -> pd.DataFrame:
    """Out-of-sample expectancy per fold, from the ledger.

    Answers the question that matters before trusting any calibration: does the
    edge hold on data it was not measured on?
    """
    from intraday_advisor.ledger.report import _stats, load_graded
    from intraday_advisor.ledger.schema import DEFAULT_LEDGER_PATH

    frame = load_graded(db_path=db_path or DEFAULT_LEDGER_PATH, **filters)
    if frame.empty:
        return pd.DataFrame()
    frame = frame.sort_values("bar_time").reset_index(drop=True)
    indices = list(range(len(frame)))

    def evaluate(train: Sequence, test: Sequence) -> dict:
        test_frame = frame.iloc[list(test)]
        stats = _stats(test_frame)
        return {
            "test_trades": stats["trades"],
            "test_win_rate": stats["win_rate"],
            "test_expectancy": stats["expectancy"],
            "test_expectancy_low": stats["expectancy_low"],
            "test_profit_factor": stats["profit_factor"],
        }

    return walk_forward_report(evaluate, indices, n_splits=n_splits, min_train=min_train)
