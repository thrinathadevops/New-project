"""Honest odds for one specific trade, before it is placed.

The question nothing here could answer
--------------------------------------
``validation.py`` answers "does this rule have an edge", with HAC errors, a
block bootstrap and overlap-free corroboration. That is the right question for
a strategy and the wrong one for a decision: it cannot tell you the odds on
*this* setup, at this hour, with this stop and this target.

The idea is borrowed from the `day-trader-skill` pack's "Time Machine", which
replays the rest of a session from the stock's own recent bars. Two things are
changed, and both changes matter more than the idea itself.

**1. Costs are charged.** The original has no cost model at all -- no spread,
no brokerage, no slippage -- so its headline example (target 29.4%, stop 25.5%,
"close to a coin flip") is a coin flip *before* costs and a loser after them.
In this project costs are the binding constraint, not a detail: ``microstructure``
measures ~1.25% round trip on a one-lot index option, which is larger than any
edge this project has ever found. So :func:`required_hit_rate` computes the win
rate the trade needs purely to break even, from the real premium, the real lot
size and the measured spread, and :func:`assess` compares the replay's odds
against *that* rather than against 50%.

**2. Blocks, not single bars.** The original draws each simulated bar
independently from a pool of past bars at the same time of day. That destroys
serial correlation -- and these indices mean-revert at five minutes, with
measured lag-1 autocorrelation of **-0.088 (NIFTY) and -0.099 (BANKNIFTY)**
against a significance threshold of 0.030. Resampling independently makes
simulated paths trend more than the real index does, which inflates the odds of
a breakout target being reached. This draws **contiguous blocks**, the same
correction ``validation.block_bootstrap_pvalue`` already applies, so the
reversion survives the resample.

What it still cannot do
-----------------------
It replays the last 60 days. Earnings, policy, a gap or a halt can do what the
last 60 days never did, and a simulation built from a quiet sample will not
know a violent day is possible. It is a statement about the recent past's shape,
not a forecast -- and it reports the honest result when that result is "this is
a coin flip you are paying to enter", which is most of the time.

The conservative conventions are kept from the original and they are the right
ones: a bar touching both stop and target counts as the **stop**, and a gap
through the stop still counts a full -1R although a real fill can be worse.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

#: Bars per contiguous draw. One 5-minute bar of autocorrelation is not enough
#: to carry mean reversion into the resample; half an hour is. Matches the
#: "block at least 2x the horizon of the dependence" rule used in validation.
BLOCK_BARS = 6

#: Replays per run. 10,000 puts the Monte Carlo error on a ~30% probability at
#: about +/-0.5 points, well inside the precision anyone should act on.
REPLAYS = 10_000

#: Fixed so the same trade and the same data give the same answer twice. A risk
#: number that changes when you re-ask it cannot be argued with.
SEED = 19

#: Below this many past sessions the pool is too thin to resample honestly.
MIN_SESSIONS = 20


@dataclass
class Odds:
    """Replay outcome counts and what they imply, before any cost."""

    replays: int = 0
    target_first: float = 0.0
    stop_first: float = 0.0
    neither: float = 0.0
    sessions_in_pool: int = 0
    bars_simulated: int = 0
    note: str = ""

    @property
    def decided(self) -> float:
        """Share of replays that hit one side or the other."""
        return self.target_first + self.stop_first

    @property
    def hit_rate(self) -> float:
        """P(target | the trade was decided by a level). 0.0 when none were."""
        return self.target_first / self.decided if self.decided else 0.0

    def as_dict(self) -> dict:
        from dataclasses import asdict
        data = asdict(self)
        data["hit_rate"] = round(self.hit_rate, 4)
        data["decided"] = round(self.decided, 4)
        return data


@dataclass
class Assessment:
    """The replay's odds set against the rate the costs demand."""

    odds: Odds | None = None
    reward_risk: float = 0.0
    breakeven_rate_gross: float = 0.0
    breakeven_rate_net: float = 0.0
    round_trip_cost: float = 0.0
    risk_rupees: float = 0.0
    reward_rupees: float = 0.0
    edge_points: float = 0.0
    verdict: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        data = asdict(self)
        data["odds"] = self.odds.as_dict() if self.odds else None
        return data


# ================================================================= the pool ==

def session_blocks(bars, *, min_sessions: int = MIN_SESSIONS) -> list[list[tuple]]:
    """Past sessions as per-bar ratios to the previous close.

    Ratios rather than prices, so a session from a different price level still
    contributes its *shape*. Each session is kept whole and separate: blocks are
    drawn from inside one session, never spliced across an overnight gap, which
    would inject a jump the intraday path cannot have.
    """
    import pandas as pd

    if bars is None or len(bars) == 0:
        return []
    frame = bars[["Open", "High", "Low", "Close"]].dropna()
    if frame.empty:
        return []

    index = pd.to_datetime(frame.index, utc=True, errors="coerce")
    days = index.tz_convert("Asia/Kolkata").date

    sessions: list[list[tuple]] = []
    for day in sorted(set(days)):
        chunk = frame[days == day]
        if len(chunk) < BLOCK_BARS * 2:
            continue
        closes = chunk["Close"].to_numpy(dtype=float)
        ratios: list[tuple] = []
        for i in range(1, len(chunk)):
            previous = closes[i - 1]
            if previous <= 0:
                continue
            row = chunk.iloc[i]
            ratios.append((
                float(row["Open"]) / previous,
                float(row["High"]) / previous,
                float(row["Low"]) / previous,
                float(row["Close"]) / previous,
            ))
        if len(ratios) >= BLOCK_BARS * 2:
            sessions.append(ratios)
    return sessions if len(sessions) >= min_sessions else []


# ================================================================== replays ==

def _walk(price: float, path, stop: float, target: float, *, long: bool):
    """One replay. Returns ('target'|'stop'|'neither', final price).

    The stop is tested before the target in every bar, so a bar straddling both
    counts as the stop. That is the conservative reading and it is the correct
    one: without tick data inside the bar you cannot know which came first, and
    assuming the good one flatters every result.
    """
    for ratio_open, ratio_high, ratio_low, ratio_close in path:
        high = price * ratio_high
        low = price * ratio_low
        if long:
            if low <= stop:
                return "stop", stop
            if high >= target:
                return "target", target
        else:
            if high >= stop:
                return "stop", stop
            if low <= target:
                return "target", target
        price = price * ratio_close
    return "neither", price


def forward_odds(
    bars, *, entry: float, stop: float, target: float, bars_left: int,
    replays: int = REPLAYS, seed: int = SEED, block: int = BLOCK_BARS,
) -> Odds:
    """Replay the rest of the session and count how it ends.

    ``bars_left`` is how many bars remain before square-off -- the trade is
    intraday, so "still open at the close" is a real third outcome and is
    reported rather than folded into a win or a loss.
    """
    long = target > entry
    if long and not (stop < entry < target):
        return Odds(note="A long needs stop < entry < target.")
    if not long and not (target < entry < stop):
        return Odds(note="A short needs target < entry < stop.")
    if bars_left <= 0:
        return Odds(note="No bars left in the session; nothing to replay.")

    sessions = session_blocks(bars)
    if not sessions:
        return Odds(note=(
            f"Fewer than {MIN_SESSIONS} usable past sessions. The pool would be "
            f"near-copies of a handful of days, so no odds are reported."
        ))

    rng = random.Random(seed)
    counts = {"target": 0, "stop": 0, "neither": 0}
    block = max(1, int(block))
    for _ in range(replays):
        path: list[tuple] = []
        while len(path) < bars_left:
            source = sessions[rng.randrange(len(sessions))]
            start = rng.randrange(max(1, len(source) - block))
            path.extend(source[start:start + block])
        outcome, _ = _walk(entry, path[:bars_left], stop, target, long=long)
        counts[outcome] += 1

    return Odds(
        replays=replays,
        target_first=counts["target"] / replays,
        stop_first=counts["stop"] / replays,
        neither=counts["neither"] / replays,
        sessions_in_pool=len(sessions),
        bars_simulated=bars_left,
        note=(f"{replays:,} replays from {len(sessions)} past sessions, "
              f"contiguous {block}-bar blocks, seed {seed}."),
    )


def history_leg(
    bars, *, stop_pct: float, target_pct: float, bars_left: int,
    long: bool = True,
) -> dict:
    """Re-run the same stop/target on every real past session, one each.

    The replay says what *could* happen given the recent distribution; this says
    what *did*, on real paths with their real ordering intact. One observation
    per session, so the sessions are independent of each other -- the unit that
    does not lie, as ``ledger_cli sessions`` puts it.
    """
    sessions = session_blocks(bars)
    if not sessions:
        return {"sessions": 0,
                "note": f"Fewer than {MIN_SESSIONS} usable sessions."}

    counts = {"target": 0, "stop": 0, "neither": 0}
    for ratios in sessions:
        entry = 100.0
        stop = entry * (1 - stop_pct) if long else entry * (1 + stop_pct)
        target = entry * (1 + target_pct) if long else entry * (1 - target_pct)
        outcome, _ = _walk(entry, ratios[:bars_left], stop, target, long=long)
        counts[outcome] += 1

    total = len(sessions)
    decided = counts["target"] + counts["stop"]
    return {
        "sessions": total,
        "target_first": counts["target"],
        "stop_first": counts["stop"],
        "neither": counts["neither"],
        "hit_rate": round(counts["target"] / decided, 4) if decided else 0.0,
        "note": (f"The same stop and target run on {total} real sessions, "
                 f"one observation each."),
    }


# ============================================================ the cost hurdle ==

def required_hit_rate(
    *, entry_premium: float, stop_premium: float, target_premium: float,
    quantity: float, costs=None,
) -> dict:
    """The win rate this trade needs to break even, gross and after costs.

    This is the number the borrowed idea was missing. A 1:1 trade needs 50% to
    break even before costs; what it needs *after* them is the only version that
    decides anything, and at index-option premiums the difference is not small.
    """
    from intraday_advisor.ledger.costs import round_trip_cost

    reward = (target_premium - entry_premium) * quantity
    risk = (entry_premium - stop_premium) * quantity
    if reward <= 0 or risk <= 0:
        return {"error": "target must be above and stop below the entry premium"}

    # Charge each leg at the premium it would actually trade at: a win exits at
    # the target, a loss exits at the stop. Averaging the two would understate
    # the cost of the outcome that matters more.
    win_cost = round_trip_cost(entry_premium, target_premium, quantity,
                               costs=costs)["total"]
    loss_cost = round_trip_cost(entry_premium, stop_premium, quantity,
                                costs=costs)["total"]

    net_reward = reward - win_cost
    net_risk = risk + loss_cost
    gross = risk / (reward + risk)
    net = (net_risk / (net_reward + net_risk)) if net_reward > 0 else 1.0

    return {
        "reward_rupees": round(reward, 2),
        "risk_rupees": round(risk, 2),
        "reward_risk": round(reward / risk, 3),
        "win_cost": round(win_cost, 2),
        "loss_cost": round(loss_cost, 2),
        "round_trip_cost": round((win_cost + loss_cost) / 2, 2),
        "breakeven_rate_gross": round(gross, 4),
        "breakeven_rate_net": round(net, 4),
        "unwinnable": net_reward <= 0,
        "note": (
            "costs exceed the entire target, so no win rate breaks even"
            if net_reward <= 0 else
            f"needs {net:.1%} after costs against {gross:.1%} before them"
        ),
    }


def assess(
    bars, *, entry_premium: float, stop_premium: float, target_premium: float,
    quantity: float, entry: float, stop: float, target: float,
    bars_left: int, costs=None, replays: int = REPLAYS, seed: int = SEED,
) -> Assessment:
    """Replay the underlying, price the costs, and compare the two.

    ``entry``/``stop``/``target`` are **underlying** levels -- the replay is
    built from index bars, which is the only series with 60 days of history.
    The ``*_premium`` values are the option's, which is where costs are paid.
    Keeping them separate avoids inventing a premium path from an index one.
    """
    result = Assessment()
    hurdle = required_hit_rate(
        entry_premium=entry_premium, stop_premium=stop_premium,
        target_premium=target_premium, quantity=quantity, costs=costs,
    )
    if "error" in hurdle:
        result.verdict = "cannot assess"
        result.notes.append(hurdle["error"])
        return result

    result.reward_risk = hurdle["reward_risk"]
    result.breakeven_rate_gross = hurdle["breakeven_rate_gross"]
    result.breakeven_rate_net = hurdle["breakeven_rate_net"]
    result.round_trip_cost = hurdle["round_trip_cost"]
    result.risk_rupees = hurdle["risk_rupees"]
    result.reward_rupees = hurdle["reward_rupees"]

    if hurdle["unwinnable"]:
        result.verdict = "REFUSED: costs exceed the target"
        result.notes.append(
            f"The round trip costs Rs {hurdle['win_cost']:,.0f} against a "
            f"Rs {hurdle['reward_rupees']:,.0f} target. No hit rate makes this "
            f"profitable, so the odds are not worth computing."
        )
        return result

    result.odds = forward_odds(
        bars, entry=entry, stop=stop, target=target,
        bars_left=bars_left, replays=replays, seed=seed,
    )
    if not result.odds.replays:
        result.verdict = "no odds available"
        result.notes.append(result.odds.note)
        return result

    result.edge_points = round(
        (result.odds.hit_rate - result.breakeven_rate_net) * 100, 1)

    gap = result.edge_points
    if gap < -5:
        result.verdict = "ODDS AGAINST, clearly"
    elif gap < 5:
        result.verdict = "COIN FLIP you are paying to enter"
    elif gap < 15:
        result.verdict = "leans toward the target"
    else:
        result.verdict = "strongly toward the target"

    result.notes.append(
        f"Replay hit rate {result.odds.hit_rate:.1%} against "
        f"{result.breakeven_rate_net:.1%} needed after costs "
        f"({result.breakeven_rate_gross:.1%} before) — "
        f"{gap:+.1f} points."
    )
    if result.odds.neither > 0.40:
        result.notes.append(
            f"{result.odds.neither:.0%} of replays were still open at the "
            f"close, so the square-off price decides most of this trade, not "
            f"the stop or the target."
        )
    result.notes.append(
        "Costs are charged at the real premium and lot size. The replay is "
        "built from the last 60 days of this index and cannot know about a day "
        "unlike them."
    )
    return result


def render(result: Assessment) -> str:
    """Plain text, leading with the comparison that decides it."""
    lines = ["=" * 68, "FORWARD ODDS FOR THIS TRADE", "=" * 68]
    if result.odds and result.odds.replays:
        o = result.odds
        lines += [
            f"  target first    : {o.target_first:6.1%}",
            f"  stop first      : {o.stop_first:6.1%}",
            f"  open at close   : {o.neither:6.1%}",
            f"  hit rate        : {o.hit_rate:6.1%}   (of those decided)",
            "",
        ]
    lines += [
        f"  reward:risk     : {result.reward_risk:.2f}  "
        f"(Rs {result.reward_rupees:,.0f} / Rs {result.risk_rupees:,.0f})",
        f"  round trip cost : Rs {result.round_trip_cost:,.0f}",
        f"  needs to win    : {result.breakeven_rate_net:6.1%} after costs, "
        f"{result.breakeven_rate_gross:.1%} before",
    ]
    if result.odds and result.odds.replays:
        lines.append(f"  edge            : {result.edge_points:+.1f} points")
    lines += ["", f"  VERDICT: {result.verdict}"]
    for note in result.notes:
        lines.append(f"    - {note}")
    if result.odds and result.odds.note:
        lines.append(f"    - {result.odds.note}")
    lines.append("=" * 68)
    return "\n".join(lines)
