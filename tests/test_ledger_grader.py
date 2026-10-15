"""Tests for signal grading.

Each test here pins down one rule that costs measured performance versus the
naive alternative. Together they are the reason a hit rate from this ledger can
be believed. The headline one is
``test_intrabar_stop_is_detected_even_when_close_recovers``: that exact bug
lives in ``backtest.py`` today and silently deletes every intrabar stop-out.
"""

from __future__ import annotations

from datetime import time

import pandas as pd
import pytest

from intraday_advisor.ledger.grader import (
    NO_DATA,
    NOT_FILLED,
    STOP,
    TARGET,
    TIMEOUT,
    grade_path,
)

LOT = 75


def bars(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    """Build an OHLC frame from (timestamp, open, high, low, close) tuples."""
    index = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [1000] * len(rows),
        },
        index=index,
    )


def signal(**overrides) -> dict:
    base = {
        "signal_id": "sig-1",
        "bar_time": "2026-01-05 10:00:00",
        "interval": "5m",
        "entry_premium": 100.0,
        "stop_premium": 80.0,
        "target_premium": 125.0,
        "stop_pct": 0.20,
        "target_pct": 0.25,
        "lot_size": LOT,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------- rule 1 and 2

def test_signal_bar_itself_is_never_tradable():
    """The close that produced the signal is unobtainable once you know it
    exists. A stop-out on the signal bar must be invisible to the grader."""
    frame = bars([
        ("2026-01-05 10:00", 100, 105, 40, 100),   # would stop instantly
        ("2026-01-05 10:05", 100, 126, 99, 125),   # real bar: hits target
    ])
    result = grade_path(signal(), frame)

    assert result.outcome == TARGET
    assert result.fill_time == "2026-01-05 10:05:00"


def test_fill_is_next_bar_open_not_signal_close():
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 104, 106, 103, 105),  # opens 4 above the quote
        ("2026-01-05 10:10", 105, 131, 104, 130),
    ])
    result = grade_path(signal(), frame)

    assert result.fill_premium == 104.0
    assert result.fill_slippage == 4.0       # the screen was quoting Rs 100


def test_no_bars_after_signal_is_not_filled():
    frame = bars([("2026-01-05 10:00", 100, 101, 99, 100)])
    result = grade_path(signal(), frame)

    assert result.outcome == NOT_FILLED
    assert result.net_pnl_per_lot is None


# ------------------------------------------------------------------- rule 3

def test_intrabar_stop_is_detected_even_when_close_recovers():
    """THE bug this module exists to prevent.

    The bar's Low pierces the stop, then it closes well above it. A grader that
    compares only Close -- as ``backtest.py`` does -- records no loss, and the
    equity curve is systematically too good.
    """
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 105, 79, 102),   # Low 79 < stop 80, Close 102
        ("2026-01-05 10:10", 102, 130, 101, 128),  # would have reached target
    ])
    result = grade_path(signal(), frame)

    assert result.outcome == STOP
    assert result.net_pnl_per_lot < 0
    assert result.bars_held == 1


def test_intrabar_target_is_detected_even_when_close_falls_back():
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 126, 98, 101),   # High 126 > target 125
    ])
    result = grade_path(signal(), frame)

    assert result.outcome == TARGET
    assert result.net_pnl_per_lot > 0


# ------------------------------------------------------------------- rule 4

def test_same_bar_stop_and_target_resolves_as_stop():
    """Bar data cannot say which came first. Assuming the target is a free
    win-rate boost that evaporates live."""
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 130, 75, 110),   # both levels touched
    ])
    result = grade_path(signal(), frame)

    assert result.outcome == STOP
    assert result.same_bar_conflict == 1
    assert "pessimistically" in result.exit_reason
    assert result.net_pnl_per_lot < 0


# ------------------------------------------------------------------- rule 5

def test_gap_through_stop_fills_at_the_open_not_the_stop():
    """A bar opening below the stop fills at the open. Pretending you got the
    stop price understates every gap loss."""
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 102, 95, 98),    # fill at 100, stop = 80
        ("2026-01-05 10:10", 60, 62, 55, 58),      # gaps below the stop
    ])
    result = grade_path(signal(), frame)

    assert result.outcome == STOP
    assert result.exit_premium == 60.0             # the open, not 80
    # Loss is materially worse than the intended 20% risk.
    assert result.net_pnl_per_lot < -0.30 * 100 * LOT


def test_gap_through_target_fills_better_than_the_target():
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 102, 98, 101),
        ("2026-01-05 10:10", 140, 145, 139, 142),  # gaps above target 125
    ])
    result = grade_path(signal(), frame)

    assert result.outcome == TARGET
    assert result.exit_premium == 140.0


# ------------------------------------------------------------------- rule 6

def test_target_hit_on_a_lottery_ticket_is_still_a_loss():
    """A Rs 2 far-OTM weekly is the contract retail buys most and understands
    least. Round-trip cost there is ~37% of the position, so a +25% target --
    a win by any screen's reckoning -- is a net loss. ``is_win`` must follow
    net P&L, never the outcome label.
    """
    lottery = signal(entry_premium=2.0, stop_premium=1.6, target_premium=2.5,
                     stop_pct=0.20, target_pct=0.25)
    frame = bars([
        ("2026-01-05 10:00", 2.0, 2.1, 1.9, 2.0),
        ("2026-01-05 10:05", 2.0, 2.6, 1.95, 2.55),  # target comfortably touched
    ])
    result = grade_path(lottery, frame)

    assert result.outcome == TARGET
    assert result.gross_pnl_per_lot > 0
    assert result.net_pnl_per_lot < 0       # costs ate more than the whole move
    assert result.is_win is False           # is_win follows net, not outcome


def test_same_move_on_a_richer_contract_is_a_real_win():
    """The contrast that makes the previous test actionable: identical +25%
    structure, 50x the premium, comfortably profitable."""
    rich = signal(entry_premium=100.0, stop_premium=80.0, target_premium=125.0)
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 126, 99, 125),
    ])
    result = grade_path(rich, frame)

    assert result.outcome == TARGET
    assert result.net_pnl_per_lot > 0
    assert result.is_win is True


def test_costs_are_recorded_separately_from_gross():
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 126, 99, 125),
    ])
    result = grade_path(signal(), frame)

    assert result.cost_per_lot > 0
    assert result.net_pnl_per_lot == pytest.approx(
        result.gross_pnl_per_lot - result.cost_per_lot, abs=0.02
    )


# ------------------------------------------------------------------- rule 7

def test_unresolved_position_times_out_at_cutoff():
    """An open position is not a win. It exits at the cutoff and is reported as
    its own category."""
    frame = bars([
        ("2026-01-05 15:00", 100, 101, 99, 100),
        ("2026-01-05 15:05", 100, 104, 98, 103),
        ("2026-01-05 15:10", 103, 105, 102, 104),
        ("2026-01-05 15:20", 104, 106, 103, 105),  # past the 15:15 cutoff
    ])
    result = grade_path(signal(bar_time="2026-01-05 15:00:00"), frame,
                        cutoff=time(15, 15))

    assert result.outcome == TIMEOUT
    assert result.exit_time == "2026-01-05 15:10:00"
    assert result.exit_premium == 104.0


def test_cutoff_can_be_disabled_for_positional_grading():
    frame = bars([
        ("2026-01-05 15:00", 100, 101, 99, 100),
        ("2026-01-05 15:20", 100, 126, 99, 125),   # after cutoff
    ])
    result = grade_path(signal(bar_time="2026-01-05 15:00:00"), frame, cutoff=None)

    assert result.outcome == TARGET


# ------------------------------------------------------------------- rule 8

def test_missing_bars_are_an_outcome_not_a_deletion():
    result = grade_path(signal(), pd.DataFrame())
    assert result.outcome == NO_DATA
    assert result.is_resolved is False


def test_missing_ohlc_columns_are_reported():
    frame = pd.DataFrame(
        {"Close": [100.0]}, index=pd.to_datetime(["2026-01-05 10:05"])
    )
    result = grade_path(signal(), frame)

    assert result.outcome == NO_DATA
    assert "missing columns" in result.notes


def test_unparseable_bar_time_is_reported():
    frame = bars([("2026-01-05 10:05", 100, 126, 99, 125)])
    result = grade_path(signal(bar_time="not a timestamp"), frame)

    assert result.outcome == NO_DATA
    assert "bar_time" in result.notes


def test_zero_lot_size_is_rejected():
    frame = bars([("2026-01-05 10:05", 100, 126, 99, 125)])
    assert grade_path(signal(lot_size=0), frame).outcome == NO_DATA


# ------------------------------------------------------- excursions and R

def test_excursions_track_best_and_worst_before_resolution():
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 118, 90, 110),   # +18% / -10%, unresolved
        ("2026-01-05 10:10", 110, 112, 79, 85),    # stops out
    ])
    result = grade_path(signal(), frame)

    assert result.outcome == STOP
    assert result.mfe_premium == 118.0
    assert result.mae_premium == 79.0
    assert result.mfe_pct == pytest.approx(0.18, abs=0.001)


def test_r_multiple_is_relative_to_intended_risk():
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 126, 99, 125),
    ])
    result = grade_path(signal(), frame)

    # Intended risk = 20% of a Rs 100 fill; target = +25%, so gross R is 1.25.
    # Net of costs it lands slightly below that.
    assert 1.0 < result.r_multiple < 1.25


def test_stop_out_is_close_to_minus_one_r():
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 102, 79, 81),
    ])
    result = grade_path(signal(), frame)

    assert result.outcome == STOP
    assert -1.2 < result.r_multiple < -1.0    # worse than -1R once costs land


# --------------------------------------------------------- timezone handling

def test_timezone_aware_bars_are_handled():
    """Angel One and Yahoo disagree about tz-awareness; comparing a naive
    bar_time against an aware index raises in pandas."""
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 126, 99, 125),
    ])
    frame.index = frame.index.tz_localize("Asia/Kolkata")
    result = grade_path(signal(), frame)

    assert result.outcome == TARGET


def test_unsorted_bars_are_sorted_before_grading():
    frame = bars([
        ("2026-01-05 10:10", 102, 130, 101, 128),
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 100, 105, 79, 102),   # the stop, chronologically first
    ])
    result = grade_path(signal(), frame)

    assert result.outcome == STOP


# ------------------------------------------- stop re-derivation from the fill

def test_stop_is_rederived_from_the_actual_fill():
    """The live rule in position_monitor.py is "-20% of what I paid", so the
    graded stop must follow the fill, not the stale quote. ``fill_slippage``
    keeps the entry degradation visible instead of hiding it in the stop.
    """
    frame = bars([
        ("2026-01-05 10:00", 100, 101, 99, 100),
        ("2026-01-05 10:05", 50, 52, 41, 45),   # fill at 50 => stop at 40
    ])
    result = grade_path(signal(), frame)

    # Low of 41 is above the re-derived stop of 40, so this is not a stop-out.
    assert result.outcome != STOP
    assert result.fill_premium == 50.0
    assert result.fill_slippage == -50.0    # entry was far better than quoted
