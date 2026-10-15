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


def delete_position(
    position_id: str, *, db_path: str | Path = DEFAULT_LEDGER_PATH
) -> bool:
    """Remove a position outright. For clearing test rows, not for closing.

    Closing a position records what happened and keeps it in the history, which
    is the point of a journal. Deleting throws it away entirely, so it exists
    only to clean up rows that were never real trades.
    """
    init_ledger(db_path)
    with connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM positions WHERE position_id = ?", (position_id,)
        )
    return cursor.rowcount > 0


def closed_trades(
    *, limit: int = 50, db_path: str | Path = DEFAULT_LEDGER_PATH
) -> list[dict]:
    """Recent closed positions, newest first, with cost broken out.

    This is the answer to "where did that number come from?". Every rupee in
    the session P&L traces to a row here.
    """
    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM positions
             WHERE status = 'CLOSED'
             ORDER BY closed_at DESC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    out = []
    for row in rows:
        quantity = int(row["lot_size"]) * int(row["lots"])
        entry = float(row["entry_premium"])
        exit_premium = float(row["exit_premium"] or 0)
        gross = (exit_premium - entry) * quantity
        net = float(row["net_pnl"] or 0)
        out.append({
            "position_id": row["position_id"],
            "session_date": row["session_date"],
            "opened_at": row["opened_at"],
            "closed_at": row["closed_at"],
            "contract": f"{row['index_name']} {row['strike']:g} {row['option_type']}",
            "index_name": row["index_name"],
            "option_type": row["option_type"],
            "strike": float(row["strike"]),
            "lots": int(row["lots"]),
            "quantity": quantity,
            "entry_premium": entry,
            "exit_premium": exit_premium,
            "deployed": round(entry * quantity, 2),
            "gross_pnl": round(gross, 2),
            # Costs are the gap between gross and net: the number that turns a
            # "small winner" into a loser and is invisible on a premium chart.
            "cost": round(gross - net, 2),
            "net_pnl": round(net, 2),
            "pnl_pct": round((exit_premium - entry) / entry, 4) if entry else 0.0,
            "exit_reason": row["exit_reason"],
        })
    return out


def daily_pnl_series(
    *, days: int = 30, db_path: str | Path = DEFAULT_LEDGER_PATH
) -> list[dict]:
    """Realised net P&L per session, oldest first, with a running total.

    The equity curve. A single day's profit says nothing; the shape of the
    cumulative line is the only honest picture of whether this is working.
    """
    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT session_date,
                   COALESCE(SUM(net_pnl), 0) AS net,
                   COUNT(*) AS trades,
                   COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins
              FROM positions
             WHERE status = 'CLOSED'
             GROUP BY session_date
             ORDER BY session_date DESC
             LIMIT ?
            """,
            (int(days),),
        ).fetchall()

    series = []
    running = 0.0
    for row in reversed(rows):            # back to oldest-first for the curve
        running += float(row["net"] or 0)
        series.append({
            "session_date": row["session_date"],
            "net": round(float(row["net"] or 0), 2),
            "cumulative": round(running, 2),
            "trades": int(row["trades"]),
            "wins": int(row["wins"]),
        })
    return series


def performance_summary(*, db_path: str | Path = DEFAULT_LEDGER_PATH) -> dict:
    """Realised performance across every session, with an honest verdict.

    Separate from the signal ledger's expectancy: that measures whether the
    *engine* has an edge, this measures whether *you* made money. They can
    disagree, and the gap is itself informative.
    """
    trades = closed_trades(limit=100_000, db_path=db_path)
    if not trades:
        return {
            "trades": 0, "net": 0.0, "gross": 0.0, "costs": 0.0,
            "wins": 0, "losses": 0, "win_rate": None,
            "avg_win": None, "avg_loss": None, "profit_factor": None,
            "best": None, "worst": None, "sessions": 0,
            "verdict": "No closed trades yet. Take a trade, close it here, and "
                       "this becomes your real track record.",
        }

    nets = [t["net_pnl"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total_costs = sum(t["cost"] for t in trades)
    net = sum(nets)
    sessions = len({t["session_date"] for t in trades})

    if len(nets) < 20:
        verdict = (
            f"{len(nets)} closed trade(s) — too few to call. A track record "
            f"needs 20+ before the average means anything."
        )
    elif net > 0:
        verdict = (
            f"Net +₹{net:,.0f} over {len(nets)} trades across {sessions} "
            f"session(s), after ₹{total_costs:,.0f} in costs."
        )
    else:
        verdict = (
            f"Net ₹{net:,.0f} over {len(nets)} trades. Costs alone were "
            f"₹{total_costs:,.0f} — check whether cheap contracts are the problem."
        )

    return {
        "trades": len(nets),
        "net": round(net, 2),
        "gross": round(sum(t["gross_pnl"] for t in trades), 2),
        "costs": round(total_costs, 2),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(nets), 4),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "best": round(max(nets), 2),
        "worst": round(min(nets), 2),
        "sessions": sessions,
        "verdict": verdict,
    }
