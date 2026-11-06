"""Multi-leg option structures: spreads, condors and the capital they tie up.

Why this exists
---------------
Until now this app could express exactly one structure -- **buy a single
option** -- which is the worst risk shape available to a small account:

* it bleeds time value every minute it is held;
* it needs a large directional move to overcome that bleed;
* one lot costs ~55% of a Rs 20,000 account and risks ~11% per trade;
* and it makes you the *buyer* of volatility, so you pay the skew instead of
  collecting it.

Trading the desk on 2026-10-21 I lost Rs 395 doing exactly this, and the
measurements taken at the time said plainly it was the wrong structure:

    NIFTY      put IV 15.2%   call IV 14.8%
    BANKNIFTY  put IV 17.1%   call IV 15.0%

Puts were the dear side on both indices and I was buying them. Worse, my target
was the 22,500 put wall -- so I was paying full price for unlimited upside past
a level I had already decided to exit at. A **vertical spread** gives that exact
payoff for a fraction of the cost, because selling the far strike recovers the
premium you were never going to collect anyway.

The four facts a multi-leg structure must get right
---------------------------------------------------
1. **Capital is not the premium.** A debit spread ties up its net debit. A
   credit spread ties up *margin*, roughly the width of the spread less the
   credit received -- which can be several times the credit.
2. **Costs multiply by legs.** A two-leg spread is **four orders** round trip,
   so flat brokerage is 4 x Rs 20, and the half-spread is paid on all four. On
   small structures this is the whole edge.
3. **Short legs carry assignment and margin risk**, so they are only ever
   written *inside* a defined-risk structure here -- never naked.
4. **The payoff is piecewise linear**, so max profit, max loss and the
   breakevens are exact arithmetic, not estimates. There is no excuse for
   showing a trader an approximate risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from intraday_advisor.ledger.costs import (
    OptionsCostModel,
    leg_costs,
    slippage_per_unit,
)


@dataclass(frozen=True)
class Leg:
    """One leg of a structure. ``action`` is BUY or SELL."""

    option_type: str          # CE / PE
    strike: float
    action: str               # BUY / SELL
    premium: float
    lots: int = 1

    @property
    def is_short(self) -> bool:
        return self.action.upper() == "SELL"

    @property
    def sign(self) -> int:
        """+1 if the leg costs you premium, -1 if it pays you."""
        return -1 if self.is_short else 1

    def intrinsic_at(self, spot: float) -> float:
        """Per-unit value of this leg's option at expiry, for ``spot``."""
        if self.option_type.upper() == "CE":
            return max(0.0, spot - self.strike)
        return max(0.0, self.strike - spot)

    def label(self) -> str:
        return f"{self.action.upper()} {self.strike:.0f} {self.option_type.upper()}"


@dataclass
class Structure:
    """A priced, sized multi-leg position with exact payoff arithmetic."""

    name: str
    legs: list[Leg]
    lot_size: int
    index: str = ""
    #: Net premium per unit. Positive = you pay (debit). Negative = you receive.
    net_premium: float = 0.0
    net_debit: float = 0.0           # rupees paid, 0 for a credit structure
    net_credit: float = 0.0          # rupees received, 0 for a debit structure
    max_profit: float = 0.0          # gross, before costs
    max_loss: float = 0.0            # gross, before costs, positive number
    breakevens: list[float] = field(default_factory=list)
    capital_required: float = 0.0    # debit, or margin for a credit structure
    round_trip_cost: float = 0.0
    net_max_profit: float = 0.0
    net_max_loss: float = 0.0
    net_rr: float = 0.0
    orders_round_trip: int = 0
    #: False when the structure is net short options at a wing, so the loss
    #: grows without limit and ``max_loss`` is a stress figure rather than a
    #: maximum. See :func:`undefined_risk_sides`.
    risk_is_defined: bool = True
    #: Which directions can lose without limit: "upside", "downside", or both.
    undefined_sides: list[str] = field(default_factory=list)
    #: Which directions can GAIN without limit -- a long straddle's upside, for
    #: instance. There ``max_profit`` is a stress figure, not a maximum, and
    #: quoting a reward:risk ratio from it understates the trade.
    unbounded_profit_sides: list[str] = field(default_factory=list)
    #: The adverse move ``max_loss`` was measured at, when risk is undefined.
    stress_move_pct: float = 0.0
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        data = asdict(self)
        data["legs"] = [leg.label() + f" @ {leg.premium:.2f}" for leg in self.legs]
        return data

    def payoff_at(self, spot: float) -> float:
        """Gross rupee P&L at expiry for an underlying of ``spot``."""
        total = 0.0
        for leg in self.legs:
            quantity = self.lot_size * leg.lots
            value = leg.intrinsic_at(spot) - leg.premium
            total += leg.sign * value * quantity
        return total


def _width(legs: list[Leg], option_type: str) -> float:
    """Strike distance between the two legs of one side."""
    same = sorted(leg.strike for leg in legs
                  if leg.option_type.upper() == option_type.upper())
    return (same[-1] - same[0]) if len(same) >= 2 else 0.0


#: How far beyond the strikes an undefined-risk structure is stressed, as a
#: fraction of the mean strike. A naked short has no maximum loss to report, so
#: the only honest figure is "this is what an adverse move of this size costs,
#: and there is no limit past it". 10% on NIFTY is roughly 2,300 points -- large
#: but not fanciful: the index has moved that much in a week.
STRESS_MOVE_FRACTION = 0.10


def missing_premiums(premiums: dict, wanted: list) -> list:
    """Which ``(strike, option_type)`` pairs the chain did not price.

    Every builder indexed ``premiums[...]`` directly, so a strike the chain did
    not carry raised ``KeyError`` out of the middle of pricing. A live chain is
    routinely missing strikes -- it returns a window around the money, and the
    wings of a condor can easily fall outside it -- so this is a normal input,
    not an exceptional one, and it belongs in a warning rather than a
    traceback. The `_volatility_read` / `_pin_read` lesson, in a different
    module: the functions that promise not to fail are the ones to check.
    """
    return [key for key in wanted if float(premiums.get(key, 0) or 0) <= 0]


def _refuse(name: str, lot_size: int, index: str, absent: list) -> Structure:
    """A structure that cannot be priced, saying which strikes were absent."""
    structure = Structure(name=name, legs=[], lot_size=int(lot_size),
                          index=index)
    pretty = ", ".join(f"{int(k[0])} {k[1]}" for k in absent)
    structure.warnings.append(
        f"Not priced: the chain carries no premium for {pretty}. Either those "
        f"strikes are outside the window the feed returned, or they are "
        f"untraded -- and an untraded strike cannot be entered at any price "
        f"you can compute here."
    )
    return structure


def _wing_slope(legs: list[Leg], option_type: str) -> float:
    """Net option count beyond the outermost strike of one side, in lots.

    Past the highest call strike every call is in the money, so the payoff's
    slope is just the net number of calls held. Negative means the structure is
    net short there and the loss grows with every further point -- which is the
    definition of undefined risk, and cannot be seen by probing a payoff at a
    few points near the strikes.
    """
    return sum(leg.sign * leg.lots for leg in legs
               if leg.option_type.upper() == option_type.upper())


def open_wings(legs: list[Leg]) -> dict:
    """Which wings are open, and in whose favour.

    Beyond the outermost strike on each side the payoff is a straight line
    whose slope is the net option count there. Three cases per side:

    * slope zero  -- the payoff is **flat**, so probing any distance outside
      the kinks gives the exact extreme. This is every defined-risk structure.
    * net short   -- the loss grows forever. There is no maximum loss.
    * net long    -- the profit grows forever. There is no maximum profit.

    Both open cases matter and only the first was ever handled. A long straddle
    reported ``max_profit`` of **-Rs 15,470** -- a negative maximum profit, i.e.
    "this cannot make money" -- for the one structure whose upside is unlimited,
    because its two legs share a strike and the probe reached one point either
    side of it.
    """
    upside = _wing_slope(legs, "CE")
    downside = _wing_slope(legs, "PE")
    return {
        "upside_slope": upside,
        "downside_slope": downside,
        "loss_sides": (["upside"] if upside < 0 else [])
                      + (["downside"] if downside < 0 else []),
        "profit_sides": (["upside"] if upside > 0 else [])
                        + (["downside"] if downside > 0 else []),
        "any_open": bool(upside or downside),
    }


def undefined_risk_sides(legs: list[Leg]) -> list[str]:
    """Which directions this structure can lose without limit.

    ``AGENTS.md`` states the invariant plainly -- *"short legs only ever inside
    defined risk, never naked"* -- and nothing in this module enforced or even
    detected it. A naked short straddle built through :func:`build_structure`
    reported a maximum loss of **Rs 15,145** with no warnings, while its own
    ``payoff_at(40000)`` returned **-Rs 1,089,530**.

    Two separate reasons it read that way, both fixed here:

    1. The payoff extremes were found by probing the kinks plus a margin of
       five times the *strike span*. For a straddle both legs share a strike,
       so the span is zero and the probe reached five **points** either side of
       23,000. For a defined-risk structure that is harmless -- beyond the
       outermost kink the payoff is flat, so any distance gives the same answer
       -- which is exactly why the defect stayed hidden until a naked structure
       was priced.
    2. Nothing computed the asymptotic slope, which is the only thing that
       distinguishes "flat beyond the wing" from "falling forever".
    """
    sides = []
    # Above the highest call strike, the slope is the net call count.
    if _wing_slope(legs, "CE") < 0:
        sides.append("upside")
    # Below the lowest put strike, a long put gains and a short put loses, so a
    # net short put position loses as spot falls. Bounded only by spot reaching
    # zero, which is not a bound worth calling one.
    if _wing_slope(legs, "PE") < 0:
        sides.append("downside")
    return sides


def build_structure(
    name: str, legs: list[Leg], lot_size: int, *,
    index: str = "", costs: OptionsCostModel | None = None,
    margin_multiple: float = 1.0,
) -> Structure:
    """Price and size a structure, net of every cost of getting in and out.

    ``margin_multiple`` scales the margin estimate for credit structures. The
    default of 1.0 assumes a broker charges the defined maximum loss, which is
    the floor; most charge somewhat more, and a real figure should come from
    the broker's margin API rather than from an assumption here.
    """
    c = costs or OptionsCostModel()
    structure = Structure(name=name, legs=list(legs), lot_size=int(lot_size),
                          index=index)
    if not legs or lot_size <= 0:
        structure.warnings.append("No legs, or an invalid lot size.")
        return structure
    if any(leg.premium <= 0 for leg in legs):
        structure.warnings.append(
            "A leg has no price — the strike is illiquid or the quote is stale."
        )
        return structure

    # --- net premium per unit --------------------------------------------- #
    per_unit = sum(leg.sign * leg.premium * leg.lots for leg in legs)
    structure.net_premium = round(per_unit, 2)
    gross = per_unit * lot_size
    if gross >= 0:
        structure.net_debit = round(gross, 2)
    else:
        structure.net_credit = round(-gross, 2)

    # --- payoff extremes, from the actual kinked payoff -------------------- #
    # Evaluated at every strike plus well outside the range: a piecewise-linear
    # payoff takes its extremes only at a kink or at infinity, so this is exact
    # rather than a sampled approximation.
    strikes = sorted({leg.strike for leg in legs})
    wings = open_wings(legs)
    structure.undefined_sides = wings["loss_sides"]
    structure.risk_is_defined = not structure.undefined_sides
    structure.unbounded_profit_sides = wings["profit_sides"]

    # How far outside the strikes to probe. Where every wing slope is zero the
    # payoff is flat beyond the outermost kink, so any positive distance gives
    # the exact extreme. Where a wing is open there is no extreme to find, and
    # the distance becomes a stated stress move instead of an arbitrary margin.
    #
    # The old "5x the strike span" was neither: it collapsed to five POINTS
    # whenever every leg shared a strike, which is exactly the single-strike
    # case -- straddles -- where both the loss and the profit run off to
    # infinity and the figure mattered most.
    mean_strike = sum(strikes) / len(strikes)
    span = strikes[-1] - strikes[0]
    if wings["any_open"]:
        reach = max(span, STRESS_MOVE_FRACTION * mean_strike)
        structure.stress_move_pct = round(reach / mean_strike * 100, 1)
    else:
        reach = max(1.0, span)

    probes = [max(0.01, strikes[0] - reach)] + strikes + [strikes[-1] + reach]
    payoffs = [structure.payoff_at(p) for p in probes]
    structure.max_profit = round(max(payoffs), 2)
    structure.max_loss = round(abs(min(payoffs)), 2)

    if not structure.risk_is_defined:
        where = " and ".join(structure.undefined_sides)
        structure.warnings.append(
            f"UNDEFINED RISK on the {where}: this structure is net short "
            f"options at that wing, so the loss has no maximum. The "
            f"Rs {structure.max_loss:,.0f} below is the loss at a "
            f"{structure.stress_move_pct:.0f}% adverse move, NOT a worst case "
            f"-- past that it keeps growing."
        )
        structure.warnings.append(
            "AGENTS.md states the rule this breaks: short legs only ever "
            "inside defined risk, never naked. Buy a wing to cap it."
        )
    if structure.unbounded_profit_sides:
        where = " and ".join(structure.unbounded_profit_sides)
        structure.notes.append(
            f"Profit is unlimited on the {where}. The "
            f"Rs {structure.max_profit:,.0f} shown is what a "
            f"{structure.stress_move_pct:.0f}% move delivers, not a cap -- so "
            f"every ratio computed from it understates this structure rather "
            f"than flattering it."
        )

    # --- breakevens, by linear interpolation between adjacent probes ------- #
    breakevens: list[float] = []
    for i in range(len(probes) - 1):
        a, b = payoffs[i], payoffs[i + 1]
        if a == 0:
            breakevens.append(probes[i])
        elif (a < 0) != (b < 0):
            span = b - a
            if span:
                breakevens.append(probes[i] + (probes[i + 1] - probes[i]) * (-a / span))
    structure.breakevens = [round(x, 2) for x in sorted(set(breakevens))]

    # --- what it ties up --------------------------------------------------- #
    if structure.net_debit > 0:
        structure.capital_required = structure.net_debit
        structure.notes.append(
            f"Debit structure: the Rs {structure.net_debit:,.0f} you pay is "
            f"also the most you can lose."
        )
    elif not structure.risk_is_defined:
        # Margin on a naked short is set by SPAN+ELM on the exchange's own
        # volatility scan, not by a maximum loss that does not exist. Deriving
        # it from `max_loss` would have quoted Rs 15,145 for a short straddle
        # whose real margin runs to lakhs -- a number small enough to look
        # affordable, which is the worst possible direction to be wrong in.
        structure.capital_required = 0.0
        structure.warnings.append(
            f"Margin cannot be estimated for an undefined-risk structure: it "
            f"is set by the exchange's SPAN+ELM scan, not by a maximum loss "
            f"that does not exist. Get the real figure from the broker's "
            f"margin API before treating the Rs {structure.net_credit:,.0f} "
            f"credit as capital-light."
        )
    else:
        # Margin for a defined-risk credit structure is about the maximum loss.
        # This is the floor, not a quote -- see margin_multiple.
        margin = structure.max_loss * margin_multiple
        structure.capital_required = round(margin, 2)
        structure.notes.append(
            f"Credit structure: you receive Rs {structure.net_credit:,.0f} but "
            f"must post about Rs {margin:,.0f} of margin, so the capital at "
            f"work is the margin, not the credit."
        )

    # --- costs: every leg, both ways --------------------------------------- #
    total_cost = 0.0
    for leg in legs:
        quantity = lot_size * leg.lots
        opening = leg_costs(leg.premium, quantity, is_sell=leg.is_short, costs=c)
        closing = leg_costs(leg.premium, quantity, is_sell=not leg.is_short, costs=c)
        total_cost += opening["total"] + closing["total"]
        # The half-spread is paid on entry AND exit of every single leg. This
        # is what makes small multi-leg structures unviable, and it is the cost
        # most often left out of a payoff diagram.
        total_cost += slippage_per_unit(leg.premium, c) * quantity * 2
    structure.round_trip_cost = round(total_cost, 2)
    structure.orders_round_trip = len(legs) * 2

    structure.net_max_profit = round(structure.max_profit - total_cost, 2)
    structure.net_max_loss = round(structure.max_loss + total_cost, 2)
    # A reward:risk ratio requires a risk. With an unbounded denominator the
    # ratio is zero, and quoting the stress loss instead would produce a
    # flattering finite number for the one structure shape that can take the
    # account out -- a short straddle would have read "net R:R 1.0".
    structure.net_rr = (
        round(structure.net_max_profit / structure.net_max_loss, 2)
        if (structure.net_max_loss > 0 and structure.risk_is_defined) else 0.0
    )
    if not structure.risk_is_defined:
        structure.notes.append(
            "No reward:risk is quoted: the risk has no maximum, so the ratio "
            "is undefined rather than large."
        )

    # Both of these compare costs against a MAXIMUM profit, so neither applies
    # where the profit has no maximum. Saying a long straddle "cannot make
    # money at this size" because a 10% move does not cover its costs would be
    # false: a 20% move does, and nothing caps the move.
    if structure.unbounded_profit_sides:
        where = " and ".join(structure.unbounded_profit_sides)
        move_needed = (total_cost / (lot_size * max(
            1, abs(wings["upside_slope"] if "upside" in
                   structure.unbounded_profit_sides
                   else wings["downside_slope"]))))
        structure.notes.append(
            f"Costs of Rs {total_cost:,.0f} across "
            f"{structure.orders_round_trip} orders are covered once the index "
            f"moves about {move_needed:,.0f} points past the breakeven on the "
            f"{where}. There is no maximum profit to compare them against."
        )
    elif structure.net_max_profit <= 0:
        structure.warnings.append(
            f"Costs of Rs {total_cost:,.0f} across {structure.orders_round_trip} "
            f"orders exceed the entire maximum profit — this structure cannot "
            f"make money at this size."
        )
    elif total_cost > structure.max_profit * 0.35:
        structure.warnings.append(
            f"Costs eat {total_cost / structure.max_profit * 100:.0f}% of the "
            f"best case. Widen the strikes or trade more lots, or skip it."
        )
    return structure


# --------------------------------------------------------------------------- #
# Named structures
# --------------------------------------------------------------------------- #

def vertical(
    index: str, option_type: str, long_strike: float, short_strike: float,
    premiums: dict, lot_size: int, *, lots: int = 1,
    costs: OptionsCostModel | None = None,
) -> Structure:
    """A two-leg vertical: long one strike, short another of the same type.

    ``premiums`` maps ``(strike, option_type)`` to the traded premium.

    Direction follows the strikes, not a flag: a put vertical that is long the
    higher strike is bearish; long the lower strike is bullish. Deriving it
    rather than passing it removes a whole class of mislabelled position.
    """
    kind = option_type.upper()
    absent = missing_premiums(
        premiums, [(long_strike, kind), (short_strike, kind)])
    if absent:
        return _refuse(f"{kind} vertical {long_strike:.0f}/{short_strike:.0f}",
                       lot_size, index, absent)
    long_leg = Leg(kind, long_strike, "BUY", float(premiums[(long_strike, kind)]), lots)
    short_leg = Leg(kind, short_strike, "SELL", float(premiums[(short_strike, kind)]), lots)

    # Both sides had the same shape and I wrote the call side backwards, which
    # the tests caught: buying the LOWER call and selling the higher one is a
    # BULL call spread -- you want price to rise to the strike you sold. The
    # put side is the mirror: buying the HIGHER put is bearish.
    #
    # Said once, for both: you are bearish when the leg you BOUGHT profits from
    # a fall, i.e. a put above the strike you sold, or a call below it.
    if kind == "PE":
        bearish = long_strike > short_strike
    else:
        bearish = long_strike > short_strike
    name = (
        f"{'Bear' if bearish else 'Bull'} {'put' if kind == 'PE' else 'call'} "
        f"spread {long_strike:.0f}/{short_strike:.0f}"
    )
    structure = build_structure(
        name, [long_leg, short_leg], lot_size, index=index, costs=costs
    )
    structure.notes.append(
        f"Caps the gain at {short_strike:.0f} — which is the point when that "
        f"level is where you intended to take profit anyway."
    )
    return structure


def iron_condor(
    index: str, strikes: dict, premiums: dict, lot_size: int, *,
    lots: int = 1, costs: OptionsCostModel | None = None,
) -> Structure:
    """Short strangle with long wings: profits if price goes nowhere.

    ``strikes`` needs ``short_put``, ``long_put``, ``short_call``, ``long_call``.
    The textbook structure for a **pinned** chain, where both sides are
    defending one strike and price is being held rather than trending.
    """
    absent = missing_premiums(premiums, [
        (strikes["long_put"], "PE"), (strikes["short_put"], "PE"),
        (strikes["short_call"], "CE"), (strikes["long_call"], "CE"),
    ])
    if absent:
        return _refuse("Iron condor", lot_size, index, absent)
    legs = [
        Leg("PE", strikes["long_put"], "BUY",
            float(premiums[(strikes["long_put"], "PE")]), lots),
        Leg("PE", strikes["short_put"], "SELL",
            float(premiums[(strikes["short_put"], "PE")]), lots),
        Leg("CE", strikes["short_call"], "SELL",
            float(premiums[(strikes["short_call"], "CE")]), lots),
        Leg("CE", strikes["long_call"], "BUY",
            float(premiums[(strikes["long_call"], "CE")]), lots),
    ]
    structure = build_structure(
        f"Iron condor {strikes['long_put']:.0f}/{strikes['short_put']:.0f}"
        f"-{strikes['short_call']:.0f}/{strikes['long_call']:.0f}",
        legs, lot_size, index=index, costs=costs,
    )
    structure.notes.append(
        "Wins while price stays between the short strikes, and time is on your "
        "side rather than against it."
    )
    return structure


def straddle(
    index: str, strike: float, premiums: dict, lot_size: int, *,
    action: str = "BUY", lots: int = 1,
    costs: OptionsCostModel | None = None,
) -> Structure:
    """Call and put at the same strike. The pure volatility position.

    A **long** straddle is the only structure here that wins from movement
    without caring about direction, which makes it the one trade this project's
    measurements actually support taking a view with: direction is close to
    unforecastable at these horizons (lag-1 autocorrelation about -0.09), while
    variance is persistent and forecastable -- that asymmetry is what
    ``vol_forecast`` exists for.

    It is also the most expensive thing on the chain. You pay two premiums and
    two round trips, so the move has to beat roughly twice the cost before
    anything is left. Compare ``volatility.implied_move_from_straddle`` against
    the move you actually expect before buying one.

    A **short** straddle has undefined risk on both sides and
    :func:`build_structure` now says so loudly. It is included because it is the
    textbook premium-harvesting trade and because refusing to price it would
    not stop anyone trading it -- but the honest version of that trade is the
    iron butterfly, which caps both wings.
    """
    side = action.upper()
    absent = missing_premiums(premiums, [(strike, "CE"), (strike, "PE")])
    if absent:
        return _refuse(f"Straddle {strike:.0f}", lot_size, index, absent)
    legs = [
        Leg("CE", strike, side, float(premiums[(strike, "CE")]), lots),
        Leg("PE", strike, side, float(premiums[(strike, "PE")]), lots),
    ]
    structure = build_structure(
        f"{'Long' if side == 'BUY' else 'Short'} straddle {strike:.0f}",
        legs, lot_size, index=index, costs=costs,
    )
    if side == "BUY":
        total = legs[0].premium + legs[1].premium
        structure.notes.append(
            f"Breakeven needs a move of about {total:.0f} points either way "
            f"before costs -- that is what the chain is charging for movement, "
            f"and it is the figure your expected move has to beat."
        )
    else:
        structure.notes.append(
            "The defined-risk version of this view is an iron butterfly: same "
            "short strike, with wings bought to cap both tails."
        )
    return structure


def strangle(
    index: str, put_strike: float, call_strike: float, premiums: dict,
    lot_size: int, *, action: str = "SELL", lots: int = 1,
    costs: OptionsCostModel | None = None,
) -> Structure:
    """Out-of-the-money put and call. A straddle with a dead zone in the middle.

    Short, this is the classic premium sale: it wins while price stays between
    the strikes, and the credit is smaller than a straddle's because both legs
    start out of the money. Risk is undefined on both sides -- the iron condor
    is this trade with wings.

    Long, it is a cheaper bet on a large move than a straddle, and needs a
    bigger one to pay.
    """
    side = action.upper()
    if put_strike >= call_strike:
        structure = Structure(name="Invalid strangle", legs=[], lot_size=lot_size,
                              index=index)
        structure.warnings.append(
            f"A strangle needs the put strike below the call strike; got "
            f"{put_strike:.0f} and {call_strike:.0f}. At or across each other "
            f"this is a straddle or a guts, priced differently."
        )
        return structure

    absent = missing_premiums(
        premiums, [(put_strike, "PE"), (call_strike, "CE")])
    if absent:
        return _refuse(f"Strangle {put_strike:.0f}/{call_strike:.0f}",
                       lot_size, index, absent)
    legs = [
        Leg("PE", put_strike, side, float(premiums[(put_strike, "PE")]), lots),
        Leg("CE", call_strike, side, float(premiums[(call_strike, "CE")]), lots),
    ]
    structure = build_structure(
        f"{'Long' if side == 'BUY' else 'Short'} strangle "
        f"{put_strike:.0f}/{call_strike:.0f}",
        legs, lot_size, index=index, costs=costs,
    )
    structure.notes.append(
        f"The dead zone is {put_strike:.0f} to {call_strike:.0f} "
        f"({call_strike - put_strike:.0f} points wide)."
        + ("" if side == "BUY" else
           " Price staying inside it is the winning case, and time helps.")
    )
    return structure


def butterfly(
    index: str, option_type: str, lower: float, body: float, upper: float,
    premiums: dict, lot_size: int, *, lots: int = 1,
    costs: OptionsCostModel | None = None,
) -> Structure:
    """Buy one wing, sell two of the body, buy the other wing. Defined risk.

    A pinning structure: maximum profit sits exactly at the body strike, so it
    is the natural trade for the chain state ``options_math.detect_pin``
    already identifies -- call wall, put wall and max pain on one strike, with
    both sides of the chain defending it. The app has been able to *see* a pin
    since 2026-10-21 and has had no structure to express it with.

    Risk is defined in both directions because the two short bodies are covered
    by the two long wings, which is what makes this the honest alternative to
    selling a straddle at the pin.

    Wings need not be equidistant; an unbalanced one is a
    :func:`broken_wing_butterfly`.
    """
    kind = option_type.upper()
    if not lower < body < upper:
        structure = Structure(name="Invalid butterfly", legs=[],
                              lot_size=lot_size, index=index)
        structure.warnings.append(
            f"A butterfly needs lower < body < upper; got {lower:.0f}, "
            f"{body:.0f}, {upper:.0f}."
        )
        return structure

    absent = missing_premiums(
        premiums, [(lower, kind), (body, kind), (upper, kind)])
    if absent:
        return _refuse(
            f"{kind} butterfly {lower:.0f}/{body:.0f}/{upper:.0f}",
            lot_size, index, absent)
    legs = [
        Leg(kind, lower, "BUY", float(premiums[(lower, kind)]), lots),
        Leg(kind, body, "SELL", float(premiums[(body, kind)]), 2 * lots),
        Leg(kind, upper, "BUY", float(premiums[(upper, kind)]), lots),
    ]
    structure = build_structure(
        f"{kind} butterfly {lower:.0f}/{body:.0f}/{upper:.0f}",
        legs, lot_size, index=index, costs=costs,
    )
    structure.notes.append(
        f"Best case is price finishing at {body:.0f}. Six orders round trip, "
        f"so this needs a wide enough body-to-wing distance to pay for itself."
    )
    return structure


def iron_butterfly(
    index: str, body: float, lower: float, upper: float, premiums: dict,
    lot_size: int, *, lots: int = 1, costs: OptionsCostModel | None = None,
) -> Structure:
    """Short straddle at the body with both wings bought. Defined risk.

    This is the structure to reach for whenever a short straddle is tempting.
    It collects a smaller credit and converts an unbounded tail into a known
    maximum loss, which is the difference between a trade that can be sized and
    one that cannot.

    Compared with an iron condor it earns more and has a narrower winning zone:
    the condor's short strikes straddle a range, the butterfly's sit on one
    point, so it is the sharper expression of a pin.
    """
    if not lower < body < upper:
        structure = Structure(name="Invalid iron butterfly", legs=[],
                              lot_size=lot_size, index=index)
        structure.warnings.append(
            f"Need lower < body < upper; got {lower:.0f}, {body:.0f}, "
            f"{upper:.0f}."
        )
        return structure

    absent = missing_premiums(premiums, [
        (lower, "PE"), (body, "PE"), (body, "CE"), (upper, "CE")])
    if absent:
        return _refuse(
            f"Iron butterfly {lower:.0f}/{body:.0f}/{upper:.0f}",
            lot_size, index, absent)
    legs = [
        Leg("PE", lower, "BUY", float(premiums[(lower, "PE")]), lots),
        Leg("PE", body, "SELL", float(premiums[(body, "PE")]), lots),
        Leg("CE", body, "SELL", float(premiums[(body, "CE")]), lots),
        Leg("CE", upper, "BUY", float(premiums[(upper, "CE")]), lots),
    ]
    structure = build_structure(
        f"Iron butterfly {lower:.0f}/{body:.0f}/{upper:.0f}",
        legs, lot_size, index=index, costs=costs,
    )
    structure.notes.append(
        f"Maximum profit at exactly {body:.0f}; both tails are capped by the "
        f"wings. This is the short straddle made sizeable."
    )
    return structure


def ratio_spread(
    index: str, option_type: str, long_strike: float, short_strike: float,
    premiums: dict, lot_size: int, *, long_lots: int = 1, short_lots: int = 2,
    costs: OptionsCostModel | None = None,
) -> Structure:
    """Buy one, sell more than one further out. Often free, never safe.

    A ratio spread can be entered for zero cost or a credit, which is exactly
    why it is popular and exactly why it is dangerous: selling two against one
    leaves you net short an option, so the far side has **undefined risk** and
    :func:`build_structure` labels it so.

    It is included for completeness and because the shape appears constantly in
    retail strategy lists presented as "free". The cost is not zero; it is
    deferred into a tail. :func:`broken_wing_butterfly` is the same idea with
    the tail bought back.
    """
    kind = option_type.upper()
    absent = missing_premiums(
        premiums, [(long_strike, kind), (short_strike, kind)])
    if absent:
        return _refuse(
            f"{kind} ratio spread {long_strike:.0f}/{short_strike:.0f}",
            lot_size, index, absent)
    legs = [
        Leg(kind, long_strike, "BUY", float(premiums[(long_strike, kind)]),
            long_lots),
        Leg(kind, short_strike, "SELL", float(premiums[(short_strike, kind)]),
            short_lots),
    ]
    structure = build_structure(
        f"{kind} ratio spread {long_strike:.0f} x{long_lots} / "
        f"{short_strike:.0f} x{short_lots}",
        legs, lot_size, index=index, costs=costs,
    )
    if structure.net_credit > 0:
        structure.notes.append(
            f"Entered for a Rs {structure.net_credit:,.0f} credit, which is "
            f"what makes this look free. It is not free: the extra short leg "
            f"is the price, payable in a tail."
        )
    return structure


def broken_wing_butterfly(
    index: str, option_type: str, lower: float, body: float, upper: float,
    premiums: dict, lot_size: int, *, lots: int = 1,
    costs: OptionsCostModel | None = None,
) -> Structure:
    """A butterfly with unequal wings, so one side is cheap and one is capped.

    The point of the asymmetry is to enter for a credit, or near zero, while
    keeping risk **defined** -- which is what separates it from a
    :func:`ratio_spread`. The wider wing is the side you are willing to be
    wrong on; the narrow one is the side you are not.

    Identical construction to :func:`butterfly`; the name is kept separate
    because the asymmetry is the whole intent and a reader scanning for
    "butterfly" should not be told these are the same trade.
    """
    structure = butterfly(index, option_type, lower, body, upper, premiums,
                          lot_size, lots=lots, costs=costs)
    if not structure.legs:
        return structure

    lower_wing = body - lower
    upper_wing = upper - body
    structure.name = (
        f"{option_type.upper()} broken-wing butterfly "
        f"{lower:.0f}/{body:.0f}/{upper:.0f}"
    )
    if abs(lower_wing - upper_wing) < 1e-9:
        structure.notes.append(
            "The wings are equal, so this is an ordinary butterfly. Widen one "
            "side for the credit a broken wing is meant to collect."
        )
    else:
        wide = "upper" if upper_wing > lower_wing else "lower"
        structure.notes.append(
            f"The {wide} wing is the wider one ({max(lower_wing, upper_wing):.0f} "
            f"vs {min(lower_wing, upper_wing):.0f} points), so that is the side "
            f"carrying the larger loss. Risk stays defined -- that is the whole "
            f"difference from a ratio spread."
        )
    return structure


def premiums_from_chain(frame) -> dict:
    """``{(strike, 'CE'|'PE'): premium}`` from the chain table."""
    out: dict[tuple[float, str], float] = {}
    if frame is None or getattr(frame, "empty", True):
        return out
    for _, row in frame.iterrows():
        strike = float(row["strike"])
        for side, column in (("CE", "ce_ltp"), ("PE", "pe_ltp")):
            price = float(row.get(column, 0) or 0)
            if price > 0:
                out[(strike, side)] = price
    return out


# --------------------------------------------------------------------------- #
# Does it actually pay? Probability, not reward:risk
# --------------------------------------------------------------------------- #

def _normal_cdf(x: float) -> float:
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def assess_edge(
    structure: Structure, *, spot: float, iv: float, days_to_expiry: float,
) -> dict:
    """Does this structure pay, once probability is accounted for?

    **Reward:risk alone is the wrong lens**, and reading it as if it were the
    answer is how a credit spread gets mistaken for a good trade. A credit
    spread wins often and small and loses rarely and large, so a reward:risk of
    0.30 is not a bad trade -- it is a trade that needs a **77% win rate**. The
    only question that matters is whether the market is offering that.

    So: convert reward:risk into the win rate it requires, estimate the win
    rate the market's own volatility implies, and report the gap.

    Measured live on 2026-10-21 at NIFTY 22,690 with IV 15.2% and 6 days left
    (a 1-sigma move of 442 points):

        bear put debit  22700/22500   needs 41.3%   implies 43.8%   +2.4pp
        bull put credit 22500/22400   needs 76.9%   implies 68.7%   -8.2pp
        bull put credit 22600/22500   needs 68.5%   implies 61.1%   -7.4pp
        bear call credit 22900/23000  needs 77.5%   implies 70.3%   -7.3pp

    Three of those four looked like sensible premium-collection trades and all
    three were negative expectancy. Nothing on the dashboard would have said so.

    Honest limits
    -------------
    The implied win rate assumes lognormal terminal prices with zero drift,
    which is the market's own pricing assumption, not a forecast. It ignores
    the chance of being stopped out *before* expiry, so for a trade that will
    be managed rather than held it is an upper bound on the win rate, and the
    edge shown here is therefore optimistic.
    """
    import math

    if structure.net_max_loss <= 0 or spot <= 0 or iv <= 0 or days_to_expiry <= 0:
        return {"ok": False, "reason": "Not enough information to assess edge."}
    if not structure.breakevens:
        return {"ok": False, "reason": "No breakeven — payoff never crosses zero."}

    sigma_points = spot * iv * math.sqrt(days_to_expiry / 365.0)
    required = 1.0 / (1.0 + structure.net_rr) if structure.net_rr > 0 else 1.0

    # Sum the probability over EVERY profitable region of the payoff.
    #
    # The first version of this took `breakevens[0]` and asked which side of it
    # won. That is right for a two-leg vertical, which has one breakeven, and
    # badly wrong for anything with two. On an iron condor -- which profits
    # BETWEEN its breakevens -- it computed the probability of finishing below
    # the lower one, i.e. the probability of losing on the downside, and
    # reported it as the win rate. It called a 28%-likely trade a 37.7% one.
    #
    # A piecewise-linear payoff changes sign only at a breakeven, so testing
    # the midpoint of each region is exact, not sampled.
    edges = sorted(structure.breakevens)
    bounds = [None] + edges + [None]
    implied = 0.0
    regions: list[dict] = []
    for i in range(len(bounds) - 1):
        low, high = bounds[i], bounds[i + 1]
        if low is None:
            probe = high - sigma_points
        elif high is None:
            probe = low + sigma_points
        else:
            probe = (low + high) / 2.0
        if structure.payoff_at(probe) <= 0:
            continue
        lower_cdf = 0.0 if low is None else _normal_cdf((low - spot) / sigma_points)
        upper_cdf = 1.0 if high is None else _normal_cdf((high - spot) / sigma_points)
        mass = max(0.0, upper_cdf - lower_cdf)
        implied += mass
        regions.append({
            "from": None if low is None else round(low, 2),
            "to": None if high is None else round(high, 2),
            "probability": round(mass, 4),
        })

    edge = implied - required
    return {
        "ok": True,
        "sigma_points": round(sigma_points, 1),
        "breakevens": [round(x, 2) for x in edges],
        "profitable_regions": regions,
        "required_win_rate": round(required, 4),
        "implied_win_rate": round(implied, 4),
        "edge_pp": round(edge * 100, 1),
        "verdict": (
            "positive expectancy" if edge > 0.02 else
            "marginal" if edge > -0.02 else
            "negative expectancy"
        ),
        "note": (
            f"Needs {required * 100:.1f}% to break even; the market's own "
            f"volatility implies {implied * 100:.1f}%. "
            + (
                "That is an edge." if edge > 0.02 else
                "Too close to call at this cost." if edge > -0.02 else
                "You are being asked to accept worse odds than the payoff pays."
            )
        ),
    }


def time_is_sufficient(structure: Structure, minutes_left: float,
                       days_to_expiry: float) -> dict:
    """Can this structure earn its cost in the time remaining today?

    An intraday position is squared off at 15:15 whatever its expiry, so a
    multi-leg structure opened late pays 100% of its entry-and-exit cost to
    capture a sliver of a multi-day edge. Four orders is roughly Rs 160-210 on
    a one-lot NIFTY spread; thirty minutes of a six-day structure is about
    0.3% of its time value.
    """
    if days_to_expiry <= 0 or minutes_left <= 0:
        return {"ok": False, "reason": "No time left."}
    session_minutes = 375.0                      # 09:15 to 15:30
    fraction = minutes_left / (days_to_expiry * session_minutes)
    harvestable = structure.max_profit * fraction
    ok = harvestable > structure.round_trip_cost
    return {
        "ok": ok,
        "minutes_left": round(minutes_left),
        "fraction_of_life": round(fraction, 4),
        "harvestable": round(harvestable, 2),
        "cost": structure.round_trip_cost,
        "note": (
            f"About {fraction * 100:.1f}% of this structure's life remains today, "
            f"worth roughly Rs {harvestable:,.0f} of its Rs "
            f"{structure.max_profit:,.0f} best case, against Rs "
            f"{structure.round_trip_cost:,.0f} to get in and out. "
            + ("Worth opening." if ok else
               "Opening it now pays the full cost for a fraction of the edge.")
        ),
    }
