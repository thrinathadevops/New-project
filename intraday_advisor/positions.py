"""Open-position store and live evaluation.

A *signal* is what the engine thought. A *position* is what you actually did.
They are stored separately so that later you can ask the question that no
indicator can answer: did I follow my own system, and did deviating help?

Positions live in the ledger database rather than in Streamlit session state,
which means they survive a browser refresh, a server restart, and being viewed
from a second device. The previous dashboard lost them on every rerun.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from intraday_advisor.ledger.costs import OptionsCostModel, net_pnl
from intraday_advisor.ledger.recorder import now_ist
from intraday_advisor.ledger.schema import DEFAULT_LEDGER_PATH, connect, init_ledger
from intraday_advisor.position_monitor import PositionStatus, evaluate_position


@dataclass
class LivePosition:
    """A stored position joined to its current market value."""

    position_id: str
    index_name: str
    option_type: str
    strike: float
    lot_size: int
    lots: int
    quantity: int
    entry_premium: float
    target_pct: float
    stop_pct: float
    target_premium: float
    stop_premium: float
    opened_at: str
    option_token: str | None = None
    signal_id: str | None = None

    # live, refreshed each snapshot
    current_premium: float | None = None
    premium_source: str = "unknown"
    status: str = "NO_DATA"
    action: str = "—"
    message: str = ""
    is_alarm: bool = False
    pnl_pct: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    cost_to_exit: float = 0.0
    deployed: float = 0.0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def open_position(
    *,
    index_name: str,
    option_type: str,
    strike: float,
    lot_size: int,
    lots: int,
    entry_premium: float,
    target_pct: float = 0.25,
    stop_pct: float = 0.20,
    option_token: str | None = None,
    signal_id: str | None = None,
    notes: str | None = None,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
) -> str:
    """Record a position you have taken. Returns its id."""
    if lots < 1 or lot_size < 1:
        raise ValueError("lots and lot_size must both be at least 1")
    if entry_premium <= 0:
        raise ValueError("entry_premium must be positive")

    position_id = uuid.uuid4().hex
    opened = now_ist()
    init_ledger(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO positions (
                position_id, opened_at, session_date, index_name, option_type,
                strike, lot_size, lots, entry_premium, target_pct, stop_pct,
                option_token, signal_id, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                position_id, opened.isoformat(timespec="seconds"),
                opened.date().isoformat(), index_name.upper(), option_type.upper(),
                float(strike), int(lot_size), int(lots), float(entry_premium),
                float(target_pct), float(stop_pct), option_token, signal_id, notes,
            ),
        )
    return position_id


def close_position(
    position_id: str,
    *,
    exit_premium: float,
    exit_reason: str = "manual",
    costs: OptionsCostModel | None = None,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
) -> dict | None:
    """Close a position, recording net P&L after real costs."""
    init_ledger(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE position_id = ? AND status = 'OPEN'",
            (position_id,),
        ).fetchone()
        if row is None:
            return None

        quantity = int(row["lot_size"]) * int(row["lots"])
        result = net_pnl(
            float(row["entry_premium"]), float(exit_premium), quantity, costs=costs
        )
        conn.execute(
            """
            UPDATE positions
               SET status = 'CLOSED', closed_at = ?, exit_premium = ?,
                   exit_reason = ?, net_pnl = ?
             WHERE position_id = ?
            """,
            (
                now_ist().isoformat(timespec="seconds"), float(exit_premium),
                exit_reason, round(result["net_pnl"], 2), position_id,
            ),
        )
    return {
        "position_id": position_id,
        "net_pnl": round(result["net_pnl"], 2),
        "gross_pnl": round(result["gross_pnl"], 2),
        "cost": round(result["total_cost"], 2),
    }


def load_open_positions(
    *, db_path: str | Path = DEFAULT_LEDGER_PATH
) -> list[LivePosition]:
    """Open positions, with target/stop premiums derived from entry."""
    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at ASC"
        ).fetchall()

    out: list[LivePosition] = []
    for row in rows:
        entry = float(row["entry_premium"])
        lots = int(row["lots"])
        lot_size = int(row["lot_size"])
        out.append(LivePosition(
            position_id=row["position_id"],
            index_name=row["index_name"],
            option_type=row["option_type"],
            strike=float(row["strike"]),
            lot_size=lot_size,
            lots=lots,
            quantity=lot_size * lots,
            entry_premium=entry,
            target_pct=float(row["target_pct"]),
            stop_pct=float(row["stop_pct"]),
            target_premium=round(entry * (1 + float(row["target_pct"])), 2),
            stop_premium=round(entry * (1 - float(row["stop_pct"])), 2),
            opened_at=row["opened_at"],
            option_token=row["option_token"],
            signal_id=row["signal_id"],
            deployed=round(entry * lot_size * lots, 2),
        ))
    return out


def evaluate_live(
    position: LivePosition,
    *,
    current_premium: float | None,
    premium_source: str,
    underlying_direction: str,
    costs: OptionsCostModel | None = None,
) -> LivePosition:
    """Attach current value, status and NET P&L to a position.

    The status/alarm logic is reused from :mod:`position_monitor` so the loud
    stop alarm behaves identically to before. What is added here is **net**
    P&L: the old screen showed gross premium movement, which overstates every
    small winner, because exiting costs money too.
    """
    position.current_premium = current_premium
    position.premium_source = premium_source

    status: PositionStatus = evaluate_position(
        position.entry_premium,
        current_premium if current_premium is not None else 0.0,
        position.option_type,
        position.quantity,
        underlying_direction,
        target_pct=position.target_pct,
        stop_pct=position.stop_pct,
    )
    position.status = status.status
    position.action = status.action
    position.message = status.message
    position.is_alarm = status.is_alarm
    position.pnl_pct = round(status.pnl_pct, 4)

    if current_premium and current_premium > 0:
        result = net_pnl(
            position.entry_premium, current_premium, position.quantity, costs=costs
        )
        position.gross_pnl = round(result["gross_pnl"], 2)
        position.net_pnl = round(result["net_pnl"], 2)
        position.cost_to_exit = round(result["total_cost"], 2)
    else:
        position.gross_pnl = position.net_pnl = position.cost_to_exit = 0.0
    return position


def session_summary(
    *, session_date: str | None = None, db_path: str | Path = DEFAULT_LEDGER_PATH
) -> dict:
    """Realised P&L and counts for a session, for the risk governor."""
    target_date = session_date or now_ist().date().isoformat()
    init_ledger(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(net_pnl), 0) AS realised,
                   COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins
              FROM positions
             WHERE status = 'CLOSED' AND session_date = ?
            """,
            (target_date,),
        ).fetchone()
        open_count = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'OPEN'"
        ).fetchone()[0]

    closed = int(row["n"] or 0)
    return {
        "session_date": target_date,
        "closed_trades": closed,
        "open_trades": int(open_count or 0),
        "realised_pnl": round(float(row["realised"] or 0), 2),
        "wins": int(row["wins"] or 0),
        "win_rate": round(int(row["wins"] or 0) / closed, 4) if closed else None,
    }
