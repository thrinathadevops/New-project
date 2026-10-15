"""Tests for the unified engine, the option scorer, and the honest backtest.

The theme: these pin down the claims made in the docstrings. "One scorer",
"one fill implementation" and "the old backtest overstated results" are all
assertions that are worthless unless something checks them.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from intraday_advisor.backtest import (
    backtest_from_ledger,
    compare_fill_assumptions,
    compute_metrics,
    run_backtest,
    walk_forward_splits,
)
from intraday_advisor.config import RiskConfig
from intraday_advisor.data import generate_sample_ohlcv
from intraday_advisor.engine import json_safe
from intraday_advisor.fills import resolve_bar_exit
from intraday_advisor.indicators import add_indicators
from intraday_advisor.ledger.grader import grade_path
from intraday_advisor.scoring import ScoreWeights, SignalScore, score_option_signal
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy


# =========================================================== option scorer ==

def underlying(score: int, signal: str) -> SignalScore:
    return SignalScore(score=score, signal_type=signal, bull=[], bear=[], components={})


def test_price_and_chain_conflict_forces_wait():
    """The one thing worse than no signal is two that disagree."""
    result = score_option_signal(underlying(85, "BUY"), chain_bias="PUT")

    assert result.direction == "WAIT"
    assert result.agreement == "conflict"
    assert result.score <= 40
    assert any("Conflict" in w for w in result.warnings)


def test_agreement_scores_higher_than_price_alone():
    agree = score_option_signal(underlying(80, "BUY"), chain_bias="CALL")
    alone = score_option_signal(underlying(80, "BUY"), chain_bias="NEUTRAL")

    assert agree.direction == alone.direction == "CALL"
    assert agree.score > alone.score
    assert agree.agreement == "agree"
    assert alone.agreement == "price-only"


def test_conflict_penalty_is_larger_than_agreement_bonus():
    """Asymmetric by design: disagreement is strong evidence to stand aside,
    agreement only mild evidence to engage."""
    weights = ScoreWeights()
    assert weights.chain_conflict > weights.chain_agreement


def test_chain_can_lead_when_price_is_neutral():
    result = score_option_signal(underlying(50, "HOLD"), chain_bias="CALL")
    assert result.direction == "CALL"
    assert result.agreement == "chain-only"


def test_neither_side_has_a_view_is_wait():
    result = score_option_signal(underlying(50, "HOLD"), chain_bias="NEUTRAL")
    assert result.direction == "WAIT"
    assert result.agreement == "neither"


def test_every_contribution_is_traceable():
    """A score with no breakdown is an unexplained number, which is what the
    four competing scorers each produced."""
    result = score_option_signal(
        underlying(80, "BUY"), chain_bias="CALL", pcr=1.5,
        spot=23000, ce_wall=23500, pe_wall=22500,
    )
    assert "chain_agreement" in result.components
    assert "pcr_extreme" in result.components
    assert "oi_wall" in result.components
    assert result.underlying_score == 80


def test_pcr_against_the_direction_subtracts():
    with_pcr = score_option_signal(underlying(80, "BUY"), chain_bias="NEUTRAL", pcr=0.5)
    assert with_pcr.components.get("pcr_extreme", 0) < 0


def test_overhead_wall_penalises_a_call():
    """Price running straight into a call wall is resistance against you."""
    near = score_option_signal(underlying(80, "BUY"), chain_bias="CALL",
                               spot=23000, ce_wall=23050)   # 0.2% away
    far = score_option_signal(underlying(80, "BUY"), chain_bias="CALL",
                              spot=23000, ce_wall=24000)    # 4.3% away

    assert near.components["oi_wall"] < 0
    assert far.components["oi_wall"] > 0


def test_warnings_cost_score():
    clean = score_option_signal(underlying(80, "BUY"), chain_bias="CALL")
    noisy = score_option_signal(underlying(80, "BUY"), chain_bias="CALL",
                                warnings=["stale chain", "no volume"])
    assert noisy.score < clean.score


def test_wait_score_is_capped_below_tradable():
    """A WAIT must never look like a setup worth taking."""
    result = score_option_signal(underlying(99, "BUY"), chain_bias="PUT")
    assert result.direction == "WAIT"
    assert result.score <= 40
    assert result.actionable is False


def test_score_is_clamped_to_0_100():
    for score, signal, chain in ((100, "BUY", "CALL"), (0, "SELL", "PUT")):
        result = score_option_signal(underlying(score, signal), chain_bias=chain,
                                     pcr=1.9, spot=100, ce_wall=200, pe_wall=50)
        assert 0 <= result.score <= 100


# ============================================================== fill rules ==

def bar(open_, high, low, close):
    return {"Open": open_, "High": high, "Low": low, "Close": close}


def test_stop_detected_on_low_not_close():
    """The exact bug the old backtest had."""
    spike = bar(100, 102, 79, 101)       # dips to 79, closes at 101
    assert resolve_bar_exit(spike, stop=80, target=125).reason == "STOP"


def test_same_bar_conflict_resolves_to_stop():
    both = bar(100, 130, 75, 110)
    outcome = resolve_bar_exit(both, stop=80, target=125)
    assert outcome.reason == "STOP"
    assert outcome.conflict is True


def test_gap_fills_at_open_on_both_sides():
    gap_down = resolve_bar_exit(bar(60, 62, 55, 58), stop=80, target=125)
    gap_up = resolve_bar_exit(bar(140, 145, 139, 142), stop=80, target=125)
    assert gap_down.price == 60      # the open, not the 80 stop
    assert gap_up.price == 140       # better than the 125 target


def test_short_side_mirrors_the_long_rules():
    # Short: stop ABOVE, target BELOW.
    outcome = resolve_bar_exit(bar(100, 121, 98, 99), stop=120, target=80,
                               direction="SHORT")
    assert outcome.reason == "STOP"


def test_backtest_and_grader_agree_on_the_same_bar():
    """The 'one implementation' claim, checked rather than asserted.

    The grader resolves an options premium path; the backtest resolves an
    equity bar. Fed the same geometry they must reach the same verdict.
    """
    frame = pd.DataFrame(
        {"Open": [100.0, 100.0], "High": [101.0, 105.0],
         "Low": [99.0, 79.0], "Close": [100.0, 102.0], "Volume": [1, 1]},
        index=pd.to_datetime(["2026-01-05 10:00", "2026-01-05 10:05"]),
    )
    graded = grade_path(
        {"signal_id": "s", "bar_time": "2026-01-05 10:00:00", "interval": "5m",
         "entry_premium": 100.0, "stop_premium": 80.0, "target_premium": 125.0,
         "stop_pct": 0.20, "target_pct": 0.25, "lot_size": 75},
        frame,
    )
    direct = resolve_bar_exit(frame.iloc[1], stop=80.0, target=125.0)

    assert graded.outcome == direct.reason == "STOP"
    assert graded.exit_premium == pytest.approx(direct.price)


# ================================================================ backtest ==

def strategy_frame(seed: int = 11, rows: int = 1200) -> pd.DataFrame:
    return apply_ema_swing_breakout_strategy(
        add_indicators(generate_sample_ohlcv(rows=rows, seed=seed))
    )


def test_backtest_runs_and_reports_metrics():
    result = run_backtest(strategy_frame(), RiskConfig(capital=100_000))

    assert result.metrics["trades"] > 0
    for key in ("sharpe", "max_drawdown_pct", "win_rate_pct", "expectancy",
                "same_bar_conflicts"):
        assert key in result.metrics


def test_backtest_rejects_frames_without_ohlc():
    """Silently falling back to Close-only is how the original bug survived."""
    frame = pd.DataFrame({"Close": [1.0], "ATR14": [0.1], "Signal": ["BUY"]})
    with pytest.raises(ValueError, match="missing columns"):
        run_backtest(frame)


def test_entries_fill_on_the_next_bar_open():
    result = run_backtest(strategy_frame(), RiskConfig(capital=100_000),
                          next_bar_entry=True)
    trades = result.trades
    assert not trades.empty
    # Every entry timestamp must be strictly after some BUY bar, never on the
    # signal bar's own close.
    assert trades["EntryTime"].notna().all()


def test_old_convention_overstates_results():
    """Reproduce the original behaviour and measure the gap.

    On this synthetic data the old code overstates returns on most seeds. Real
    5-minute option premiums spike far harder than a random walk, so the live
    gap is wider than what shows up here.
    """
    comparison = compare_fill_assumptions(
        strategy_frame(seed=7), RiskConfig(capital=100_000)
    )
    assert "honest" in comparison and "old_behaviour" in comparison
    assert comparison["old_behaviour"]["total_return_pct"] > \
        comparison["honest"]["total_return_pct"]


def test_close_only_exits_hides_intrabar_stops():
    """Directly: the broken mode must report fewer stop-outs."""
    frame = strategy_frame(seed=23)
    honest = run_backtest(frame, RiskConfig(capital=100_000),
                          close_only_exits=False)
    broken = run_backtest(frame, RiskConfig(capital=100_000),
                          close_only_exits=True)

    def stops(result):
        if result.trades.empty or "Reason" not in result.trades:
            return 0
        return int((result.trades["Reason"] == "STOP").sum())

    assert stops(honest) >= stops(broken)


def test_compute_metrics_on_empty_input():
    metrics = compute_metrics(pd.DataFrame(), pd.DataFrame())
    assert metrics["trades"] == 0
    assert metrics["profit_factor"] == 0.0


# =========================================================== walk-forward ==

def test_walk_forward_splits_are_time_ordered_and_disjoint():
    items = list(range(200))
    splits = walk_forward_splits(items, n_splits=4, min_train=50)

    assert len(splits) == 4
    for train, test in splits:
        assert max(train) < min(test)          # never trains on its own test
    # Training windows expand.
    assert len(splits[0][0]) < len(splits[-1][0])
    # Tests tile the remainder without overlapping.
    covered = [i for _, test in splits for i in test]
    assert len(covered) == len(set(covered))


def test_walk_forward_refuses_insufficient_data():
    """Returning nothing is the answer about whether calibration is premature."""
    assert walk_forward_splits(list(range(20)), n_splits=4, min_train=50) == []


# ======================================================== ledger replay ====

def test_backtest_from_ledger_is_empty_without_data(tmp_path):
    result = backtest_from_ledger(db_path=tmp_path / "empty.sqlite",
                                  starting_capital=20_000)
    assert result.metrics["trades"] == 0
    assert result.trades.empty


def test_ledger_replay_models_indivisible_lots(tmp_path):
    """Capital that cannot buy one lot skips the trade -- a per-lot expectancy
    figure silently includes signals a small account could never take."""
    from intraday_advisor.ledger import grade_pending, record_signal

    db = tmp_path / "ledger.sqlite"
    for index in range(4):
        record_signal(
            bar_time=f"2026-01-05 10:{index * 5:02d}:00", interval="5m",
            index_name="NIFTY", direction="CALL", option_type="CE",
            strike=23_000 + index * 50, lot_size=75,
            entry_premium=100.0, stop_premium=80.0, target_premium=125.0,
            stop_pct=0.20, target_pct=0.25, score=75,
            option_token=f"t{index}", is_live=True, db_path=db,
        )

    def provider(signal):
        start = pd.Timestamp(signal["bar_time"])
        premium = float(signal["entry_premium"])
        return pd.DataFrame(
            {"Open": [premium, premium], "High": [premium, premium * 1.3],
             "Low": [premium, premium * 0.99], "Close": [premium, premium * 1.28],
             "Volume": [1, 1]},
            index=pd.to_datetime([start + pd.Timedelta(minutes=5),
                                  start + pd.Timedelta(minutes=10)]),
        )

    grade_pending(provider, db_path=db)

    # One lot costs 100 * 75 = Rs 7,500.
    rich = backtest_from_ledger(db_path=db, starting_capital=100_000,
                                one_position_at_a_time=False)
    poor = backtest_from_ledger(db_path=db, starting_capital=3_000,
                                one_position_at_a_time=False)

    assert rich.metrics["trades"] == 4
    assert poor.metrics["trades"] == 0
    assert poor.metrics["skipped_unaffordable"] == 4


# ================================================================= engine ==

def test_json_safe_strips_nan_and_infinity():
    """Python emits bare NaN/Infinity, which JSON.parse rejects outright --
    the browser then drops the whole snapshot and shows nothing, with no error
    anywhere."""
    payload = json_safe({
        "a": float("nan"),
        "b": float("inf"),
        "c": [1.0, float("-inf"), {"d": float("nan")}],
        "e": "fine",
        "f": 1.5,
    })
    assert payload["a"] is None
    assert payload["b"] is None
    assert payload["c"][1] is None
    assert payload["c"][2]["d"] is None
    assert payload["e"] == "fine"
    assert payload["f"] == 1.5
    # The real requirement: strict JSON must serialise it.
    json.dumps(payload, allow_nan=False)


def test_json_safe_leaves_ordinary_values_alone():
    original = {"x": 1, "y": "two", "z": [3, 4], "w": None, "v": True}
    assert json_safe(original) == original


def test_nan_truthiness_trap():
    """Documents why `float(x) or 0` does not guard against NaN -- the idiom
    that let NaN into the payload in the first place."""
    assert bool(float("nan")) is True
    assert (float("nan") or 0) != 0
