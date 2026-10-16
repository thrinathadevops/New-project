"""Write emitted signals to the ledger, exactly once each.

Design rules this module enforces:

1. **Append only.** A signal row is written once and never updated. What the
   engine believed at 10:35 stays on record even after the thesis fails. Any
   system that edits its own past predictions cannot be evaluated.

2. **First observation wins.** The option chain keeps moving, so the same
   5-minute bar re-observed 30 seconds later may score differently. The row
   kept is the *first* one, because that is the one you could actually have
   acted on. Later observations of the same bar are dropped, not merged.

3. **Synthetic data is marked, never hidden.** Rows from the synthetic feed are
   stored with ``is_synthetic = 1`` and excluded from every statistic by the
   ``graded_signals`` view. They are kept rather than rejected so that a demo
   session is visibly a demo session instead of a silent gap.

4. **Nothing is invented.** If the option token cannot be resolved, the row is
   written with ``option_token = NULL`` and the grader will mark it
   ``NO_DATA``. A signal that cannot be graded must be visible as such, not
   quietly dropped to flatter the hit rate.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intraday_advisor.scoring import SCORED_FEATURES
from intraday_advisor.ledger.schema import (
    DEFAULT_LEDGER_PATH,
    ENGINE_VERSION,
    connect,
    init_ledger,
)

IST = timezone(timedelta(hours=5, minutes=30))

# Features worth persisting from the analysis row. Kept explicit rather than
# dumping everything, so the stored vector stays stable enough to calibrate on.
# Context columns worth storing that the scorer does not read directly, but
# which any later analysis will want (price level, volatility, liquidity).
_CONTEXT_KEYS = (
    "Close", "Open", "High", "Low", "Volume", "ADTV20",
    "EMA9", "EMA21", "EMA200", "VWAP", "ATR14", "RSI14",
)

#: Everything recorded on each signal: exactly what the scorer scored, plus
#: context. Derived from :data:`~intraday_advisor.scoring.SCORED_FEATURES`
#: rather than hand-maintained, because the hand-maintained version fell 73
#: columns behind the scorer and silently made most weights uncalibratable.
FEATURE_KEYS: tuple[str, ...] = tuple(sorted(set(SCORED_FEATURES) | set(_CONTEXT_KEYS)))


def now_ist() -> datetime:
    return datetime.now(IST)


def _json(value: Any) -> str | None:
    """Serialise to JSON, surviving numpy scalars and dataclasses."""
    if value is None:
        return None
    try:
        return json.dumps(value, default=_fallback)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def _fallback(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    for attr in ("item", "isoformat", "tolist"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return method()
            except Exception:  # noqa: BLE001
                pass
    return str(obj)


def _session_date(bar_time: str) -> str:
    """Trading day for a bar timestamp, as YYYY-MM-DD.

    Falls back to the date portion of the raw string when it cannot be parsed,
    so a malformed timestamp degrades a grouping key rather than losing the row.
    """
    try:
        return datetime.fromisoformat(str(bar_time)).date().isoformat()
    except (TypeError, ValueError):
        return str(bar_time)[:10]


def _dedupe_key(
    bar_time: str, index_name: str, option_type: str, strike: float, engine_version: str
) -> str:
    return f"{bar_time}|{index_name}|{option_type}|{strike:g}|{engine_version}"


def extract_features(row: Any) -> dict:
    """Pull the persisted feature subset out of an analysis row or Series."""
    if row is None:
        return {}
    getter = row.get if hasattr(row, "get") else None
    if getter is None:
        return {}
    out: dict[str, Any] = {}
    for key in FEATURE_KEYS:
        value = getter(key, None)
        if value is None:
            continue
        try:
            if isinstance(value, bool):
                out[key] = bool(value)
            elif isinstance(value, (int, float)):
                out[key] = float(value)
            else:
                item = getattr(value, "item", None)
                out[key] = float(item()) if callable(item) else str(value)
        except (TypeError, ValueError):
            continue
    return out


def record_signal(
    *,
    bar_time: str,
    interval: str,
    index_name: str,
    direction: str,
    option_type: str,
    strike: float,
    lot_size: int,
    entry_premium: float,
    stop_premium: float,
    target_premium: float,
    score: int,
    expiry: str | None = None,
    option_token: str | None = None,
    tradingsymbol: str | None = None,
    spot: float | None = None,
    target_pct: float | None = None,
    stop_pct: float | None = None,
    score_components: dict | None = None,
    features: dict | None = None,
    underlying_bias: str | None = None,
    chain_bias: str | None = None,
    pcr: float | None = None,
    max_pain: float | None = None,
    reasons: list | None = None,
    warnings: list | None = None,
    available_capital: float | None = None,
    planned_lots: int | None = None,
    planned_risk: float | None = None,
    planned_risk_pct: float | None = None,
    cost_drag: float | None = None,
    breakeven: float | None = None,
    data_source: str | None = None,
    is_live: bool = False,
    is_synthetic: bool = False,
    engine_version: str = ENGINE_VERSION,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Record one directional signal. Returns its ``signal_id``.

    Returns ``None`` when the signal is a duplicate of one already stored for
    the same bar and contract -- the caller can treat that as "already known".

    Raises ``ValueError`` for a non-tradable signal (no direction, or a
    non-positive entry premium), because storing one would create a row the
    grader can never resolve.
    """
    direction = (direction or "").strip().upper()
    if direction not in ("CALL", "PUT"):
        raise ValueError(
            f"Only CALL/PUT signals are recordable, got {direction!r}. "
            f"WAIT means no trade and has nothing to grade."
        )
    if entry_premium is None or float(entry_premium) <= 0:
        raise ValueError("entry_premium must be positive to record a signal")

    bar_time = str(bar_time)
    signal_id = uuid.uuid4().hex
    key = _dedupe_key(bar_time, index_name, option_type, float(strike), engine_version)

    payload = {
        "signal_id": signal_id,
        "dedupe_key": key,
        "emitted_at": now_ist().isoformat(timespec="seconds"),
        "bar_time": bar_time,
        "interval": interval,
        "session_date": _session_date(bar_time),
        "index_name": (index_name or "").upper(),
        "direction": direction,
        "option_type": (option_type or "").upper(),
        "strike": float(strike),
        "expiry": expiry,
        "option_token": str(option_token) if option_token else None,
        "tradingsymbol": tradingsymbol,
        "lot_size": int(lot_size),
        "spot": float(spot) if spot is not None else None,
        "entry_premium": float(entry_premium),
        "stop_premium": float(stop_premium),
        "target_premium": float(target_premium),
        "target_pct": float(target_pct) if target_pct is not None else None,
        "stop_pct": float(stop_pct) if stop_pct is not None else None,
        "score": int(score),
        "score_components": _json(score_components),
        "features": _json(features),
        "underlying_bias": underlying_bias,
        "chain_bias": chain_bias,
        "pcr": float(pcr) if pcr is not None else None,
        "max_pain": float(max_pain) if max_pain is not None else None,
        "reasons": _json(reasons),
        "warnings": _json(warnings),
        "available_capital": float(available_capital) if available_capital is not None else None,
        "planned_lots": int(planned_lots) if planned_lots is not None else None,
        "planned_risk": float(planned_risk) if planned_risk is not None else None,
        "planned_risk_pct": float(planned_risk_pct) if planned_risk_pct is not None else None,
        "cost_drag": float(cost_drag) if cost_drag is not None else None,
        "breakeven": float(breakeven) if breakeven is not None else None,
        "data_source": data_source,
        "is_live": 1 if is_live else 0,
        "is_synthetic": 1 if is_synthetic else 0,
        "engine_version": engine_version,
    }

    columns = ", ".join(payload)
    placeholders = ", ".join(f":{c}" for c in payload)
    sql = f"INSERT OR IGNORE INTO signals ({columns}) VALUES ({placeholders})"

    own_conn = conn is None
    if own_conn:
        init_ledger(db_path)
        conn = connect(db_path)
    try:
        cursor = conn.execute(sql, payload)
        if own_conn:
            conn.commit()
        # rowcount 0 => the UNIQUE dedupe_key already existed.
        return signal_id if cursor.rowcount else None
    finally:
        if own_conn:
            conn.close()


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a dataclass-like object or a mapping.

    The dashboards pass ``IndexOptionSignal.as_dict()`` through Streamlit's
    cache (dataclasses do not survive it cleanly), while the CLI passes the
    object itself. Supporting both keeps one recording path.
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def record_index_signal(
    signal: Any,
    *,
    bar_time: str,
    interval: str = "5m",
    lot_size: int | None = None,
    features: dict | None = None,
    score_components: dict | None = None,
    option_token: str | None = None,
    expiry: str | None = None,
    tradingsymbol: str | None = None,
    plan: Any = None,
    available_capital: float | None = None,
    is_synthetic: bool = False,
    engine_version: str = ENGINE_VERSION,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Adapter for :class:`~intraday_advisor.index_options.IndexOptionSignal`.

    Returns ``None`` for a WAIT signal (nothing to grade) or a duplicate.
    ``plan`` is an optional :class:`~intraday_advisor.ledger.sizing.PositionPlan`
    whose sizing context is recorded alongside the signal.

    ``bar_time`` must be passed explicitly: ``IndexOptionSignal.timestamp`` is
    wall-clock emission time, not the closed bar the decision came from, and
    grading against the wrong timestamp is how look-ahead creeps in.
    """
    direction = _field(signal, "direction", "WAIT")
    if direction not in ("CALL", "PUT"):
        return None
    if float(_field(signal, "entry_premium", 0) or 0) <= 0:
        return None

    index_name = _field(signal, "index", "")
    if lot_size is None:
        from intraday_advisor.ledger.sizing import lot_size_for
        lot_size = lot_size_for(index_name)

    return record_signal(
        bar_time=bar_time,
        interval=interval,
        index_name=index_name,
        direction=direction,
        option_type=_field(signal, "option_type", ""),
        strike=float(_field(signal, "strike", 0) or 0),
        lot_size=int(lot_size),
        entry_premium=float(_field(signal, "entry_premium")),
        stop_premium=float(_field(signal, "stop_premium")),
        target_premium=float(_field(signal, "target_premium")),
        score=int(_field(signal, "confidence", 0) or 0),
        expiry=expiry,
        option_token=option_token,
        tradingsymbol=tradingsymbol,
        spot=float(_field(signal, "spot", 0) or 0),
        target_pct=_field(signal, "target_pct"),
        stop_pct=_field(signal, "stop_pct"),
        score_components=score_components,
        features=features,
        underlying_bias=_field(signal, "underlying_bias"),
        chain_bias=_field(signal, "chain_bias"),
        pcr=_field(signal, "pcr"),
        max_pain=_field(signal, "max_pain"),
        reasons=list(_field(signal, "reasons", []) or []),
        warnings=list(_field(signal, "warnings", []) or []),
        available_capital=available_capital,
        planned_lots=getattr(plan, "lots", None),
        planned_risk=getattr(plan, "risk_amount", None),
        planned_risk_pct=getattr(plan, "risk_pct_of_capital", None),
        cost_drag=getattr(plan, "cost_drag", None),
        breakeven=getattr(plan, "breakeven", None),
        data_source=_field(signal, "data_source"),
        is_live=bool(_field(signal, "is_live", False)),
        is_synthetic=is_synthetic,
        engine_version=engine_version,
        db_path=db_path,
        conn=conn,
    )


def record_cost_fill(
    *,
    quantity: int,
    entry_premium: float,
    exit_premium: float,
    actual_total_cost: float,
    index_name: str | None = None,
    tradingsymbol: str | None = None,
    notes: str | None = None,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
) -> float:
    """Store one real round trip from a contract note and return the implied
    half-spread, so the cost model can be fitted to your actual fills.
    """
    from intraday_advisor.ledger.costs import OptionsCostModel, calibrate_from_fill

    fitted = calibrate_from_fill(
        entry_premium, exit_premium, quantity, actual_total_cost,
        costs=OptionsCostModel(),
    )
    init_ledger(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cost_fills (
                recorded_at, index_name, tradingsymbol, quantity,
                entry_premium, exit_premium, actual_total_cost,
                implied_half_spread_pct, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_ist().isoformat(timespec="seconds"),
                index_name, tradingsymbol, int(quantity),
                float(entry_premium), float(exit_premium), float(actual_total_cost),
                float(fitted.half_spread_pct), notes,
            ),
        )
    return fitted.half_spread_pct


def fitted_cost_model(db_path: str | Path = DEFAULT_LEDGER_PATH):
    """Cost model using the median implied half-spread from recorded fills.

    Median rather than mean: one fat-fingered entry in a thin contract should
    not redefine your assumed execution quality. Falls back to defaults when
    fewer than three fills have been recorded, because two data points are not
    a spread estimate.
    """
    from statistics import median

    from intraday_advisor.ledger.costs import OptionsCostModel

    default = OptionsCostModel()
    try:
        with connect(db_path) as conn:
            rows = conn.execute(
                "SELECT implied_half_spread_pct FROM cost_fills "
                "WHERE implied_half_spread_pct IS NOT NULL"
            ).fetchall()
    except sqlite3.Error:
        return default

    values = [float(r[0]) for r in rows if r[0] is not None]
    if len(values) < 3:
        return default
    return default.with_measured_spread(median(values))
