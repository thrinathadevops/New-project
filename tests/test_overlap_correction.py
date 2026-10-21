"""The gate was too permissive, and these measure by how much.

``validation.py`` measured a 24-bar forward return at **every** bar, so
consecutive observations shared 23 of their 24 bars of path, and then divided
by ``std/sqrt(n)`` -- the standard error of a mean of *independent* draws. The
headline finding of the whole project ("a real, out-of-sample, statistically
significant edge exists on BANKNIFTY", t=+3.87) came out of that calculation.

:func:`test_the_naive_statistic_finds_an_edge_in_pure_noise` is the test that
settles it: on a random walk with **no edge whatsoever** and a persistent
trend-following score of the kind ``scoring.py`` actually produces, the naive
t-statistic clears its own significance bar nearly half the time.

That is not a marginal correction. It means the check the project relied on to
separate a finding from a story was firing on noise roughly every other time it
was asked.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.validation import (
    COST_INDEX_PCT,
    EdgeStudy,
    block_bootstrap_pvalue,
    effective_sample_size,
    evaluate_rule,
    newey_west_tstat,
    non_overlapping_tstat,
    validate_rule,
)

HORIZON = 24


def noise_study(seed: int, bars: int = 1_500, horizon: int = HORIZON,
                drift: float = 0.0) -> pd.DataFrame:
    """A random walk with a PERSISTENT score. True edge: zero.

    The score is price against its own EMA, ranked -- a stand-in for the
    trend-following components that make up most of ``scoring.py``. Its
    persistence matters: a randomly scattered score breaks the overlap by
    alternating long and short observations, which cancels the inflation and
    hides the bug. A real scorer is persistent, so a real study does not.
    """
    rng = np.random.default_rng(seed)
    close = 22_000 * np.exp(np.cumsum(rng.normal(drift, 0.0008, bars)))
    series = pd.Series(close)
    momentum = series / series.ewm(span=20).mean() - 1
    score = (momentum.rank(pct=True) * 100).fillna(50).to_numpy()
    forward = (series.shift(-horizon) / series - 1) * 100
    return pd.DataFrame(
        {"close": close, "score": score, f"fwd{horizon}": forward.values},
        index=pd.date_range("2026-09-01 09:15", periods=bars, freq="5min"),
    )


# =============================================== the effective sample size ==

def test_overlapping_observations_are_counted_once_not_h_times():
    sizing = effective_sample_size(453, 24)

    assert sizing["effective_observations"] == pytest.approx(18.9, abs=0.1)
    assert sizing["inflation_factor"] == pytest.approx(4.9, abs=0.05)
    assert "overstated by roughly 4.9x" in sizing["note"]


def test_the_headline_banknifty_result_does_not_survive_the_correction():
    """The project's own flagship number, corrected.

    n=453, 24-bar hold, reported t=+3.87. Dividing by the inflation factor
    leaves roughly +0.79, which is not significance by any threshold.
    """
    inflation = effective_sample_size(453, 24)["inflation_factor"]
    corrected = 3.87 / inflation

    assert corrected == pytest.approx(0.79, abs=0.02)
    assert abs(corrected) < 2.0, (
        "the edge the project called statistically significant was not"
    )


def test_a_single_bar_horizon_needs_no_correction():
    sizing = effective_sample_size(500, 1)
    assert sizing["effective_observations"] == 500
    assert sizing["inflation_factor"] == 1.0


# ================================================== the false-positive rate ==

def test_the_naive_statistic_finds_an_edge_in_pure_noise():
    """THE test. A correct significance test rejects a true null about 5% of
    the time. Measured over 60 independent random walks with no edge at all.

    The naive figure is left on EdgeStudy as ``t_stat_iid`` precisely so this
    comparison stays visible rather than becoming folklore.
    """
    naive = corrected = bootstrap = non_overlap = trials = 0
    for seed in range(60):
        result = evaluate_rule(noise_study(seed), horizon=HORIZON,
                               buy_below=9, sell_above=90, fade=True)
        if result.observations < 30:
            continue
        trials += 1
        naive += abs(result.t_stat_iid) >= 2
        corrected += abs(result.t_stat) >= 2
        if result.bootstrap.get("usable"):
            bootstrap += bool(result.bootstrap["significant_at_5pct"])
        if result.non_overlapping.get("usable"):
            non_overlap += abs(result.non_overlapping["median_t"]) >= 2

    assert trials >= 50, "need a real sample of trials to measure a rate"

    naive_rate = naive / trials
    corrected_rate = corrected / trials
    non_overlap_rate = non_overlap / trials

    # The bug: the naive statistic is wrong by an order of magnitude, not by a
    # rounding error. Measured at 53% on this sample -- it finds an edge in
    # noise MORE OFTEN THAN NOT.
    assert naive_rate > 0.40, (
        f"expected the naive rate to be badly inflated; got {naive_rate:.0%}"
    )
    # The HAC fix is a large improvement and still not sufficient on its own,
    # which is the documented weakness of Newey-West on overlapping sums.
    assert corrected_rate <= 0.25, (
        f"HAC correction left a {corrected_rate:.0%} false-positive rate"
    )
    assert corrected_rate < naive_rate / 2, (
        "the correction must materially reduce the error, not shave it"
    )
    # The overlap-free test is the properly conservative backstop, and is the
    # reason validate_rule requires it to agree.
    assert non_overlap_rate <= corrected_rate, (
        "the non-overlapping test must not be the loosest of the three"
    )
    assert non_overlap_rate <= 0.10


def test_a_genuine_effect_still_registers_after_the_correction():
    """A correction that destroys real findings is not a correction, it is a
    way of never being wrong.

    Tested directly on the statistic rather than through a scoring rule: a
    series with a real drift produces overlapping forward returns with a
    genuinely non-zero mean, and the HAC estimator must still see it.

    (My first attempt at this test asserted that a drifting *random walk* would
    register through the rank-based fade rule. It does not, and that is a fact
    about the rule, not about the statistic -- the top decile of price-over-EMA
    is the most extended point of a run, not the most promising one. The test
    was measuring the wrong thing.)
    """
    rng = np.random.default_rng(42)
    sigma, mu = 0.0008, 0.00015          # a real, strong, honest trend
    close = 22_000 * np.exp(np.cumsum(rng.normal(mu, sigma, 1_500)))
    series = pd.Series(close)
    forward = ((series.shift(-HORIZON) / series - 1) * 100).dropna()

    hac = newey_west_tstat(forward, HORIZON)
    assert hac["t_stat"] >= 2, (
        f"a real effect was corrected away: t={hac['t_stat']}"
    )
    bootstrap = block_bootstrap_pvalue(forward, HORIZON, draws=500)
    assert bootstrap["significant_at_5pct"] is True


# ========================================================= the HAC estimator ==

def test_the_hac_standard_error_exceeds_the_naive_one_on_overlapping_data():
    study = noise_study(7)
    series = study["fwd24"].dropna()

    naive_se = series.std(ddof=1) / math.sqrt(len(series))
    hac = newey_west_tstat(series, HORIZON)

    assert hac["standard_error"] > naive_se, (
        "overlap makes the true standard error LARGER, never smaller"
    )
    assert hac["lags"] == HORIZON - 1, "the MA order the overlap induces"


def test_the_hac_lag_count_cannot_exceed_the_sample():
    hac = newey_west_tstat([1.0, 2.0, 3.0, 2.0, 1.0], horizon=500)
    assert hac["lags"] <= 3
    assert hac["t_stat"] is not None


def test_a_degenerate_sample_is_perfect_consistency_not_noise():
    """The same trap the module already fixed once for the i.i.d. path: zero
    dispersion with a non-zero mean is maximal significance, not zero."""
    hac = newey_west_tstat([0.6] * 80, horizon=12)

    assert math.isinf(hac["t_stat"]) and hac["t_stat"] > 0
    assert any("Perfect consistency" in n for n in hac["notes"])


def test_an_all_zero_sample_has_no_signal_either_way():
    hac = newey_west_tstat([0.0] * 80, horizon=12)
    assert hac["t_stat"] == 0.0


def test_too_short_a_series_gets_no_inference():
    hac = newey_west_tstat([1.0, 2.0], horizon=4)
    assert hac["t_stat"] is None
    assert any("no inference" in n for n in hac["notes"])


# ============================================ the overlap-free alternatives ==

def test_every_non_overlapping_offset_is_reported_not_just_a_lucky_one():
    """Picking one offset would be a free parameter, and a free parameter is
    how a story becomes a finding."""
    out = non_overlapping_tstat(noise_study(3)["fwd24"].dropna(), HORIZON)

    assert out["usable"]
    assert out["subsamples"] == HORIZON
    assert len(out["per_offset"]) == HORIZON
    assert out["min_t"] <= out["median_t"] <= out["max_t"]


def test_disagreement_on_the_sign_is_called_what_it_is():
    out = non_overlapping_tstat(noise_study(5)["fwd24"].dropna(), HORIZON)
    if not out["all_offsets_agree_on_sign"]:
        assert "do not even agree on the SIGN" in out["note"]


def test_a_study_too_short_to_subsample_says_so():
    out = non_overlapping_tstat([0.1] * 10, horizon=24)
    assert out["usable"] is False
    assert "too short" in out["note"]


def test_the_bootstrap_block_is_longer_than_the_hold_because_equal_was_worse():
    """A block exactly the length of the dependence is marginal, and measuring
    it said so: 15.0% false positives at 1x the horizon against 13.3% at 2x,
    with 100% power either way. The default is 2x for that reason."""
    out = block_bootstrap_pvalue(noise_study(4)["fwd24"].dropna(), HORIZON,
                                 draws=400)

    assert out["usable"]
    assert out["block_length"] == HORIZON * 2
    assert 0 < out["p_value"] <= 1


def test_the_block_length_is_configurable_for_a_calibration_study():
    out = block_bootstrap_pvalue(noise_study(4)["fwd24"].dropna(), HORIZON,
                                 draws=200, block_multiple=4)
    assert out["block_length"] == HORIZON * 4


def test_the_bootstrap_p_value_is_never_exactly_zero():
    """A p-value of 0 claims more resolution than the resample count provides."""
    out = block_bootstrap_pvalue([0.5] * 200, horizon=12, draws=200)
    assert out["p_value"] > 0


def test_the_bootstrap_is_reproducible():
    series = noise_study(8)["fwd24"].dropna()
    first = block_bootstrap_pvalue(series, HORIZON, draws=300)
    second = block_bootstrap_pvalue(series, HORIZON, draws=300)
    assert first["p_value"] == second["p_value"], (
        "a risk check that changes its mind at random cannot be tested"
    )


def test_too_few_observations_to_bootstrap_is_refused():
    out = block_bootstrap_pvalue([0.1] * 8, horizon=24)
    assert out["usable"] is False
    assert "near-copies" in out["note"]


# ============================================= the gate uses the correction ==

def test_evaluate_rule_reports_both_statistics_and_the_effective_count():
    result = evaluate_rule(noise_study(2), horizon=HORIZON, buy_below=9,
                           sell_above=90, fade=True)

    assert result.t_stat_iid != 0
    assert result.effective_observations == pytest.approx(
        result.observations / HORIZON, abs=0.1)
    assert result.non_overlapping.get("usable") is True
    assert result.bootstrap.get("usable") is True
    assert any("Overlap correction" in n for n in result.notes)


def test_the_verdict_cites_the_uncorrected_number_when_it_disagreed():
    """If the naive figure would have called it significant and the corrected
    one does not, say so -- that is the whole lesson."""
    for seed in range(40):
        result = evaluate_rule(noise_study(seed), horizon=HORIZON,
                               buy_below=9, sell_above=90, fade=True)
        if abs(result.t_stat_iid) >= 2 > abs(result.t_stat):
            assert result.verdict == "no measurable edge"
            assert any("uncorrected i.i.d. figure" in n for n in result.notes)
            return
    pytest.skip("no seed produced the disagreement in this sample")


def _stub(mean_pct: float, t_stat: float, n: int = 3_000, *,
          bootstrap: dict | None = None, non_overlapping: dict | None = None):
    return EdgeStudy(
        label="stub", horizon_bars=24, observations=n, mean_pct=mean_pct,
        t_stat=t_stat, win_rate=55.0,
        bootstrap=bootstrap or {}, non_overlapping=non_overlapping or {},
        net_by_instrument={
            k: {"cost_pct": c, "net_pct": mean_pct - c,
                "profitable": mean_pct - c > 0}
            for k, c in COST_INDEX_PCT.items()
        },
    )


def test_a_bootstrap_that_cannot_reject_zero_blocks_the_rule():
    verdict = validate_rule(
        train=_stub(0.60, 3.0), instrument="equity",
        test=_stub(0.60, 3.0, bootstrap={"usable": True, "p_value": 0.41,
                                         "significant_at_5pct": False}),
    )

    assert verdict.tradeable is False
    assert verdict.checks["corroborated"] is False
    assert any("does not reject a zero mean" in r for r in verdict.reasons)


def test_subsamples_disagreeing_on_sign_block_the_rule():
    verdict = validate_rule(
        train=_stub(0.60, 3.0), instrument="equity",
        test=_stub(0.60, 3.0, non_overlapping={
            "usable": True, "median_t": 2.5,
            "all_offsets_agree_on_sign": False}),
    )

    assert verdict.tradeable is False
    assert any("disagree on the sign" in r for r in verdict.reasons)


def test_a_median_subsample_t_inside_the_noise_blocks_the_rule():
    verdict = validate_rule(
        train=_stub(0.60, 3.0), instrument="equity",
        test=_stub(0.60, 3.0, non_overlapping={
            "usable": True, "median_t": 0.8,
            "all_offsets_agree_on_sign": True}),
    )

    assert verdict.tradeable is False
    assert any("inside the noise once the overlap is discarded" in r
               for r in verdict.reasons)


def test_corroborating_evidence_lets_a_rule_through():
    verdict = validate_rule(
        train=_stub(0.60, 3.0), instrument="equity",
        test=_stub(0.60, 3.0,
                   bootstrap={"usable": True, "p_value": 0.004,
                              "significant_at_5pct": True},
                   non_overlapping={"usable": True, "median_t": 2.4,
                                    "all_offsets_agree_on_sign": True}),
    )

    assert verdict.tradeable is True
    assert verdict.checks["corroborated"] is True


def test_absent_corroboration_does_not_block_a_hand_built_study():
    """Absence of corroboration is not disconfirmation. A study from
    evaluate_rule always carries it; a hand-built one cannot, and must not be
    rejected for a test that was never run."""
    verdict = validate_rule(train=_stub(0.60, 3.0), test=_stub(0.60, 3.0),
                            instrument="equity")

    assert verdict.checks["corroborated"] is None
    assert verdict.tradeable is True
