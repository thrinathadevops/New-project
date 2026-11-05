"""Keeping the day's data, including the parts that expire at the source.

Yahoo serves roughly 60 days of 5-minute history, so every day that passes
destroys a day permanently. `data.store_ohlcv_sqlite` existed for this and had
zero callers, which meant the one piece of data that cannot be rebuilt later at
any price was the one nobody kept.

Two of the tests below are regressions for bugs found on the first real run:
pooling two indices' spots reported a -57% move on a flat day, and
`Series.tz_convert` (instead of `.dt.tz_convert`) silently emptied every
realised-volatility figure.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.archive import (
    ARCHIVE_AFTER,
    archive_bars,
    archive_due,
    archive_symbols,
    bar_coverage,
    build_daily_record,
    daily_records,
    is_safe_to_archive,
    record_chain_spreads,
    record_prediction,
    record_spread_sample,
    resolve_prediction,
    stored_bars,
)
from intraday_advisor.ledger.schema import connect, init_ledger


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "ledger.sqlite")
    init_ledger(path)
    return path


def fake_bars(sessions: int = 3, bars: int = 75, start: str = "2026-11-02"):
    """A frame shaped like what `fetch_yahoo_ohlcv` returns, in IST."""
    rng = np.random.default_rng(5)
    rows, stamps = [], []
    price = 24_500.0
    for day in range(sessions):
        opening = pd.Timestamp(start, tz="Asia/Kolkata") + pd.Timedelta(days=day)
        opening += pd.Timedelta(hours=9, minutes=15)
        for bar in range(bars):
            close = price * (1 + rng.normal(0, 0.0009))
            rows.append((price, max(price, close) * 1.0004,
                         min(price, close) * 0.9996, close, 1000.0))
            stamps.append(opening + pd.Timedelta(minutes=5 * bar))
            price = close
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.DatetimeIndex(stamps))


def feed(frame):
    def fetch(symbol, period=None, interval=None):
        return frame
    return fetch


# ===================================================================== bars ==

def test_bars_are_stored_with_canonical_ist_timestamps(db):
    """The key must be canonical or the same bar spelled two ways becomes two
    rows -- the bug that duplicated half of three sessions in `signals`."""
    archive_bars(symbols=("^NSEI",), db_path=db, fetch=feed(fake_bars()))
    with connect(db) as conn:
        total, ist = conn.execute(
            "SELECT COUNT(*), SUM(bar_time LIKE '%+05:30') FROM bars"
        ).fetchone()
    assert total == 225
    assert ist == total


def test_a_utc_spelled_source_is_normalised_not_stored_raw(db):
    """Yahoo returns UTC for some calls and IST for others. Both must land on
    one row, not two."""
    frame = fake_bars(sessions=1)
    archive_bars(symbols=("^NSEI",), db_path=db, fetch=feed(frame))
    utc = frame.tz_convert("UTC")
    archive_bars(symbols=("^NSEI",), db_path=db, fetch=feed(utc))
    with connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    assert total == 75, "the UTC spelling must collapse onto the IST rows"


def test_rerunning_the_archive_writes_nothing(db):
    frame = fake_bars()
    archive_bars(symbols=("^NSEI",), db_path=db, fetch=feed(frame))
    again = archive_bars(symbols=("^NSEI",), db_path=db, fetch=feed(frame))
    assert again["^NSEI"] == 0


def test_a_failing_symbol_does_not_stop_the_others(db):
    def fetch(symbol, period=None, interval=None):
        if symbol == "BAD":
            raise RuntimeError("no data")
        return fake_bars(sessions=1)

    written = archive_bars(symbols=("BAD", "^NSEI"), db_path=db, fetch=fetch)
    assert written["BAD"] == 0
    assert written["^NSEI"] == 75


def test_an_empty_frame_is_handled(db):
    written = archive_bars(symbols=("^NSEI",), db_path=db,
                           fetch=feed(pd.DataFrame()))
    assert written["^NSEI"] == 0


def test_stored_bars_come_back_as_a_usable_ist_frame(db):
    """Regression: `Series.tz_convert` instead of `.dt.tz_convert` raised
    "index is not a valid DatetimeIndex", which emptied every realised
    volatility figure in the daily record without raising anywhere visible."""
    archive_bars(symbols=("^NSEI",), db_path=db, fetch=feed(fake_bars()))
    frame = stored_bars("^NSEI", db_path=db)
    assert len(frame) == 225
    assert str(frame.index.tz) == "Asia/Kolkata"
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.index.is_monotonic_increasing


def test_stored_bars_can_be_limited_to_recent_sessions(db):
    archive_bars(symbols=("^NSEI",), db_path=db, fetch=feed(fake_bars()))
    recent = stored_bars("^NSEI", db_path=db, sessions=1)
    assert len(recent) == 75


def test_stored_bars_of_an_empty_archive_is_an_empty_frame_not_none(db):
    frame = stored_bars("^NSEI", db_path=db)
    assert frame is not None and frame.empty


def test_coverage_reports_what_is_held(db):
    archive_bars(symbols=("^NSEI",), db_path=db, fetch=feed(fake_bars()))
    coverage = bar_coverage(db_path=db)
    assert coverage["sessions"] == 3
    assert coverage["per_symbol"][0]["symbol"] == "^NSEI"


def test_the_symbol_list_reuses_the_breadth_basket():
    """Not redeclared: the basket is already downloaded every five minutes for
    the regime check, so archiving it adds history at no new network cost."""
    from intraday_advisor.regime import BREADTH_BASKET

    symbols = archive_symbols()
    assert "^NSEI" in symbols and "^NSEBANK" in symbols
    assert set(BREADTH_BASKET).issubset(set(symbols))
    assert len(symbols) == len(set(symbols)), "no duplicates"


# ========================================================== spread samples ==

def test_a_spread_sample_is_stored_with_its_hour(db):
    written = record_spread_sample(
        db_path=db, index_name="nifty", tradingsymbol="N-24500-CE",
        half_spread_pct=0.012, premium=194.0,
        observed_at="2026-11-05T10:20:00+05:30")
    with connect(db) as conn:
        row = dict(conn.execute("SELECT * FROM spread_samples").fetchone())
    assert written == 1
    assert row["hour"] == 10
    assert row["index_name"] == "NIFTY"
    assert row["session_date"] == "2026-11-05"


@pytest.mark.parametrize("bad", [None, "wide", float("nan"), -0.01])
def test_a_nonsense_spread_is_refused(db, bad):
    assert record_spread_sample(
        db_path=db, index_name="NIFTY", tradingsymbol="X",
        half_spread_pct=bad) == 0


def test_two_samples_of_one_contract_in_one_second_are_one_row(db):
    for _ in range(2):
        record_spread_sample(db_path=db, index_name="NIFTY",
                             tradingsymbol="N-24500-CE", half_spread_pct=0.01,
                             observed_at="2026-11-05T10:20:00+05:30")
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM spread_samples").fetchone()[0] == 1


def test_a_synthetic_chain_contributes_no_spread_samples(db, monkeypatch):
    """A calibration built from invented books would be worse than the
    published prior it replaced, so the fallback path must write nothing --
    even during market hours, when the guard above would not catch it."""
    monkeypatch.setattr("intraday_advisor.engine.is_market_open",
                        lambda *a, **k: True)
    chain = {"atm": 24500,
             "df": pd.DataFrame({"strike": [24400, 24500, 24600],
                                 "ce_ltp": [250, 194, 150],
                                 "pe_ltp": [140, 190, 240]})}
    assert record_chain_spreads(chain, db_path=db, index_name="NIFTY") == 0


def test_no_spread_is_recorded_outside_market_hours(db, monkeypatch):
    """Regression, and the one that mattered most.

    Angel keeps serving last-traded depth after the close, so the first live run
    recorded 20 samples at 21:20 -- nine hours after the 15:30 close, all
    stamped hour 21. A spread you cannot trade at is not evidence about cost,
    and because `spread_multiplier_at` snaps an out-of-session hour onto the
    nearest in-session one, those rows would have been folded into the closing
    hour's calibration -- which auto-applies to every verdict.
    """
    import pandas as pd

    monkeypatch.setattr("intraday_advisor.engine.is_market_open",
                        lambda *a, **k: False)
    chain = {"atm": 24500, "df": pd.DataFrame({
        "strike": [24400, 24500, 24600],
        "ce_ltp": [200.0] * 3, "pe_ltp": [200.0] * 3,
        "ce_half_spread_pct": [0.01] * 3, "pe_half_spread_pct": [0.01] * 3,
    })}
    assert record_chain_spreads(chain, db_path=db, index_name="NIFTY") == 0
    with connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM spread_samples").fetchone()[0] == 0


def test_a_live_chain_records_the_strikes_around_the_money(db, monkeypatch):
    monkeypatch.setattr("intraday_advisor.engine.is_market_open",
                        lambda *a, **k: True)
    strikes = list(range(24000, 25100, 100))
    chain = {"atm": 24500, "df": pd.DataFrame({
        "strike": strikes,
        "ce_ltp": [200.0] * len(strikes),
        "pe_ltp": [200.0] * len(strikes),
        "ce_half_spread_pct": [0.01] * len(strikes),
        "pe_half_spread_pct": [0.012] * len(strikes),
    })}
    written = record_chain_spreads(chain, db_path=db, index_name="NIFTY",
                                  strikes=2)
    # 5 strikes (ATM +/- 2), both sides.
    assert written == 10
    with connect(db) as conn:
        kept = {r[0] for r in conn.execute(
            "SELECT tradingsymbol FROM spread_samples")}
    assert "NIFTY-24500-CE" in kept
    assert not any("24000" in name for name in kept), "far wings excluded"


def test_a_chain_without_an_atm_records_nothing(db, monkeypatch):
    monkeypatch.setattr("intraday_advisor.engine.is_market_open",
                        lambda *a, **k: True)
    chain = {"df": pd.DataFrame({"strike": [24500],
                                 "ce_half_spread_pct": [0.01],
                                 "pe_half_spread_pct": [0.01]})}
    assert record_chain_spreads(chain, db_path=db, index_name="NIFTY") == 0


def test_recording_a_spread_never_raises_on_a_bad_database(tmp_path):
    bad = str(tmp_path / "no" / "such" / "dir" / "x.sqlite")
    assert record_spread_sample(db_path=bad, index_name="NIFTY",
                                tradingsymbol="X", half_spread_pct=0.01) in (0, 1)


# ============================================================= predictions ==

def test_a_prediction_is_stored_then_resolved(db):
    assert record_prediction(db_path=db, kind="forward_odds", index_name="NIFTY",
                             predicted=0.47, payload={"entry": 194},
                             made_at="2026-11-05T10:00:00+05:30") == 1
    assert resolve_prediction(db_path=db, made_at="2026-11-05T10:00:00+05:30",
                              kind="forward_odds", index_name="NIFTY",
                              realised=1.0) == 1
    with connect(db) as conn:
        row = dict(conn.execute("SELECT * FROM predictions").fetchone())
    assert row["predicted"] == 0.47
    assert row["realised"] == 1.0
    assert row["resolved_at"]


def test_an_unresolved_prediction_keeps_a_null_outcome(db):
    record_prediction(db_path=db, kind="forward_odds", index_name="NIFTY",
                      predicted=0.47)
    with connect(db) as conn:
        assert conn.execute(
            "SELECT realised FROM predictions").fetchone()[0] is None


def test_resolving_an_unknown_prediction_changes_nothing(db):
    assert resolve_prediction(db_path=db, made_at="nope", kind="forward_odds",
                              index_name="NIFTY", realised=1.0) == 0


# ============================================================ daily record ==

def _observations(db, session="2026-11-05"):
    with connect(db) as conn:
        for i, (index, spot) in enumerate(
                [("NIFTY", 24_500.0), ("BANKNIFTY", 57_800.0),
                 ("NIFTY", 24_600.0), ("BANKNIFTY", 57_500.0)]):
            conn.execute(
                "INSERT INTO observations (observed_at, session_date, "
                "index_name, spot, regime, conviction, vix, breadth_pct, "
                "agent_stance) VALUES (?, ?, ?, ?, 'TRENDING_UP', 70, 15.5, "
                "0.6, 'VETO')",
                (f"{session}T10:{i:02d}:00+05:30", session, index, spot))
        conn.commit()


def test_the_record_does_not_pool_two_indices_spots(db):
    """Regression. Mixing NIFTY (~24,500) with BANKNIFTY (~57,800) in one
    series reported a -57.53% move on a day NIFTY rose 0.4%, because the first
    row was one index and the last was the other."""
    import json

    _observations(db)
    record = build_daily_record("2026-11-05", db_path=db)
    assert record["spot_move_pct"] == pytest.approx(0.408, abs=0.01)
    per_index = json.loads(record["per_index"])
    assert per_index["NIFTY"]["move_pct"] > 0
    assert per_index["BANKNIFTY"]["move_pct"] < 0, "both are kept, neither pooled"


def test_the_record_summarises_the_session(db):
    import json

    _observations(db)
    record = build_daily_record("2026-11-05", db_path=db)
    assert record["mean_conviction"] == 70
    assert record["mean_vix"] == 15.5
    assert json.loads(record["regimes"]) == {"TRENDING_UP": 4}
    assert "signals" in record["note"]


def test_rebuilding_a_record_replaces_it_rather_than_duplicating(db):
    _observations(db)
    build_daily_record("2026-11-05", db_path=db)
    build_daily_record("2026-11-05", db_path=db)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_record").fetchone()[0] == 1


def test_a_record_is_rebuildable_because_inputs_improve(db):
    """A session graded later has outcomes it did not have at 15:45, so the row
    must be replaceable rather than append-only."""
    _observations(db)
    first = build_daily_record("2026-11-05", db_path=db)
    assert first["graded"] == 0
    archive_bars(symbols=("^NSEI",), db_path=db, fetch=feed(fake_bars()))
    second = build_daily_record("2026-11-05", db_path=db)
    assert second["bars_archived"] >= first["bars_archived"]


def test_an_unobserved_session_still_produces_a_row(db):
    record = build_daily_record("2026-01-01", db_path=db)
    assert record["session_date"] == "2026-01-01"
    assert record["signals_total"] == 0


def test_daily_records_come_back_oldest_last(db):
    for day in ("2026-11-03", "2026-11-04", "2026-11-05"):
        build_daily_record(day, db_path=db)
    rows = daily_records(db_path=db)
    assert [r["session_date"] for r in rows] == ["2026-11-03", "2026-11-04",
                                                 "2026-11-05"]


# ================================================================ scheduling ==

def test_an_earlier_session_is_always_safe_to_record():
    import datetime as dt

    now = dt.datetime(2026, 11, 5, 10, 0)
    safe, reason = is_safe_to_archive("2026-11-04", now=now)
    assert safe and "earlier" in reason


def test_today_is_not_recorded_before_the_session_settles():
    import datetime as dt

    now = dt.datetime(2026, 11, 5, 11, 0)
    safe, reason = is_safe_to_archive("2026-11-05", now=now)
    assert not safe
    assert ARCHIVE_AFTER.strftime("%H:%M") in reason


def test_today_is_recorded_after_it_settles():
    import datetime as dt

    now = dt.datetime(2026, 11, 5, 16, 0)
    safe, _ = is_safe_to_archive("2026-11-05", now=now)
    assert safe


def test_a_future_session_is_refused():
    import datetime as dt

    safe, reason = is_safe_to_archive(
        "2026-12-01", now=dt.datetime(2026, 11, 5, 16, 0))
    assert not safe and "future" in reason


def test_archive_due_catches_up_on_a_missed_day(db):
    """The case that matters: on 2026-11-05 this machine slept 09:41 to 12:37
    and a session was lost. A job that only ever runs "today at 15:45" loses
    that day's record too."""
    import datetime as dt

    _observations(db, session="2026-11-03")
    _observations(db, session="2026-11-04")
    summary = archive_due(db, now=dt.datetime(2026, 11, 5, 16, 0),
                          with_bars=False)
    assert set(summary["recorded"]) == {"2026-11-03", "2026-11-04"}


def test_archive_due_is_idempotent(db):
    import datetime as dt

    _observations(db, session="2026-11-04")
    now = dt.datetime(2026, 11, 5, 16, 0)
    archive_due(db, now=now, with_bars=False)
    second = archive_due(db, now=now, with_bars=False)
    assert second["recorded"] == []


def test_archive_due_defers_an_unsettled_today(db):
    import datetime as dt

    today = dt.datetime(2026, 11, 5, 11, 0)
    _observations(db, session="2026-11-05")
    summary = archive_due(db, now=today, with_bars=False)
    assert summary["recorded"] == []
    assert summary["deferred"][0]["session"] == "2026-11-05"


def test_archive_due_reports_errors_rather_than_raising(db, monkeypatch):
    """It runs on a background thread in a desk app; a provider hiccup must
    never take the screen down."""
    import datetime as dt

    monkeypatch.setattr("intraday_advisor.archive.archive_bars",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    summary = archive_due(db, now=dt.datetime(2026, 11, 5, 16, 0),
                          with_bars=True)
    assert any("boom" in e for e in summary["errors"])
