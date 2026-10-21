"""Would a resting order have filled, and what did filling cost?

This collects the only measurements that let
:mod:`intraday_advisor.order_execution` claim a passive saving. Without them a
passive plan is priced as if it crossed, which is the correct default and also
a permanently pessimistic one -- so somebody has to go and measure.

What it can and cannot establish
---------------------------------
It observes **quotes**, not fills. A real fill depends on queue position: being
at the right price is necessary and not sufficient, because everyone already
resting at that price trades ahead of you. So what this measures is

    P(the market traded through my limit price)

which is an **upper bound** on P(my order filled). Every result carries that
caveat, and the bound is reported rather than quietly treated as the answer. A
62% "fill rate" here might be a 30% real one.

The only way to measure the true rate is to rest real orders and count, which
needs an order-placement path this project deliberately does not have. Until
then the upper bound is useful for one specific purpose: **ruling the idea
out.** If passive execution fails to pay even at its optimistic bound, it
cannot pay in reality, and that is a conclusion worth having cheaply.

The adverse-selection term
--------------------------
Measured as the mid's move *against* the intended side, conditional on the
limit being touched. Buying at mid and watching mid fall is the cost that
replaces the spread, and it is the term that decides whether resting is worth
anything -- see ``break_even_fill_rate``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("fill_study")

#: How long a resting order is given before it is considered unfilled.
DEFAULT_HORIZON_SECONDS = 30.0

#: Below this many paired observations nothing is reported as a rate.
MIN_OBSERVATIONS = 30


@dataclass
class Observation:
    """One contract, sampled twice: when posted, and at the deadline."""

    token: str
    symbol: str
    side: str                      # the side we intended to take
    posted_at: float
    limit_price: float             # where we would have rested (the mid)
    mid_at_post: float
    ask_at_post: float
    bid_at_post: float
    #: Filled at the deadline.
    resolved_at: float = 0.0
    mid_at_resolve: float | None = None
    best_opposing: float | None = None    # best ask (buy) / bid (sell) seen
    touched: bool | None = None           # did the market reach our limit?

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


@dataclass
class FillStudyResult:
    """What the sampled quotes say about resting an order."""

    observations: int = 0
    touched: int = 0
    #: UPPER BOUND on the fill rate -- see the module docstring.
    touch_rate: float = 0.0
    adverse_selection_pct: float = 0.0
    chase_penalty_pct: float = 0.0
    horizon_seconds: float = DEFAULT_HORIZON_SECONDS
    per_symbol: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    usable: bool = False

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

    def to_fill_statistics(self):
        """Convert to what :mod:`order_execution` consumes.

        Deliberately returns ``None`` below the minimum sample, so a thin study
        cannot be mistaken for a measurement downstream.
        """
        from intraday_advisor.order_execution import FillStatistics

        if not self.usable:
            return None
        return FillStatistics(
            fill_rate=self.touch_rate,
            adverse_selection_pct=self.adverse_selection_pct,
            chase_penalty_pct=self.chase_penalty_pct,
            samples=self.observations,
            horizon_seconds=self.horizon_seconds,
            note=(
                "Derived from quote movement, so the fill rate is an UPPER "
                "BOUND: being at the price is necessary, not sufficient, "
                "because queue position decides who trades."
            ),
        )


def observe(
    token: str, symbol: str, side: str, book, *, now: float | None = None,
) -> Observation | None:
    """Record where a resting order would have gone, from a live book."""
    bid, ask, mid = book.best_bid, book.best_ask, book.mid
    if not bid or not ask or not mid or ask <= bid:
        return None
    return Observation(
        token=str(token), symbol=symbol, side=side.lower(),
        posted_at=now if now is not None else time.time(),
        limit_price=float(mid), mid_at_post=float(mid),
        ask_at_post=float(ask), bid_at_post=float(bid),
    )


def resolve(observation: Observation, book, *, now: float | None = None) -> Observation:
    """Close an observation against the book at the deadline.

    ``touched`` asks whether the market came to our price: for a buy resting at
    mid, the best ask must have fallen to or below it; for a sell, the best bid
    must have risen to or above it.
    """
    observation.resolved_at = now if now is not None else time.time()
    observation.mid_at_resolve = book.mid
    if observation.side == "buy":
        observation.best_opposing = book.best_ask
        observation.touched = (
            book.best_ask is not None and book.best_ask <= observation.limit_price
        )
    else:
        observation.best_opposing = book.best_bid
        observation.touched = (
            book.best_bid is not None and book.best_bid >= observation.limit_price
        )
    return observation


def summarise(
    observations: list[Observation], *,
    horizon_seconds: float = DEFAULT_HORIZON_SECONDS,
) -> FillStudyResult:
    """Turn paired observations into the three numbers execution needs."""
    result = FillStudyResult(horizon_seconds=horizon_seconds)
    paired = [o for o in observations
              if o.touched is not None and o.mid_at_resolve is not None]
    result.observations = len(paired)
    if not paired:
        result.notes.append("No resolved observations.")
        return result

    touched = [o for o in paired if o.touched]
    missed = [o for o in paired if not o.touched]
    result.touched = len(touched)
    result.touch_rate = round(len(touched) / len(paired), 4)

    # Adverse selection: the mid's move AGAINST the intended side, given a
    # touch. Buying and then watching mid fall is the cost that replaces the
    # spread.
    adverse: list[float] = []
    for o in touched:
        if not o.mid_at_post:
            continue
        move = (o.mid_at_resolve - o.mid_at_post) / o.mid_at_post
        against = -move if o.side == "buy" else move
        adverse.append(against)
    if adverse:
        result.adverse_selection_pct = round(sum(adverse) / len(adverse), 6)

    # Chase penalty: having failed to fill, what does crossing now cost versus
    # crossing at the start? Positive means waiting made it worse.
    chase: list[float] = []
    for o in missed:
        start = o.ask_at_post if o.side == "buy" else o.bid_at_post
        later = o.best_opposing
        if not start or later is None or not o.mid_at_post:
            continue
        worse = (later - start) if o.side == "buy" else (start - later)
        chase.append(worse / o.mid_at_post)
    if chase:
        result.chase_penalty_pct = round(sum(chase) / len(chase), 6)

    by_symbol: dict[str, dict] = {}
    for o in paired:
        entry = by_symbol.setdefault(o.symbol, {"n": 0, "touched": 0})
        entry["n"] += 1
        entry["touched"] += 1 if o.touched else 0
    for entry in by_symbol.values():
        entry["touch_rate"] = round(entry["touched"] / entry["n"], 3)
    result.per_symbol = by_symbol

    result.usable = len(paired) >= MIN_OBSERVATIONS
    if not result.usable:
        result.notes.append(
            f"{len(paired)} observations is below the {MIN_OBSERVATIONS} "
            f"needed; this is not yet a measurement."
        )
    result.notes.append(
        f"Touch rate {result.touch_rate:.0%} is an UPPER BOUND on the fill "
        f"rate: queue position decides who actually trades at a price, so the "
        f"real rate is lower by an unknown amount."
    )
    if result.adverse_selection_pct > 0:
        result.notes.append(
            f"Conditional on being touched, the mid moved "
            f"{result.adverse_selection_pct * 100:.3f}% against the intended "
            f"side -- this is the cost that replaces the spread."
        )
    return result


def verdict(result: FillStudyResult, *, premium: float, quantity: float,
            costs=None) -> dict:
    """Does passive execution pay, even at its optimistic bound?

    The useful asymmetry: because the touch rate is an upper bound and the
    adverse selection is measured, a **negative** answer here is conclusive --
    if resting fails to pay at its best case it cannot pay in practice. A
    positive answer is only a reason to go and measure real fills.
    """
    from intraday_advisor.order_execution import (
        AGGRESSIVE,
        PASSIVE,
        price_execution,
    )

    stats = result.to_fill_statistics()
    if stats is None:
        return {
            "ok": False,
            "reason": "Not enough observations to reach a verdict.",
            "observations": result.observations,
        }

    crossing = price_execution(premium=premium, quantity=quantity,
                               style=AGGRESSIVE, costs=costs)
    resting = price_execution(premium=premium, quantity=quantity,
                              style=PASSIVE, costs=costs, fills=stats)
    saving = crossing.expected_cost_pct - resting.expected_cost_pct
    pays = saving > 0
    return {
        "ok": True,
        "crossing_pct": crossing.expected_cost_pct,
        "resting_pct": resting.expected_cost_pct,
        "saving_pp": round(saving, 4),
        "pays_at_upper_bound": pays,
        "conclusive": not pays,
        "note": (
            (
                f"Even at the optimistic bound, resting saves only "
                f"{saving:+.3f}pp. Because the bound overstates the fill rate, "
                f"this is conclusive: passive execution does not pay here."
            ) if not pays else (
                f"At the optimistic bound resting saves {saving:.3f}pp. That is "
                f"a reason to measure real fills, not a result -- the true fill "
                f"rate is lower than the {result.touch_rate:.0%} touch rate."
            )
        ),
    }
