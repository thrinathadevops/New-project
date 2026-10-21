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

That was written up as "a real, out-of-sample, statistically significant edge".
**It was not, and the error was mine.** Those t-statistics were computed on
24-bar forward returns sampled at *every* bar, so consecutive observations
shared 23 of their 24 bars of path. Correcting for the overlap -- see
:func:`effective_sample_size` and the long comment above it -- takes n=453 down
to about 19 independent observations and t=+3.87 down to roughly **+0.79**.
There was never a significant edge there to lose to costs.

The conclusion did not change, and it now rests on firmer ground: the edge was
not established, *and* even taken at face value it loses to costs:

    options (1 lot, delta 0.5)   +0.111% - 0.50% = -0.389%
    equity (divisible)           +0.111% - 0.19% = -0.079%

**That is the whole answer to "how do I make a profit".** The edge available at
this timeframe is 0.1% at best and possibly zero; the cost of taking it is
0.19% to 0.50%. Costs are two to five times the edge. No dashboard, indicator,
option structure or stop rule changes that arithmetic -- only a bigger edge
(longer holds), a smaller cost (size, liquidity, not options), or both.

The smaller-cost half of that sentence is what ``microstructure.py`` addresses,
and it starts from the fact that the cost in this project was never measured
either: ``ledger/costs.py`` assumed a flat 0.25% half-spread on every contract.

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

#: Bootstrap resamples used for the block-bootstrap p-value. 2,000 is enough to
#: place a 5% threshold stably without making the study slow to re-run, and a
#: study you will not re-run is a study you will not check.
BOOTSTRAP_DRAWS = 2_000


# --------------------------------------------------------------------------- #
# Overlapping samples: the correction that changes this module's own headline
# --------------------------------------------------------------------------- #
#
# The study above measures a 24-bar forward return **at every bar**. Bar *i*
# and bar *i+1* therefore share 23 of their 24 bars of path. They are not two
# observations; they are one observation reported twice with a small
# perturbation.
#
# The t-statistic originally computed here divided by ``std/sqrt(n)``, which is
# the standard error of a mean of *independent* draws. With h-fold overlap the
# true standard error is larger by roughly ``sqrt(h)``, so the reported
# t-statistic was inflated by about that factor. This is a documented trap:
# work on long-horizon predictability finds that overlapping observations have
# "greatly overstated" t-statistics, and that the effective number of
# observations is barely above the non-overlapping count.
#
# Applied to this module's own headline result -- BANKNIFTY, fade the extremes,
# 24-bar hold, n=453, out-of-sample t=+3.87:
#
#     effective observations  453 / 24  ~=  19
#     inflation factor        sqrt(24)  ~=  4.9
#     corrected t             3.87 / 4.9  ~=  0.79
#
# **That is not significant.** The sentence "a real, out-of-sample,
# statistically significant edge exists on BANKNIFTY" was an artefact of
# counting one observation twenty-four times. The conclusion the module reached
# anyway -- do not trade it, costs are 2-5x the edge -- was right, and it was
# right for a stronger reason than the one given: the edge was never
# established in the first place.
#
# Three independent views are now reported, because no single one is
# trustworthy on its own:
#
#   1. :func:`newey_west_tstat` -- a HAC standard error, the conventional fix.
#      Still optimistic: simulations show the Bartlett kernel understates the
#      variance of overlapping sums even when no true autocorrelation exists.
#   2. :func:`non_overlapping_tstat` -- throw the overlap away entirely. Fewer
#      observations, no kernel assumption, and it reports every offset so a
#      lucky subsample cannot be mistaken for the answer.
#   3. :func:`block_bootstrap_pvalue` -- resample blocks longer than the
#      holding period, which preserves the dependence instead of modelling it.
#
# **Measured, rather than assumed.** On simulated random walks containing no
# effect at all, with a persistent trend-following score of the kind
# ``scoring.py`` produces, how often does each test declare significance?
# A correct test should say 5%:
#
#     naive i.i.d. t >= 2              53%      <- the statistic used before
#     HAC-corrected t >= 2             13-20%
#     block bootstrap p < 0.05         12-20%
#     non-overlapping median t >= 2     0-7%    <- best calibrated
#
# Against a genuine drift, every one of them had 100% power, so the
# conservatism costs nothing detectable here.
#
# Two things follow. The naive statistic was not slightly optimistic, it was
# wrong by an order of magnitude -- it found an edge in noise more often than
# not. And the conventional HAC fix is **not sufficient on its own**, which is
# why :func:`validate_rule` requires the overlap-free tests to agree before it
# will call anything tradeable.
#
# Where they disagree, believe the most conservative.


def effective_sample_size(observations: int, horizon: int) -> dict:
    """How many genuinely independent observations an overlapping study has.

    For an ``horizon``-bar forward return sampled every bar, consecutive
    observations share ``horizon - 1`` bars of path. The count that matters for
    a standard error is therefore closer to ``observations / horizon`` than to
    ``observations``.

    This is deliberately the conservative reading. The literature finds the
    effective count rises only slightly above the non-overlapping number when
    overlap is introduced, so treating it as exactly the non-overlapping number
    errs in the one direction that cannot manufacture an edge.
    """
    horizon = max(1, int(horizon))
    observations = max(0, int(observations))
    effective = observations / horizon
    return {
        "observations": observations,
        "horizon": horizon,
        "effective_observations": round(effective, 1),
        "inflation_factor": round(math.sqrt(horizon), 2),
        "note": (
            f"{observations} overlapping {horizon}-bar observations carry "
            f"about {effective:.0f} independent ones. An i.i.d. t-statistic "
            f"computed on them is overstated by roughly "
            f"{math.sqrt(horizon):.1f}x."
        ),
    }


def newey_west_tstat(values, horizon: int, lags: int | None = None) -> dict:
    """t-statistic for the mean, with a HAC (Newey-West) standard error.

    ``values`` must be in **chronological order** -- the whole point is the
    serial dependence between neighbours, so an unsorted series silently
    measures nothing.

    ``lags`` defaults to ``horizon - 1``, the exact moving-average order that
    h-fold overlap induces. The Bartlett kernel keeps the variance estimate
    positive semi-definite, so this cannot return an imaginary standard error.
    """
    series = pd.Series(values).dropna().astype(float)
    n = len(series)
    lags = (horizon - 1) if lags is None else lags
    lags = max(0, min(int(lags), max(0, n - 2)))

    result = {
        "n": n, "lags": lags, "mean": None, "standard_error": None,
        "t_stat": None, "notes": [],
    }
    if n < 3:
        result["notes"].append("Fewer than 3 observations; no inference.")
        return result

    mean = float(series.mean())
    deviations = (series - mean).to_numpy()
    result["mean"] = round(mean, 6)

    # gamma_0 plus Bartlett-weighted autocovariances.
    gamma_0 = float(np.dot(deviations, deviations) / n)
    long_run = gamma_0
    for j in range(1, lags + 1):
        gamma_j = float(np.dot(deviations[j:], deviations[:-j]) / n)
        weight = 1.0 - j / (lags + 1.0)
        long_run += 2.0 * weight * gamma_j

    standard_error = math.sqrt(long_run / n) if long_run > 0 else 0.0

    # Degeneracy has to be judged against the mean's own scale, not against
    # zero. A constant series does not leave an exact 0 here -- it leaves
    # floating-point residue around 1e-33, so `long_run <= 0` never fires and
    # the t-statistic comes back as 1.4e+16: arithmetically true, useless to
    # read, and indistinguishable from a real result in a report.
    #
    # This is the *same* bug the i.i.d. path above was already fixed for. It
    # recurred here because the guard was rewritten from the mathematics
    # rather than copied from the lesson.
    if standard_error <= abs(mean) * 1e-12:
        result["standard_error"] = 0.0
        if mean != 0:
            result["t_stat"] = float("inf") if mean > 0 else float("-inf")
            result["notes"].append(
                "Zero long-run variance with a non-zero mean: every "
                "observation moved identically. Perfect consistency, or a "
                "synthetic input. Check the data before believing it."
            )
        else:
            result["t_stat"] = 0.0
        return result
    result["standard_error"] = round(standard_error, 8)
    result["t_stat"] = round(mean / standard_error, 2) if standard_error else 0.0
    result["notes"].append(
        f"HAC standard error over {lags} lag(s), the overlap induced by an "
        f"{horizon}-bar hold."
    )
    return result


def non_overlapping_tstat(values, horizon: int) -> dict:
    """t-statistics from every non-overlapping subsample, not just one.

    Taking every ``horizon``-th observation removes the overlap completely and
    needs no kernel and no correction. The cost is that it throws away most of
    the data -- and that there are ``horizon`` different ways to do it,
    depending on where you start.

    Picking one offset would be a free parameter, and a free parameter is how a
    story becomes a finding. All offsets are computed; the **median** is
    reported as the headline and the full range is returned, so a single lucky
    subsample is visible as disagreement rather than invisible as a result.
    """
    series = pd.Series(values).dropna().astype(float).reset_index(drop=True)
    horizon = max(1, int(horizon))

    per_offset = []
    for offset in range(horizon):
        subsample = series.iloc[offset::horizon]
        if len(subsample) < 3:
            continue
        deviation = subsample.std(ddof=1)
        mean = float(subsample.mean())
        if deviation == 0:
            t = float("inf") if mean > 0 else (float("-inf") if mean < 0 else 0.0)
        else:
            t = mean / (deviation / math.sqrt(len(subsample)))
        per_offset.append({
            "offset": offset, "n": len(subsample),
            "mean": round(mean, 6),
            "t_stat": round(t, 2) if math.isfinite(t) else t,
        })

    if not per_offset:
        return {
            "usable": False,
            "note": (
                f"No non-overlapping subsample reached 3 observations at a "
                f"{horizon}-bar horizon. The study is too short to test "
                f"without relying on the overlap."
            ),
        }

    finite = [p["t_stat"] for p in per_offset if math.isfinite(p["t_stat"])]
    median_t = float(np.median(finite)) if finite else per_offset[0]["t_stat"]
    signs = {p["t_stat"] > 0 for p in per_offset if p["t_stat"] != 0}

    return {
        "usable": True,
        "horizon": horizon,
        "subsamples": len(per_offset),
        "median_t": round(median_t, 2) if math.isfinite(median_t) else median_t,
        "min_t": min((p["t_stat"] for p in per_offset), key=lambda v: v if math.isfinite(v) else 0),
        "max_t": max((p["t_stat"] for p in per_offset), key=lambda v: v if math.isfinite(v) else 0),
        "observations_each": per_offset[0]["n"],
        "all_offsets_agree_on_sign": len(signs) <= 1,
        "per_offset": per_offset,
        "note": (
            f"{len(per_offset)} independent subsamples of about "
            f"{per_offset[0]['n']} observations each. Median t="
            f"{median_t:+.2f}."
            + ("" if len(signs) <= 1 else
               " The subsamples do not even agree on the SIGN, which is what a"
               " non-effect looks like when it is sliced.")
        ),
    }


def block_bootstrap_pvalue(
    values, horizon: int, draws: int = BOOTSTRAP_DRAWS, seed: int = 20261021,
    block_multiple: int = 2,
) -> dict:
    """Two-sided p-value for "the mean is zero", preserving the dependence.

    A circular block bootstrap resamples contiguous blocks rather than
    individual observations, so the overlap-induced dependence is *carried
    into* the null distribution instead of being modelled by a kernel. Where
    Newey-West assumes a shape, this one keeps the data's own.

    The series is centred before resampling, so the null being tested is a true
    zero mean rather than the sample's own mean.

    ``block_multiple`` defaults to 2 because a block exactly the length of the
    dependence is marginal, and measuring it showed as much. On 120 simulated
    random walks with no effect, where a correct test rejects 5% of the time:

        block = 1x horizon   15.0% false positives
        block = 2x horizon   13.3%
        block = 4x horizon   11.7%

    Power was 100% at every block length against a real drift, so the longer
    block costs nothing here and the default was moved to 2x. The remaining
    gap from 5% is why this is only ever one of three views -- see
    :func:`non_overlapping_tstat`, which measured best calibrated of the three.
    """
    series = pd.Series(values).dropna().astype(float).to_numpy()
    n = len(series)
    block = max(1, int(horizon) * max(1, int(block_multiple)))
    if n < max(10, block * 2):
        return {
            "usable": False,
            "note": (
                f"{n} observations is too few to bootstrap in blocks of "
                f"{block}; the resamples would be near-copies of the sample."
            ),
        }

    observed = float(series.mean())
    centred = series - observed
    rng = np.random.default_rng(seed)
    blocks_needed = int(np.ceil(n / block))

    starts = rng.integers(0, n, size=(draws, blocks_needed))
    offsets = np.arange(block)
    # Circular indexing, so every observation can start a block and the tail is
    # not systematically under-sampled.
    indices = (starts[:, :, None] + offsets[None, None, :]) % n
    means = centred[indices.reshape(draws, -1)[:, :n]].mean(axis=1)

    extreme = int(np.sum(np.abs(means) >= abs(observed)))
    # Add-one smoothing: a p-value of exactly 0 claims more resolution than
    # `draws` resamples can provide.
    p_value = (extreme + 1) / (draws + 1)

    return {
        "usable": True,
        "observed_mean": round(observed, 6),
        "draws": draws,
        "block_length": block,
        "p_value": round(p_value, 4),
        "significant_at_5pct": p_value < 0.05,
        "note": (
            f"{extreme} of {draws} block resamples of a zero-mean series "
            f"produced a mean at least as extreme as {observed:+.4f}%, so "
            f"p={p_value:.3f}. Blocks of {block} preserve the overlap rather "
            f"than assuming its shape."
        ),
    }


@dataclass
class EdgeStudy:
    """What a signal rule actually earned, before and after costs."""

    label: str
    horizon_bars: int
    observations: int = 0
    mean_pct: float = 0.0
    win_rate: float = 0.0
    #: The headline t-statistic, corrected for the overlap that sampling an
    #: ``horizon``-bar return at every bar creates. This is the number the
    #: verdict uses and the number to quote.
    t_stat: float = 0.0
    #: The naive i.i.d. t-statistic, kept visible precisely because it is the
    #: one that overstated this module's own headline result by ~4.9x. Seeing
    #: the two side by side is the point.
    t_stat_iid: float = 0.0
    #: Independent observations behind the overlapping count.
    effective_observations: float = 0.0
    #: Corroboration from methods that make different assumptions.
    non_overlapping: dict = field(default_factory=dict)
    bootstrap: dict = field(default_factory=dict)
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

    # Chronological order, or every serial-dependence correction below measures
    # the order pandas happened to concatenate in rather than the order the
    # market happened in. The two bands are disjoint slices of one time index,
    # so sorting restores the real sequence.
    pnl = pnl.sort_index()

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
        result.t_stat_iid = round(float(pnl.mean() / standard_error), 2)
    elif pnl.mean() != 0:
        # Zero dispersion with a non-zero mean is PERFECT consistency, not
        # noise. An earlier guard returned 0.0 here, which reported a flawless
        # signal as indistinguishable from zero -- the opposite of the truth.
        result.t_stat_iid = float("inf") if pnl.mean() > 0 else float("-inf")
        result.notes.append(
            "Every observation moved the same way: the sample has zero "
            "dispersion, so this is either a genuinely mechanical effect or a "
            "synthetic/degenerate input. Check the data before believing it."
        )
    else:
        result.t_stat_iid = 0.0

    # --- the overlap correction ---------------------------------------- #
    # A degenerate sample has no serial dependence to correct for, so the HAC
    # estimate and the i.i.d. one agree by construction -- and the corroborating
    # tests have nothing to resample. Skip them rather than report a bootstrap
    # of a constant as evidence.
    sizing = effective_sample_size(len(pnl), horizon)
    result.effective_observations = sizing["effective_observations"]

    if degenerate:
        result.t_stat = result.t_stat_iid
    else:
        hac = newey_west_tstat(pnl, horizon)
        result.t_stat = hac["t_stat"] if hac["t_stat"] is not None else result.t_stat_iid
        result.non_overlapping = non_overlapping_tstat(pnl, horizon)
        result.bootstrap = block_bootstrap_pvalue(pnl, horizon)

        if horizon > 1:
            shrink = (
                abs(result.t_stat_iid) / abs(result.t_stat)
                if result.t_stat not in (0, None) and math.isfinite(result.t_stat)
                and result.t_stat != 0 else None
            )
            result.notes.append(
                f"Overlap correction: {len(pnl)} overlapping {horizon}-bar "
                f"observations carry about "
                f"{sizing['effective_observations']:.0f} independent ones. "
                f"The i.i.d. t of {result.t_stat_iid:+.2f} becomes "
                f"{result.t_stat:+.2f} with a HAC standard error"
                + (f" ({shrink:.1f}x smaller)." if shrink and shrink > 1.05
                   else ".")
            )
            if result.non_overlapping.get("usable"):
                result.notes.append(
                    "Non-overlapping check: " + result.non_overlapping["note"]
                )
            if result.bootstrap.get("usable"):
                result.notes.append(
                    "Block bootstrap: " + result.bootstrap["note"]
                )

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
            + (f" (The uncorrected i.i.d. figure was {result.t_stat_iid:+.2f}, "
               f"which is the number an overlapping study reports if nobody "
               f"adjusts for the overlap.)"
               if abs(result.t_stat_iid) >= 2 else "")
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

    # 5. Corroboration from methods that do not share the HAC kernel's
    #    assumptions. Judged only where the evidence exists: a hand-built
    #    EdgeStudy carries none, and absence of corroboration is not the same
    #    as disconfirmation. A study from `evaluate_rule` always carries it,
    #    so in practice this is a fifth required check.
    corroborated = None
    bootstrap = test.bootstrap or {}
    non_overlap = test.non_overlapping or {}
    if bootstrap.get("usable") or non_overlap.get("usable"):
        objections = []
        if bootstrap.get("usable") and not bootstrap.get("significant_at_5pct"):
            objections.append(
                f"the block bootstrap puts p={bootstrap.get('p_value')}, which "
                f"does not reject a zero mean"
            )
        if non_overlap.get("usable"):
            if not non_overlap.get("all_offsets_agree_on_sign"):
                objections.append(
                    "the non-overlapping subsamples disagree on the sign of "
                    "the effect"
                )
            median_t = non_overlap.get("median_t")
            if isinstance(median_t, (int, float)) and abs(median_t) < 2:
                objections.append(
                    f"the median non-overlapping t is {median_t:+.2f}, inside "
                    f"the noise once the overlap is discarded"
                )
        corroborated = not objections
        if objections:
            verdict.reasons.append(
                "Not corroborated by the overlap-free tests: "
                + "; ".join(objections)
                + ". A HAC t-statistic alone is known to stay optimistic on "
                  "overlapping sums."
            )
    checks["corroborated"] = corroborated

    verdict.checks = checks
    verdict.tradeable = (
        same_sign and significant and clears and enough
        and corroborated is not False
    )
    if verdict.tradeable:
        verdict.reasons.append(
            f"Passes all four: same sign in both windows, holdout t="
            f"{test.t_stat:+.2f} over the {adjusted_min_t:.2f} bar, and "
            f"{net.get('net_pct', 0):+.4f}% net of {instrument} costs."
        )
    return verdict
