from __future__ import annotations

from dataclasses import dataclass, field

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


@dataclass
class LiveRiskState:
    """Stateful risk governor for the live loop.

    The repo already had ``can_open_trade`` and ``should_halt_trading`` but
    nothing called them in a loop, so nothing actually stopped after a bad day
    or capped how many trade ideas were surfaced at once. This binds those rules
    to mutable session state and exposes a single gate, :meth:`can_admit`, that
    the real-time engine checks before marking any signal actionable.
    """

    config: RiskConfig = field(default_factory=RiskConfig)
    starting_equity: float | None = None
    peak_equity: float | None = None
    current_equity: float | None = None
    realized_pnl: float = 0.0
    open_risk: float = 0.0
    force_halt: bool = False
    halt_reason: str = ""

    def __post_init__(self) -> None:
        if self.starting_equity is None:
            self.starting_equity = self.config.capital
        if self.peak_equity is None:
            self.peak_equity = self.starting_equity
        if self.current_equity is None:
            self.current_equity = self.starting_equity

    def begin_cycle(self) -> None:
        """Reset per-broadcast open-risk accounting before re-evaluating signals."""
        self.open_risk = 0.0

    def update_equity(self, current_equity: float) -> None:
        self.current_equity = float(current_equity)
        self.peak_equity = max(self.peak_equity, self.current_equity)
        self.realized_pnl = self.current_equity - self.starting_equity

    @property
    def halted(self) -> bool:
        if self.force_halt:
            return True
        return should_halt_trading(
            self.realized_pnl, self.peak_equity, self.current_equity, self.config
        )

    def can_admit(self, new_trade_risk: float) -> bool:
        """True if a new trade of this rupee-risk may be surfaced right now."""
        if self.halted:
            return False
        return can_open_trade(
            self.open_risk, new_trade_risk, self.config.capital,
            self.config.max_total_open_risk_pct,
        )

    def admit(self, new_trade_risk: float) -> None:
        """Record that a surfaced trade consumes part of the open-risk budget."""
        self.open_risk += max(0.0, float(new_trade_risk))

    def status(self) -> dict:
        reason = self.halt_reason
        if not reason and self.halted and not self.force_halt:
            if self.realized_pnl <= -(self.config.capital * self.config.daily_loss_limit_pct):
                reason = "daily loss limit hit"
            else:
                reason = "max drawdown pause hit"
        return {
            "halted": self.halted,
            "halt_reason": reason,
            "open_risk": round(self.open_risk, 2),
            "open_risk_budget": round(self.config.capital * self.config.max_total_open_risk_pct, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "current_equity": round(self.current_equity, 2),
            "peak_equity": round(self.peak_equity, 2),
        }
