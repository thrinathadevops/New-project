"""One bar is one row, however the feed spelled its timestamp.

The engine runs two candle feeds. Yahoo stamps bars in UTC, Angel stamps them
in IST, and ``bar_time`` was stored as ``str(index[-1])`` from whichever one
answered. The same 09:30 bar therefore arrived as two different strings, the
UNIQUE ``dedupe_key`` matched neither, and the ledger's "first observation
wins" rule stopped applying -- 73 NIFTY rows for 26 bars on 2026-11-04.

These tests pin the collapse at the one layer that can guarantee it, so a third
feed with a third convention cannot re-open the hole.
"""

from __future__ import annotations

import pytest

from intraday_advisor.ledger.dedupe_repair import render, repair
from intraday_advisor.ledger.recorder import (
    canonical_bar_time,
    _session_date,
    record_signal,
)
from intraday_advisor.ledger.schema import ENGINE_VERSION, connect, init_ledger


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "ledger.sqlite")
    init_ledger(path)
    return path


def write(db: str, bar_time: str, *, strike: float = 22_700.0,
          premium: float = 194.0, index_name: str = "NIFTY") -> str | None:
    return record_signal(
        db_path=db, bar_time=bar_time, interval="5m", index_name=index_name,
        direction="CALL", option_type="CE", strike=strike,
        expiry="2026-11-06", tradingsymbol="NIFTY06NOV2622700CE",
        lot_size=65, spot=22_690.0, entry_premium=premium,
        stop_premium=160.0, target_premium=250.0, score=42,
    )


# ============================================================ canonical form ==

def test_the_same_instant_in_two_timezones_is_one_string():
    """This is the whole bug: equal instants that compared unequal."""
    ist = canonical_bar_time("2026-11-04 09:30:00+05:30")
    utc = canonical_bar_time("2026-11-04 04:00:00+00:00")
    assert ist == utc == "2026-11-04 09:30:00+05:30"


def test_the_iso_t_separator_does_not_make_a_different_bar():
    assert (canonical_bar_time("2026-11-04T04:00:00+00:00")
            == canonical_bar_time("2026-11-04 04:00:00+00:00"))


def test_a_naive_timestamp_is_read_as_ist_not_utc():
    """Both producers deal in Indian market bars. Reading a naive 09:30 as UTC
    would move it to 15:00 IST and file it under the wrong part of the day."""
    assert canonical_bar_time("2026-11-04 09:30:00") == "2026-11-04 09:30:00+05:30"


def test_an_unparseable_timestamp_survives_as_itself():
    """Rule 4: a malformed value stays visible as a row rather than being
    dropped to flatter the record."""
    assert canonical_bar_time("not a timestamp") == "not a timestamp"
    assert canonical_bar_time("") == ""
    assert canonical_bar_time(None) == "None"


def test_canonicalising_twice_changes_nothing():
    once = canonical_bar_time("2026-11-04 04:00:00+00:00")
    assert canonical_bar_time(once) == once


def test_a_utc_bar_after_1830_belongs_to_the_next_session():
    """19:00 UTC is 00:30 IST the following day. The raw string would have
    dated it to the wrong session."""
    assert _session_date("2026-11-04 19:00:00+00:00") == "2026-11-05"
    assert _session_date("2026-11-04 09:55:00+00:00") == "2026-11-04"


# ================================================================== recording ==

def test_two_spellings_of_one_bar_write_one_row(db):
    first = write(db, "2026-11-04 09:30:00+05:30", premium=194.0)
    second = write(db, "2026-11-04 04:00:00+00:00", premium=192.35)
    with connect(db) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM signals")]
    assert first is not None
    assert second is None, "the UTC spelling must be recognised as the same bar"
    assert len(rows) == 1
    assert rows[0]["entry_premium"] == 194.0, "first observation wins"
    assert rows[0]["bar_time"] == "2026-11-04 09:30:00+05:30"


def test_different_bars_still_write_separate_rows(db):
    assert write(db, "2026-11-04 09:30:00+05:30") is not None
    assert write(db, "2026-11-04 09:35:00+05:30") is not None
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2


def test_the_stored_session_date_follows_the_canonical_bar(db):
    write(db, "2026-11-04 19:00:00+00:00")
    with connect(db) as conn:
        row = conn.execute(
            "SELECT bar_time, session_date FROM signals").fetchone()
    assert row["session_date"] == "2026-11-05"
    assert row["bar_time"].startswith("2026-11-05 00:30")


# ====================================================================== repair ==

def _mixed_history(db: str) -> None:
    """Two spellings of three bars, written straight past the dedupe key the
    way the live ledger accumulated them."""
    with connect(db) as conn:
        for i, (ist, utc) in enumerate((
            ("2026-11-04 09:30:00+05:30", "2026-11-04 04:00:00+00:00"),
            ("2026-11-04 09:35:00+05:30", "2026-11-04 04:05:00+00:00"),
            ("2026-11-04 09:40:00+05:30", "2026-11-04 04:10:00+00:00"),
        )):
            for j, spelling in enumerate((ist, utc)):
                conn.execute(
                    "INSERT INTO signals (signal_id, dedupe_key, emitted_at, "
                    "bar_time, interval, session_date, index_name, direction, "
                    "option_type, strike, lot_size, entry_premium, "
                    "stop_premium, target_premium, score, is_live, "
                    "is_synthetic, engine_version) VALUES (?, ?, ?, ?, '5m', "
                    "'2026-11-04', 'NIFTY', 'CALL', 'CE', 22700, 65, ?, "
                    "160.0, 250.0, 42, 1, 0, ?)",
                    (f"sig{i}{j}", f"{spelling}|NIFTY|CE|22700|{ENGINE_VERSION}",
                     f"2026-11-04T09:3{i}:0{j}+05:30", spelling,
                     194.0 if j == 0 else 192.35, ENGINE_VERSION),
                )
        conn.commit()


def test_a_dry_run_writes_nothing(db):
    _mixed_history(db)
    report = repair(db_path=db, dry_run=True)
    assert report.rows_before == 6
    assert report.distinct_bars == 3
    assert report.rows_removed == 3
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 6


def test_the_repair_keeps_the_first_observation_of_each_bar(db):
    _mixed_history(db)
    repair(db_path=db, dry_run=False, backup=False)
    with connect(db) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT bar_time, entry_premium FROM signals ORDER BY bar_time")]
    assert len(rows) == 3
    assert [r["bar_time"] for r in rows] == [
        "2026-11-04 09:30:00+05:30",
        "2026-11-04 09:35:00+05:30",
        "2026-11-04 09:40:00+05:30",
    ]
    assert all(r["entry_premium"] == 194.0 for r in rows), \
        "the earlier-emitted row is the one that survives"


def test_the_repair_leaves_the_dedupe_key_able_to_do_its_job(db):
    """After the repair the constraint must actually block a re-observation,
    which it could not while two spellings existed."""
    _mixed_history(db)
    repair(db_path=db, dry_run=False, backup=False)
    assert write(db, "2026-11-04 04:00:00+00:00", premium=1.0) is None
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 3


def test_running_the_repair_twice_is_a_no_op(db):
    _mixed_history(db)
    repair(db_path=db, dry_run=False, backup=False)
    second = repair(db_path=db, dry_run=False, backup=False)
    assert second.rows_removed == 0
    assert second.rows_restamped == 0


def test_the_repair_backs_the_database_up_before_deleting(db, tmp_path):
    _mixed_history(db)
    report = repair(db_path=db, dry_run=False, backup=True)
    from pathlib import Path
    assert report.backup_path
    assert Path(report.backup_path).exists()
    with connect(report.backup_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 6


def test_an_already_clean_ledger_is_untouched(db):
    write(db, "2026-11-04 09:30:00+05:30")
    write(db, "2026-11-04 09:35:00+05:30")
    report = repair(db_path=db, dry_run=True)
    assert report.rows_removed == 0
    assert report.distinct_bars == 2


def test_an_empty_ledger_reports_nothing_rather_than_dividing_by_zero(db):
    report = repair(db_path=db, dry_run=True)
    assert report.rows_before == 0
    assert "1.00x" in render(report)


def test_the_report_states_the_inflation_as_a_ratio(db):
    _mixed_history(db)
    text = render(repair(db_path=db, dry_run=True))
    assert "2.00x" in text
    assert "dry run" in text
