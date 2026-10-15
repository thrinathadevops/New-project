"""Signal ledger: record every signal, grade what happened, measure the edge.

The feedback loop the system was missing. Before this package, a signal was
generated, displayed, and forgotten -- so no data existed that could answer
"does a score of 78 make money?"

Daily use
---------
During the session, each surfaced signal is recorded::

    from intraday_advisor.ledger import record_index_signal, plan_position

    plan = plan_position(
        available_capital=5_000,
        entry_premium=signal.entry_premium,
        stop_premium=signal.stop_premium,
        target_premium=signal.target_premium,
        lot_size=75,
    )
    record_index_signal(signal, bar_time=str(closed.index[-1]), plan=plan,
                        available_capital=5_000)

After the close, grade the day and read the result::

    from intraday_advisor.ledger import grade_session, summary

    grade_session()          # pulls that day's option candles from Angel One
    print(summary())

Grading after the close rather than live is deliberate: the premium path is
only final once the session ends, and grading against unfinished bars is how
look-ahead gets in.

What to expect
--------------
The first useful reading is roughly 30 graded trades; weight calibration needs
200+ across 20+ sessions. :func:`verdict` will keep saying "not enough data"
until that is genuinely untrue, and :func:`can_calibrate` will refuse to fit
weights before then.
"""

from intraday_advisor.ledger.costs import (
    OptionsCostModel,
    breakeven_premium,
    calibrate_from_fill,
    cost_drag_pct,
    describe_costs,
    leg_costs,
    net_pnl,
    round_trip_cost,
)
from intraday_advisor.ledger.grader import (
    GradedOutcome,
    angel_bars_provider,
    grade_path,
    grade_pending,
    grade_session,
    pending_signals,
    save_outcome,
)
from intraday_advisor.ledger.recorder import (
    extract_features,
    fitted_cost_model,
    record_cost_fill,
    record_index_signal,
    record_signal,
)
from intraday_advisor.ledger.report import (
    by_dimension,
    can_calibrate,
    data_quality,
    expectancy_report,
    load_graded,
    score_band_report,
    score_rank_correlation,
    summary,
    verdict,
    wilson_interval,
)
from intraday_advisor.ledger.schema import (
    DEFAULT_LEDGER_PATH,
    ENGINE_VERSION,
    GRADER_VERSION,
    init_ledger,
    ledger_stats,
)
from intraday_advisor.ledger.sizing import (
    LOT_SIZES,
    PositionPlan,
    describe_plan,
    lot_size_for,
    plan_position,
)

__all__ = [
    # costs
    "OptionsCostModel", "breakeven_premium", "calibrate_from_fill", "cost_drag_pct",
    "describe_costs", "leg_costs", "net_pnl", "round_trip_cost",
    # sizing
    "LOT_SIZES", "PositionPlan", "describe_plan", "lot_size_for", "plan_position",
    # recording
    "extract_features", "fitted_cost_model", "record_cost_fill",
    "record_index_signal", "record_signal",
    # grading
    "GradedOutcome", "angel_bars_provider", "grade_path", "grade_pending",
    "grade_session", "pending_signals", "save_outcome",
    # reporting
    "by_dimension", "can_calibrate", "data_quality", "expectancy_report",
    "load_graded", "score_band_report", "score_rank_correlation", "summary",
    "verdict", "wilson_interval",
    # schema
    "DEFAULT_LEDGER_PATH", "ENGINE_VERSION", "GRADER_VERSION",
    "init_ledger", "ledger_stats",
]
