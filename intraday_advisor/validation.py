"""Does the signal predict anything? Measure it, do not assume it.

Why this module exists
----------------------
After six sessions of building -- indicators, scoring, a dashboard, implied
volatility, spreads, stop management -- nobody had asked the only question that
decides whether any of it can make money: **when the score is high, does the
index actually go up?**

Measured on 2026-10-21 over 4,321 five-minute bars of each index:

    NIFTY score band     forward 1h     up-rate
       0-9  (max bear)     -0.041%        41.0%
      40-59 (neutral)      +0.016%        52.2%     <- the BEST band
      90-100 (max bull)    -0.011%        49.5%

    BANKNIFTY, 24 bars ahead:
      BUY signals  -0.076%      SELL signals  +0.007%      t = -5.64

The best forward return sat in the NEUTRAL band, and on BANKNIFTY the signal
was **significantly inverted**. That is diagnosable rather than mysterious: the
scorer is built almost entirely from trend-following components (SuperTrend,
PSAR, MACD, ADX, ribbons, Chandelier...) while the 5-minute index series
mean-reverts -- measured lag-1 autocorrelation **-0.089**. A maximally bullish
score means maximally extended, and extended reverts.

Fading the extremes was then tested on a 70/30 split, with the last 30% never
looked at until the rule was fixed:

    BANKNIFTY  fade score<=9 / >=90, hold 24 bars
               in-sample  t=+4.08  mean +0.041%
               OUT-OF-SAMPLE  n=453  mean +0.111%  win 56.3%  t=+3.87
    NIFTY      no edge in either direction (out-of-sample t=-0.73)

So a real, out-of-sample, statistically significant edge exists on BANKNIFTY.
**And it still loses to costs**:

    options (1 lot, delta 0.5)   +0.111% - 0.50% = -0.389%
    equity (divisible)           +0.111% - 0.19% = -0.079%

**That is the whole answer to "how do I make a profit".** The edge available at
this timeframe is 0.1%; the cost of taking it is 0.19% to 0.50%. Costs are two
to five times the edge. No dashboard, indicator, option structure or stop rule
changes that arithmetic -- only a bigger edge (longer holds), a smaller cost
(size, liquidity, not options), or both.

What "confidence" can actually mean
-----------------------------------
Not certainty -- that is not available in markets. What is available is a
**calibrated** estimate: knowing your win rate and its confidence interval, and
knowing whether the edge clears the cost. This module produces that number, so
the question stops being a matter of belief.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: Round-trip cost as a percentage of the INDEX move, by instrument.
#:
#: Options are brutal here not because the fees are large but because the
#: premium is a small number: a half-spread of Rs 1 on a Rs 160 premium is
#: 0.6%, and at delta 0.5 the index must move twice that far just to stand
#: still. Equity costs are measured from the screener's own cost model.
COST_INDEX_PCT = {
    "option_1lot": 0.50,
    "equity": 0.19,
    "futures": 0.03,
}

#: Below this many observations a win rate is not a measurement.
MIN_SAMPLE = 30


@dataclass
class EdgeStudy:
    """What a signal rule actually earned, before and after costs."""

    label: str
    horizon_bars: int
    observations: int = 0
    mean_pct: float = 0.0
    win_rate: float = 0.0
    t_stat: float = 0.0
    #: Wilson interval on the win rate, which is honest at small samples where
    #: the normal approximation is not.
    win_rate_low: float = 0.0
    win_rate_high: float = 0.0
    net_by_instrument: dict = field(default_factory=dict)
    verdict: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _wilson(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval -- correct at the small samples we actually have."""
    if total <= 0:
        return (0.0, 0.0)
    p = wins / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total)
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def score_forward_returns(
    frame: pd.DataFrame, scores: "pd.Series | list[float]",
    horizons: tuple[int, ...] = (6, 12, 24),
) -> pd.DataFrame:
    """Attach each bar's score and its forward returns, with no lookahead.

    ``frame`` needs a ``Close`` column. The forward return for bar *i* is
    measured from its close to the close *horizon* bars later, which is the
    earliest a signal from a closed bar could have been acted on.
    """
    out = pd.DataFrame(index=frame.index)
    out["close"] = frame["Close"].astype(float)
    out["score"] = list(scores)
    for horizon in horizons:
        out[f"fwd{horizon}"] = (
            out["close"].shift(-horizon) / out["close"] - 1.0
        ) * 100.0
    return out


def evaluate_rule(
    study: pd.DataFrame, *, horizon: int, buy_below: float, sell_above: float,
    fade: bool = False, label: str = "",
) -> EdgeStudy:
    """Score a threshold rule and report whether it beats its own costs.

    ``fade=False`` follows the score (buy a high score, sell a low one).
    ``fade=True`` does the opposite, which is what the measurements above say
    the 5-minute series actually rewards.
    """
    column = f"fwd{horizon}"
    if column not in study.columns:
        return EdgeStudy(label=label or "rule", horizon_bars=horizon,
                         verdict="no data", notes=[f"{column} not computed."])

    low_band = study[study["score"] <= buy_below][column].dropna()
    high_band = study[study["score"] >= sell_above][column].dropna()

    if fade:
        # Buy the washed-out band, sell the extended one.
        pnl = pd.concat([low_band, -high_band])
    else:
        # Follow: buy the strong band, sell the weak one.
        pnl = pd.concat([high_band, -low_band])

    result = EdgeStudy(label=label or ("fade" if fade else "follow"),
                       horizon_bars=horizon, observations=len(pnl))
    if len(pnl) < MIN_SAMPLE:
        result.verdict = "insufficient sample"
        result.notes.append(
            f"{len(pnl)} observations is below the {MIN_SAMPLE} needed for a "
            f"win rate to mean anything."
        )
        return result

    wins = int((pnl > 0).sum())
    result.mean_pct = round(float(pnl.mean()), 5)
    result.win_rate = round(wins / len(pnl) * 100, 2)
    standard_error = pnl.std(ddof=1) / np.sqrt(len(pnl))
    # Compare against the mean's own scale, not against zero. A degenerate
    # sample leaves floating-point residue (~1e-17) rather than an exact 0, so
    # a `> 0` test lets it through and reports a t-statistic of 1e17, which is
    # arithmetically true and useless to read.
    degenerate = standard_error <= abs(float(pnl.mean())) * 1e-12
    if not degenerate:
        result.t_stat = round(float(pnl.mean() / standard_error), 2)
    elif pnl.mean() != 0:
        # Zero dispersion with a non-zero mean is PERFECT consistency, not
        # noise. An earlier guard returned 0.0 here, which reported a flawless
        # signal as indistinguishable from zero -- the opposite of the truth.
        result.t_stat = float("inf") if pnl.mean() > 0 else float("-inf")
        result.notes.append(
            "Every observation moved the same way: the sample has zero "
            "dispersion, so this is either a genuinely mechanical effect or a "
            "synthetic/degenerate input. Check the data before believing it."
        )
    else:
        result.t_stat = 0.0
    low, high = _wilson(wins, len(pnl))
    result.win_rate_low = round(low * 100, 2)
    result.win_rate_high = round(high * 100, 2)

    # The part that decides everything.
    for instrument, cost in COST_INDEX_PCT.items():
        net = result.mean_pct - cost
        result.net_by_instrument[instrument] = {
            "cost_pct": cost,
            "net_pct": round(net, 5),
            "profitable": net > 0,
        }

    profitable = [k for k, v in result.net_by_instrument.items() if v["profitable"]]
    if abs(result.t_stat) < 2:
        result.verdict = "no measurable edge"
        result.notes.append(
            f"t={result.t_stat} is inside the noise. At this sample the mean "
            f"return is indistinguishable from zero."
        )
    elif not profitable:
        result.verdict = "edge is real but smaller than costs"
        worst = min(COST_INDEX_PCT.values())
        result.notes.append(
            f"The gross edge of {result.mean_pct:+.4f}% is statistically real "
            f"(t={result.t_stat}), and still below the cheapest round trip "
            f"available ({worst:.2f}%). Trading it loses money on every "
            f"instrument priced here."
        )
    else:
        result.verdict = f"tradeable on: {', '.join(profitable)}"
        result.notes.append(
            f"Gross edge {result.mean_pct:+.4f}% (t={result.t_stat}) clears "
            f"costs on {', '.join(profitable)}."
        )
    return result


def split_sample(study: pd.DataFrame, train_fraction: float = 0.70):
    """Chronological train/test split. Never shuffled.

    Shuffling a time series leaks the future into the past: neighbouring bars
    are correlated, so a random split puts a bar's own neighbours in both sets
    and every rule looks predictive. The split must be by time.
    """
    if not 0.1 <= train_fraction <= 0.95:
        raise ValueError("train_fraction must be between 0.1 and 0.95")
    cut = int(len(study) * train_fraction)
    return study.iloc[:cut], study.iloc[cut:]


def autocorrelation_note(frame: pd.DataFrame, lag: int = 1) -> dict:
    """Does this series trend or revert at this bar size?

    The single most useful diagnostic for why a trend-following scorer
    underperforms: if returns are negatively autocorrelated, strength is
    followed by weakness and every momentum component is fighting the series.
    """
    returns = frame["Close"].astype(float).pct_change() * 100
    value = float(returns.autocorr(lag)) if len(returns) > lag + 2 else 0.0

    # The threshold has to scale with the sample, or it reports sampling noise
    # as structure. The standard error of an autocorrelation estimate is about
    # 1/sqrt(n), so anything inside two of those is indistinguishable from a
    # random walk. A fixed +/-0.03 called a 900-bar random walk "trending".
    usable = max(1, returns.notna().sum())
    threshold = max(0.03, 2.0 / math.sqrt(usable))

    if value < -threshold:
        character = "mean-reverting"
        meaning = (
            "Strength tends to be followed by weakness at this bar size, so "
            "trend-following components are fighting the series. A contrarian "
            "reading of the same inputs is the thing to test."
        )
    elif value > threshold:
        character = "trending"
        meaning = "Moves tend to persist, which is what momentum components assume."
    else:
        character = "neither"
        meaning = (
            "No usable persistence or reversion at this bar size -- the series "
            "is close to a random walk, and any edge must come from elsewhere."
        )
    return {
        "lag": lag,
        "autocorrelation": round(value, 4),
        "significance_threshold": round(threshold, 4),
        "sample": int(usable),
        "character": character,
        "meaning": meaning,
    }


# --------------------------------------------------------------------------- #
# The gate: is a result an edge, or a story about one window?
# --------------------------------------------------------------------------- #

@dataclass
class RuleVerdict:
    """Whether a measured rule may be traded, and precisely why not."""

    tradeable: bool = False
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def validate_rule(
    train: EdgeStudy, test: EdgeStudy, *, instrument: str = "equity",
    hypotheses_tested: int = 1, min_t: float = 2.0,
) -> RuleVerdict:
    """Four checks a rule must pass before it is allowed to be called an edge.

    Written immediately after it would have caught me shipping a fake one.

    Searching 5 years of Nifty-50 daily bars for a contrarian edge produced 20
    cells (5 horizons x 2 directions x 2 windows). One cleared costs: a 20-day
    contrarian hold, test t = +2.61, excess +0.260% against a 0.19% cost. It
    looked like the answer. Its **train** t was **-2.03** -- significantly
    negative in the same data a rule would have been fitted on. Seven of ten
    train/test pairs flipped sign, and nothing was significant in both windows
    at once. That is regime dependence plus multiple testing, not an edge.

    The checks:

    1. **Sign agreement.** A real effect does not reverse between the first 70%
       of history and the last 30%. A flip means the rule describes a regime.
    2. **Holdout significance**, adjusted for how many hypotheses were tried.
       Testing 20 cells and reporting the best needs a higher bar than testing
       one: the threshold is scaled by sqrt(log(n)), a practical stand-in for a
       Bonferroni-style correction that does not collapse to absurdity at n=20.
    3. **Costs cleared in the holdout**, on the instrument actually traded.
    4. **Enough observations** for the win rate to mean anything.

    All four, or it is not tradeable. Three of four is a story.
    """
    verdict = RuleVerdict()
    checks: dict = {}

    same_sign = (train.mean_pct > 0) == (test.mean_pct > 0)
    checks["sign_agreement"] = same_sign
    if not same_sign:
        verdict.reasons.append(
            f"Sign flips between windows: train {train.mean_pct:+.4f}%, test "
            f"{test.mean_pct:+.4f}%. A rule fitted on the first window would "
            f"have traded the second one backwards."
        )

    adjusted_min_t = min_t * math.sqrt(max(1.0, math.log(max(hypotheses_tested, 1) + 1)))
    significant = abs(test.t_stat) >= adjusted_min_t
    checks["holdout_significance"] = significant
    checks["required_t"] = round(adjusted_min_t, 2)
    if not significant:
        verdict.reasons.append(
            f"Holdout t={test.t_stat:+.2f} is below the {adjusted_min_t:.2f} "
            f"required after searching {hypotheses_tested} hypothes"
            f"{'is' if hypotheses_tested == 1 else 'es'}."
        )

    net = test.net_by_instrument.get(instrument, {})
    clears = bool(net.get("profitable"))
    checks["clears_costs"] = clears
    if not clears:
        verdict.reasons.append(
            f"Holdout edge {test.mean_pct:+.4f}% does not clear the "
            f"{COST_INDEX_PCT.get(instrument, 0):.2f}% {instrument} round trip."
        )

    enough = test.observations >= MIN_SAMPLE and train.observations >= MIN_SAMPLE
    checks["sample"] = enough
    if not enough:
        verdict.reasons.append(
            f"Too few observations: train {train.observations}, "
            f"test {test.observations}."
        )

    verdict.checks = checks
    verdict.tradeable = same_sign and significant and clears and enough
    if verdict.tradeable:
        verdict.reasons.append(
            f"Passes all four: same sign in both windows, holdout t="
            f"{test.t_stat:+.2f} over the {adjusted_min_t:.2f} bar, and "
            f"{net.get('net_pct', 0):+.4f}% net of {instrument} costs."
        )
    return verdict
