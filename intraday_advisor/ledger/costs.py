"""Real NSE F&O cost model for index options.

Why this exists
---------------
``intraday_advisor.config.CostModel`` is shaped for **equity** trading: flat
brokerage, STT on sell turnover, percentage slippage. Applying it to index
options understates costs badly, because for options:

  * STT is charged on **premium**, not on notional (strike x lot), and only on
    the sell leg.
  * Exchange transaction charges are a percentage of **premium turnover**.
  * Brokerage is **flat per order** (Rs 20), which means it is a *huge*
    percentage cost on cheap OTM contracts and a trivial one on expensive ITM
    contracts. A percentage-only model hides this completely.
  * The dominant real-world cost for a retail options buyer is the **bid-ask
    spread**, not any statutory charge. It is modelled explicitly here.

Worked example (why this matters)
---------------------------------
Round-trip cost as a % of position value, NIFTY lot 75, default rates,
including a 0.25% half-spread:

    premium Rs 100 -> position Rs 7,500  -> cost ~Rs 99   -> 1.3%
    premium Rs  20 -> position Rs 1,500  -> cost ~Rs 58   -> 3.8%
    premium Rs  10 -> position Rs   750  -> cost ~Rs 56   -> 7.5%

Same trade structure, ~6x the cost drag between the first and last, purely
because Rs 20-per-order brokerage does not scale down with premium. A +25%
target on a Rs 10 premium needs a 7.5% move just to break even; the same +25%
on a Rs 100 premium needs 1.3%. No percentage-only cost model can see this,
which is exactly why signal grading has to run through this module.

VERIFY THESE RATES
------------------
Statutory rates change with every budget and exchange circular. The defaults
below are best-known values for FY2025-26 and are **deliberately configurable**.
Do not trust them blindly: take one real contract note, run
:func:`describe_costs` for that trade, and compare. :func:`calibrate_from_fill`
turns an actual round-trip cost into a matching model. Grading signals with
wrong costs produces a confidently wrong edge estimate, which is worse than no
estimate at all.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# NSE options tick size (rupees). Premiums quote in 5-paise steps, so the
# tightest possible bid-ask spread is one tick.
TICK_SIZE = 0.05


@dataclass(frozen=True)
class OptionsCostModel:
    """Per-leg cost rates for NSE index options (F&O segment).

    All percentage rates are fractions of **premium turnover**
    (premium x quantity), which is how the exchange and the tax actually work
    for options. Defaults assume a discount broker on flat per-order pricing.
    """

    # --- Brokerage ---
    # For NSE F&O OPTIONS, Indian discount brokers (Angel One, Zerodha, Upstox)
    # charge a FLAT Rs 20 per executed order with no percentage alternative.
    # That is why ``brokerage_pct`` defaults to 0: the "Rs 20 or 0.03%,
    # whichever is lower" structure belongs to *equity* intraday, and applying
    # it here would make a Rs 10 premium look as cheap to trade as a Rs 200 one
    # (0.03% of a small premium turnover is pennies). Set ``brokerage_pct``
    # above 0 only if your broker genuinely prices options as a percentage.
    brokerage_flat: float = 20.0
    brokerage_pct: float = 0.0

    # --- Statutory, as fractions of premium turnover ---
    # STT: sell side only. Raised from 0.0625% to 0.1% effective 2024-10-01.
    stt_sell_pct: float = 0.001
    # NSE transaction charge on options premium (revised 2024-10-01).
    exchange_txn_pct: float = 0.0003503
    # SEBI turnover fee: Rs 10 per crore.
    sebi_turnover_pct: float = 0.000001
    # NSE Investor Protection Fund Trust levy: Rs 50 per crore of premium.
    ipft_pct: float = 0.000005
    # Stamp duty: buy side only, Rs 300 per crore.
    stamp_duty_buy_pct: float = 0.00003
    # GST on (brokerage + exchange txn + SEBI + IPFT).
    gst_pct: float = 0.18

    # --- Execution friction (NOT statutory; the part that actually hurts) ---
    # Half the bid-ask spread, as a fraction of premium, paid on EVERY leg.
    # 0.0025 => a 0.5% round-trip spread cost. Liquid weekly ATM contracts can
    # be tighter; far-OTM and far-expiry are much worse. Measure your own.
    half_spread_pct: float = 0.0025
    # Floor the spread cost at this many ticks per leg, so cheap contracts are
    # not modelled as free to cross. A Rs 2 premium with a 1-tick spread is a
    # 2.5% cost per leg, which half_spread_pct alone would never capture.
    min_half_spread_ticks: float = 1.0

    def with_measured_spread(self, half_spread_pct: float) -> "OptionsCostModel":
        """Return a copy using a spread you actually measured from the book."""
        return replace(self, half_spread_pct=max(0.0, float(half_spread_pct)))


_ZERO_LEG = ("brokerage", "stt", "exchange_txn", "sebi", "ipft", "stamp_duty", "gst", "total")


def _brokerage(premium: float, quantity: float, costs: OptionsCostModel) -> float:
    """Brokerage for one leg.

    Flat per order by default (the real F&O structure). When
    ``brokerage_pct`` is configured above zero, the lower of flat and
    percentage applies, matching brokers who offer both.
    """
    if costs.brokerage_pct <= 0:
        return costs.brokerage_flat
    return min(costs.brokerage_flat, premium * quantity * costs.brokerage_pct)


def slippage_per_unit(premium: float, costs: OptionsCostModel) -> float:
    """Per-unit spread cost for crossing the book once, in rupees.

    Max of (percentage of premium) and (a tick floor), so both expensive
    contracts with wide relative spreads and cheap contracts with a 1-tick
    absolute spread are charged sensibly.
    """
    if premium <= 0:
        return 0.0
    pct_component = premium * costs.half_spread_pct
    tick_component = TICK_SIZE * costs.min_half_spread_ticks
    return max(pct_component, tick_component)


def leg_costs(
    premium: float,
    quantity: float,
    *,
    is_sell: bool,
    costs: OptionsCostModel | None = None,
) -> dict[str, float]:
    """Itemised statutory + brokerage cost for ONE leg (buy or sell).

    Excludes spread/slippage, which :func:`round_trip_cost` adds separately.
    Returns every component so the UI can show exactly where the money went.
    """
    c = costs or OptionsCostModel()
    if premium <= 0 or quantity <= 0:
        return {key: 0.0 for key in _ZERO_LEG}

    turnover = premium * quantity
    brokerage = _brokerage(premium, quantity, c)
    stt = turnover * c.stt_sell_pct if is_sell else 0.0
    exchange_txn = turnover * c.exchange_txn_pct
    sebi = turnover * c.sebi_turnover_pct
    ipft = turnover * c.ipft_pct
    stamp = 0.0 if is_sell else turnover * c.stamp_duty_buy_pct
    # GST applies to services (brokerage + exchange/regulator fees), never to
    # STT or stamp duty.
    gst = (brokerage + exchange_txn + sebi + ipft) * c.gst_pct

    total = brokerage + stt + exchange_txn + sebi + ipft + stamp + gst
    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_txn": exchange_txn,
        "sebi": sebi,
        "ipft": ipft,
        "stamp_duty": stamp,
        "gst": gst,
        "total": total,
    }


def round_trip_cost(
    entry_premium: float,
    exit_premium: float,
    quantity: float,
    *,
    costs: OptionsCostModel | None = None,
    include_slippage: bool = True,
) -> dict[str, float]:
    """Total cost of a complete buy-then-sell options trade.

    ``quantity`` is units, i.e. ``lot_size * lots``.

    Slippage is charged as a fee here (rather than silently worsening the fill)
    so every rupee of drag is visible and auditable in one number. The caller
    must therefore pass the *quoted* premiums, not already-slipped ones, or the
    spread gets counted twice.
    """
    c = costs or OptionsCostModel()
    entry = leg_costs(entry_premium, quantity, is_sell=False, costs=c)
    exit_ = leg_costs(exit_premium, quantity, is_sell=True, costs=c)

    slippage = 0.0
    if include_slippage:
        slippage = (
            slippage_per_unit(entry_premium, c) + slippage_per_unit(exit_premium, c)
        ) * quantity

    statutory = entry["total"] + exit_["total"]
    return {
        "entry_cost": entry["total"],
        "exit_cost": exit_["total"],
        "slippage": slippage,
        "total": statutory + slippage,
        "entry_breakdown": entry,
        "exit_breakdown": exit_,
    }


def net_pnl(
    entry_premium: float,
    exit_premium: float,
    quantity: float,
    *,
    costs: OptionsCostModel | None = None,
    include_slippage: bool = True,
) -> dict[str, float]:
    """Gross and net P&L for a long options round trip, after all costs.

    This is the only P&L number the grader is allowed to record. Gross P&L on
    an options trade is close to meaningless at retail size.
    """
    gross = (exit_premium - entry_premium) * quantity
    breakdown = round_trip_cost(
        entry_premium, exit_premium, quantity,
        costs=costs, include_slippage=include_slippage,
    )
    total_cost = breakdown["total"]
    return {
        "gross_pnl": gross,
        "total_cost": total_cost,
        "net_pnl": gross - total_cost,
        "cost_breakdown": breakdown,
    }


def breakeven_premium(
    entry_premium: float,
    quantity: float,
    *,
    costs: OptionsCostModel | None = None,
    include_slippage: bool = True,
    tolerance: float = 1e-9,
    max_iterations: int = 200,
) -> float:
    """Exit premium at which net P&L is exactly zero.

    Solved numerically because exit-leg costs (STT, txn charges, spread) depend
    on the exit premium itself, so there is no clean closed form.

    Worth showing on every signal: if breakeven already sits above your target,
    the trade loses money by construction and should never be surfaced.
    """
    if entry_premium <= 0 or quantity <= 0:
        return 0.0

    def net_at(exit_premium: float) -> float:
        return net_pnl(
            entry_premium, exit_premium, quantity,
            costs=costs, include_slippage=include_slippage,
        )["net_pnl"]

    low = entry_premium
    # Costs are a small fraction of premium, so 2x entry virtually always
    # brackets the root; widen defensively rather than assuming it.
    high = entry_premium * 2.0 + 1.0
    for _ in range(20):
        if net_at(high) >= 0:
            break
        high = high * 2.0
    else:
        return high  # pathological rates; refuse to extrapolate further

    for _ in range(max_iterations):
        if high - low < tolerance:
            break
        mid = (low + high) / 2.0
        if net_at(mid) >= 0:
            high = mid
        else:
            low = mid
    return high


def cost_drag_pct(
    entry_premium: float,
    quantity: float,
    *,
    costs: OptionsCostModel | None = None,
    include_slippage: bool = True,
) -> float:
    """Round-trip cost as a fraction of position value, assuming a flat exit.

    The most useful pre-trade number this module produces: roughly the minimum
    percentage move in premium needed just to break even. Use it to reject
    contracts whose cost drag eats the target.
    """
    if entry_premium <= 0 or quantity <= 0:
        return 0.0
    position_value = entry_premium * quantity
    total = round_trip_cost(
        entry_premium, entry_premium, quantity,
        costs=costs, include_slippage=include_slippage,
    )["total"]
    return total / position_value


def describe_costs(
    entry_premium: float,
    exit_premium: float,
    quantity: float,
    *,
    costs: OptionsCostModel | None = None,
    include_slippage: bool = True,
) -> str:
    """Human-readable cost breakdown, for comparing against a contract note."""
    result = net_pnl(
        entry_premium, exit_premium, quantity,
        costs=costs, include_slippage=include_slippage,
    )
    cb = result["cost_breakdown"]
    entry_bd = cb["entry_breakdown"]
    exit_bd = cb["exit_breakdown"]
    fees = entry_bd["sebi"] + exit_bd["sebi"] + entry_bd["ipft"] + exit_bd["ipft"]
    return "\n".join([
        f"Buy {quantity:g} @ Rs {entry_premium:.2f}  ->  Sell @ Rs {exit_premium:.2f}",
        f"  Position value      Rs {entry_premium * quantity:>12,.2f}",
        f"  Gross P&L           Rs {result['gross_pnl']:>12,.2f}",
        "  --- costs ---",
        f"  Brokerage           Rs {entry_bd['brokerage'] + exit_bd['brokerage']:>12,.2f}",
        f"  STT (sell)          Rs {exit_bd['stt']:>12,.2f}",
        f"  Exchange txn        Rs {entry_bd['exchange_txn'] + exit_bd['exchange_txn']:>12,.2f}",
        f"  SEBI + IPFT         Rs {fees:>12,.2f}",
        f"  Stamp duty (buy)    Rs {entry_bd['stamp_duty']:>12,.2f}",
        f"  GST                 Rs {entry_bd['gst'] + exit_bd['gst']:>12,.2f}",
        f"  Spread/slippage     Rs {cb['slippage']:>12,.2f}",
        f"  TOTAL COST          Rs {cb['total']:>12,.2f}",
        f"  Net P&L             Rs {result['net_pnl']:>12,.2f}",
    ])


def calibrate_from_fill(
    entry_premium: float,
    exit_premium: float,
    quantity: float,
    actual_total_cost: float,
    *,
    costs: OptionsCostModel | None = None,
) -> OptionsCostModel:
    """Fit ``half_spread_pct`` so modelled cost matches a real contract note.

    Statutory rates are known and fixed; the unknown is your real execution
    friction. Feed in one actual round-trip cost from your broker and this
    attributes the residual to spread, giving a cost model calibrated to how
    *you* actually get filled.
    """
    c = costs or OptionsCostModel()
    statutory = (
        leg_costs(entry_premium, quantity, is_sell=False, costs=c)["total"]
        + leg_costs(exit_premium, quantity, is_sell=True, costs=c)["total"]
    )
    residual = float(actual_total_cost) - statutory
    if residual <= 0 or quantity <= 0:
        # Real cost was at or below statutory: no measurable spread cost.
        return replace(c, half_spread_pct=0.0, min_half_spread_ticks=0.0)

    premium_sum = entry_premium + exit_premium
    if premium_sum <= 0:
        return c
    implied = residual / (quantity * premium_sum)
    return replace(c, half_spread_pct=max(0.0, implied), min_half_spread_ticks=0.0)
