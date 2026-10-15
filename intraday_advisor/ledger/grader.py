"""Decide what actually happened to each recorded signal.

This module is the one that decides whether the whole project is honest, so
every rule below is deliberate and every one of them costs you measured
performance compared to the naive alternative.

The rules, and what each one prevents
-------------------------------------

1. **Only bars strictly after ``bar_time`` are eligible.**
   The signal was computed from the *close* of ``bar_time``. Reading any price
   stamped at or before that moment is look-ahead.

2. **Entry fills at the NEXT bar's OPEN, never at the signal bar's close.**
   The close that produced the signal is unobtainable by the time you know the
   signal exists. ``fill_slippage`` records the gap between the premium the
   dashboard showed and the one actually achievable -- if that number is
   persistently positive, the screen was quoting prices you could never get.

3. **Stops and targets are tested against bar LOW and HIGH, never the close.**
   The existing ``backtest.py`` compares only ``Close`` to the stop, so an
   intrabar spike through the stop that closes back above it records no loss at
   all. Every such stop-out is invisible and the equity curve is systematically
   too good. Options, with their 20% stops on a volatile premium, are the worst
   possible instrument for that bug.

4. **When one bar touches both stop and target, the STOP wins.**
   Bar data cannot say which came first. Assuming the target is a free
   win-rate boost that evaporates in live trading. The pessimistic choice is
   recorded in ``same_bar_conflict`` so you can see how often it mattered.

5. **Gaps fill at the open, not at your level.**
   If a bar opens below the stop, the exit is the open, not the stop -- that is
   what your broker gives you. Symmetrically, a gap through the target fills
   better than the target. Options gap hard at 09:15; pretending otherwise
   understates losses.

6. **Every P&L figure is net of the full options cost model.**
   Gross P&L on a retail options trade is close to meaningless. See
   :mod:`intraday_advisor.ledger.costs`.

7. **Intraday positions are squared off at the cutoff.**
   An ungraded open position is not a win. Unresolved trades exit at the last
   close before the cutoff and are recorded as ``TIMEOUT``, which is its own
   outcome category and must be reported separately -- a strategy whose
   "wins" are mostly timeouts has no edge, it has drift.

8. **Missing data is an outcome, not a deletion.**
   Signals that cannot be graded are stored as ``NO_DATA`` /
   ``INSUFFICIENT_DATA``. Silently dropping them is how a hit rate gets
   flattered by survivorship.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from intraday_advisor.data import drop_forming_candle
from intraday_advisor.fills import resolve_bar_exit
from intraday_advisor.ledger.costs import OptionsCostModel, net_pnl
from intraday_advisor.ledger.recorder import now_ist
from intraday_advisor.ledger.schema import (
    DEFAULT_LEDGER_PATH,
    GRADER_VERSION,
    connect,
    init_ledger,
)

# NSE equity-derivatives intraday square-off. Positions still open at this time
# are closed at market, so this is where grading stops.
DEFAULT_CUTOFF = time(15, 15)

TARGET = "TARGET"
STOP = "STOP"
TIMEOUT = "TIMEOUT"
NOT_FILLED = "NOT_FILLED"
NO_DATA = "NO_DATA"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

RESOLVED = (TARGET, STOP, TIMEOUT)


@dataclass(frozen=True)
class GradedOutcome:
    """The graded result of one signal. Mirrors the ``signal_outcomes`` table."""

    signal_id: str
    outcome: str
    grader_version: str = GRADER_VERSION

    fill_premium: float | None = None
    fill_time: str | None = None
    fill_slippage: float | None = None

    exit_premium: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    bars_held: int | None = None

    mfe_premium: float | None = None
    mae_premium: float | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None

    gross_pnl_per_lot: float | None = None
    cost_per_lot: float | None = None
    net_pnl_per_lot: float | None = None
    net_pnl_pct: float | None = None
    r_multiple: float | None = None

    same_bar_conflict: int = 0
    price_source: str | None = None
    bars_available: int | None = None
    notes: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.outcome in RESOLVED

    @property
    def is_win(self) -> bool:
        """A win is positive NET P&L, not merely 'target touched'.

        A target hit on a cheap contract can still lose money after costs.
        """
        return self.net_pnl_per_lot is not None and self.net_pnl_per_lot > 0

    def as_dict(self) -> dict:
        return asdict(self)


def _to_timestamp(value: Any) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return ts.tz_localize(None) if ts.tzinfo is not None else ts


def _naive_index(df: pd.DataFrame) -> pd.DataFrame:
    """Strip timezone from the index so comparisons cannot silently fail.

    Mixing a tz-aware index with a naive ``bar_time`` raises in pandas; mixing
    two different zones compares wrongly. Everything is normalised to naive
    local time, which is what both Angel One and the stored ``bar_time`` use.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    return out.sort_index()


def grade_path(
    signal: dict,
    premium_bars: pd.DataFrame,
    *,
    costs: OptionsCostModel | None = None,
    cutoff: time | None = DEFAULT_CUTOFF,
    interval: str | None = None,
    price_source: str = "option_candles",
    drop_forming: bool = True,
) -> GradedOutcome:
    """Grade one signal against the option's own premium bars.

    ``premium_bars`` must be OHLC of the **option contract** (not the
    underlying), indexed by bar-open timestamp. ``signal`` is a mapping with at
    least ``signal_id``, ``bar_time``, ``entry_premium``, ``stop_premium``,
    ``target_premium`` and ``lot_size``.
    """
    signal_id = str(signal.get("signal_id", ""))
    quantity = int(signal.get("lot_size") or 0)
    entry_quote = float(signal.get("entry_premium") or 0)
    stop = float(signal.get("stop_premium") or 0)
    target = float(signal.get("target_premium") or 0)

    if quantity <= 0 or entry_quote <= 0:
        return GradedOutcome(signal_id, NO_DATA, price_source=price_source,
                             notes="Signal has no lot size or no entry premium.")

    bars = _naive_index(premium_bars)
    if bars is None or bars.empty:
        return GradedOutcome(signal_id, NO_DATA, price_source=price_source,
                             bars_available=0, notes="No premium bars available.")

    missing = {"Open", "High", "Low", "Close"} - set(bars.columns)
    if missing:
        return GradedOutcome(signal_id, NO_DATA, price_source=price_source,
                             notes=f"Premium bars missing columns: {sorted(missing)}")

    # Never grade against a bar that is still forming.
    if drop_forming and interval:
        bars = drop_forming_candle(bars, interval)
        if bars is None or bars.empty:
            return GradedOutcome(signal_id, NO_DATA, price_source=price_source,
                                 bars_available=0,
                                 notes="All premium bars were still forming.")

    signal_ts = _to_timestamp(signal.get("bar_time"))
    if signal_ts is None:
        return GradedOutcome(signal_id, NO_DATA, price_source=price_source,
                             notes=f"Unparseable bar_time {signal.get('bar_time')!r}.")

    # RULE 1: strictly after the signal bar's open timestamp.
    eligible = bars[bars.index > signal_ts]

    # RULE 7: stop grading at the intraday square-off.
    if cutoff is not None and not eligible.empty:
        session_day = eligible.index[0].normalize()
        deadline = session_day + timedelta(
            hours=cutoff.hour, minutes=cutoff.minute, seconds=cutoff.second
        )
        eligible = eligible[eligible.index <= deadline]

    if eligible.empty:
        return GradedOutcome(
            signal_id, NOT_FILLED, price_source=price_source,
            bars_available=int(len(bars)),
            notes="No bars after the signal bar before cutoff; entry never fillable.",
        )

    # RULE 2: fill at the next bar's open.
    fill_bar = eligible.iloc[0]
    fill_premium = float(fill_bar["Open"])
    fill_time = eligible.index[0]
    if fill_premium <= 0:
        return GradedOutcome(signal_id, NO_DATA, price_source=price_source,
                             bars_available=int(len(bars)),
                             notes="Next-bar open premium was not positive.")

    # Positive => the quote shown was better than the fill actually available.
    fill_slippage = fill_premium - entry_quote

    # Stop and target were derived from the quoted premium. Re-derive them from
    # the achieved fill so the risk/reward graded is the one actually taken on.
    stop_pct = signal.get("stop_pct")
    target_pct = signal.get("target_pct")
    if stop_pct is not None and target_pct is not None and entry_quote > 0:
        eff_stop = fill_premium * (1 - float(stop_pct))
        eff_target = fill_premium * (1 + float(target_pct))
    else:
        eff_stop, eff_target = stop, target

    outcome = TIMEOUT
    exit_premium = float(eligible.iloc[-1]["Close"])
    exit_time = eligible.index[-1]
    exit_reason = "cutoff square-off"
    same_bar_conflict = 0
    bars_held = int(len(eligible))

    mfe = fill_premium
    mae = fill_premium

    for position, (timestamp, bar) in enumerate(eligible.iterrows(), start=1):
        mfe = max(mfe, float(bar["High"]))
        mae = min(mae, float(bar["Low"]))

        # RULES 3, 4 and 5 all live in one shared function, which the
        # historical backtest calls too -- so the two can never disagree.
        resolved = resolve_bar_exit(bar, stop=eff_stop, target=eff_target)
        if resolved.hit:
            outcome = resolved.reason
            exit_premium = float(resolved.price)
            exit_time = timestamp
            bars_held = position
            if resolved.conflict:
                same_bar_conflict = 1
                exit_reason = "stop (same-bar conflict, resolved pessimistically)"
            else:
                exit_reason = "stop hit" if outcome == STOP else "target hit"
            break

    # RULE 6: all P&L net of real costs, for exactly one lot.
    pnl = net_pnl(fill_premium, exit_premium, quantity, costs=costs)
    deployed = fill_premium * quantity
    intended_risk = abs(fill_premium - eff_stop) * quantity

    return GradedOutcome(
        signal_id=signal_id,
        outcome=outcome,
        fill_premium=round(fill_premium, 2),
        fill_time=str(fill_time),
        fill_slippage=round(fill_slippage, 2),
        exit_premium=round(exit_premium, 2),
        exit_time=str(exit_time),
        exit_reason=exit_reason,
        bars_held=bars_held,
        mfe_premium=round(mfe, 2),
        mae_premium=round(mae, 2),
        mfe_pct=round((mfe - fill_premium) / fill_premium, 4),
        mae_pct=round((mae - fill_premium) / fill_premium, 4),
        gross_pnl_per_lot=round(pnl["gross_pnl"], 2),
        cost_per_lot=round(pnl["total_cost"], 2),
        net_pnl_per_lot=round(pnl["net_pnl"], 2),
        net_pnl_pct=round(pnl["net_pnl"] / deployed, 4) if deployed > 0 else None,
        r_multiple=round(pnl["net_pnl"] / intended_risk, 3) if intended_risk > 0 else None,
        same_bar_conflict=same_bar_conflict,
        price_source=price_source,
        bars_available=int(len(bars)),
        notes=None,
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def save_outcome(
    outcome: GradedOutcome,
    *,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Upsert one graded outcome. Regrading overwrites; signals are untouched."""
    payload = outcome.as_dict()
    payload["graded_at"] = now_ist().isoformat(timespec="seconds")
    columns = ", ".join(payload)
    placeholders = ", ".join(f":{c}" for c in payload)
    sql = (
        f"INSERT INTO signal_outcomes ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(signal_id) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in payload if c != "signal_id")
    )
    own = conn is None
    if own:
        init_ledger(db_path)
        conn = connect(db_path)
    try:
        conn.execute(sql, payload)
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def pending_signals(
    *,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
    include_synthetic: bool = False,
    session_date: str | None = None,
    limit: int | None = None,
    regrade: bool = False,
) -> list[dict]:
    """Signals awaiting grading, oldest first.

    ``regrade=True`` returns every signal regardless of existing outcome, for
    re-running after a cost-model or grading-rule change.
    """
    where = []
    params: list[Any] = []
    if not include_synthetic:
        where.append("s.is_synthetic = 0")
    if session_date:
        where.append("s.session_date = ?")
        params.append(session_date)
    if not regrade:
        where.append("o.signal_id IS NULL")

    sql = (
        "SELECT s.* FROM signals s "
        "LEFT JOIN signal_outcomes o ON o.signal_id = s.signal_id "
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY s.bar_time ASC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"

    init_ledger(db_path)
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def grade_pending(
    bars_provider: Callable[[dict], pd.DataFrame | None],
    *,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
    costs: OptionsCostModel | None = None,
    cutoff: time | None = DEFAULT_CUTOFF,
    include_synthetic: bool = False,
    session_date: str | None = None,
    limit: int | None = None,
    regrade: bool = False,
) -> dict:
    """Grade every pending signal and persist the results.

    ``bars_provider`` maps a signal row to that contract's premium bars. It is
    injected rather than hardwired so the same grading code runs against live
    Angel One candles, a cached historical store, or test fixtures -- the whole
    point being that live and backtest grading share one implementation.

    A provider that raises for one signal must not abort the batch, so failures
    are captured as ``NO_DATA`` with the error in ``notes``.
    """
    signals = pending_signals(
        db_path=db_path, include_synthetic=include_synthetic,
        session_date=session_date, limit=limit, regrade=regrade,
    )
    tally: dict[str, int] = {}
    graded = 0

    init_ledger(db_path)
    with connect(db_path) as conn:
        for signal in signals:
            try:
                bars = bars_provider(signal)
            except Exception as exc:  # noqa: BLE001 - one bad fetch must not stop the batch
                outcome = GradedOutcome(
                    str(signal["signal_id"]), NO_DATA,
                    notes=f"bars_provider failed: {exc}",
                )
            else:
                if bars is None or (hasattr(bars, "empty") and bars.empty):
                    outcome = GradedOutcome(
                        str(signal["signal_id"]), NO_DATA,
                        notes="No premium bars returned for this contract.",
                    )
                else:
                    outcome = grade_path(
                        signal, bars, costs=costs, cutoff=cutoff,
                        interval=signal.get("interval"),
                    )
            save_outcome(outcome, conn=conn)
            tally[outcome.outcome] = tally.get(outcome.outcome, 0) + 1
            graded += 1
        conn.commit()

    return {"graded": graded, "pending": len(signals), "by_outcome": tally}


# --------------------------------------------------------------------------- #
# Angel One premium-bar provider
# --------------------------------------------------------------------------- #

def angel_bars_provider(
    client: Any = None,
    *,
    interval_override: str | None = None,
    session_end: time = time(15, 30),
) -> Callable[[dict], pd.DataFrame | None]:
    """Build a ``bars_provider`` that pulls option candles from Angel One.

    Requires ``option_token`` on the signal row -- the NFO token of the exact
    contract. Without it there is no way to know what that strike's premium did,
    and the signal grades as ``NO_DATA`` rather than being approximated from
    the underlying. An approximation would quietly become the thing you
    calibrate on.
    """
    _ANGEL_INTERVALS = {
        "1m": "ONE_MINUTE", "3m": "THREE_MINUTE", "5m": "FIVE_MINUTE",
        "10m": "TEN_MINUTE", "15m": "FIFTEEN_MINUTE",
        "30m": "THIRTY_MINUTE", "1h": "ONE_HOUR", "60m": "ONE_HOUR",
    }
    state: dict[str, Any] = {"client": client}

    def _client():
        if state["client"] is None:
            from intraday_advisor.angel_one import AngelOneClient
            created = AngelOneClient()
            created.login()
            state["client"] = created
        return state["client"]

    def provider(signal: dict) -> pd.DataFrame | None:
        token = signal.get("option_token")
        if not token:
            return None

        bar_ts = _to_timestamp(signal.get("bar_time"))
        if bar_ts is None:
            return None

        day = bar_ts.normalize()
        # Start at the signal bar so the "strictly after" filter in grade_path
        # has the boundary bar available to exclude, then run to session end.
        from_dt = bar_ts.strftime("%Y-%m-%d %H:%M")
        to_dt = (day + timedelta(hours=session_end.hour, minutes=session_end.minute)).strftime(
            "%Y-%m-%d %H:%M"
        )
        raw_interval = interval_override or signal.get("interval") or "5m"
        angel_interval = _ANGEL_INTERVALS.get(raw_interval, raw_interval)

        return _client().get_candles(
            str(token), exchange="NFO", interval=angel_interval,
            from_date=from_dt, to_date=to_dt,
        )

    return provider


def grade_session(
    session_date: str | None = None,
    *,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
    client: Any = None,
    costs: OptionsCostModel | None = None,
    regrade: bool = False,
) -> dict:
    """Grade one trading day's signals using live Angel One option candles.

    Defaults to today. Intended to run once after the close -- the premium path
    for the whole session is then final, which is the earliest grading can be
    done without peeking at unfinished bars.
    """
    from intraday_advisor.ledger.recorder import fitted_cost_model

    target_date = session_date or now_ist().date().isoformat()
    return grade_pending(
        angel_bars_provider(client),
        db_path=db_path,
        costs=costs or fitted_cost_model(db_path),
        session_date=target_date,
        regrade=regrade,
    )
