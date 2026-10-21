"""Is the signal backwards? Train/test split, so I cannot fool myself.

Hypothesis from the predictive study: the scorer is built almost entirely from
trend-following components, while the 5-minute index series mean-reverts
(lag-1 autocorrelation -0.089). If so, FADING an extreme score should have
positive expectancy where following it does not.

Method: first 70% of bars to form the rule, last 30% never looked at until the
rule is fixed. Costs applied in index-percent terms so the answer is in the
same units as the decision.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logging.disable(logging.WARNING)

from intraday_advisor.data import fetch_yahoo_ohlcv
from intraday_advisor.indicators import add_indicators
from intraday_advisor.scoring import ScoreWeights, score_signal
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy
from intraday_advisor.validation import evaluate_rule, validate_rule

#: Round-trip cost as a fraction of the INDEX move, for each instrument.
#: An option at delta ~0.5 costing 1.1% of a premium that is ~0.75% of spot
#: needs roughly 0.016% of index move per 1% of premium cost -- but the move
#: must also overcome the premium's own percentage. Expressed directly:
COST_INDEX_PCT = {
    "option (1 lot, delta 0.5)": 0.50,   # ~1.1% of premium, levered ~0.5 delta
    "equity (divisible)": 0.19,          # measured by the screener
}


def build(ticker: str, period: str = "58d", interval: str = "5m") -> pd.DataFrame:
    frame = fetch_yahoo_ohlcv(ticker, period=period, interval=interval)
    enriched = apply_ema_swing_breakout_strategy(add_indicators(frame))
    enriched = enriched.dropna(subset=["Close", "EMA9", "EMA21", "ATR14"])

    weights = ScoreWeights.load()
    scores, raws = [], []
    for record in enriched.to_dict("records"):
        clean = {}
        for key, value in record.items():
            if isinstance(value, (bool, np.bool_)):
                clean[key] = bool(value)
            elif isinstance(value, (int, np.integer)):
                clean[key] = int(value)
            elif isinstance(value, (float, np.floating)):
                clean[key] = None if pd.isna(value) else float(value)
            else:
                clean[key] = value
        result = score_signal(clean, decision_signal=clean.get("Signal") or None,
                              vwap_relation=None, weights=weights)
        scores.append(result.score)
        raws.append(result.raw_score)

    out = pd.DataFrame({"close": enriched["Close"].astype(float).values,
                        "score": scores, "raw": raws}, index=enriched.index)
    for horizon in (6, 12, 24):
        out[f"fwd{horizon}"] = (out["close"].shift(-horizon) / out["close"] - 1) * 100
    return out


def evaluate(chunk: pd.DataFrame, horizon: int, low: int, high: int) -> dict:
    """Fade the extremes: short the top band, buy the bottom band.

    **Rewritten 2026-10-21 and this is the point of the rewrite.** The original
    version computed its own t-statistic as ``mean / (std / sqrt(n))`` -- the
    standard error of a mean of *independent* draws, applied to 24-bar forward
    returns sampled at every bar, where neighbouring observations share 23 of
    their 24 bars. It reported t=+3.87 out-of-sample and this project wrote
    that up as a real, statistically significant edge.

    It was not. Re-run through ``validation.evaluate_rule``, which corrects for
    the overlap, the same data gives:

        BANKNIFTY out-of-sample   n=455   mean +0.1117%
          i.i.d. t              +3.90      <- what this script used to print
          HAC-corrected t       +1.83      <- below the significance bar
          effective sample          19      <- not 455
          non-overlapping t     +0.86      <- and the subsamples disagree on sign
          block bootstrap     p=0.056      <- does not reject a zero mean

    A simulation in ``tests/test_overlap_correction.py`` shows why: on random
    walks containing no effect at all, that naive statistic declares
    significance about **53%** of the time.

    This function now delegates, so the error cannot be reintroduced here
    while the fix sits unused in the module next door.
    """
    column = f"fwd{horizon}"
    longs = chunk[chunk.score <= low][column].dropna()
    shorts = chunk[chunk.score >= high][column].dropna()
    if len(longs) < 30 or len(shorts) < 30:
        return {}

    study = evaluate_rule(chunk, horizon=horizon, buy_below=low,
                          sell_above=high, fade=True)
    if study.observations < 30:
        return {}
    non_overlap = study.non_overlapping or {}
    return {
        "n": study.observations,
        "mean_pct": study.mean_pct,
        "win_rate": study.win_rate,
        "t": study.t_stat,                 # HAC-corrected
        "t_iid": study.t_stat_iid,         # the inflated figure, for contrast
        "effective_n": study.effective_observations,
        "t_nonoverlap": non_overlap.get("median_t"),
        "signs_agree": non_overlap.get("all_offsets_agree_on_sign"),
        "bootstrap_p": (study.bootstrap or {}).get("p_value"),
        "study": study,
    }


def run(ticker: str, label: str):
    data = build(ticker)
    split = int(len(data) * 0.70)
    train, test = data.iloc[:split], data.iloc[split:]

    print("=" * 76)
    print(f"{label}  train {len(train):,} bars ({train.index[0].date()}..{train.index[-1].date()})"
          f"  |  test {len(test):,} bars ({test.index[0].date()}..{test.index[-1].date()})")
    print("=" * 76)

    print(f"\nIN-SAMPLE: fade the extremes (buy score<=low, short score>=high)")
    print(f"{'rule':>22} {'n':>6} {'mean%':>9} {'win%':>7} "
          f"{'t_iid':>7} {'t_HAC':>7} {'eff_n':>6}")
    best = None
    searched = 0
    for low, high in ((9, 90), (14, 85), (24, 75), (39, 60)):
        for horizon in (6, 12, 24):
            stats = evaluate(train, horizon, low, high)
            if not stats:
                continue
            searched += 1
            tag = f"<={low} / >={high} @{horizon}b"
            print(f"{tag:>22} {stats['n']:6,} {stats['mean_pct']:9.4f} "
                  f"{stats['win_rate']:6.1f}% {stats['t_iid']:7.2f} "
                  f"{stats['t']:7.2f} {stats['effective_n']:6.0f}")
            if best is None or stats["t"] > best[1]["t"]:
                best = ((low, high, horizon), stats)

    if not best:
        print("  no rule had enough observations.")
        return
    (low, high, horizon), in_stats = best
    print(f"\nBest in-sample rule: fade score<={low} / >={high} over {horizon} bars "
          f"(t={in_stats['t']:.2f}, mean {in_stats['mean_pct']:+.4f}%)")

    out_stats = evaluate(test, horizon, low, high)
    print(f"\nOUT-OF-SAMPLE (never used to pick the rule):")
    if not out_stats:
        print("  too few test observations.")
        return
    print(f"  n={out_stats['n']:,}  mean {out_stats['mean_pct']:+.4f}%  "
          f"win {out_stats['win_rate']:.1f}%")
    print(f"\n  Is it significant? Four views, because one is not enough:")
    print(f"    i.i.d. t                 {out_stats['t_iid']:+.2f}   "
          f"<- inflated: treats {out_stats['n']} overlapping observations as "
          f"independent")
    print(f"    HAC-corrected t          {out_stats['t']:+.2f}   "
          f"(effective sample {out_stats['effective_n']:.0f})")
    print(f"    non-overlapping median t {out_stats['t_nonoverlap']:+.2f}   "
          f"(all offsets agree on sign: {out_stats['signs_agree']})")
    print(f"    block bootstrap          p={out_stats['bootstrap_p']:.3f}")

    print(f"\n  Does it survive costs, per round trip?")
    for instrument, cost in COST_INDEX_PCT.items():
        net = out_stats["mean_pct"] - cost
        verdict = "PROFITABLE" if net > 0 else "loses to costs"
        print(f"    {instrument:28} edge {out_stats['mean_pct']:+.4f}% "
              f"- cost {cost:.2f}% = {net:+.4f}%  {verdict}")

    # The gate, with the number of rules actually searched declared. Reporting
    # the best of 12 needs a higher bar than testing one, and the gate knows it.
    verdict = validate_rule(
        in_stats["study"], out_stats["study"],
        instrument="equity", hypotheses_tested=max(1, searched),
    )
    print(f"\n  GATE (validation.validate_rule): "
          f"tradeable = {verdict.tradeable}")
    for reason in verdict.reasons:
        print(f"    - {reason}")


if __name__ == "__main__":
    for ticker, label in (("^NSEI", "NIFTY"), ("^NSEBANK", "BANKNIFTY")):
        try:
            run(ticker, label)
            print()
        except Exception as exc:  # noqa: BLE001
            print(f"{label}: failed — {exc}")
