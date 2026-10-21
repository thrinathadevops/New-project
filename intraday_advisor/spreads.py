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
    probes = [max(0.01, strikes[0] - 5 * max(1.0, strikes[-1] - strikes[0]))]
    probes += strikes
    probes += [strikes[-1] + 5 * max(1.0, strikes[-1] - strikes[0])]
    payoffs = [structure.payoff_at(p) for p in probes]
    structure.max_profit = round(max(payoffs), 2)
    structure.max_loss = round(abs(min(payoffs)), 2)

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
    structure.net_rr = (
        round(structure.net_max_profit / structure.net_max_loss, 2)
        if structure.net_max_loss > 0 else 0.0
    )

    if structure.net_max_profit <= 0:
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
