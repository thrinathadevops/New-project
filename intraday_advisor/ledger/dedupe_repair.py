"""Collapse signal rows that are the same bar spelled two ways.

What happened
-------------
``bar_time`` was stored as ``str(DataFrame.index[-1])`` from whichever feed
produced the candles, and the engine runs two: the chart path fetches Yahoo,
which stamps bars in UTC, and the signal path fetches Angel, which stamps them
in IST. The same 09:30 bar reached the ledger as both::

    2026-11-04 09:30:00+05:30
    2026-11-04 04:00:00+00:00

Equal instants, different strings. The UNIQUE ``dedupe_key`` is built from that
string, so it matched neither, and "first observation wins" -- the rule that is
supposed to keep one row per bar -- silently stopped applying.

How much it mattered
--------------------
Measured before the repair:

    session       rows   distinct bars
    2026-10-16      52        52        clean (one feed that day)
    2026-10-22     222       222        clean
    2026-10-23     214       212
    2026-11-02     324       173        47% duplicated
    2026-11-03     376       195        48% duplicated
    2026-11-04     160        84        48% duplicated

So October is sound and November is inflated roughly twofold. That matters
beyond tidiness: a doubled sample halves every standard error and narrows every
confidence interval by about sqrt(2), which is exactly the direction that makes
a result look more certain than the evidence supports.

Which row is kept
-----------------
The **earliest emitted** one, per the ledger's own rule 2: the first
observation is the one that could actually have been acted on, and the chain has
moved by the time the second arrives. The pair differs slightly for that reason
-- 194.00 against 192.35 on the same bar, five seconds apart.

A ``signal_outcomes`` row attached to a discarded signal is removed with it by
``ON DELETE CASCADE``. That loses nothing recoverable: an outcome is *derived*
from stored bars, not observed, so re-running the grader restores it. The repair
reports how many grades will need regenerating rather than quietly preferring a
graded row over the honest first observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RepairReport:
    """What the repair did, or would do under ``dry_run``."""

    rows_before: int = 0
    distinct_bars: int = 0
    rows_removed: int = 0
    rows_restamped: int = 0
    outcomes_removed: int = 0
    grades_to_regenerate: int = 0
    per_session: list = field(default_factory=list)
    backup_path: str = ""
    dry_run: bool = True

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _canonical_rows(conn) -> list[dict]:
    from intraday_advisor.ledger.recorder import canonical_bar_time

    rows = []
    for row in conn.execute(
        "SELECT signal_id, emitted_at, bar_time, session_date, index_name, "
        "option_type, strike, engine_version FROM signals"
    ):
        record = dict(row)
        record["canonical"] = canonical_bar_time(record["bar_time"])
        strike = record["strike"]
        try:
            strike_text = f"{float(strike):g}"
        except (TypeError, ValueError):
            strike_text = str(strike)
        record["key"] = "|".join((
            record["canonical"],
            str(record["index_name"]),
            str(record["option_type"]),
            strike_text,
            str(record["engine_version"]),
        ))
        rows.append(record)
    return rows


def repair(*, db_path: str | Path, dry_run: bool = True,
           backup: bool = True) -> RepairReport:
    """Restamp every ``bar_time`` canonically and keep one row per bar.

    With ``dry_run`` (the default) nothing is written -- the report says what
    would change. A real run copies the database beside itself first, because
    this deletes rows and the only thing worse than a duplicated ledger is a
    truncated one with no copy of what it held.
    """
    import sqlite3

    from intraday_advisor.ledger.recorder import canonical_bar_time
    from intraday_advisor.ledger.schema import connect, init_ledger

    report = RepairReport(dry_run=dry_run)
    init_ledger(db_path)

    with connect(db_path) as conn:
        rows = _canonical_rows(conn)
        graded = {r[0] for r in conn.execute(
            "SELECT signal_id FROM signal_outcomes")}

    report.rows_before = len(rows)
    if not rows:
        return report

    groups: dict[str, list[dict]] = {}
    for record in rows:
        groups.setdefault(record["key"], []).append(record)
    report.distinct_bars = len(groups)

    discard: list[str] = []
    for members in groups.values():
        if len(members) == 1:
            continue
        # Earliest emitted wins; ties broken by signal_id so the choice is
        # deterministic and a re-run of the repair is a no-op.
        members.sort(key=lambda r: (str(r["emitted_at"]), str(r["signal_id"])))
        keeper = members[0]
        for loser in members[1:]:
            discard.append(loser["signal_id"])
            if loser["signal_id"] in graded and keeper["signal_id"] not in graded:
                report.grades_to_regenerate += 1
    report.rows_removed = len(discard)
    report.outcomes_removed = sum(1 for sid in discard if sid in graded)

    restamp = [
        (record["canonical"], record["signal_id"])
        for record in rows
        if record["signal_id"] not in set(discard)
        and record["bar_time"] != record["canonical"]
    ]
    report.rows_restamped = len(restamp)

    by_session: dict[str, dict] = {}
    kept = set(r["signal_id"] for r in rows) - set(discard)
    for record in rows:
        session = record["canonical"][:10]
        entry = by_session.setdefault(
            session, {"session": session, "before": 0, "after": 0})
        entry["before"] += 1
        if record["signal_id"] in kept:
            entry["after"] += 1
    report.per_session = [by_session[k] for k in sorted(by_session)]

    if dry_run:
        return report

    if backup:
        # SQLite's online backup, NOT a file copy. The ledger runs in WAL mode,
        # so committed rows can still be sitting in the -wal sidecar: copying
        # only the .sqlite produced a "backup" with no signals table at all,
        # which is worse than having none because it looks like one.
        source = Path(db_path)
        target = source.with_suffix(source.suffix + ".pre-dedupe")
        with connect(source) as live, sqlite3.connect(target) as copy:
            live.backup(copy)
        report.backup_path = str(target)

    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for chunk_start in range(0, len(discard), 500):
            chunk = discard[chunk_start:chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM signals WHERE signal_id IN ({marks})",
                         chunk)
        # Restamp survivors, and rebuild the key and session from the canonical
        # value so the UNIQUE constraint starts doing its job.
        for canonical, signal_id in restamp:
            row = conn.execute(
                "SELECT index_name, option_type, strike, engine_version "
                "FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
            if row is None:
                continue
            try:
                strike_text = f"{float(row['strike']):g}"
            except (TypeError, ValueError):
                strike_text = str(row["strike"])
            key = "|".join((canonical, str(row["index_name"]),
                            str(row["option_type"]), strike_text,
                            str(row["engine_version"])))
            conn.execute(
                "UPDATE signals SET bar_time = ?, dedupe_key = ?, "
                "session_date = ? WHERE signal_id = ?",
                (canonical, key, canonical[:10], signal_id),
            )
        # Any row left untouched above still needs its session_date to agree
        # with its canonical bar, which the UTC spelling could have dated to
        # the previous day.
        for signal_id, bar_time in conn.execute(
                "SELECT signal_id, bar_time FROM signals").fetchall():
            canonical = canonical_bar_time(bar_time)
            if canonical[:10] and len(canonical) >= 10:
                conn.execute(
                    "UPDATE signals SET session_date = ? "
                    "WHERE signal_id = ? AND session_date <> ?",
                    (canonical[:10], signal_id, canonical[:10]))
        conn.commit()

    return report


def render(report: RepairReport) -> str:
    """Plain text, with the inflation stated as a ratio."""
    ratio = (report.rows_before / report.distinct_bars
             if report.distinct_bars else 1.0)
    lines = [
        "=" * 68,
        "LEDGER DEDUPE REPAIR" + ("  (dry run — nothing written)"
                                  if report.dry_run else ""),
        "=" * 68,
        f"  rows before      : {report.rows_before}",
        f"  distinct bars    : {report.distinct_bars}",
        f"  inflation        : {ratio:.2f}x",
        f"  rows removed     : {report.rows_removed}",
        f"  rows restamped   : {report.rows_restamped}",
        f"  outcomes removed : {report.outcomes_removed}",
        f"  grades to re-run : {report.grades_to_regenerate}",
    ]
    if report.backup_path:
        lines.append(f"  backup           : {report.backup_path}")
    if report.per_session:
        lines += ["", f"  {'session':12} {'before':>8} {'after':>8} {'dupes':>8}"]
        for row in report.per_session:
            lines.append(
                f"  {row['session']:12} {row['before']:8} {row['after']:8} "
                f"{row['before'] - row['after']:8}"
            )
    lines.append("=" * 68)
    return "\n".join(lines)
