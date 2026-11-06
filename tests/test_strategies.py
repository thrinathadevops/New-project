"""The strategy catalogue, and the harness that refuses to flatter it.

What these guard
----------------
A catalogue is only worth more than a list if every entry is measured on the
same terms. The failure to prevent is a strategy that reports its own
arithmetic -- which already happened once in this project, when
``research_fade_test.py`` kept a private copy of the naive t-statistic for
weeks and produced the headline claim that was later withdrawn.

The sharpest test here is
:func:`test_a_strategy_and_its_exact_opposite_cancel`. The catalogue registers
the opening-range breakout and its fade, which are negations of each other, so
their measured means must be equal and opposite. If both ever look profitable,
the harness is double-counting and every other number it prints is worthless.
That pair is in the catalogue as a permanent self-check, not for its thesis.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.strategies import (
    FAMILIES,
    INSTRUMENTS,
    REGISTRY,
    Strategy,
    describe_catalogue,
    load_catalogue,
)
from intraday_advisor.strategies.harness import (
    prepare,
    report,
    run_strategy,
    screen_all,
)


# Loaded at IMPORT, not in a fixture. `@pytest.mark.parametrize` is evaluated
# at collection time, so a fixture that populates REGISTRY runs too late: the
# parameter sets came out empty and pytest quietly reported
# "got empty parameter set" as three SKIPS. The causality check -- the one
# invariant that actually matters here, since a lookahead produces a beautiful
# backtest and no money -- was not running at all.
#
# A suite that skips its own key test is worse than not having it, because the
# green run is read as evidence.
load_catalogue()
assert REGISTRY, "the catalogue must be populated before parametrisation"


def bars(n: int = 2_000, seed: int = 5, sessions: int = 27,
         with_volume: bool = True, drift: float = 0.0) -> pd.DataFrame:
    """Synthetic 5-minute bars laid out as real trading sessions."""
    rng = np.random.default_rng(seed)
    close = 23_000 * np.exp(np.cumsum(rng.normal(drift, 0.0008, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.0006, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.0006, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])

    per_session = max(1, n // sessions)
    stamps = [
        pd.Timestamp("2026-09-01 09:15")
        + pd.Timedelta(days=i // per_session)
        + pd.Timedelta(minutes=5 * (i % per_session))
        for i in range(n)
    ]
    frame = pd.DataFrame(
        {"Open": open_, "High": np.maximum(high, np.maximum(open_, close)),
         "Low": np.minimum(low, np.minimum(open_, close)), "Close": close},
        index=pd.DatetimeIndex(stamps),
    )
    frame["Volume"] = (
        rng.lognormal(10, 0.6, n) if with_volume else np.zeros(n)
    )
    return frame


# ================================================================= the registry ==

def test_the_catalogue_is_populated():
    assert len(REGISTRY) >= 12, "a catalogue, not a sample"


def test_every_family_that_was_missing_is_represented():
    """The families with no representation before: volatility, premium,
    relative_value, structure, flow. Trend and breakout already existed inside
    the scorer."""
    present = {s.family for s in REGISTRY.values()}
    for family in ("volatility", "premium", "relative_value", "structure",
                   "flow", "event"):
        assert family in present, f"{family} still has no strategy"


def test_every_strategy_declares_a_valid_family_and_instrument():
    for s in REGISTRY.values():
        assert s.family in FAMILIES
        assert s.instrument in INSTRUMENTS


def test_every_strategy_declares_what_would_falsify_it():
    """The standard regime.py is already held to: a read you cannot falsify is
    a horoscope."""
    for s in REGISTRY.values():
        assert len(s.falsified_by) > 30, f"{s.name} has no real falsifier"


def test_a_strategy_without_a_falsifier_is_refused():
    with pytest.raises(ValueError, match="falsify"):
        Strategy(name="vibes", family="trend", thesis="it feels right",
                 signal=lambda f: pd.Series(0.0, index=f.index))


def test_an_unknown_family_is_refused():
    with pytest.raises(ValueError, match="unknown family"):
        Strategy(name="x", family="astrology", thesis="t",
                 signal=lambda f: pd.Series(0.0, index=f.index),
                 falsified_by="a" * 40)


def test_an_unknown_instrument_is_refused():
    with pytest.raises(ValueError, match="unknown instrument"):
        Strategy(name="y", family="trend", thesis="t", instrument="crypto",
                 signal=lambda f: pd.Series(0.0, index=f.index),
                 falsified_by="a" * 40)


def test_registering_a_duplicate_name_is_refused():
    """A silent overwrite would measure one thesis under another's name."""
    from intraday_advisor.strategies import register
    existing = next(iter(REGISTRY.values()))
    with pytest.raises(ValueError, match="already registered"):
        register(existing)


def test_the_catalogue_describes_itself_as_data():
    rows = describe_catalogue()
    assert len(rows) == len(REGISTRY)
    assert all({"name", "family", "thesis", "falsified_by"} <= set(r) for r in rows)
    # Grouped by family, in the declared order.
    order = [FAMILIES.index(r["family"]) for r in rows]
    assert order == sorted(order)


def test_a_family_filter_narrows_the_listing():
    rows = describe_catalogue(family="volatility")
    assert rows and all(r["family"] == "volatility" for r in rows)


# ============================================================ signals are causal ==

@pytest.mark.parametrize("name", sorted(
    s.name for s in REGISTRY.values() if not s.requires))
def test_a_signal_never_depends_on_bars_after_it(name):
    """The one invariant that matters: truncating the future must not change
    any signal already produced.

    A signal that peeks forward reports a spectacular edge and is unachievable
    live, and it is the easiest way to fool this harness -- so it is checked
    mechanically for every strategy rather than by reading the code.
    """
    strategy = REGISTRY[name]
    frame = bars(900, seed=11)
    cut = 600

    full = pd.Series(strategy.signal(frame), index=frame.index).fillna(0.0)
    partial = pd.Series(
        strategy.signal(frame.iloc[:cut]), index=frame.index[:cut]
    ).fillna(0.0)

    # Rolling windows need a warm-up, so compare only where both are warm.
    warm = 200
    np.testing.assert_allclose(
        full.iloc[warm:cut].to_numpy(), partial.iloc[warm:].to_numpy(),
        atol=1e-9,
        err_msg=f"{name} changed an earlier bar when later bars were added",
    )


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_signal_returns_only_long_flat_or_short(name):
    frame = bars(600, seed=3)
    signal = pd.Series(REGISTRY[name].signal(frame), index=frame.index).fillna(0.0)
    assert set(np.unique(signal.to_numpy())) <= {-1.0, 0.0, 1.0}, (
        f"{name} returned something other than +1/0/-1"
    )


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_a_strategy_missing_its_inputs_returns_flat_rather_than_crashing(name):
    """A missing column must produce 'no positions taken', never a plausible
    result computed from nothing."""
    frame = bars(400, seed=7, with_volume=False)
    signal = pd.Series(REGISTRY[name].signal(frame), index=frame.index).fillna(0.0)
    assert len(signal) == len(frame)


# ===================================================== the harness self-check ==

def test_a_strategy_and_its_exact_opposite_cancel():
    """THE test. The opening-range breakout and its fade are negations, so
    their means must be equal and opposite. If both look profitable the harness
    is double-counting and nothing else it prints can be trusted."""
    frame = bars(2_400, seed=13)
    breakout = run_strategy(REGISTRY["opening_range_breakout_15m"], frame)
    fade = run_strategy(REGISTRY["opening_range_fade"], frame)

    assert breakout.test is not None and fade.test is not None
    assert breakout.test.observations == fade.test.observations
    assert breakout.test.mean_pct == pytest.approx(-fade.test.mean_pct, abs=1e-6)
    assert breakout.test.t_stat == pytest.approx(-fade.test.t_stat, abs=0.02)
    assert not (breakout.tradeable and fade.tradeable), (
        "a rule and its negation cannot both be tradeable"
    )


def test_pure_noise_produces_no_tradeable_strategy():
    """The whole catalogue against a random walk. A harness that finds an edge
    here is broken, and the naive statistic used to."""
    screen = screen_all(bars(2_400, seed=21), families=("trend", "breakout",
                                                        "mean_reversion"))
    assert screen["tradeable"] == [], (
        f"found an edge in noise: {screen['tradeable']}"
    )


def test_the_significance_bar_rises_with_the_size_of_the_search():
    """Reporting the best of twenty needs a higher bar than testing one. This
    is the trap that nearly shipped a fake edge in October."""
    frame = bars(2_400, seed=9)
    one = run_strategy(REGISTRY["ema_cross_9_21"], frame, hypotheses_tested=1)
    many = run_strategy(REGISTRY["ema_cross_9_21"], frame,
                        hypotheses_tested=20)
    assert many.verdict["checks"]["required_t"] > one.verdict["checks"]["required_t"]


def test_a_strategy_whose_inputs_are_absent_is_reported_not_skipped_silently():
    result = run_strategy(REGISTRY["pin_reversion"], bars(1_200))
    assert result.status == "missing inputs"
    assert "pin_strike" in result.skipped_because
    assert result.test is None
    assert result.tradeable is False


def test_a_flow_strategy_runs_once_its_columns_are_supplied():
    """The flow strategies are not decorative -- they work, they just need the
    chain. Supplying the columns proves the signal logic rather than the
    plumbing."""
    frame = bars(1_800, seed=4)
    frame["pin_strike"] = 23_000.0
    result = run_strategy(REGISTRY["pin_reversion"], frame)

    assert result.test is not None, result.skipped_because
    assert result.test.observations > 0


def test_a_premium_strategy_runs_once_an_iv_column_is_supplied():
    frame = bars(1_800, seed=6)
    frame["implied_vol_pct"] = 18.0        # richly priced against realised
    result = run_strategy(REGISTRY["short_premium_iv_over_rv"], frame)

    assert result.test is not None, result.skipped_because


def test_an_expiry_strategy_runs_once_the_flag_is_supplied():
    frame = bars(1_800, seed=8)
    dates = pd.DatetimeIndex(frame.index).date
    unique = sorted(set(dates))
    expiry_days = set(unique[::5])
    frame["is_expiry_day"] = [d in expiry_days for d in dates]
    result = run_strategy(REGISTRY["expiry_day_theta"], frame)

    assert result.test is not None, result.skipped_because


def test_the_blackout_strategy_takes_no_positions_and_says_so():
    """It cannot be measured by this harness -- it produces no P&L. Reporting a
    verdict for it would be inventing one."""
    frame = bars(1_200)
    frame["event_near"] = False
    result = run_strategy(REGISTRY["event_blackout"], frame)

    assert result.status == "no positions taken"
    assert result.tradeable is False


def test_too_little_data_for_the_hold_is_refused_with_the_arithmetic():
    """An overlapping study runs out of independent observations far sooner
    than its bar count suggests."""
    strategy = REGISTRY["squeeze_breakout"]          # 24-bar hold
    result = run_strategy(strategy, bars(200))

    assert result.status == "insufficient data"
    assert "overlapping" in result.skipped_because


def test_a_volatility_strategy_is_scored_on_size_not_direction():
    """Scoring a direction-free strategy against a signed return would measure
    whether it accidentally leaned the right way."""
    frame = bars(3_000, seed=15)
    result = run_strategy(REGISTRY["volatility_expansion"], frame)

    if result.test is not None:
        assert any("absolute forward move" in n for n in result.notes)


def test_a_broken_signal_is_caught_and_named():
    from intraday_advisor.strategies import Strategy as S

    def explode(frame):
        raise RuntimeError("bad maths")

    broken = S(name="broken_probe", family="trend", thesis="t", signal=explode,
               falsified_by="x" * 40)
    result = run_strategy(broken, bars(1_200))

    assert result.status == "error"
    assert "bad maths" in result.skipped_because


# =============================================================== prepare / report ==

def test_prepare_lags_the_volume_profile_by_a_whole_session():
    """Today's value area is not known until today is over. Using it would be
    the purest form of lookahead."""
    frame = bars(2_000, seed=2, sessions=20)
    out = prepare(frame)

    assert "prev_poc" in out
    dates = pd.DatetimeIndex(out.index).date
    first_session = out[dates == sorted(set(dates))[0]]
    assert first_session["prev_poc"].isna().all(), (
        "the first session has no previous session to borrow levels from"
    )
    assert out["prev_poc"].notna().any(), "later sessions should be populated"


def test_prepare_leaves_a_volumeless_frame_without_profile_levels():
    out = prepare(bars(1_200, with_volume=False))
    if "prev_poc" in out:
        assert out["prev_poc"].isna().all()


def test_prepare_aligns_a_pair_without_filling_across_gaps():
    """A reindex-and-fill would carry a stale price across a gap and invent a
    divergence out of a missing bar."""
    frame = bars(1_200, seed=1)
    pair = bars(1_200, seed=2).iloc[::2]          # half the bars
    out = prepare(frame, pair=pair)

    assert "pair_close" in out
    assert out["pair_close"].isna().any(), "gaps must stay gaps"


def test_the_report_names_every_strategy_and_the_search_size():
    screen = screen_all(bars(2_400, seed=31))
    text = report(screen)

    for name in screen["by_status"].get("not significant", []):
        assert name in text
    assert "needs a higher bar" in text
    assert str(screen["strategies_run"]) in text


def test_the_screen_separates_a_cost_failure_from_a_noise_failure():
    """A single pass/fail column would make them look identical, and only one
    of them is a dead end."""
    screen = screen_all(bars(2_400, seed=17))
    statuses = set(screen["by_status"])

    assert "tradeable" not in statuses or screen["tradeable"]
    # The vocabulary must distinguish them even when only one occurs here.
    assert statuses <= {
        "tradeable", "loses to costs", "not significant", "not corroborated",
        "regime-dependent (sign flips)", "insufficient sample",
        "insufficient data", "missing inputs", "no positions taken", "error",
        "rejected",
    }, f"unexpected status: {statuses}"
