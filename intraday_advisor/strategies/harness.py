"""Run every registered strategy through one evaluator and report verdicts.

Why one harness
---------------
``research_fade_test.py`` kept its own copy of a t-statistic for weeks while
the corrected version sat unused in ``validation.py``, and the copy was the one
that produced the project's headline claim. Two implementations of a
significance test is how one of them stays wrong.

So every strategy in the catalogue is scored by ``validation.evaluate_signal``
with no exceptions and no per-strategy arithmetic: same train/test split, same
HAC standard error, same non-overlapping subsamples, same block bootstrap, same
cost gate on the instrument the strategy actually trades. A verdict here is
therefore comparable across the catalogue, which is the only reason a catalogue
is worth more than a list.

What a verdict means
--------------------
``validate_rule`` requires four things plus corroboration, and the failures are
not interchangeable:

* **not significant** -- there is no effect to trade. Nothing fixes this.
* **does not clear costs** -- the effect is real and too small *for that
  instrument*. The same signal can fail on a one-lot option and pass on
  futures, because the round trip is 0.50-1.25% against 0.03%. This failure is
  a sizing and instrument problem, not a dead end.
* **sign flip between windows** -- the rule describes a regime, not an effect.
* **insufficient sample** -- the honest "do not know", which an overlapping
  study reaches far sooner than its raw bar count suggests.

Reporting those separately is the point. A single pass/fail column would make
a cost failure look identical to a noise failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..validation import (
    COST_INDEX_PCT,
    MIN_SAMPLE,
    EdgeStudy,
    evaluate_signal,
    score_forward_returns,
    split_sample,
    validate_rule,
)
from . import REGISTRY, Strategy, load_catalogue


@dataclass
class StrategyResult:
    """One strategy, measured."""

    name: str
    family: str
    instrument: str
    horizon: int
    status: str = "not run"
    train: EdgeStudy | None = None
    test: EdgeStudy | None = None
    verdict: dict = field(default_factory=dict)
    tradeable: bool = False
    #: Why it could not be measured, where that is the outcome.
    skipped_because: str = ""
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "name": self.name, "family": self.family,
            "instrument": self.instrument, "horizon": self.horizon,
            "status": self.status, "tradeable": self.tradeable,
            "observations": self.test.observations if self.test else 0,
            "test_mean_pct": self.test.mean_pct if self.test else None,
            "test_t_hac": self.test.t_stat if self.test else None,
            "test_t_iid": self.test.t_stat_iid if self.test else None,
            "win_rate": self.test.win_rate if self.test else None,
            "skipped_because": self.skipped_because,
        }


def prepare(frame: pd.DataFrame, *, pair: pd.DataFrame | None = None,
            volume_profile_sessions: bool = True) -> pd.DataFrame:
    """Attach the extra columns the catalogue's strategies ask for.

    Strategies declare their needs in ``Strategy.requires`` and return a flat
    signal when a column is absent, so a missing input produces "no positions
    taken" rather than a crash or, worse, a plausible-looking result computed
    from nothing.

    The previous session's volume-profile levels are attached here because they
    must be **lagged by a whole session**: today's value area is not known until
    today is over, and using it would be the purest form of lookahead.
    """
    out = frame.copy()

    if pair is not None and not pair.empty:
        # Align on the shared index only -- a reindex-and-fill would carry a
        # stale price from the other index across a gap and invent a divergence.
        joined = pair["Close"].reindex(out.index)
        out["pair_close"] = joined

    if volume_profile_sessions and "Volume" in out.columns:
        try:
            from ..volume_profile import build_profile

            dates = pd.DatetimeIndex(out.index).date
            levels: dict = {}
            for day, chunk in out.groupby(dates):
                profile = build_profile(chunk)
                if profile.usable:
                    levels[day] = profile
            ordered = sorted(levels)
            # Map each session to the PREVIOUS session's levels.
            previous = {
                day: levels[ordered[i - 1]]
                for i, day in enumerate(ordered) if i > 0
            }
            out["prev_poc"] = [
                previous[d].poc if d in previous else np.nan for d in dates
            ]
            out["prev_value_area_high"] = [
                previous[d].value_area_high if d in previous else np.nan
                for d in dates
            ]
            out["prev_value_area_low"] = [
                previous[d].value_area_low if d in previous else np.nan
                for d in dates
            ]
        except Exception:  # noqa: BLE001 -- a missing profile is not a failure
            pass

    return out


def _absolute_move_study(study: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """A study whose forward return is the ABSOLUTE move.

    A volatility strategy does not take a direction, so scoring it against a
    signed return would measure whether it accidentally leaned the right way.
    What it claims is that the move will be LARGE, so the outcome it must be
    scored against is the size of the move -- with the median absolute move
    subtracted, so "larger than usual" is the thing being tested rather than
    "non-zero", which is trivially true.
    """
    column = f"fwd{horizon}"
    out = study.copy()
    absolute = out[column].abs()
    out[column] = absolute - absolute.median()
    return out


def run_strategy(
    strategy: Strategy, frame: pd.DataFrame, *, train_fraction: float = 0.70,
    hypotheses_tested: int | None = None,
) -> StrategyResult:
    """Measure one strategy on one prepared frame."""
    result = StrategyResult(
        name=strategy.name, family=strategy.family,
        instrument=strategy.instrument, horizon=strategy.horizon,
    )

    missing = [c for c in strategy.requires if c not in frame.columns]
    if missing:
        result.status = "missing inputs"
        result.skipped_because = (
            f"needs {missing}, which this frame does not carry"
        )
        return result

    if len(frame) < MIN_SAMPLE * strategy.horizon:
        result.status = "insufficient data"
        result.skipped_because = (
            f"{len(frame)} bars cannot support {MIN_SAMPLE} independent "
            f"observations at a {strategy.horizon}-bar hold -- overlapping "
            f"samples need roughly {MIN_SAMPLE * strategy.horizon}"
        )
        return result

    try:
        signal = strategy.signal(frame)
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.skipped_because = f"{type(exc).__name__}: {exc}"
        return result

    signal = pd.Series(signal, index=frame.index).fillna(0.0)
    if not (signal != 0).any():
        result.status = "no positions taken"
        result.skipped_because = (
            "the signal never left flat on this data -- either its inputs are "
            "absent or its thresholds were never reached"
        )
        return result

    study = score_forward_returns(frame, [0] * len(frame),
                                  horizons=(strategy.horizon,))
    if strategy.family == "volatility":
        study = _absolute_move_study(study, strategy.horizon)
        result.notes.append(
            "Scored against the absolute forward move less its median: this "
            "strategy claims size, not direction."
        )

    train_study, test_study = split_sample(study, train_fraction)
    train_signal = signal.loc[train_study.index]
    test_signal = signal.loc[test_study.index]

    result.train = evaluate_signal(train_study, train_signal,
                                   horizon=strategy.horizon, label="train")
    result.test = evaluate_signal(test_study, test_signal,
                                  horizon=strategy.horizon, label="test")

    if result.test.observations < MIN_SAMPLE:
        result.status = "insufficient sample"
        result.skipped_because = (
            f"{result.test.observations} holdout observations, below "
            f"{MIN_SAMPLE}"
        )
        return result

    searched = hypotheses_tested if hypotheses_tested is not None else len(REGISTRY)
    verdict = validate_rule(
        result.train, result.test, instrument=strategy.instrument,
        hypotheses_tested=max(1, searched),
    )
    result.verdict = verdict.as_dict()
    result.tradeable = verdict.tradeable
    result.status = "tradeable" if verdict.tradeable else _why_not(verdict)
    return result


def _why_not(verdict) -> str:
    """Name the binding failure, because they are not interchangeable."""
    checks = verdict.checks or {}
    if checks.get("sample") is False:
        return "insufficient sample"
    if checks.get("sign_agreement") is False:
        return "regime-dependent (sign flips)"
    if checks.get("holdout_significance") is False:
        return "not significant"
    if checks.get("corroborated") is False:
        return "not corroborated"
    if checks.get("clears_costs") is False:
        return "loses to costs"
    return "rejected"


def screen_all(
    frame: pd.DataFrame, *, pair: pd.DataFrame | None = None,
    families: tuple | None = None, train_fraction: float = 0.70,
) -> dict:
    """Measure the whole catalogue on one instrument's bars.

    ``hypotheses_tested`` is set to the number of strategies actually run, so
    the significance bar rises with the size of the search. Reporting the best
    of twenty strategies needs a higher threshold than testing one, and a
    catalogue is a search whether or not it is described as one -- this is the
    trap that nearly shipped a fake edge in October: one cell of twenty at
    t=+2.61 is what chance delivers.
    """
    load_catalogue()
    chosen = [s for s in REGISTRY.values()
              if families is None or s.family in families]
    prepared = prepare(frame, pair=pair)

    results = [
        run_strategy(s, prepared, train_fraction=train_fraction,
                     hypotheses_tested=len(chosen))
        for s in chosen
    ]

    by_status: dict[str, list] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r.name)

    tradeable = [r for r in results if r.tradeable]
    measured = [r for r in results if r.test is not None]

    return {
        "strategies_run": len(chosen),
        "measured": len(measured),
        "tradeable": [r.name for r in tradeable],
        "by_status": by_status,
        "results": results,
        "significance_bar": (
            f"Raised for a search of {len(chosen)} strategies: reporting the "
            f"best of {len(chosen)} needs a higher bar than testing one."
        ),
        "note": (
            f"{len(measured)} of {len(chosen)} strategies produced a "
            f"measurable P&L series; {len(tradeable)} passed every check. "
            + ("Nothing passed, which is the expected result and consistent "
               "with the standing conclusion that no rule in this project "
               "clears its own transaction costs."
               if not tradeable else
               "A pass earns a forward test, not capital: the holdout is still "
               "a backtest, and this catalogue was searched to find it.")
        ),
    }


def report(screen: dict) -> str:
    """A readable table of what survived and what did not."""
    lines = [
        "=" * 92,
        "STRATEGY SCREEN",
        "=" * 92,
        f"{'strategy':32} {'family':15} {'instrument':13} {'n':>5} "
        f"{'mean%':>8} {'t_iid':>7} {'t_HAC':>7}  status",
        "-" * 92,
    ]
    order = {"tradeable": 0, "loses to costs": 1, "not corroborated": 2,
             "not significant": 3, "regime-dependent (sign flips)": 4}
    for r in sorted(screen["results"],
                    key=lambda x: (order.get(x.status, 9), x.name)):
        if r.test is None:
            lines.append(
                f"{r.name:32} {r.family:15} {r.instrument:13} "
                f"{'-':>5} {'-':>8} {'-':>7} {'-':>7}  {r.status}"
            )
            if r.skipped_because:
                lines.append(f"{'':32} -> {r.skipped_because}")
            continue
        lines.append(
            f"{r.name:32} {r.family:15} {r.instrument:13} "
            f"{r.test.observations:>5} {r.test.mean_pct:>8.4f} "
            f"{r.test.t_stat_iid:>7.2f} {r.test.t_stat:>7.2f}  {r.status}"
        )
    lines.append("-" * 92)
    lines.append(screen["significance_bar"])
    lines.append(screen["note"])
    return "\n".join(lines)
