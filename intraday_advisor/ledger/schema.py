"""Append-only storage for emitted signals and their graded outcomes.

The gap this closes
-------------------
Before this package the system could emit a signal, display it, and forget it.
``TradeJournal.append_order`` was never called by any surface and the
``trade_journal`` table was never written to, so there was no data anywhere that
could answer the only question that matters: *does a score of 78 actually make
money?*

Two tables, with a hard separation of concerns:

``signals``
    What the engine believed, at the moment it believed it. Written once,
    never updated. Every field needed to reconstruct the decision is captured,
    including the full feature vector and the **closed-bar timestamp** the
    decision was made from. That timestamp is the audit trail against
    look-ahead: a grader may only ever read prices stamped *after* ``bar_time``.

``signal_outcomes``
    What actually happened, written later by the grader. One row per signal.
    Separate table so that regrading (better cost model, fixed bug, new exit
    rule) never mutates the historical record of what was predicted. Delete and
    regrade outcomes freely; signals are immutable.

Dedupe
------
A Streamlit dashboard refreshing every 8 seconds would otherwise write ~450
identical rows for a single 5-minute bar, drowning the sample in duplicates and
making any hit-rate calculation meaningless. ``dedupe_key`` is a UNIQUE column
on (bar_time, index, option_type, strike, engine_version), so repeated
observations of the same setup on the same bar collapse to one row.

Versioning
----------
``engine_version`` and ``grader_version`` are stamped on every row. When the
scorer changes, old signals must not be silently pooled with new ones -- a
calibration fitted across a scoring change is fitted to noise. Reports group by
engine_version by default.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_LEDGER_PATH = Path("data/ledger.sqlite")

# Bump when the meaning of a signal's fields or the scoring changes in a way
# that makes old rows non-comparable with new ones.
ENGINE_VERSION = "options-v2"
# Bump when grading rules change (fill convention, conflict resolution, costs).
GRADER_VERSION = "grader-v1"


SCHEMA = """
PRAGMA journal_mode = WAL;

-- ------------------------------------------------------------------ signals
-- Immutable. One row per (bar, contract, engine version).
CREATE TABLE IF NOT EXISTS signals (
    signal_id        TEXT PRIMARY KEY,
    dedupe_key       TEXT NOT NULL UNIQUE,

    -- when
    emitted_at       TEXT NOT NULL,   -- wall clock the row was written (IST ISO)
    bar_time         TEXT NOT NULL,   -- CLOSED candle the decision used
    interval         TEXT NOT NULL,   -- e.g. '5m'
    session_date     TEXT NOT NULL,   -- trading day, for per-day aggregation

    -- what
    index_name       TEXT NOT NULL,   -- NIFTY / BANKNIFTY
    direction        TEXT NOT NULL,   -- CALL / PUT
    option_type      TEXT NOT NULL,   -- CE / PE
    strike           REAL NOT NULL,
    expiry           TEXT,
    option_token     TEXT,            -- NFO token; required to grade from option candles
    tradingsymbol    TEXT,
    lot_size         INTEGER NOT NULL,

    -- prices at decision time (quoted, not slipped)
    spot             REAL,
    entry_premium    REAL NOT NULL,
    stop_premium     REAL NOT NULL,
    target_premium   REAL NOT NULL,
    target_pct       REAL,
    stop_pct         REAL,

    -- why
    score            INTEGER NOT NULL,
    score_components TEXT,            -- JSON: feature -> signed points
    features         TEXT,            -- JSON: full feature vector at bar_time
    underlying_bias  TEXT,
    chain_bias       TEXT,
    pcr              REAL,
    max_pain         REAL,
    reasons          TEXT,            -- JSON array
    warnings         TEXT,            -- JSON array

    -- sizing context (capital is a runtime input, so record what it was)
    available_capital REAL,
    planned_lots      INTEGER,
    planned_risk      REAL,
    planned_risk_pct  REAL,
    cost_drag         REAL,
    breakeven         REAL,

    -- provenance: anything synthetic must be excluded from every statistic
    data_source      TEXT,
    is_live          INTEGER NOT NULL DEFAULT 0,
    is_synthetic     INTEGER NOT NULL DEFAULT 0,
    engine_version   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_session  ON signals(session_date);
CREATE INDEX IF NOT EXISTS idx_signals_bar      ON signals(bar_time);
CREATE INDEX IF NOT EXISTS idx_signals_index    ON signals(index_name, option_type);
CREATE INDEX IF NOT EXISTS idx_signals_score    ON signals(score);
CREATE INDEX IF NOT EXISTS idx_signals_version  ON signals(engine_version);

-- ----------------------------------------------------------------- outcomes
-- Mutable by the grader only. Safe to delete and rebuild.
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id         TEXT PRIMARY KEY REFERENCES signals(signal_id) ON DELETE CASCADE,
    graded_at         TEXT NOT NULL,
    grader_version    TEXT NOT NULL,

    -- TARGET / STOP / TIMEOUT / NOT_FILLED / NO_DATA / INSUFFICIENT_DATA
    outcome           TEXT NOT NULL,

    -- entry actually achievable on the NEXT bar after the signal bar
    fill_premium      REAL,
    fill_time         TEXT,
    -- how far the quoted signal premium was from an achievable fill. A
    -- persistently positive number means the dashboard was showing prices you
    -- could never get.
    fill_slippage     REAL,

    exit_premium      REAL,
    exit_time         TEXT,
    exit_reason       TEXT,
    bars_held         INTEGER,

    -- excursions: how far it ran for / against before resolving. These are
    -- what tell you whether your stop was too tight or target too far.
    mfe_premium       REAL,
    mae_premium       REAL,
    mfe_pct           REAL,
    mae_pct           REAL,

    -- P&L for ONE lot, always net of the full options cost model
    gross_pnl_per_lot REAL,
    cost_per_lot      REAL,
    net_pnl_per_lot   REAL,
    net_pnl_pct       REAL,           -- on premium deployed
    r_multiple        REAL,           -- net P&L / intended risk

    -- honesty flags
    same_bar_conflict INTEGER NOT NULL DEFAULT 0,  -- stop AND target in one bar
    price_source      TEXT,           -- option_candles / underlying_proxy
    bars_available    INTEGER,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_outcomes_outcome ON signal_outcomes(outcome);
CREATE INDEX IF NOT EXISTS idx_outcomes_version ON signal_outcomes(grader_version);

-- ------------------------------------------------------- cost calibration
-- Real fills from contract notes, used to fit the spread term in the cost
-- model so graded P&L matches reality instead of an assumption.
CREATE TABLE IF NOT EXISTS cost_fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at       TEXT NOT NULL,
    index_name        TEXT,
    tradingsymbol     TEXT,
    quantity          INTEGER NOT NULL,
    entry_premium     REAL NOT NULL,
    exit_premium      REAL NOT NULL,
    actual_total_cost REAL NOT NULL,
    implied_half_spread_pct REAL,
    notes             TEXT
);

-- --------------------------------------------------------------- positions
-- Open and closed positions you actually took. Separate from ``signals``:
-- a signal is what the engine thought, a position is what you did. Keeping
-- them apart is what later allows "did I follow my own system?" to be asked.
CREATE TABLE IF NOT EXISTS positions (
    position_id     TEXT PRIMARY KEY,
    opened_at       TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    index_name      TEXT NOT NULL,
    option_type     TEXT NOT NULL,   -- CE / PE
    strike          REAL NOT NULL,
    lot_size        INTEGER NOT NULL,
    lots            INTEGER NOT NULL,
    entry_premium   REAL NOT NULL,
    target_pct      REAL NOT NULL,
    stop_pct        REAL NOT NULL,
    option_token    TEXT,
    -- Links the position to the signal that prompted it, when there was one.
    signal_id       TEXT,
    status          TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN / CLOSED
    closed_at       TEXT,
    exit_premium    REAL,
    exit_reason     TEXT,
    net_pnl         REAL,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_session ON positions(session_date);

-- --------------------------------------------------------------- reporting
-- Signals joined to outcomes, synthetic data excluded. Every report reads
-- this rather than re-deriving the join (and re-forgetting the filter).
CREATE VIEW IF NOT EXISTS graded_signals AS
SELECT
    s.*,
    o.outcome,
    o.fill_premium,
    o.fill_slippage,
    o.exit_premium,
    o.exit_reason,
    o.bars_held,
    o.mfe_pct,
    o.mae_pct,
    o.gross_pnl_per_lot,
    o.cost_per_lot,
    o.net_pnl_per_lot,
    o.net_pnl_pct,
    o.r_multiple,
    o.same_bar_conflict,
    o.price_source,
    o.grader_version
FROM signals s
JOIN signal_outcomes o ON o.signal_id = s.signal_id
WHERE s.is_synthetic = 0;
"""


def connect(db_path: str | Path = DEFAULT_LEDGER_PATH) -> sqlite3.Connection:
    """Open the ledger with foreign keys on and Row access.

    Foreign keys matter here: deleting a signal must take its outcome with it,
    or reports silently count orphaned outcomes.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_ledger(db_path: str | Path = DEFAULT_LEDGER_PATH) -> None:
    """Create tables, indexes and the reporting view. Idempotent."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def ledger_stats(db_path: str | Path = DEFAULT_LEDGER_PATH) -> dict:
    """Row counts and coverage, for the dashboard header.

    ``ungraded`` is the number worth watching: signals waiting on the grader.

    Creates the schema first, so reporting against a ledger that has never been
    written to returns zeros instead of raising.
    """
    init_ledger(db_path)
    with connect(db_path) as conn:
        def scalar(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        total = scalar("SELECT COUNT(*) FROM signals")
        real = scalar("SELECT COUNT(*) FROM signals WHERE is_synthetic = 0")
        graded = scalar("SELECT COUNT(*) FROM signal_outcomes")
        sessions = scalar("SELECT COUNT(DISTINCT session_date) FROM signals WHERE is_synthetic = 0")
        row = conn.execute(
            "SELECT MIN(session_date), MAX(session_date) FROM signals WHERE is_synthetic = 0"
        ).fetchone()
        first_session, last_session = (row[0], row[1]) if row else (None, None)

    return {
        "total_signals": total,
        "real_signals": real,
        "synthetic_signals": total - real,
        "graded": graded,
        "ungraded": max(0, real - graded),
        "sessions": sessions,
        "first_session": first_session,
        "last_session": last_session,
    }
