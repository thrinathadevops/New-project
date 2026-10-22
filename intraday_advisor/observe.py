"""Record what the market was doing, not just what the system decided.

The gap this closes
-------------------
After three graded sessions the ledger could say what was decided and what
happened to it, and still could not answer the most useful question available:
**what was different about the one day that worked?**

    2026-10-16    34 signals   -Rs 1,222/signal     0% hit target
    2026-10-21   111 signals   -Rs 1,262/signal     9% hit target
    2026-10-22    92 signals     +Rs 478/signal    60% hit target

One session in three was strongly positive and nothing stored says why. Not the
regime, not where volatility sat against its own realised level, not whether
breadth confirmed, not how any of it moved through the day. Without that, a
good day is unexplainable and therefore unrepeatable -- and the next good day
cannot be recognised while it is happening.

Why sampling, rather than one row per trade
-------------------------------------------
A trade is recorded where the system acted, which is a biased slice of the
session: it says nothing about the hours when nothing was taken, and those
hours are most of the day. Sampling every cycle gives a day a **shape**. One
row is cheap, a session is roughly five hundred, and five hundred rows a day is
what makes "which kind of day is this?" a question with an answer.

What is deliberately NOT here
-----------------------------
No judgement. This module records; it does not score, rank or conclude. The
temptation with a new table is to start fitting to it immediately, and three
sessions of anything cannot support a fit -- the between-session spread already
measured is 1.5x the mean. Collect first.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("observe")


def _num(value, default=None):
    """A float, or the default. Never raises, never returns NaN."""
    try:
        if value is None:
            return default
        result = float(value)
        return default if result != result else result
    except (TypeError, ValueError):
        return default


def observation_rows(snapshot: dict, *, open_positions: int = 0,
                      open_pnl: float = 0.0) -> list[dict]:
    """Flatten a snapshot into one row per index.

    Reads only what the engine already publishes, so this costs no extra
    network call -- the whole point is that a sample should be nearly free or
    it will not be taken often enough to be worth having.
    """
    from intraday_advisor.ledger.recorder import now_ist

    moment = now_ist()
    rows: list[dict] = []
    for signal in snapshot.get("signals", []) or []:
        regime = signal.get("regime") or {}
        volatility = regime.get("volatility") or {}
        breadth = regime.get("breadth") or {}
        levels = regime.get("levels") or {}
        iv = signal.get("volatility") or {}
        agent = signal.get("agent") or {}

        rows.append({
            "observed_at": moment.isoformat(timespec="seconds"),
            "session_date": moment.date().isoformat(),
            "index_name": str(signal.get("index") or "").upper(),
            "spot": _num(signal.get("spot")),
            "direction": signal.get("direction"),
            "score": int(signal.get("score") or 0),
            "underlying_score": int(signal.get("underlying_score") or 0),
            "chain_bias": signal.get("chain_bias"),
            "agreement": signal.get("agreement"),
            "agent_stance": agent.get("stance"),
            "regime": regime.get("regime"),
            "conviction": _num(regime.get("conviction")),
            "vix": _num(volatility.get("vix")),
            "breadth_pct": _num(breadth.get("pct")),
            "range_position": _num(levels.get("range_position")),
            "atm_ce_iv": _num(iv.get("atm_ce_iv")),
            "atm_pe_iv": _num(iv.get("atm_pe_iv")),
            "iv_skew": _num(iv.get("skew")),
            "pcr": _num(signal.get("pcr")),
            "max_pain": _num(signal.get("max_pain")),
            "open_positions": int(open_positions),
            "open_pnl": _num(open_pnl, 0.0),
        })
    return rows


def record_observations(
    snapshot: dict, *, db_path: str | Path, open_positions: int = 0,
    open_pnl: float = 0.0,
) -> int:
    """Store one row per index. Returns how many were written.

    ``INSERT OR IGNORE`` on (observed_at, index_name): two samples in the same
    second are the same observation, and a desk that retries a cycle must not
    double-count the market.
    """
    from intraday_advisor.ledger.schema import connect, init_ledger

    rows = observation_rows(snapshot, open_positions=open_positions,
                            open_pnl=open_pnl)
    if not rows:
        return 0

    columns = list(rows[0])
    sql = (
        f"INSERT OR IGNORE INTO observations ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + c for c in columns)})"
    )
    try:
        init_ledger(db_path)
        with connect(db_path) as conn:
            cursor = conn.executemany(sql, rows)
            conn.commit()
            return int(cursor.rowcount or 0)
    except Exception as exc:  # noqa: BLE001 - observing must never break trading
        logger.debug("Could not record observations: %s", exc)
        return 0


def session_shape(
    session_date: str | None = None, *, db_path: str | Path,
) -> dict:
    """Describe one day from its samples: what kind of day was it?

    Deliberately descriptive. It reports how the day moved and where
    volatility, breadth and conviction sat -- it does not say whether that was
    good, because with three sessions nothing can.
    """
    import pandas as pd

    from intraday_advisor.ledger.recorder import now_ist
    from intraday_advisor.ledger.schema import connect, init_ledger

    target = session_date or now_ist().date().isoformat()
    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM observations WHERE session_date = ? "
            "ORDER BY observed_at", (target,)
        )]
    if not rows:
        return {"session": target, "samples": 0,
                "note": "No observations recorded for this session."}

    frame = pd.DataFrame(rows)
    out: dict = {"session": target, "samples": int(len(frame)),
                 "indices": sorted(frame["index_name"].dropna().unique().tolist())}

    first = frame["observed_at"].iloc[0]
    last = frame["observed_at"].iloc[-1]
    out["from"] = first
    out["to"] = last

    per_index: dict = {}
    for name, chunk in frame.groupby("index_name"):
        spot = chunk["spot"].dropna()
        regimes = chunk["regime"].dropna()
        per_index[name] = {
            "samples": int(len(chunk)),
            "spot_open": round(float(spot.iloc[0]), 2) if len(spot) else None,
            "spot_close": round(float(spot.iloc[-1]), 2) if len(spot) else None,
            "spot_move_pct": (
                round(float(spot.iloc[-1] / spot.iloc[0] - 1) * 100, 3)
                if len(spot) > 1 and spot.iloc[0] else None
            ),
            "spot_range_pct": (
                round(float((spot.max() - spot.min()) / spot.iloc[0]) * 100, 3)
                if len(spot) > 1 and spot.iloc[0] else None
            ),
            "regimes_seen": regimes.value_counts().to_dict() if len(regimes) else {},
            "mean_conviction": _round(chunk["conviction"].mean()),
            "mean_vix": _round(chunk["vix"].mean()),
            "mean_breadth": _round(chunk["breadth_pct"].mean()),
            "mean_iv_skew": _round(chunk["iv_skew"].mean()),
            "directions": chunk["direction"].value_counts().to_dict(),
            "stances": chunk["agent_stance"].dropna().value_counts().to_dict(),
        }
    out["per_index"] = per_index
    return out


def _round(value, places: int = 2):
    try:
        if value is None or value != value:
            return None
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


def compare_sessions(*, db_path: str | Path, limit: int = 20) -> "object":
    """One row per session: the shape of every day, side by side.

    This is the table that will eventually answer "what was different about the
    day that worked" -- once there are enough days for the question to have an
    answer rather than a coincidence.
    """
    import pandas as pd

    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM observations ORDER BY observed_at"
        )]
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    grouped = frame.groupby("session_date").agg(
        samples=("observed_at", "size"),
        indices=("index_name", "nunique"),
        mean_score=("score", "mean"),
        mean_conviction=("conviction", "mean"),
        mean_vix=("vix", "mean"),
        mean_breadth=("breadth_pct", "mean"),
        mean_skew=("iv_skew", "mean"),
        mean_range_pos=("range_position", "mean"),
    ).round(2)
    return grouped.tail(limit)
