"""How the order is placed, which is worth more than any signal in this project.

The arithmetic that makes this the first thing to fix
-----------------------------------------------------
``ledger/costs.py`` charges the half-spread on **every leg, unconditionally**.
That is the cost of a *market* order, and until now it was the only execution
this system could express. Measured on one NIFTY lot at a Rs 160 premium with
the Rs 2 book ``microstructure.py`` now reads:

    1 lot, crossing the spread      1.89% of position, round trip
    1 lot, posted and filled        0.64%
    10 lots, posted and filled      0.23%

So execution and size move the cost by about **8x**, while no signal in this
project has moved the edge at all. The measured, overlap-corrected edge is
~0.11% and not statistically significant. Against a 1.89% cost that is hopeless;
against 0.23% it is merely bad. Nothing else available changes the problem by
that much.

Why a passive saving must be *measured*, never assumed
------------------------------------------------------
The naive version of this is "post at mid and save 1.25 percentage points",
and it is wrong, because a resting order does not fill at random. It fills when
somebody wants to trade against it -- which is disproportionately when the price
is about to move through it. Two costs replace the spread:

**Adverse selection.** Conditional on filling, the price tends to have moved
against you. You buy at mid exactly when mid is about to fall.

**Non-fill.** Conditional on *not* filling, you either miss the trade (an
opportunity cost that is invisible in any ledger) or you chase it and cross a
spread that has usually widened -- so the eventual cost can exceed simply
having crossed at the start.

Expected passive cost is therefore

    fees + P(fill) * adverse_selection + (1 - P(fill)) * chase_cost

and both of those terms are empirical. Until they are measured from this
account's own fills, :func:`price_execution` **refuses to report a saving**: a
passive plan with no measured statistics is priced as if it crossed, and flagged
``measured=False``. That is the same discipline ``microstructure.parse_book``
uses when the depth is missing -- an unmeasured optimistic number is worse than
an honest pessimistic one, because it survives into a verdict.

Named ``order_execution`` rather than ``execution`` because this project
already has an ``execution.py`` holding a vestigial equity paper broker.

:mod:`intraday_advisor.fill_study` collects the statistics this module needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from intraday_advisor.ledger.costs import (
    OptionsCostModel,
    leg_costs,
    slippage_per_unit,
)

#: The three ways an order actually reaches the exchange.
AGGRESSIVE = "aggressive"   # market / marketable limit: crosses, always fills
PASSIVE = "passive"         # rests at or inside mid: may not fill
PATIENT = "patient"         # rests, then crosses if unfilled by a deadline

STYLES = (AGGRESSIVE, PASSIVE, PATIENT)


@dataclass(frozen=True)
class FillStatistics:
    """What this account's own passive orders actually did.

    Every field is a measurement, not a parameter. ``samples`` is how many
    fills it rests on, and a small number here should be read as "we do not
    know yet", not as a result.
    """

    #: Share of resting orders that filled before the deadline, 0-1.
    fill_rate: float = 0.0
    #: Mean adverse move, as a fraction of premium, CONDITIONAL on filling.
    #: Positive means the price moved against you after you were filled.
    adverse_selection_pct: float = 0.0
    #: Mean extra cost of chasing, as a fraction of premium, conditional on
    #: NOT filling: what crossing later cost versus crossing at the start.
    chase_penalty_pct: float = 0.0
    samples: int = 0
    horizon_seconds: float = 0.0
    note: str = ""

    @property
    def usable(self) -> bool:
        """Enough fills to price anything on. Deliberately strict."""
        return self.samples >= 30 and 0.0 <= self.fill_rate <= 1.0

    def as_dict(self) -> dict:
        from dataclasses import asdict
        data = asdict(self)
        data["usable"] = self.usable
        return data


@dataclass
class ExecutionPlan:
    """The expected cost of getting in and out under one execution style."""

    style: str
    legs: int = 2                       # entry + exit, for a single-leg trade
    #: Expected round-trip cost in rupees, including fees and friction.
    expected_cost: float = 0.0
    #: As a fraction of the position, which is the number that competes
    #: against an edge.
    expected_cost_pct: float = 0.0
    #: Breakdown, so a surprising total can always be traced.
    fees: float = 0.0
    spread_cost: float = 0.0
    adverse_cost: float = 0.0
    chase_cost: float = 0.0
    #: False when a passive style had no measured statistics and was therefore
    #: priced as if it crossed. Read this before believing a saving.
    measured: bool = False
    fill_rate: float | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def price_execution(
    *, premium: float, quantity: float, style: str = AGGRESSIVE,
    costs: OptionsCostModel | None = None,
    fills: FillStatistics | None = None,
    round_trip: bool = True,
) -> ExecutionPlan:
    """Expected cost of one position under ``style``.

    ``premium`` is the quoted mid (or LTP) and ``quantity`` is units, i.e.
    ``lot_size * lots``. Fees are charged on both legs of a round trip because
    both legs happen; only the *friction* differs by style.
    """
    c = costs or OptionsCostModel()
    style = (style or AGGRESSIVE).lower()
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}, not {style!r}")

    plan = ExecutionPlan(style=style, legs=2 if round_trip else 1)
    if premium <= 0 or quantity <= 0:
        plan.notes.append("No premium or quantity; nothing to price.")
        return plan

    position = premium * quantity

    # --- statutory fees happen whatever the style ------------------------- #
    opening = leg_costs(premium, quantity, is_sell=False, costs=c)
    closing = leg_costs(premium, quantity, is_sell=True, costs=c)
    plan.fees = round(opening["total"] + (closing["total"] if round_trip else 0.0), 2)

    cross_per_leg = slippage_per_unit(premium, c) * quantity
    legs = 2 if round_trip else 1

    if style == AGGRESSIVE:
        plan.spread_cost = round(cross_per_leg * legs, 2)
        plan.measured = True       # crossing is certain; nothing is assumed
        plan.notes.append(
            "Crosses the spread on every leg. The cost is certain and the fill "
            "is certain."
        )

    elif style in (PASSIVE, PATIENT):
        if fills is None or not fills.usable:
            # The whole point: no measurement, no saving.
            plan.spread_cost = round(cross_per_leg * legs, 2)
            plan.measured = False
            plan.notes.append(
                "Priced as if it crossed, because this account has no measured "
                "fill statistics. A passive saving that has not been measured "
                "is an assumption, and an optimistic assumption survives into "
                "a verdict. Run intraday_advisor.fill_study first."
            )
        else:
            rate = max(0.0, min(1.0, fills.fill_rate))
            plan.fill_rate = round(rate, 4)
            plan.measured = True

            # Filled: no spread paid, but the price moved against you.
            adverse = fills.adverse_selection_pct * premium * quantity * legs
            # Not filled: either chase (PATIENT) or lose the trade (PASSIVE).
            if style == PATIENT:
                chase = (cross_per_leg + fills.chase_penalty_pct * premium * quantity)
                not_filled_cost = chase * legs
                plan.notes.append(
                    f"Rests first, crosses if unfilled. Measured fill rate "
                    f"{rate:.0%} over {fills.horizon_seconds:.0f}s on "
                    f"{fills.samples} fills."
                )
            else:
                not_filled_cost = 0.0
                plan.notes.append(
                    f"Rests only. Measured fill rate {rate:.0%} on "
                    f"{fills.samples} fills, so {1 - rate:.0%} of intended "
                    f"trades never happen -- an opportunity cost no ledger "
                    f"records."
                )

            plan.adverse_cost = round(rate * adverse, 2)
            plan.chase_cost = round((1.0 - rate) * not_filled_cost, 2)
            plan.spread_cost = 0.0

    plan.expected_cost = round(
        plan.fees + plan.spread_cost + plan.adverse_cost + plan.chase_cost, 2
    )
    plan.expected_cost_pct = round(plan.expected_cost / position * 100, 4)
    return plan


def compare_styles(
    *, premium: float, quantity: float, costs: OptionsCostModel | None = None,
    fills: FillStatistics | None = None,
) -> dict:
    """Every style side by side, plus the edge each one would require."""
    plans = {
        style: price_execution(premium=premium, quantity=quantity, style=style,
                               costs=costs, fills=fills)
        for style in STYLES
    }
    # Prefer a MEASURED style on a tie. When nothing has been measured all
    # three cost the same, and the honest pick is the one whose cost is
    # certain -- crossing. Relying on the order of STYLES for that would make
    # the answer depend on a tuple literal somebody could reorder.
    cheapest = min(
        plans.values(),
        key=lambda p: (round(p.expected_cost_pct, 6), not p.measured),
    )
    return {
        "plans": {k: v.as_dict() for k, v in plans.items()},
        "cheapest": cheapest.style,
        "required_edge_pct": {
            k: v.expected_cost_pct for k, v in plans.items()
        },
        "note": (
            f"The cheapest execution available here is '{cheapest.style}' at "
            f"{cheapest.expected_cost_pct:.2f}% round trip, so a strategy must "
            f"clear that before anything else."
            + ("" if cheapest.measured else
               " It is NOT measured, so treat it as the aggressive figure "
               "until fill statistics exist.")
        ),
    }


def break_even_fill_rate(
    *, premium: float, quantity: float, adverse_selection_pct: float,
    costs: OptionsCostModel | None = None,
) -> dict:
    """How often a passive order must fill to beat crossing.

    Answers the question that decides whether passive execution is worth
    pursuing at all, before any data is collected: given an adverse-selection
    cost, what fill rate makes resting cheaper than crossing? If the answer is
    above about 80% the idea is fragile, because real passive fill rates on
    retail-sized option orders are rarely that high.
    """
    c = costs or OptionsCostModel()
    if premium <= 0 or quantity <= 0:
        return {"ok": False, "reason": "No premium or quantity."}

    cross_per_leg = slippage_per_unit(premium, c) * quantity
    adverse_per_leg = adverse_selection_pct * premium * quantity

    if adverse_per_leg >= cross_per_leg:
        return {
            "ok": False,
            "reason": (
                f"Adverse selection of {adverse_selection_pct * 100:.3f}% "
                f"already costs at least as much as crossing. Resting cannot "
                f"win at any fill rate."
            ),
            "cross_per_leg": round(cross_per_leg, 2),
            "adverse_per_leg": round(adverse_per_leg, 2),
        }

    # Patient: filled legs pay adverse, unfilled legs pay the cross anyway.
    # Cost(rate) = rate*adverse + (1-rate)*cross  <=  cross  is true for all
    # rate > 0 when adverse < cross, so the interesting question is the PASSIVE
    # case where an unfilled order simply loses the trade.
    required = adverse_per_leg / cross_per_leg
    return {
        "ok": True,
        "cross_per_leg": round(cross_per_leg, 2),
        "adverse_per_leg": round(adverse_per_leg, 2),
        "break_even_fill_rate": round(required, 4),
        "note": (
            f"Adverse selection is {required:.0%} of the crossing cost, so a "
            f"patient order is cheaper than crossing whenever it fills at all. "
            f"The real question is how often it fills and what the unfilled "
            f"trades would have earned."
        ),
    }


def with_execution(
    costs: OptionsCostModel, plan: ExecutionPlan, *, premium: float,
    quantity: float,
) -> OptionsCostModel:
    """A cost model whose spread term reflects a measured execution plan.

    Lets the rest of the system -- sizing, breakeven, candidate ranking --
    inherit the execution assumption without every caller learning about
    execution styles.
    """
    if premium <= 0 or quantity <= 0 or plan.legs <= 0:
        return costs
    friction = plan.spread_cost + plan.adverse_cost + plan.chase_cost
    per_leg_fraction = friction / plan.legs / (premium * quantity)
    return replace(costs, half_spread_pct=max(0.0, per_leg_fraction),
                   min_half_spread_ticks=0.0)
