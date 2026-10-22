"""Session-over-session comparison: is it an edge, or was one day lucky?

Why this is the tool today's data demanded
------------------------------------------
Three graded sessions produced this:

    2026-10-16    34 signals   -Rs 1,222/signal     0% hit target
    2026-10-21   111 signals   -Rs 1,262/signal     9% hit target
    2026-10-22    92 signals   +Rs   478/signal    60% hit target

The pooled figure over all 237 trades is -Rs 580/lot, which reads as a settled
answer and is not one. **The spread between days is larger than the effect being
measured**, so a pooled mean tells you about the mix of days in the sample, not
about the strategy. One session at +60% target rate cannot be distinguished from
luck by averaging it with two bad ones.

What separates the two is consistency across sessions, so that is what this
measures: the per-session mean, how often a session was positive, and whether
the spread between sessions swamps the average. A single day, however good,
only ever moves the numerator.

The number that matters
-----------------------
``sessions_needed`` answers "how long until this means anything?" from the
observed between-session spread, rather than from a rule of thumb. With a daily
standard deviation several times the daily mean, the answer is weeks -- which is
worth knowing before, not after, concluding anything from three days.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

#: Below this many sessions, report the spread and refuse to call a direction.
MIN_SESSIONS = 5

#: Target precision for sessions_needed: a confidence interval whose half-width
#: is this fraction of the mean. 0.5 means "the mean is at least twice its own
#: margin of error", which is modest and still usually takes weeks.
TARGET_PRECISION = 0.5


@dataclass
class SessionSeries:
    """Per-session results and what they do and do not support."""

    sessions: int = 0
    per_session: list = field(default_factory=list)
    mean_per_signal: float = 0.0
    sd_between_sessions: float = 0.0
    positive_sessions: int = 0
    t_stat: float = 0.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    sessions_needed: int | None = None
    verdict: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def session_series(
    *, db_path: str | Path = "data/ledger.sqlite", **filters,
) -> SessionSeries:
    """Per-session expectancy, treating each SESSION as one observation.

    This is the correct unit. Trades inside a day are not independent -- they
    share the day's regime, its volatility and often its direction -- so
    pooling 237 trades and dividing by the square root of 237 overstates the
    precision badly. Treating each session as one draw is conservative and
    honest, and it is the same overlap lesson that turned an i.i.d. t of +3.90
    into a HAC t of +1.83 on the intraday study.
    """
    from intraday_advisor.ledger.report import load_graded

    result = SessionSeries()
    frame = load_graded(db_path=db_path, **filters)
    if frame.empty:
        result.notes.append("No graded trades yet.")
        return result

    column = ("net_pnl_per_lot" if "net_pnl_per_lot" in frame.columns
              else "net_pnl")
    grouped = frame.groupby("session_date")[column]
    per_session = grouped.mean()
    counts = grouped.size()
    target_rate = frame.groupby("session_date")["outcome"].apply(
        lambda x: float((x == "TARGET").mean())
    )

    result.sessions = int(len(per_session))
    result.per_session = [
        {
            "session": str(day),
            "signals": int(counts[day]),
            "per_signal": round(float(per_session[day]), 2),
            "total": round(float(per_session[day] * counts[day]), 2),
            "target_rate": round(float(target_rate[day]), 3),
        }
        for day in per_session.index
    ]
    result.mean_per_signal = round(float(per_session.mean()), 2)
    result.positive_sessions = int((per_session > 0).sum())

    if result.sessions < 2:
        result.verdict = "one session: nothing can be concluded"
        result.notes.append(
            "A single session cannot distinguish a strategy from a day."
        )
        return result

    sd = float(per_session.std(ddof=1))
    result.sd_between_sessions = round(sd, 2)
    standard_error = sd / math.sqrt(result.sessions)
    if standard_error > 0:
        result.t_stat = round(result.mean_per_signal / standard_error, 2)
        margin = 1.96 * standard_error
        result.ci_low = round(result.mean_per_signal - margin, 2)
        result.ci_high = round(result.mean_per_signal + margin, 2)

    # How many sessions until the interval is tight enough to read?
    if result.mean_per_signal and sd > 0:
        wanted = (1.96 * sd / (TARGET_PRECISION * abs(result.mean_per_signal))) ** 2
        result.sessions_needed = int(math.ceil(wanted))

    spread_vs_mean = (sd / abs(result.mean_per_signal)
                      if result.mean_per_signal else float("inf"))
    if spread_vs_mean > 1:
        result.notes.append(
            f"The spread between sessions (Rs {sd:,.0f}) is "
            f"{spread_vs_mean:.1f}x the average session (Rs "
            f"{result.mean_per_signal:,.0f}). Day-to-day variation dominates "
            f"the effect, so the pooled average describes this sample's mix of "
            f"days rather than the strategy."
        )

    if result.sessions < MIN_SESSIONS:
        result.verdict = (
            f"{result.sessions} session(s): too few to call a direction"
        )
        result.notes.append(
            f"{result.positive_sessions} of {result.sessions} session(s) were "
            f"positive. At this count that is a coin-flip's worth of evidence."
        )
    elif result.ci_low > 0:
        result.verdict = "consistently positive across sessions"
    elif result.ci_high < 0:
        result.verdict = "consistently negative across sessions"
    else:
        result.verdict = "straddles zero: not yet separable from noise"

    if result.sessions_needed:
        result.notes.append(
            f"About {result.sessions_needed} session(s) are needed before the "
            f"average is twice its own margin of error. You have "
            f"{result.sessions}."
        )
    return result


def render(series: SessionSeries) -> str:
    """A plain-text report, for the CLI and for reading in a log."""
    if not series.per_session:
        return "No graded sessions yet."

    lines = [
        "=" * 72,
        "SESSION-BY-SESSION  (each session is ONE observation, not each trade)",
        "=" * 72,
        f"  {'session':12} {'signals':>8} {'per signal':>12} {'total':>13} "
        f"{'target':>8}",
    ]
    for row in series.per_session:
        lines.append(
            f"  {row['session']:12} {row['signals']:8} "
            f"{row['per_signal']:12,.0f} {row['total']:13,.0f} "
            f"{row['target_rate'] * 100:7.0f}%"
        )

    lines += [
        "",
        f"  sessions                 {series.sessions}",
        f"  positive sessions        {series.positive_sessions}"
        f" of {series.sessions}",
        f"  average per signal       Rs {series.mean_per_signal:+,.0f}",
        f"  spread between sessions  Rs {series.sd_between_sessions:,.0f}",
    ]
    if series.sessions >= 2:
        lines.append(
            f"  95% interval             Rs {series.ci_low:+,.0f} to "
            f"Rs {series.ci_high:+,.0f}   (t = {series.t_stat:+.2f})"
        )
    lines += ["", f"  VERDICT: {series.verdict}"]
    for note in series.notes:
        lines.append(f"    - {note}")
    lines.append("=" * 72)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Coverage: is an hour-by-hour comparison even legitimate?
# --------------------------------------------------------------------------- #

#: NSE trades 09:15-15:30, so these are the hour buckets a full session has.
SESSION_HOURS = (9, 10, 11, 12, 13, 14, 15)


def hour_coverage(*, db_path: str | Path = "data/ledger.sqlite", **filters) -> dict:
    """Which hours each session actually sampled, and whether hours comparable.

    This exists because an hour breakdown lied and nearly took me with it.

    Pooled over the first three sessions, hour 9 showed an **88.9% win rate and
    +Rs 1,630 per lot** against negative figures everywhere else -- an apparent
    opening edge. All 18 of those trades were from 2026-10-22, the single
    positive session, because the recorder was not running before 09:15 on the
    other two days. The "opening edge" and "22 October was a good day" were the
    same fact, sliced differently.

    The coverage differs per session for a mundane reason -- the server was
    started at a different time each day -- and the consequence is not mundane:
    **every hour-versus-hour comparison is confounded with which day it was.**
    An hour sampled on one session cannot be compared with an hour sampled on
    another, and nothing in a pooled table shows that.

    So this reports, per hour, how many distinct sessions contributed. An hour
    seen in one session is an anecdote about that session.
    """
    from intraday_advisor.ledger.report import load_graded

    frame = load_graded(db_path=db_path, **filters)
    if frame.empty:
        return {"sessions": 0, "hours": {}, "comparable_hours": [],
                "notes": ["No graded trades yet."]}

    frame = frame.copy()
    frame["hour"] = (
        pd.to_datetime(frame["bar_time"], errors="coerce", utc=True)
        .dt.tz_convert("Asia/Kolkata").dt.hour
    )
    sessions = sorted(frame["session_date"].dropna().unique())
    hours: dict = {}
    for hour in SESSION_HOURS:
        chunk = frame[frame["hour"] == hour]
        covered = sorted(chunk["session_date"].dropna().unique())
        hours[hour] = {
            "trades": int(len(chunk)),
            "sessions": [str(s) for s in covered],
            "session_count": len(covered),
            "mean_net": (round(float(chunk["net_pnl_per_lot"].mean()), 2)
                         if len(chunk) else None),
        }

    comparable = [h for h, v in hours.items() if v["session_count"] >= 3]
    single = [h for h, v in hours.items() if v["session_count"] == 1]
    missing = [h for h, v in hours.items() if v["session_count"] == 0]

    notes = []
    if single:
        notes.append(
            f"Hour(s) {single} appear in ONE session only. Their numbers "
            f"describe that day, not that hour -- pooling them with other "
            f"hours compares days, not times."
        )
    if missing:
        notes.append(
            f"Hour(s) {missing} were never sampled. Start the recorder before "
            f"09:15 and leave it to the close, or the day's shape is unknown."
        )
    if not comparable:
        notes.append(
            "No hour yet appears in three or more sessions, so no hour-by-hour "
            "claim is supportable."
        )
    return {
        "sessions": len(sessions),
        "session_dates": [str(s) for s in sessions],
        "hours": hours,
        "comparable_hours": comparable,
        "single_session_hours": single,
        "unsampled_hours": missing,
        "notes": notes,
    }


def render_coverage(coverage: dict) -> str:
    """Plain text, so a confounded comparison is visible before it is believed."""
    if not coverage.get("hours"):
        return "No graded trades yet."

    lines = [
        "=" * 72,
        "HOUR COVERAGE  (can these hours legitimately be compared?)",
        "=" * 72,
        f"  {'hour':>5} {'trades':>8} {'sessions':>9} {'mean net':>11}  status",
    ]
    for hour, entry in coverage["hours"].items():
        count = entry["session_count"]
        status = ("comparable" if count >= 3
                  else f"only {count} session(s)" if count
                  else "NEVER SAMPLED")
        mean = (f"{entry['mean_net']:,.0f}" if entry["mean_net"] is not None
                else "-")
        lines.append(
            f"  {hour:5} {entry['trades']:8} {count:9} {mean:>11}  {status}"
        )
    lines.append("")
    for note in coverage["notes"]:
        lines.append(f"  - {note}")
    lines.append("=" * 72)
    return "\n".join(lines)
