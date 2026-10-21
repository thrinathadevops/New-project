"""Position sizing for index options when capital is whatever is available.

The equity sizing model in :mod:`intraday_advisor.risk` assumes a fixed
``RiskConfig.capital`` and derives share count from a 2%-of-capital risk
budget. That model does not survive contact with index options:

  * You cannot buy a fraction of a lot. Quantity is ``lot_size * lots``, and
    lot_size is set by the exchange (NIFTY 75, BANKNIFTY 35). The smallest
    position you can take may already exceed a 2% risk budget.
  * Capital is not a constant. It is whatever is in the account that morning.

So this module inverts the question. Instead of "how much should I buy given a
risk budget", it answers "given the capital I actually have, what is the real
risk of the smallest position I can take, and is it sane?"

It never blocks a trade. It makes the true risk percentage impossible to miss,
because the number that matters is not the premium or the lot size, it is
**what fraction of your account one stop-out costs you**.

The arithmetic nobody wants to do
---------------------------------
Full capital in one lot with a -20% premium stop means each loss is -20% of the
account (slightly worse after costs). Consecutive losses compound:

    1 loss  ->  -20%     4 losses ->  -59%
    2 losses -> -36%     5 losses ->  -67%
    3 losses -> -49%     8 losses ->  -83%

A 50%-win-rate strategy throws 5 straight losses roughly once every 32 losing
sequences. Recovering from -67% needs a +203% gain. This is why
:func:`plan_position` reports ``losses_to_halve`` on every single signal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from intraday_advisor.ledger.costs import (
    OptionsCostModel,
    breakeven_premium,
    cost_drag_pct,
    net_pnl,
)

# Fallback lot sizes, used ONLY when the broker's instrument master cannot be
# read. Do not trust them: checked against Angel's own master on 2026-10-16,
# every one of these was wrong (NIFTY 75 vs 65, BANKNIFTY 35 vs 30, FINNIFTY
# 65 vs 60, MIDCPNIFTY 140 vs 120) -- a 13-15% error in capital per lot, risk
# and P&L for every calculation downstream.
#
# The exchange revises lot sizes and the broker publishes the current value;
# hardcoding it is guessing when the answer is available. lot_size_for() reads
# the master first and only falls back to this.
FALLBACK_LOT_SIZES: dict[str, int] = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
    "SENSEX": 20,
}

# Kept as the old public name so existing imports keep working.
LOT_SIZES = FALLBACK_LOT_SIZES

# index -> lot size, read once from the broker's instrument master.
_MASTER_CACHE: dict[str, int] = {}


def lot_size_from_broker(index: str) -> int | None:
    """Lot size as the broker publishes it. None if unavailable.

    The instrument master is the authority: it is what the exchange is
    actually trading today, including any revision this file has not heard
    about.
    """
    key = (index or "").strip().upper()
    if key in _MASTER_CACHE:
        return _MASTER_CACHE[key]
    try:
        from intraday_advisor.nse_option_chain import _load_instruments

        instruments = _load_instruments().copy()
        instruments.columns = [c.strip().lower() for c in instruments.columns]
        rows = instruments[
            (instruments["name"].astype(str).str.upper() == key)
            & (instruments["instrumenttype"].astype(str).str.upper() == "OPTIDX")
        ]
        if rows.empty:
            return None
        # The modal value, so one malformed row cannot set the size.
        size = int(rows["lotsize"].astype(str).mode().iloc[0])
        if size > 0:
            _MASTER_CACHE[key] = size
            return size
    except Exception:  # noqa: BLE001 - fall back rather than fail
        return None
    return None

# Risk thresholds for warnings, as a fraction of available capital. These are
# advisory only: nothing here refuses to size a trade.
RISK_PCT_CAUTION = 0.05
RISK_PCT_SEVERE = 0.15

# Reject-worthy when cost drag consumes this much of the gross target move.
COST_DRAG_WARN_FRACTION = 0.25


@dataclass(frozen=True)
class PositionPlan:
    """A sized options position with its true, cost-inclusive risk."""

    # --- what to trade ---
    lots: int
    lot_size: int
    quantity: int                  # lot_size * lots
    entry_premium: float
    stop_premium: float
    target_premium: float

    # --- capital ---
    available_capital: float
    capital_per_lot: float
    deployed_capital: float
    capital_utilisation: float     # deployed / available, 0-1
    leftover_capital: float

    # --- risk and reward, NET of all costs ---
    risk_amount: float             # rupees lost if stop fills
    reward_amount: float           # rupees gained if target fills
    risk_pct_of_capital: float     # the number that actually matters
    reward_pct_of_capital: float
    net_rr: float                  # reward / risk, after costs

    # --- cost reality ---
    cost_drag: float               # round-trip cost as fraction of position
    breakeven: float               # exit premium for zero net P&L
    round_trip_cost: float

    # --- survival maths ---
    losses_to_halve: int           # consecutive stop-outs to lose 50% of capital

    affordable: bool
    warnings: list[str] = field(default_factory=list)
    #: What the deploy cap actually leaves spendable (<= available_capital).
    #: Without this the UI could only show TOTAL capital, so an unaffordable
    #: plan read "needs Rs 11,089 / you have Rs 20,000" -- which looks like a
    #: bug to anyone who can subtract, because the cap was the real
    #: constraint, not the balance.
    spendable_capital: float = 0.0
    #: The deploy cap this plan was sized under, 0-1.
    max_capital_fraction: float = 1.0

    @property
    def risk_band(self) -> str:
        """SAFE / CAUTION / SEVERE, from risk as a share of capital."""
        if not self.affordable:
            return "UNAFFORDABLE"
        if self.risk_pct_of_capital >= RISK_PCT_SEVERE:
            return "SEVERE"
        if self.risk_pct_of_capital >= RISK_PCT_CAUTION:
            return "CAUTION"
        return "SAFE"

    def as_dict(self) -> dict:
        data = self.__dict__.copy()
        data["risk_band"] = self.risk_band
        return data


def lot_size_for(index: str, overrides: dict | None = None) -> int:
    """Lot size for an index: UI override, then the broker, then the fallback.

    The broker's instrument master is consulted first because it is the only
    source that is current by construction.
    """
    key = (index or "").strip().upper()
    if overrides and key in overrides:
        return int(overrides[key])

    from_broker = lot_size_from_broker(key)
    if from_broker:
        return from_broker

    if key not in FALLBACK_LOT_SIZES:
        raise ValueError(
            f"Unknown index {key!r}; pass an explicit lot size. "
            f"Known: {sorted(FALLBACK_LOT_SIZES)}"
        )
    return FALLBACK_LOT_SIZES[key]


def _losses_to_halve(risk_pct: float) -> int:
    """Consecutive stop-outs that take the account below 50% of start."""
    if risk_pct <= 0:
        return 0
    if risk_pct >= 1:
        return 1
    # (1 - r)^n <= 0.5
    return max(1, math.ceil(math.log(0.5) / math.log(1 - risk_pct)))


def suggest_capital_fraction(target_risk_pct: float, stop_pct: float) -> float:
    """Fraction of capital to deploy so one stop-out costs ``target_risk_pct``.

    The arithmetic small accounts cannot escape: when you deploy 100% of
    capital, a -20% premium stop is a -20% account loss **regardless of how
    much capital you have**. More money buys more lots, not less risk. The only
    lever is deploying less.

    To risk 5% with a 20% stop you must deploy 25% of the account -- which, for
    an indivisible lot, may be impossible. :func:`min_capital_for_risk_pct`
    says what that costs.
    """
    if stop_pct <= 0:
        return 1.0
    return max(0.0, min(1.0, target_risk_pct / stop_pct))


def min_capital_for_risk_pct(
    *,
    entry_premium: float,
    lot_size: int,
    target_risk_pct: float,
    stop_pct: float,
) -> float:
    """Capital needed before ONE lot represents only ``target_risk_pct`` of risk.

    This is the number that tells a small account the truth: with one NIFTY lot
    at a Rs 100 premium and a 20% stop, risking 2% per trade requires about
    Rs 7.5 lakh. Below that, index options cannot be traded at textbook risk --
    not because of discipline, but because the lot is indivisible.
    """
    if target_risk_pct <= 0 or stop_pct <= 0:
        return float("inf")
    one_lot_risk = entry_premium * lot_size * stop_pct
    return one_lot_risk / target_risk_pct


def plan_position(
    *,
    available_capital: float,
    entry_premium: float,
    stop_premium: float,
    target_premium: float,
    lot_size: int,
    max_lots: int | None = None,
    max_capital_fraction: float = 1.0,
    costs: OptionsCostModel | None = None,
) -> PositionPlan:
    """Size a long options position against the capital actually on hand.

    ``max_capital_fraction`` caps how much of the account may be deployed
    (1.0 = all of it, which is what "if I have 5k and a lot costs 5k, I invest
    5k" means). Lowering it is the single most effective risk control available
    when the minimum position is one whole lot.

    Risk and reward are computed through the full options cost model, so
    ``risk_amount`` is what actually leaves the account on a stop-out, not the
    textbook ``(entry - stop) * quantity``.
    """
    c = costs or OptionsCostModel()
    warnings: list[str] = []

    available_capital = max(0.0, float(available_capital))
    lot_size = int(lot_size)
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")
    if entry_premium <= 0:
        raise ValueError("entry_premium must be positive")

    capital_per_lot = entry_premium * lot_size
    spendable = available_capital * max(0.0, min(1.0, max_capital_fraction))

    lots = int(spendable // capital_per_lot) if capital_per_lot > 0 else 0
    if max_lots is not None:
        lots = min(lots, int(max_lots))

    affordable = lots >= 1
    if not affordable:
        shortfall = capital_per_lot - spendable
        warnings.append(
            f"Cannot afford one lot: needs Rs {capital_per_lot:,.0f}, "
            f"Rs {spendable:,.0f} available (short Rs {shortfall:,.0f})."
        )

    quantity = lot_size * max(lots, 0)
    deployed = entry_premium * quantity

    # Price the trade at both outcomes through the real cost model. When the
    # position is unaffordable, price one notional lot so the UI can still show
    # what the trade *would* cost and risk.
    pricing_quantity = quantity if quantity > 0 else lot_size

    stop_result = net_pnl(entry_premium, stop_premium, pricing_quantity, costs=c)
    target_result = net_pnl(entry_premium, target_premium, pricing_quantity, costs=c)

    risk_amount = abs(stop_result["net_pnl"])
    reward_amount = target_result["net_pnl"]
    rt_cost = stop_result["total_cost"]

    denominator = available_capital if available_capital > 0 else float("inf")
    risk_pct = risk_amount / denominator
    reward_pct = reward_amount / denominator
    net_rr = (reward_amount / risk_amount) if risk_amount > 0 else 0.0

    drag = cost_drag_pct(entry_premium, pricing_quantity, costs=c)
    be = breakeven_premium(entry_premium, pricing_quantity, costs=c)

    # --- the warnings that matter ---
    if affordable and risk_pct >= RISK_PCT_CAUTION:
        severity = "SEVERE" if risk_pct >= RISK_PCT_SEVERE else "CAUTION"
        warnings.append(
            f"{severity}: one stop-out costs {risk_pct * 100:.1f}% of your capital "
            f"(Rs {risk_amount:,.0f} of Rs {available_capital:,.0f}); "
            f"{_losses_to_halve(risk_pct)} in a row halves the account."
        )
        # Make it actionable rather than just alarming. With more than one lot
        # the fix is to buy fewer; at one lot the lot is indivisible and the
        # only honest answer is "this needs more capital or a cheaper contract".
        effective_stop = abs(entry_premium - stop_premium) / entry_premium
        if lots > 1:
            safer_lots = max(1, int(lots * suggest_capital_fraction(
                RISK_PCT_CAUTION, effective_stop or 1.0)))
            if safer_lots < lots:
                warnings.append(
                    f"Drop to {safer_lots} lot(s) to bring risk near "
                    f"{RISK_PCT_CAUTION * 100:.0f}%."
                )
        elif effective_stop > 0:
            needed = min_capital_for_risk_pct(
                entry_premium=entry_premium, lot_size=lot_size,
                target_risk_pct=RISK_PCT_CAUTION, stop_pct=effective_stop,
            )
            warnings.append(
                f"One lot is the minimum position, so this cannot be reduced. "
                f"Risking {RISK_PCT_CAUTION * 100:.0f}% here would need "
                f"Rs {needed:,.0f} capital, or a cheaper contract."
            )

    if be >= target_premium > 0:
        warnings.append(
            f"Breakeven Rs {be:.2f} is at or above target Rs {target_premium:.2f} - "
            f"this trade cannot make money after costs. Skip it."
        )

    gross_target_move = (target_premium - entry_premium) * pricing_quantity
    if gross_target_move > 0 and rt_cost / gross_target_move >= COST_DRAG_WARN_FRACTION:
        warnings.append(
            f"Costs eat {rt_cost / gross_target_move * 100:.0f}% of the target move "
            f"(Rs {rt_cost:,.0f} of Rs {gross_target_move:,.0f}). "
            f"Premium Rs {entry_premium:.2f} is too cheap for flat brokerage."
        )

    if net_rr > 0 and net_rr < 1.0:
        warnings.append(
            f"Net reward:risk is only {net_rr:.2f}:1 after costs - you risk more "
            f"than you stand to make."
        )

    return PositionPlan(
        lots=max(lots, 0),
        lot_size=lot_size,
        quantity=quantity,
        entry_premium=round(entry_premium, 2),
        stop_premium=round(stop_premium, 2),
        target_premium=round(target_premium, 2),
        available_capital=round(available_capital, 2),
        # What the deploy cap actually leaves available. Without this the UI
        # could only show total capital, so an unaffordable plan read
        # "needs Rs 11,089 / you have Rs 20,000" -- which looks like a bug to
        # anyone who can subtract, because the cap was the real constraint.
        spendable_capital=round(spendable, 2),
        max_capital_fraction=round(float(max(0.0, min(1.0, max_capital_fraction))), 4),
        capital_per_lot=round(capital_per_lot, 2),
        deployed_capital=round(deployed, 2),
        capital_utilisation=round(deployed / denominator, 4) if available_capital > 0 else 0.0,
        leftover_capital=round(max(0.0, available_capital - deployed), 2),
        risk_amount=round(risk_amount, 2),
        reward_amount=round(reward_amount, 2),
        risk_pct_of_capital=round(risk_pct, 4),
        reward_pct_of_capital=round(reward_pct, 4),
        net_rr=round(net_rr, 2),
        cost_drag=round(drag, 5),
        breakeven=round(be, 2),
        round_trip_cost=round(rt_cost, 2),
        losses_to_halve=_losses_to_halve(risk_pct),
        affordable=affordable,
        warnings=warnings,
    )


def describe_plan(plan: PositionPlan) -> str:
    """One-screen summary of a sized position, risk first."""
    if not plan.affordable:
        head = (
            f"UNAFFORDABLE - needs Rs {plan.capital_per_lot:,.0f} for one lot. "
            f"Figures below are for one hypothetical lot."
        )
        risk_label = "Risk would be   "
        reward_label = "Reward would be "
    else:
        head = (
            f"{plan.lots} lot(s) x {plan.lot_size} = {plan.quantity} units "
            f"@ Rs {plan.entry_premium:.2f}"
        )
        risk_label = "Risk if stopped "
        reward_label = "Reward if target"
    lines = [
        head,
        f"  Capital deployed    Rs {plan.deployed_capital:>12,.2f} "
        f"({plan.capital_utilisation * 100:.0f}% of Rs {plan.available_capital:,.0f})",
        f"  {risk_label}    Rs {plan.risk_amount:>12,.2f} "
        f"({plan.risk_pct_of_capital * 100:.1f}% of capital)  [{plan.risk_band}]",
        f"  {reward_label}    Rs {plan.reward_amount:>12,.2f} "
        f"({plan.reward_pct_of_capital * 100:.1f}% of capital)",
        f"  Net reward:risk     {plan.net_rr:>12.2f} : 1   (after all costs)",
        f"  Round-trip cost     Rs {plan.round_trip_cost:>12,.2f} "
        f"({plan.cost_drag * 100:.2f}% of position)",
        f"  Breakeven premium   Rs {plan.breakeven:>12.2f} "
        f"(target Rs {plan.target_premium:.2f})",
        f"  Consecutive stop-outs to halve the account: {plan.losses_to_halve}",
    ]
    lines.extend(f"  ! {w}" for w in plan.warnings)
    return "\n".join(lines)
