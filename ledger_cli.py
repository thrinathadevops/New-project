"""Command line for the signal ledger.

The daily loop:

    python ledger_cli.py record            # during the session, repeatedly
    python ledger_cli.py grade             # once, after 15:30
    python ledger_cli.py report            # read the verdict

Supporting tools:

    python ledger_cli.py demo              # end-to-end proof on synthetic data
    python ledger_cli.py costs 100         # cost drag for a Rs 100 premium
    python ledger_cli.py plan --capital 5000 --premium 100 --index NIFTY
    python ledger_cli.py fill --qty 75 --entry 100 --exit 110 --cost 190
    python ledger_cli.py bands             # performance by score band

``record`` is safe to run on a cron every few minutes: the ledger dedupes by
(bar, contract), so repeated runs inside one candle store exactly one row.
"""

from __future__ import annotations

import argparse
import sys

from intraday_advisor.envfile import load_env
from intraday_advisor.ledger import (
    DEFAULT_LEDGER_PATH,
    by_dimension,
    can_calibrate,
    describe_costs,
    describe_plan,
    expectancy_report,
    grade_session,
    init_ledger,
    ledger_stats,
    lot_size_for,
    plan_position,
    record_cost_fill,
    record_index_signal,
    score_band_report,
    summary,
)
from intraday_advisor.ledger.costs import breakeven_premium, cost_drag_pct


def cmd_record(args) -> int:
    """Scan the indices once and record any directional signal."""
    from intraday_advisor.index_options import scan_indices
    from intraday_advisor.ledger.sizing import lot_size_for as lots_for
    from intraday_advisor.tick_stream import resolve_option_token

    signals = scan_indices()
    recorded, skipped = 0, 0

    for signal in signals:
        if signal.direction not in ("CALL", "PUT"):
            print(f"  {signal.index:<10} WAIT  (score {signal.confidence}) - nothing to record")
            skipped += 1
            continue
        if not signal.bar_time:
            print(f"  {signal.index:<10} SKIP  - no closed candle; cannot grade without a bar time")
            skipped += 1
            continue

        try:
            lot_size = lots_for(signal.index)
        except ValueError:
            lot_size = 0
        if lot_size <= 0:
            print(f"  {signal.index:<10} SKIP  - unknown lot size")
            skipped += 1
            continue

        plan = plan_position(
            available_capital=args.capital,
            entry_premium=signal.entry_premium,
            stop_premium=signal.stop_premium,
            target_premium=signal.target_premium,
            lot_size=lot_size,
            max_capital_fraction=args.max_fraction,
        )

        # Without an NFO token the grader cannot read this contract's premium
        # path, so the signal would land as NO_DATA. Resolve it now, at record
        # time, while the expiry is unambiguous.
        token = resolve_option_token(signal.index, signal.strike, signal.option_type)
        if token is None:
            print(f"  {signal.index:<10} WARN  - no option token; will grade as NO_DATA")

        signal_id = record_index_signal(
            signal,
            bar_time=signal.bar_time,
            lot_size=lot_size,
            option_token=token,
            plan=plan,
            available_capital=args.capital,
            is_synthetic=not signal.is_live,
            db_path=args.db,
        )
        if signal_id:
            recorded += 1
            flag = "" if signal.is_live else "  [SYNTHETIC - excluded from stats]"
            print(f"  {signal.index:<10} {signal.direction:<5} "
                  f"{signal.strike:g}{signal.option_type} @ Rs {signal.entry_premium:.2f}  "
                  f"score {signal.confidence}  risk {plan.risk_pct_of_capital * 100:.1f}%"
                  f"  [{plan.risk_band}]{flag}")
            for warning in plan.warnings:
                print(f"      ! {warning}")
        else:
            print(f"  {signal.index:<10} dup   - already recorded for bar {signal.bar_time}")
            skipped += 1

    print(f"\nRecorded {recorded}, skipped {skipped}.")
    return 0


def cmd_grade(args) -> int:
    """Grade a session's signals against real option premium paths."""
    print(f"Grading session {args.date or 'today'} ...")
    try:
        result = grade_session(args.date, db_path=args.db, regrade=args.regrade)
    except Exception as exc:  # noqa: BLE001
        print(f"Grading failed: {exc}", file=sys.stderr)
        print("Angel One credentials and a live session are required to fetch "
              "option candles.", file=sys.stderr)
        return 1

    print(f"  graded {result['graded']} of {result['pending']} pending")
    for outcome, count in sorted(result["by_outcome"].items()):
        print(f"    {outcome:<20} {count}")
    return 0


def cmd_report(args) -> int:
    print(summary(db_path=args.db))
    gate = can_calibrate(db_path=args.db)
    print(f"\nCalibration: {gate['reason']}")
    return 0


def cmd_coverage(args) -> int:
    """Can the hours legitimately be compared, or is each a different day?"""
    from intraday_advisor.sessions import hour_coverage, render_coverage

    print(render_coverage(hour_coverage(db_path=args.db)))
    return 0


def cmd_shape(args) -> int:
    """What kind of day was it: regime, volatility, breadth, conviction."""
    from intraday_advisor.observe import compare_sessions, session_shape
    import json

    frame = compare_sessions(db_path=args.db)
    if frame is not None and not frame.empty:
        print("SESSION SHAPE (from market samples, one row per day)")
        print(frame.to_string())
        print()
    print(json.dumps(session_shape(getattr(args, "date", None),
                                   db_path=args.db), indent=1, default=str))
    return 0


def cmd_sessions(args) -> int:
    """Session-by-session, because the spread between days exceeds the effect."""
    from intraday_advisor.sessions import render, session_series

    print(render(session_series(db_path=args.db)))
    return 0


def cmd_bands(args) -> int:
    """Performance by score band: does a higher score mean a better trade?"""
    bands = score_band_report(db_path=args.db)
    if bands.empty:
        print("No graded trades yet.")
        return 0
    print(bands.to_string(index=False))
    print("\nIf win rate and expectancy do not rise with the band, the score is")
    print("not ranking trades and a 'confidence 85' deserves no more capital")
    print("than a 'confidence 60'.")

    for dimension in ("index_name", "option_type", "hour"):
        try:
            frame = by_dimension(dimension, db_path=args.db)
        except ValueError:
            continue
        if not frame.empty:
            print(f"\nBy {dimension}:")
            print(frame.to_string(index=False))
    return 0


def cmd_costs(args) -> int:
    """What a round trip costs at a given premium."""
    lot = args.lot or lot_size_for(args.index)
    print(describe_costs(args.premium, args.premium * (1 + args.target), lot))
    print()
    drag = cost_drag_pct(args.premium, lot)
    be = breakeven_premium(args.premium, lot)
    print(f"Cost drag        {drag * 100:.2f}% of position value")
    print(f"Breakeven        Rs {be:.2f}  (+{(be / args.premium - 1) * 100:.2f}% move "
          f"just to stand still)")
    if drag > 0.03:
        print("\nThis premium is too cheap for flat Rs 20-per-order brokerage.")
        print("Prefer a richer contract or more lots per order.")
    return 0


def cmd_plan(args) -> int:
    """Size a position against the capital actually available."""
    lot = args.lot or lot_size_for(args.index)
    stop = args.stop if args.stop else args.premium * (1 - args.stop_pct)
    target = args.target if args.target else args.premium * (1 + args.target_pct)
    plan = plan_position(
        available_capital=args.capital,
        entry_premium=args.premium,
        stop_premium=stop,
        target_premium=target,
        lot_size=lot,
        max_capital_fraction=args.max_fraction,
    )
    print(describe_plan(plan))
    return 0


def cmd_archive(args) -> int:
    """Store today's candles, build any outstanding daily record, show coverage."""
    from intraday_advisor.archive import (
        archive_due,
        bar_coverage,
        render_coverage,
    )

    summary = archive_due(args.db, with_bars=not args.no_bars)
    written = sum((summary.get("bars") or {}).values())
    print(f"bars written      : {written:,}")
    print(f"records built     : {summary.get('recorded') or 'none'}")
    for deferred in summary.get("deferred", []):
        print(f"  deferred {deferred['session']}: {deferred['reason']}")
    for error in summary.get("errors", []):
        print(f"  ERROR {error}")
    print()
    print(render_coverage(bar_coverage(db_path=args.db)))
    return 0


def cmd_agent(args) -> int:
    """Would the vetoed trades have made money?"""
    from intraday_advisor.calibration import agent_scorecard, render_agent

    print(render_agent(agent_scorecard(db_path=args.db)))
    return 0


def cmd_spreads(args) -> int:
    """Measured half-spread by hour against the published prior."""
    from intraday_advisor.calibration import render_spreads, spread_shape

    print(render_spreads(spread_shape(db_path=args.db)))
    return 0


def cmd_calibration(args) -> int:
    """Is conviction a probability, and do stated odds happen?"""
    from intraday_advisor.calibration import (
        conviction_calibration,
        odds_reliability,
        render_conviction,
        render_reliability,
        volatility_history,
    )

    print(render_conviction(conviction_calibration(db_path=args.db)))
    print()
    print(render_reliability(odds_reliability(db_path=args.db)))
    print()
    history = volatility_history(db_path=args.db)
    print("=" * 68)
    print(f"REALISED VOLATILITY HISTORY  ({history.get('symbol')})")
    print("=" * 68)
    print(f"  sessions : {history.get('sessions', 0)}")
    if history.get("forecast"):
        forecast = history["forecast"]
        print(f"  HAR-RV next-day forecast : {forecast}")
    if history.get("note"):
        print(f"    - {history['note']}")
    for note in history.get("notes", []):
        print(f"    - {note}")
    print("=" * 68)
    return 0


def cmd_day(args) -> int:
    """One day's consolidated record, or the last few side by side."""
    import json as _json

    from intraday_advisor.archive import daily_records

    rows = daily_records(db_path=args.db, limit=args.limit)
    if not rows:
        print("No daily records yet. Run `ledger_cli.py archive` after a session.")
        return 0
    if args.session:
        rows = [r for r in rows if r["session_date"] == args.session]
        if not rows:
            print(f"No record for {args.session}.")
            return 0
        print(_json.dumps(rows[0], indent=2, default=str))
        return 0

    print("=" * 94)
    print("DAILY RECORDS  (one row per session — the table days are compared across)")
    print("=" * 94)
    print(f"  {'session':12} {'sig':>5} {'live':>5} {'graded':>7} "
          f"{'P&L/lot':>9} {'conv':>6} {'vix':>6} {'r.vol':>7} {'move%':>7} {'bars':>6}")
    for row in rows:
        def show(key, fmt="{:>6}"):
            value = row.get(key)
            return fmt.format("-" if value is None else value)
        print(f"  {row['session_date']:12} {row.get('signals_total') or 0:5} "
              f"{row.get('signals_live') or 0:5} {row.get('graded') or 0:7} "
              f"{show('net_pnl_per_lot', '{:>9}')} {show('mean_conviction')} "
              f"{show('mean_vix')} {show('realised_vol', '{:>7}')} "
              f"{show('spot_move_pct', '{:>7}')} {row.get('bars_archived') or 0:6}")
    print("=" * 94)
    return 0


def cmd_odds(args) -> int:
    """Forward odds on one trade, with its costs charged.

    Answers the question the edge tests cannot: not "does this rule work" but
    "what are the odds on *this* setup, and does that beat what the costs
    demand". Reads the index's own last 60 days, so it needs no broker login.
    """
    from intraday_advisor.data import fetch_yahoo_ohlcv
    from intraday_advisor.forward_odds import assess, history_leg, render
    from intraday_advisor.index_options import INDEX_CONFIG

    index = args.index.upper()
    if index not in INDEX_CONFIG:
        print(f"Unsupported index {index!r}; use one of {list(INDEX_CONFIG)}")
        return 2
    lot = args.lot or lot_size_for(index)

    stop_premium = args.stop or args.premium * (1 - args.stop_pct)
    target_premium = args.target or args.premium * (1 + args.target_pct)

    # Underlying levels. Default them to the same percentage move as the
    # premium's, which is deliberately crude and is stated as such below --
    # an option moves several times its underlying, so a real stop comes from
    # the chart, not from scaling the premium.
    spot = args.spot
    u_stop = args.u_stop
    u_target = args.u_target

    try:
        # Archive first: it accumulates, where Yahoo's 60-day window slides.
        from intraday_advisor.forward_odds import pool_bars

        bars = pool_bars(INDEX_CONFIG[index]["yahoo"], db_path=args.db,
                         interval="5m", period="60d")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not fetch {index} candles: {exc}")
        return 1
    if bars is None or len(bars) == 0:
        print(f"No candles available for {index}.")
        return 1
    if spot is None:
        spot = float(bars["Close"].iloc[-1])
    if u_stop is None or u_target is None:
        print("Note: --u-stop / --u-target not given, so the underlying levels "
              "were derived from the premium percentages. An option moves "
              "several times its underlying, so pass the real chart levels for "
              "a number worth acting on.")
        print()
        move = args.u_move
        if u_stop is None:
            u_stop = spot * (1 - move)
        if u_target is None:
            u_target = spot * (1 + move)

    result = assess(
        bars, entry_premium=args.premium, stop_premium=stop_premium,
        target_premium=target_premium, quantity=lot * args.lots,
        entry=spot, stop=u_stop, target=u_target,
        bars_left=args.bars_left, replays=args.replays,
    )
    print(render(result))

    if result.odds and result.odds.replays:
        history = history_leg(
            bars, stop_pct=abs(spot - u_stop) / spot,
            target_pct=abs(u_target - spot) / spot,
            bars_left=args.bars_left, long=u_target > spot,
        )
        if history.get("sessions"):
            print()
            print(f"  Same levels on {history['sessions']} REAL sessions: "
                  f"target {history['target_first']}, stop "
                  f"{history['stop_first']}, neither {history['neither']} "
                  f"(hit rate {history['hit_rate']:.1%})")
            print(f"    - {history['note']}")
    return 0


def cmd_fill(args) -> int:
    """Record a real contract-note cost so the model matches your fills."""
    implied = record_cost_fill(
        quantity=args.qty,
        entry_premium=args.entry,
        exit_premium=args.exit,
        actual_total_cost=args.cost,
        index_name=args.index,
        notes=args.notes,
        db_path=args.db,
    )
    print(f"Recorded. Implied half-spread: {implied * 100:.4f}% of premium.")
    print("Three or more fills switch the grader onto your measured costs.")
    return 0


def cmd_demo(args) -> int:
    """End-to-end proof on synthetic data, in a throwaway database.

    Shows the full loop working without needing a broker session or market
    hours. The data is fabricated, so the numbers mean nothing -- the point is
    that record -> grade -> report is wired up.
    """
    import tempfile
    from pathlib import Path

    import pandas as pd

    from intraday_advisor.ledger import grade_pending, record_signal

    db = Path(tempfile.mkdtemp(prefix="ledger-demo-")) / "demo.sqlite"
    init_ledger(db)
    print(f"Demo ledger: {db}\n")

    # A deliberately mixed set: high scores mostly work, low scores mostly do
    # not, plus one cheap contract whose target hit still loses after costs.
    setups = [
        # (score, premium, outcome_high, outcome_low)
        (85, 120.0, 1.30, 0.95), (82, 110.0, 1.28, 0.96), (80, 95.0, 1.27, 0.97),
        (78, 130.0, 1.26, 0.94), (75, 100.0, 1.26, 0.93), (72, 115.0, 0.82, 0.78),
        (68, 90.0, 1.27, 0.95), (65, 105.0, 0.80, 0.76), (62, 85.0, 0.79, 0.75),
        (58, 125.0, 0.81, 0.77), (55, 3.0, 1.30, 0.95), (52, 95.0, 0.80, 0.76),
    ]
    for index, (score, premium, high_mult, low_mult) in enumerate(setups):
        minute = index * 5
        record_signal(
            bar_time=f"2026-01-05 {10 + minute // 60:02d}:{minute % 60:02d}:00",
            interval="5m", index_name="NIFTY", direction="CALL", option_type="CE",
            strike=23_000 + index * 50, lot_size=75,
            entry_premium=premium,
            stop_premium=premium * 0.80, target_premium=premium * 1.25,
            stop_pct=0.20, target_pct=0.25, score=score,
            option_token=f"tok{index}", data_source="demo-live", is_live=True,
            db_path=db,
        )

    def provider(signal):
        """Replay a premium path consistent with that setup's multipliers."""
        premium = float(signal["entry_premium"])
        score = int(signal["score"])
        setup = next(s for s in setups if s[0] == score)
        high_mult, low_mult = setup[2], setup[3]
        start = pd.Timestamp(signal["bar_time"])
        return pd.DataFrame(
            {
                "Open": [premium, premium],
                "High": [premium * 1.01, premium * high_mult],
                "Low": [premium * 0.99, premium * low_mult],
                "Close": [premium, premium * high_mult],
                "Volume": [100, 100],
            },
            index=pd.to_datetime([
                start + pd.Timedelta(minutes=5), start + pd.Timedelta(minutes=10)
            ]),
        )

    result = grade_pending(provider, db_path=db)
    print(f"Recorded {ledger_stats(db)['real_signals']} signals, "
          f"graded {result['graded']}.")
    print(f"Outcomes: {result['by_outcome']}\n")
    print(summary(db_path=db))
    print("\nBy score band:")
    print(score_band_report(db_path=db).to_string(index=False))
    print("\nNOTE: synthetic data. This proves the pipeline runs, nothing more.")
    return 0



def cmd_doctor(args) -> int:
    """Check configuration and say plainly what is and is not working."""
    from intraday_advisor.agents import agent_status
    from intraday_advisor.envfile import describe_config

    report = describe_config()
    print("=" * 66)
    print("CONFIGURATION")
    print("=" * 66)
    print(f"  .env file   {report['env_file']}")
    print(f"  found       {'yes' if report['env_file_found'] else 'NO'}")
    print()

    for name, info in report["settings"].items():
        mark = "OK " if info["set"] else ("!! " if info["critical"] else "-- ")
        detail = f"{info['length']} chars" if info["set"] else "not set"
        print(f"  {mark}{name:24} {detail:<14} {info['label']}")

    if report["problems"]:
        print()
        print("PROBLEMS IN .env")
        print("-" * 66)
        for problem in report["problems"]:
            print(f"  line {problem['line']}: {problem['problem']}")
            print(f"    {problem['text']}")
            print(f"    fix: {problem['fix']}")

    print()
    print("BROKER")
    print("-" * 66)
    print(f"  {report['summary']}")

    status = agent_status()
    print()
    print("AGENT")
    print("-" * 66)
    print(f"  {status['detail']}")
    if status.get("llm_active") and not status.get("llm_ready"):
        print("  Free options:")
        for name, spec in status["presets"].items():
            key = "needs a key" if spec["needs_key"] else "no key needed"
            print(f"    {name:10} ({key}) {spec['note']}")

    if args.test_agent and status.get("llm_ready"):
        print()
        print("  Testing the agent with a synthetic trade ...")
        from intraday_advisor.agents.base import TradeBrief
        from intraday_advisor.agents.llm import LLMAgent
        probe = TradeBrief(
            index="NIFTY", direction="CALL", score=68, contract="23000 CE",
            entry_premium=100.0, target_premium=125.0, stop_premium=80.0,
            lots=1, risk_amount=1500.0, risk_pct_of_capital=0.05, net_rr=1.3,
            breakeven=101.3, cost_drag=0.013, affordable=True,
            available_capital=30000.0, regime="TRENDING_UP", regime_bias="UP",
            regime_conviction=60, vix=14.0, vix_level="normal",
            market_open=True, data_is_live=True, graded_trades=0,
        )
        # "auto" and comma-lists are chains, not a single provider -- building
        # one LLMAgent from the literal string "auto" has no preset and no URL.
        chain = status.get("chain") or [status["provider"]]
        if len(chain) > 1:
            from intraday_advisor.agents.llm import ChainAgent

            reviewer = ChainAgent(chain, timeout=25.0)
        else:
            reviewer = LLMAgent(provider=chain[0], timeout=40.0)

        verdict = reviewer.review(probe)
        state = "FAILED" if verdict.degraded else "working"
        print(f"    {state}: {verdict.model} responded in {verdict.latency_ms}ms "
              f"with {verdict.stance}")
        for support in verdict.supports:
            if "after" in support and "failed" in support:
                print(f"    note: {support[:160]}")
        if verdict.error:
            print(f"    error: {verdict.error[:220]}")
    print("=" * 66)
    return 0 if report["broker_ready"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Signal ledger: record signals, grade outcomes, measure the edge.",
    )
    parser.add_argument("--db", default=str(DEFAULT_LEDGER_PATH),
                        help=f"ledger path (default: {DEFAULT_LEDGER_PATH})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="scan and record current signals")
    record.add_argument("--capital", type=float, required=True,
                        help="capital available right now, in rupees")
    record.add_argument("--max-fraction", type=float, default=1.0,
                        dest="max_fraction",
                        help="max fraction of capital to deploy (default 1.0 = all)")
    record.set_defaults(func=cmd_record)

    grade = subparsers.add_parser("grade", help="grade a session after the close")
    grade.add_argument("--date", help="session date YYYY-MM-DD (default: today)")
    grade.add_argument("--regrade", action="store_true",
                       help="re-grade already-graded signals")
    grade.set_defaults(func=cmd_grade)

    report = subparsers.add_parser("report", help="print the performance summary")
    report.set_defaults(func=cmd_report)

    bands = subparsers.add_parser("bands", help="performance by score band")
    bands.set_defaults(func=cmd_bands)
    sessions = subparsers.add_parser(
        "sessions",
        help="session-by-session: is it an edge, or was one day lucky?",
    )
    sessions.set_defaults(func=cmd_sessions)
    coverage = subparsers.add_parser(
        "coverage", help="are the hours comparable, or is each a different day?")
    coverage.set_defaults(func=cmd_coverage)
    shape = subparsers.add_parser(
        "shape", help="what kind of day was it (regime, vix, breadth, skew)")
    shape.add_argument("--date", default=None, help="session date, default today")
    shape.set_defaults(func=cmd_shape)

    costs = subparsers.add_parser("costs", help="cost drag at a given premium")
    costs.add_argument("premium", type=float)
    costs.add_argument("--lot", type=int, help="lot size (default: from index)")
    costs.add_argument("--index", default="NIFTY")
    costs.add_argument("--target", type=float, default=0.25,
                       help="target move as a fraction (default 0.25)")
    costs.set_defaults(func=cmd_costs)

    archive = subparsers.add_parser(
        "archive", help="store today's candles and build the daily record")
    archive.add_argument("--no-bars", action="store_true",
                         help="build records only, skip the candle fetch")
    archive.set_defaults(func=cmd_archive)

    agent = subparsers.add_parser(
        "agent", help="would the vetoed trades have made money?")
    agent.set_defaults(func=cmd_agent)

    spreads = subparsers.add_parser(
        "spreads", help="measured half-spread by hour vs the prior")
    spreads.set_defaults(func=cmd_spreads)

    calibration = subparsers.add_parser(
        "calibration", help="is conviction a probability? do stated odds happen?")
    calibration.set_defaults(func=cmd_calibration)

    day = subparsers.add_parser("day", help="daily records, side by side")
    day.add_argument("--session", help="one session date, as JSON")
    day.add_argument("--limit", type=int, default=30)
    day.set_defaults(func=cmd_day)

    odds = subparsers.add_parser(
        "odds", help="forward odds on one trade, with costs charged")
    odds.add_argument("--premium", type=float, required=True,
                      help="entry premium of the option")
    odds.add_argument("--index", default="NIFTY")
    odds.add_argument("--lot", type=int, help="lot size (default: from index)")
    odds.add_argument("--lots", type=int, default=1)
    odds.add_argument("--stop", type=float, help="absolute stop premium")
    odds.add_argument("--target", type=float, help="absolute target premium")
    odds.add_argument("--stop-pct", type=float, default=0.20, dest="stop_pct")
    odds.add_argument("--target-pct", type=float, default=0.25, dest="target_pct")
    odds.add_argument("--spot", type=float, help="underlying now (default: last close)")
    odds.add_argument("--u-stop", type=float, dest="u_stop",
                      help="underlying level that invalidates the trade")
    odds.add_argument("--u-target", type=float, dest="u_target",
                      help="underlying level that completes it")
    odds.add_argument("--u-move", type=float, default=0.004,
                      help="fallback underlying move either way (default 0.4%%)")
    odds.add_argument("--bars-left", type=int, default=40, dest="bars_left",
                      help="5-minute bars until square-off (default 40)")
    odds.add_argument("--replays", type=int, default=10_000)
    odds.set_defaults(func=cmd_odds)

    plan = subparsers.add_parser("plan", help="size a position")
    plan.add_argument("--capital", type=float, required=True)
    plan.add_argument("--premium", type=float, required=True)
    plan.add_argument("--index", default="NIFTY")
    plan.add_argument("--lot", type=int)
    plan.add_argument("--stop", type=float, help="absolute stop premium")
    plan.add_argument("--target", type=float, help="absolute target premium")
    plan.add_argument("--stop-pct", type=float, default=0.20, dest="stop_pct")
    plan.add_argument("--target-pct", type=float, default=0.25, dest="target_pct")
    plan.add_argument("--max-fraction", type=float, default=1.0, dest="max_fraction")
    plan.set_defaults(func=cmd_plan)

    fill = subparsers.add_parser("fill", help="record a real contract-note cost")
    fill.add_argument("--qty", type=int, required=True)
    fill.add_argument("--entry", type=float, required=True)
    fill.add_argument("--exit", type=float, required=True)
    fill.add_argument("--cost", type=float, required=True,
                      help="total charges from the contract note")
    fill.add_argument("--index")
    fill.add_argument("--notes")
    fill.set_defaults(func=cmd_fill)

    demo = subparsers.add_parser("demo", help="end-to-end proof on synthetic data")
    demo.set_defaults(func=cmd_demo)

    doctor = subparsers.add_parser(
        "doctor", help="check .env, broker credentials and the agent")
    doctor.add_argument("--test-agent", action="store_true",
                        help="make one real call to the configured LLM agent")
    doctor.set_defaults(func=cmd_doctor)

    load_env()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
