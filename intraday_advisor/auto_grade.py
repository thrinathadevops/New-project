"""Grade the ledger without being asked, because the manual step never happens.

The whole record -> grade -> report pipeline existed and worked, and **151 real
signals sat ungraded** because grading was a command somebody had to remember
to type after the close. When it finally ran it produced the most useful
measurement in the project -- 108 trades, an 11.1% win rate and an expectancy
of -Rs 1,117 per lot with the entire confidence interval below zero.

A measurement that only happens when you remember to take it is a measurement
that does not happen. So this runs itself.

When it is safe to grade
------------------------
Not before the session is over. Grading reads the premium path for the whole
day, and a path that is still being written would resolve stops and targets
against unfinished bars -- the exact lookahead the grader exists to avoid. So
grading waits until ``GRADE_AFTER`` (15:45 IST by default), which is thirty
minutes past the 15:15 square-off and fifteen past the 15:30 close, leaving
room for the broker's last candles to settle.

Catching up
-----------
On startup it also grades any **earlier** session that still has pending
signals. A desk that was switched off for a week should not silently carry a
week of ungraded trades, and the only thing standing between those signals and
a measurement is a provider call that can be made at any time.

Everything here is idempotent: ``grade_pending`` touches only ungraded rows, so
running twice costs one wasted query and changes nothing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import time as dt_time
from pathlib import Path

logger = logging.getLogger("auto_grade")

#: Grade only after this IST time: past square-off, past the close, and past
#: the broker's last candle settling.
GRADE_AFTER = dt_time(15, 45)

#: How many earlier sessions to catch up on in one pass. A bound, so a long-idle
#: ledger cannot turn a dashboard start into a hundred provider calls.
MAX_CATCH_UP_SESSIONS = 10


@dataclass
class AutoGradeState:
    """What the grader has done, for the log and the dashboard header."""

    enabled: bool = True
    last_attempt: str | None = None
    last_success: str | None = None
    sessions_graded: list[str] = field(default_factory=list)
    graded_total: int = 0
    pending_total: int = 0
    last_error: str | None = None
    running: bool = False

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


_LOCK = threading.Lock()
_STATE = AutoGradeState()


def status() -> dict:
    """Current state, safe to call from the snapshot loop."""
    with _LOCK:
        return _STATE.as_dict()


def disable() -> None:
    """Turn automatic grading off for this process (tests, replay runs)."""
    with _LOCK:
        _STATE.enabled = False


def _now_ist():
    from intraday_advisor.ledger.recorder import now_ist
    return now_ist()


def sessions_awaiting_grade(
    db_path: str | Path, *, limit: int = MAX_CATCH_UP_SESSIONS,
) -> list[str]:
    """Session dates that still hold ungraded real signals, oldest first.

    Synthetic rows are excluded: they exist to prove the pipeline, and grading
    them would put invented trades into a track record.
    """
    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.session_date, COUNT(*) AS pending
              FROM signals s
              LEFT JOIN signal_outcomes o ON o.signal_id = s.signal_id
             WHERE o.signal_id IS NULL
               AND COALESCE(s.is_synthetic, 0) = 0
             GROUP BY s.session_date
             ORDER BY s.session_date ASC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [str(row["session_date"]) for row in rows]


def is_safe_to_grade(session_date: str, *, now=None) -> tuple[bool, str]:
    """Is this session finished enough to grade?

    A past session always is. Today's only once the close has settled, because
    grading an unfinished path resolves stops against bars that have not
    happened -- which would flatter every result in the ledger.
    """
    moment = now or _now_ist()
    today = moment.date().isoformat()
    if session_date < today:
        return True, "an earlier session is complete"
    if session_date > today:
        return False, "session is in the future"
    if moment.time() >= GRADE_AFTER:
        return True, f"past {GRADE_AFTER.strftime('%H:%M')}, the session has settled"
    return False, (
        f"today's session is still open or settling; grading before "
        f"{GRADE_AFTER.strftime('%H:%M')} would resolve trades against "
        f"unfinished bars"
    )


def grade_due(
    db_path: str | Path, *, client=None, now=None, limit: int = MAX_CATCH_UP_SESSIONS,
) -> dict:
    """Grade every session that is both pending and safe to grade.

    Returns a summary rather than raising: this runs on a background thread in
    a desk application, and a provider hiccup must never take the screen down.
    """
    from intraday_advisor.ledger.grader import grade_session

    with _LOCK:
        if not _STATE.enabled:
            return {"skipped": "disabled"}
        if _STATE.running:
            return {"skipped": "already running"}
        _STATE.running = True
        _STATE.last_attempt = (now or _now_ist()).isoformat(timespec="seconds")

    summary: dict = {"graded": [], "deferred": [], "errors": []}
    try:
        pending = sessions_awaiting_grade(db_path, limit=limit)
        with _LOCK:
            _STATE.pending_total = len(pending)

        for session_date in pending:
            safe, reason = is_safe_to_grade(session_date, now=now)
            if not safe:
                summary["deferred"].append({"session": session_date,
                                            "reason": reason})
                continue
            try:
                result = grade_session(session_date, db_path=db_path,
                                       client=client)
                graded = int(result.get("graded") or 0)
                summary["graded"].append({"session": session_date,
                                          "graded": graded,
                                          "outcomes": result.get("outcomes")})
                with _LOCK:
                    _STATE.graded_total += graded
                    if session_date not in _STATE.sessions_graded:
                        _STATE.sessions_graded.append(session_date)
                logger.info("Auto-graded %s: %d signal(s)", session_date, graded)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append({"session": session_date,
                                          "error": str(exc)[:200]})
                logger.warning("Auto-grade failed for %s: %s", session_date, exc)

        with _LOCK:
            _STATE.last_error = (summary["errors"][-1]["error"]
                                 if summary["errors"] else None)
            if summary["graded"]:
                _STATE.last_success = (now or _now_ist()).isoformat(
                    timespec="seconds")
    finally:
        with _LOCK:
            _STATE.running = False
    return summary


def describe(db_path: str | Path, *, now=None) -> dict:
    """A line for the dashboard: what is waiting, and when it will be graded."""
    state = status()
    try:
        pending = sessions_awaiting_grade(db_path)
    except Exception:  # noqa: BLE001
        pending = []

    moment = now or _now_ist()
    if not state["enabled"]:
        note = "Automatic grading is off for this process."
    elif not pending:
        note = "Nothing waiting — every recorded signal has been graded."
    else:
        ready = [s for s in pending if is_safe_to_grade(s, now=moment)[0]]
        if ready:
            note = (
                f"{len(ready)} session(s) ready to grade; it runs by itself "
                f"within the minute."
            )
        else:
            note = (
                f"{len(pending)} session(s) waiting. Today's is graded "
                f"automatically after {GRADE_AFTER.strftime('%H:%M')}, once "
                f"the close has settled."
            )
    return {**state, "pending_sessions": pending, "note": note}
