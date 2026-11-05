"""Keep today's data, because most of it expires or is thrown away.

Why this exists
---------------
The standing problem in this project is sample size. ``ledger_cli sessions``
says it plainly: 7 sessions recorded, **about 25 needed** before the average is
twice its own margin of error. Every statistical conclusion so far has been
withdrawn or weakened for that reason.

Meanwhile a great deal was measured every refresh and almost none of it kept.
One item was not merely discarded but **destroyed**: Yahoo serves roughly 60
days of 5-minute history, so every day that passed removed a day from the only
source of it. ``data.store_ohlcv_sqlite`` existed for exactly this and had zero
callers, which meant the one piece of data that cannot be rebuilt later at any
price was the one piece nobody kept.

So this module runs once per day and writes four things:

* **bars** -- the raw candles, before they expire at the source;
* **spread samples** -- the measured half-spread, which the cost model needs
  and which was recomputed and dropped on every single refresh;
* **a daily record** -- one consolidated row per session, so "what was
  different about the day that worked" is a comparison rather than a scan;
* **predictions** -- a stated probability beside what actually happened, which
  is the only way conviction or forward odds can ever be calibrated.

Nothing here is stored without a consumer in :mod:`intraday_advisor.calibration`.
Storage without a consumer is a landfill.

How it schedules
----------------
Deliberately modelled on :mod:`intraday_advisor.auto_grade`, which already
solved this: a module-level lock so two threads cannot run it at once, a
**bounded** catch-up so a long-idle ledger cannot turn a dashboard start into
hundreds of fetches, and it **never raises** -- it runs on a background thread
in a desk application and a provider hiccup must not take the screen down.

Catching up matters more than it looks. On 2026-11-05 this machine slept from
09:41 to 12:37 IST and the first half of a session was lost. A daily job that
only ever runs "today at 15:45" would have lost that day's archive too; one
that catches up recovers whatever the source still holds.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import time as dt_time
from pathlib import Path

logger = logging.getLogger("archive")

_LOCK = threading.Lock()

#: Archive after this time, for the same reason the grader waits: the session
#: has to have settled and the provider's last candles have to have landed.
ARCHIVE_AFTER = dt_time(15, 45)

#: How many earlier sessions to build records for in one pass. A bound, so a
#: long-idle ledger cannot turn a start into a hundred provider calls.
MAX_CATCH_UP_SESSIONS = 10

#: Yahoo's 5-minute window. Asking for more returns nothing useful, and asking
#: for this much is what makes the first run a backfill of everything still
#: recoverable.
BACKFILL_PERIOD = "60d"

DEFAULT_INTERVAL = "5m"


@dataclass
class ArchiveState:
    """What the archiver has done, for the log and the dashboard header."""

    enabled: bool = True
    last_attempt: str | None = None
    last_success: str | None = None
    bars_written: int = 0
    sessions_recorded: list = field(default_factory=list)
    last_error: str | None = None
    running: bool = False

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


_STATE = ArchiveState()


def state() -> dict:
    with _LOCK:
        return _STATE.as_dict()


def disable() -> None:
    """Switch the archiver off, for tests and for a read-only run."""
    with _LOCK:
        _STATE.enabled = False


def enable() -> None:
    with _LOCK:
        _STATE.enabled = True


def _now_ist():
    from intraday_advisor.ledger.recorder import now_ist
    return now_ist()


def archive_symbols() -> tuple:
    """The two indices plus the breadth basket.

    The basket is reused rather than redeclared: it is already the list the
    regime check downloads every five minutes, so archiving it adds history for
    names the system is reading anyway, at no extra network cost on a backfill.
    """
    from intraday_advisor.index_options import INDEX_CONFIG
    from intraday_advisor.regime import BREADTH_BASKET

    indices = tuple(config["yahoo"] for config in INDEX_CONFIG.values())
    return tuple(dict.fromkeys(indices + tuple(BREADTH_BASKET)))


# ===================================================================== bars ==

def archive_bars(
    symbols=None, *, db_path, interval: str = DEFAULT_INTERVAL,
    period: str = BACKFILL_PERIOD, fetch=None,
) -> dict:
    """Store candles for each symbol. Returns counts per symbol.

    ``INSERT OR IGNORE`` on the canonical key, so re-running costs nothing and
    cannot duplicate. ``fetch`` is injectable for tests; by default it reuses
    :func:`intraday_advisor.data.fetch_yahoo_ohlcv`, which already cleans and
    sorts the frame.
    """
    from intraday_advisor.ledger.recorder import canonical_bar_time
    from intraday_advisor.ledger.schema import connect, init_ledger

    if fetch is None:
        from intraday_advisor.data import fetch_yahoo_ohlcv as fetch

    names = tuple(symbols) if symbols is not None else archive_symbols()
    init_ledger(db_path)
    written: dict = {}
    for symbol in names:
        try:
            frame = fetch(symbol, period=period, interval=interval)
        except Exception as exc:  # noqa: BLE001 - one symbol must not stop the set
            logger.debug("No candles for %s: %s", symbol, exc)
            written[symbol] = 0
            continue
        if frame is None or len(frame) == 0:
            written[symbol] = 0
            continue

        rows = []
        for stamp, row in frame.iterrows():
            # Canonical IST before the key. The raw index repr is UTC or IST
            # depending on which fetch answered, and keying on it is precisely
            # the bug that duplicated half of three sessions in `signals`.
            bar_time = canonical_bar_time(stamp)
            try:
                rows.append({
                    "symbol": symbol, "bar_time": bar_time,
                    "interval": interval, "session_date": bar_time[:10],
                    "open": float(row["Open"]), "high": float(row["High"]),
                    "low": float(row["Low"]), "close": float(row["Close"]),
                    "volume": float(row.get("Volume", 0) or 0),
                })
            except (TypeError, ValueError, KeyError):
                continue

        if not rows:
            written[symbol] = 0
            continue
        try:
            with connect(db_path) as conn:
                cursor = conn.executemany(
                    "INSERT OR IGNORE INTO bars (symbol, bar_time, interval, "
                    "session_date, open, high, low, close, volume) VALUES "
                    "(:symbol, :bar_time, :interval, :session_date, :open, "
                    ":high, :low, :close, :volume)", rows)
                conn.commit()
                written[symbol] = int(cursor.rowcount or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not write bars for %s: %s", symbol, exc)
            written[symbol] = 0
    return written


def stored_bars(symbol: str, *, db_path, interval: str = DEFAULT_INTERVAL,
                sessions: int | None = None):
    """Archived candles for one symbol as an OHLCV frame, oldest first.

    Returns an empty frame when nothing is stored, so a caller can fall back to
    a live fetch without special-casing ``None``.
    """
    import pandas as pd

    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    sql = ("SELECT bar_time, open, high, low, close, volume FROM bars "
           "WHERE symbol = ? AND interval = ? ORDER BY bar_time")
    params: tuple = (symbol, interval)
    if sessions:
        sql = (
            "SELECT bar_time, open, high, low, close, volume FROM bars "
            "WHERE symbol = ? AND interval = ? AND session_date IN ("
            "  SELECT session_date FROM bars WHERE symbol = ? AND interval = ? "
            "  GROUP BY session_date ORDER BY session_date DESC LIMIT ?"
            ") ORDER BY bar_time"
        )
        params = (symbol, interval, symbol, interval, int(sessions))
    with connect(db_path) as conn:
        rows = [tuple(r) for r in conn.execute(sql, params)]
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    frame = pd.DataFrame(rows, columns=["bar_time", "Open", "High", "Low",
                                        "Close", "Volume"])
    # `.dt.tz_convert`, not `.tz_convert`: to_datetime on a Series returns a
    # Series, and Series.tz_convert operates on the INDEX -- which is still the
    # default RangeIndex here, so it raised "index is not a valid
    # DatetimeIndex" and every realised-vol figure came back empty.
    stamps = pd.to_datetime(frame.pop("bar_time"), utc=True, errors="coerce")
    frame.index = stamps.dt.tz_convert("Asia/Kolkata")
    return frame.dropna(subset=["Open", "High", "Low", "Close"])


def bar_coverage(*, db_path, interval: str = DEFAULT_INTERVAL) -> dict:
    """How much history is held, per symbol. The answer to "is this worth it yet"."""
    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT symbol, COUNT(*) bars, COUNT(DISTINCT session_date) sessions, "
            "MIN(session_date) first_session, MAX(session_date) last_session "
            "FROM bars WHERE interval = ? GROUP BY symbol ORDER BY symbol",
            (interval,))]
        total = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT session_date) FROM bars "
            "WHERE interval = ?", (interval,)).fetchone()
    return {"interval": interval, "per_symbol": rows,
            "bars": int(total[0] or 0), "sessions": int(total[1] or 0)}


# ========================================================= spread samples ==

def record_spread_sample(
    *, db_path, index_name: str, tradingsymbol: str, half_spread_pct: float,
    premium: float | None = None, measured: bool = True, observed_at=None,
) -> int:
    """Store one measured half-spread. Returns rows written (0 or 1).

    Called where the book is already parsed, so it costs no network call. Never
    raises: recording the cost of a trade must not be able to prevent it.
    """
    from intraday_advisor.ledger.schema import connect, init_ledger

    try:
        spread = float(half_spread_pct)
    except (TypeError, ValueError):
        return 0
    if spread != spread or spread < 0:        # NaN or nonsense
        return 0

    moment = observed_at or _now_ist()
    stamp = (moment if isinstance(moment, str)
             else moment.isoformat(timespec="seconds"))
    try:
        hour = int(str(stamp)[11:13])
    except (TypeError, ValueError):
        hour = None

    try:
        init_ledger(db_path)
        with connect(db_path) as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO spread_samples (observed_at, "
                "session_date, index_name, tradingsymbol, hour, "
                "half_spread_pct, premium, measured) VALUES (?,?,?,?,?,?,?,?)",
                (stamp, str(stamp)[:10], (index_name or "").upper(),
                 tradingsymbol or "", hour, spread,
                 float(premium) if premium is not None else None,
                 1 if measured else 0))
            conn.commit()
            return int(cursor.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not record a spread sample: %s", exc)
        return 0


def record_chain_spreads(
    chain: dict, *, db_path, index_name: str, strikes: int = 2,
    observed_at=None,
) -> int:
    """Store the measured half-spread for the strikes around the money.

    Only the ATM and ``strikes`` either side, both CE and PE. The far wings are
    never traded here and recording all 42 contracts every cycle would write
    tens of thousands of rows a day to calibrate an hourly mean that a handful
    of near-the-money observations already pins.

    Returns rows written. Writes **nothing** when the spread columns are absent,
    which is the case on the synthetic and LTP-only fallbacks: a calibration
    built from invented books would be worse than the published prior it
    replaced.

    It also writes nothing **outside market hours**, and that guard was earned.
    Angel keeps serving last-traded depth after the close, so the first run of
    this recorded 20 samples at 21:20 -- nine hours after the 15:30 close -- all
    stamped hour 21. A spread you cannot trade at is not evidence about cost,
    and because ``spread_multiplier_at`` snaps an out-of-session hour onto the
    nearest in-session one, those rows would have been folded into the closing
    hour's calibration. The same trap the engine already documents for
    ``is_live``: the broker answering is not the market being open.
    """
    try:
        from intraday_advisor.engine import is_market_open

        if not is_market_open():
            return 0
    except Exception:  # noqa: BLE001 - if the clock cannot be read, do not guess
        return 0
    try:
        frame = chain.get("df") if isinstance(chain, dict) else None
        if frame is None or len(frame) == 0:
            return 0
        if "ce_half_spread_pct" not in frame.columns:
            return 0          # not a live FULL-mode book; nothing real to store
        atm = chain.get("atm") or chain.get("rec_strike")
        if atm is None:
            return 0

        ordered = frame.copy()
        ordered["__distance"] = (ordered["strike"] - float(atm)).abs()
        near = ordered.nsmallest(max(1, strikes * 2 + 1), "__distance")

        written = 0
        for _, row in near.iterrows():
            strike = row.get("strike")
            for side, column in (("CE", "ce_half_spread_pct"),
                                 ("PE", "pe_half_spread_pct")):
                value = row.get(column)
                if value is None or value != value or float(value) <= 0:
                    continue
                premium = row.get("ce_ltp" if side == "CE" else "pe_ltp")
                written += record_spread_sample(
                    db_path=db_path, index_name=index_name,
                    tradingsymbol=f"{index_name}-{strike:g}-{side}",
                    half_spread_pct=float(value), premium=premium,
                    observed_at=observed_at,
                )
        return written
    except Exception as exc:  # noqa: BLE001 - measuring cost must not block trading
        logger.debug("Could not record chain spreads for %s: %s",
                     index_name, exc)
        return 0


# ========================================================== predictions ==

def record_prediction(
    *, db_path, kind: str, index_name: str, predicted: float,
    payload: dict | None = None, made_at=None, session_date: str | None = None,
) -> int:
    """Store a stated probability so it can be checked later.

    ``kind`` is free text, e.g. ``"forward_odds"`` or ``"conviction"``. The
    realised side is filled in by :func:`resolve_prediction` once the answer is
    known, which keeps the prediction immutable in the meantime -- the same
    append-only discipline the signals table follows.
    """
    from intraday_advisor.ledger.schema import connect, init_ledger

    try:
        value = float(predicted)
    except (TypeError, ValueError):
        return 0
    moment = made_at or _now_ist()
    stamp = (moment if isinstance(moment, str)
             else moment.isoformat(timespec="seconds"))
    try:
        init_ledger(db_path)
        with connect(db_path) as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO predictions (made_at, session_date, "
                "kind, index_name, predicted, payload) VALUES (?,?,?,?,?,?)",
                (stamp, session_date or str(stamp)[:10], kind,
                 (index_name or "").upper(), value,
                 json.dumps(payload or {}, default=str)))
            conn.commit()
            return int(cursor.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not record a prediction: %s", exc)
        return 0


def resolve_prediction(
    *, db_path, made_at: str, kind: str, index_name: str, realised: float,
) -> int:
    """Fill in what actually happened. Returns rows updated."""
    from intraday_advisor.ledger.schema import connect, init_ledger

    try:
        value = float(realised)
    except (TypeError, ValueError):
        return 0
    try:
        init_ledger(db_path)
        with connect(db_path) as conn:
            cursor = conn.execute(
                "UPDATE predictions SET realised = ?, resolved_at = ? "
                "WHERE made_at = ? AND kind = ? AND index_name = ?",
                (value, _now_ist().isoformat(timespec="seconds"), made_at,
                 kind, (index_name or "").upper()))
            conn.commit()
            return int(cursor.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not resolve a prediction: %s", exc)
        return 0


# ========================================================== daily record ==

def build_daily_record(session_date: str, *, db_path, events: dict | None = None) -> dict:
    """Consolidate one session into a single row, and store it.

    Reads what is already recorded rather than refetching anything. Rebuilding
    a day is safe: the row is replaced, because the inputs can legitimately
    improve (a session graded later has outcomes it did not have at 15:45).
    """
    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    record: dict = {"session_date": session_date,
                    "built_at": _now_ist().isoformat(timespec="seconds")}
    with connect(db_path) as conn:
        obs = [dict(r) for r in conn.execute(
            "SELECT index_name, regime, conviction, vix, breadth_pct, "
            "agent_stance, spot FROM observations WHERE session_date = ? "
            "ORDER BY observed_at", (session_date,))]
        counts = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN is_synthetic = 0 THEN 1 ELSE 0 END) "
            "FROM signals WHERE session_date = ?", (session_date,)).fetchone()
        graded = conn.execute(
            "SELECT COUNT(*), AVG(o.net_pnl_per_lot), "
            "SUM(CASE WHEN o.outcome = 'TARGET' THEN 1 ELSE 0 END) "
            "FROM signals s JOIN signal_outcomes o ON o.signal_id = s.signal_id "
            "WHERE s.session_date = ? AND s.is_synthetic = 0",
            (session_date,)).fetchone()
        stances = {r[0] or "UNKNOWN": r[1] for r in conn.execute(
            "SELECT agent_stance, COUNT(*) FROM signals WHERE session_date = ? "
            "GROUP BY 1", (session_date,))}
        bars = conn.execute(
            "SELECT COUNT(*) FROM bars WHERE session_date = ?",
            (session_date,)).fetchone()[0]

    def mean(key):
        values = [o[key] for o in obs if o.get(key) is not None]
        return round(sum(values) / len(values), 3) if values else None

    regimes: dict = {}
    for row in obs:
        if row.get("regime"):
            regimes[row["regime"]] = regimes.get(row["regime"], 0) + 1

    # Per index, never pooled. Mixing NIFTY (~24,500) with BANKNIFTY (~57,800)
    # in one series reported a -57% move on a day NIFTY barely moved, because
    # the first row was one index and the last was the other.
    per_index: dict = {}
    for name in {o.get("index_name") for o in obs if o.get("index_name")}:
        series = [o["spot"] for o in obs
                  if o.get("index_name") == name and o.get("spot")]
        if len(series) > 1 and series[0]:
            per_index[name] = {
                "move_pct": round((series[-1] / series[0] - 1) * 100, 3),
                "range_pct": round((max(series) - min(series)) / series[0] * 100, 3),
                "open": round(series[0], 2), "close": round(series[-1], 2),
            }
    spots = [o["spot"] for o in obs
             if o.get("index_name") == "NIFTY" and o.get("spot")]
    record["per_index"] = json.dumps(per_index)
    record["regimes"] = json.dumps(regimes)
    record["mean_conviction"] = mean("conviction")
    record["mean_vix"] = mean("vix")
    record["mean_breadth"] = mean("breadth_pct")
    # The headline columns are NIFTY's; `per_index` carries every index, so
    # neither is lost and the scalar is unambiguous.
    nifty = per_index.get("NIFTY") or {}
    record["spot_move_pct"] = nifty.get("move_pct")
    record["spot_range_pct"] = nifty.get("range_pct")
    record["realised_vol"] = _realised_vol(session_date, db_path=db_path)
    record["signals_total"] = int(counts[0] or 0)
    record["signals_live"] = int(counts[1] or 0)
    record["graded"] = int(graded[0] or 0)
    record["net_pnl_per_lot"] = (round(float(graded[1]), 2)
                                 if graded[1] is not None else None)
    record["hit_target"] = int(graded[2] or 0)
    record["stances"] = json.dumps(stances)
    record["events"] = json.dumps(events or {}, default=str)
    record["measured_hours"] = json.dumps(_measured_hours(db_path=db_path))
    record["bars_archived"] = int(bars or 0)
    record["note"] = (
        f"{record['signals_total']} signals ({record['signals_live']} live), "
        f"{record['graded']} graded, {record['bars_archived']} bars archived."
    )

    columns = list(record)
    with connect(db_path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO daily_record ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + c for c in columns)})", record)
        conn.commit()
    return record


def _realised_vol(session_date: str, *, db_path) -> float | None:
    """Annualised realised volatility for NIFTY that day, from the archive.

    Garman-Klass on 5-minute bars, ``session_aware`` so the overnight gap is
    not annualised as if it were a five-minute move -- which
    :mod:`intraday_advisor.volatility` calls the single biggest way to get a
    nonsense number out of it. A day with too few bars returns None rather
    than a figure computed from a fragment.
    """
    try:
        from intraday_advisor.index_options import INDEX_CONFIG
        from intraday_advisor.volatility import realized_volatility

        symbol = INDEX_CONFIG["NIFTY"]["yahoo"]
        frame = stored_bars(symbol, db_path=db_path)
        if frame is None or frame.empty:
            return None
        day = frame[frame.index.strftime("%Y-%m-%d") == session_date]
        if len(day) < 10:
            return None
        result = realized_volatility(day, method="garman_klass",
                                     bar_size="5m", session_aware=True)
        if not result.usable:
            return None
        return round(float(result.annualized_pct), 3)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Realised vol unavailable for %s: %s", session_date, exc)
        return None


def _measured_hours(*, db_path) -> list:
    """Which hours currently have enough spread samples to be measured."""
    try:
        from intraday_advisor.calibration import measured_hours
        return measured_hours(db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Measured hours unavailable: %s", exc)
        return []


# ================================================================ scheduling ==

def sessions_awaiting_record(db_path, *, limit: int = MAX_CATCH_UP_SESSIONS) -> list:
    """Session dates with signals or observations but no daily record yet."""
    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT session_date FROM ("
            "  SELECT session_date FROM signals WHERE is_synthetic = 0 "
            "  UNION SELECT session_date FROM observations"
            ") WHERE session_date NOT IN (SELECT session_date FROM daily_record) "
            "ORDER BY session_date LIMIT ?", (int(limit),)).fetchall()
    return [r[0] for r in rows]


def is_safe_to_archive(session_date: str, *, now=None) -> tuple:
    """Whether a session has settled enough to record. Same logic as grading.

    An earlier session is always safe. Today is safe only after
    :data:`ARCHIVE_AFTER`, so a record is not built from half a day and then
    frozen -- which is why the record is replaceable rather than append-only.
    """
    moment = now or _now_ist()
    today = moment.date().isoformat()
    if session_date < today:
        return True, "an earlier session, already complete"
    if session_date > today:
        return False, "in the future"
    if moment.time() >= ARCHIVE_AFTER:
        return True, f"past {ARCHIVE_AFTER.strftime('%H:%M')}, the session has settled"
    return False, f"today, and it is before {ARCHIVE_AFTER.strftime('%H:%M')}"


def archive_due(
    db_path, *, now=None, limit: int = MAX_CATCH_UP_SESSIONS,
    with_bars: bool = True,
) -> dict:
    """Archive bars and build any outstanding daily record.

    Returns a summary rather than raising: this runs on a background thread in
    a desk application, and a provider hiccup must never take the screen down.
    """
    with _LOCK:
        if not _STATE.enabled:
            return {"skipped": "disabled"}
        if _STATE.running:
            return {"skipped": "already running"}
        _STATE.running = True
        _STATE.last_attempt = (now or _now_ist()).isoformat(timespec="seconds")

    summary: dict = {"bars": {}, "recorded": [], "deferred": [], "errors": []}
    try:
        if with_bars:
            try:
                summary["bars"] = archive_bars(db_path=db_path)
                written = sum(summary["bars"].values())
                with _LOCK:
                    _STATE.bars_written += written
                if written:
                    logger.info("Archived %s new bars", written)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"bars: {exc}")

        for session_date in sessions_awaiting_record(db_path, limit=limit):
            safe, reason = is_safe_to_archive(session_date, now=now)
            if not safe:
                summary["deferred"].append({"session": session_date,
                                            "reason": reason})
                continue
            try:
                record = build_daily_record(session_date, db_path=db_path)
                summary["recorded"].append(session_date)
                logger.info("Daily record for %s: %s", session_date,
                            record.get("note"))
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"{session_date}: {exc}")

        with _LOCK:
            _STATE.sessions_recorded.extend(summary["recorded"])
            if not summary["errors"]:
                _STATE.last_success = _now_ist().isoformat(timespec="seconds")
            else:
                _STATE.last_error = "; ".join(summary["errors"])[:300]
    finally:
        with _LOCK:
            _STATE.running = False
    return summary


def daily_records(*, db_path, limit: int = 60) -> list:
    """The stored day rows, newest last. The table days are compared across."""
    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM daily_record ORDER BY session_date DESC LIMIT ?",
            (int(limit),))]
    return list(reversed(rows))


def render_coverage(coverage: dict) -> str:
    """Plain text: how much history is held, and whether it beats the source."""
    lines = ["=" * 68,
             f"ARCHIVED BARS  ({coverage.get('interval')})",
             "=" * 68,
             f"  bars     : {coverage.get('bars', 0):,}",
             f"  sessions : {coverage.get('sessions', 0)}"]
    rows = coverage.get("per_symbol") or []
    if rows:
        lines += ["", f"  {'symbol':16} {'bars':>8} {'sessions':>9}  range"]
        for row in rows:
            lines.append(
                f"  {row['symbol']:16} {row['bars']:8,} {row['sessions']:9}  "
                f"{row['first_session']} .. {row['last_session']}")
    if coverage.get("sessions", 0) > 60:
        lines += ["", "  Past Yahoo's 60-day window: this archive now holds "
                      "history the source no longer serves."]
    else:
        lines += ["", f"  Yahoo still serves ~60 days, so nothing is lost yet. "
                      f"Every day from here adds one the source will drop."]
    lines.append("=" * 68)
    return "\n".join(lines)
