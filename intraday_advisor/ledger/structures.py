"""Recording a multi-leg position, so a spread can actually be traded.

Why this was the blocking gap
-----------------------------
``spreads.py`` can price a vertical or an iron condor exactly, and the measured
evidence favours structures over the naked long options this project kept
buying -- the variance premium runs +2 to +4 volatility points to the seller,
and a bear put spread costs half the capital of the naked put it replaces for
the same view to the same target.

None of that could be *taken*, because the ledger held one leg per position:
one ``option_type``, one ``strike``, one ``entry_premium``. A structure could
be computed, displayed and admired, and never opened, never graded, never
counted in a track record.

Design: additive, not a reshape
-------------------------------
``positions`` is read by the live monitor, the grader, the dashboard and every
report. Reshaping it to carry N legs would have touched all of them. So the
legs live in their own table and the parent row carries the **net**:

* ``option_type`` is ``'MULTI'``
* ``strike`` is the primary (longest-dated intent) leg's strike, kept only so
  existing displays have something sane to show
* ``entry_premium`` is the **net premium per unit**, positive for a debit and
  negative for a credit

A single-leg position writes no legs at all and behaves exactly as before.
Anything that wants the detail joins; anything that wants the net is untouched.
The migration is "open the ledger": ``init_ledger`` is idempotent and
``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing file.

The sign convention, stated once
--------------------------------
A leg's ``action`` is ``BUY`` or ``SELL``. Its contribution to cost at entry is
``+premium`` for a buy and ``-premium`` for a sell; its contribution to value at
exit is the same with the same sign. P&L is therefore

    sum over legs of  sign * (exit - entry) * lot_size * lots

with ``sign = +1`` for a bought leg and ``-1`` for a sold one. Getting this
backwards on one leg turns a hedge into a doubled position, so it is written in
exactly one place -- :func:`structure_pnl` -- and everything else calls it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from intraday_advisor.ledger.costs import OptionsCostModel, leg_costs
from intraday_advisor.ledger.recorder import now_ist
from intraday_advisor.ledger.schema import DEFAULT_LEDGER_PATH, connect, init_ledger

#: What the parent row's ``option_type`` says for a structure.
MULTI = "MULTI"


@dataclass
class StoredLeg:
    """One leg as the ledger holds it."""

    leg_index: int
    option_type: str
    strike: float
    action: str
    lots: int
    entry_premium: float
    exit_premium: float | None = None
    option_token: str | None = None

    @property
    def sign(self) -> int:
        return -1 if self.action.upper() == "SELL" else 1

    def as_dict(self) -> dict:
        from dataclasses import asdict
        data = asdict(self)
        data["sign"] = self.sign
        return data


@dataclass
class StoredStructure:
    """A multi-leg position, as recorded."""

    position_id: str
    index_name: str
    lot_size: int
    legs: list[StoredLeg] = field(default_factory=list)
    net_entry_premium: float = 0.0
    status: str = "OPEN"
    opened_at: str = ""
    closed_at: str | None = None
    net_pnl: float | None = None
    notes: str = ""

    @property
    def is_credit(self) -> bool:
        return self.net_entry_premium < 0

    def as_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "index_name": self.index_name,
            "lot_size": self.lot_size,
            "net_entry_premium": self.net_entry_premium,
            "is_credit": self.is_credit,
            "status": self.status,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "net_pnl": self.net_pnl,
            "legs": [leg.as_dict() for leg in self.legs],
        }


def structure_pnl(
    legs: list[StoredLeg], lot_size: int, exits: dict[int, float], *,
    costs: OptionsCostModel | None = None,
) -> dict:
    """Gross and net P&L for a set of legs at the given exit premiums.

    The single place the leg sign convention is applied. ``exits`` maps
    ``leg_index`` to the premium that leg was closed at.
    """
    c = costs or OptionsCostModel()
    gross = 0.0
    total_cost = 0.0
    priced = 0

    for leg in legs:
        quantity = lot_size * leg.lots
        exit_premium = exits.get(leg.leg_index)
        if exit_premium is None:
            continue
        priced += 1
        gross += leg.sign * (float(exit_premium) - leg.entry_premium) * quantity
        # Opening takes one side, closing takes the other; a sold leg is sold
        # to open and bought to close.
        opening = leg_costs(leg.entry_premium, quantity,
                            is_sell=leg.sign < 0, costs=c)
        closing = leg_costs(float(exit_premium), quantity,
                            is_sell=leg.sign > 0, costs=c)
        total_cost += opening["total"] + closing["total"]

    return {
        "gross_pnl": round(gross, 2),
        "total_cost": round(total_cost, 2),
        "net_pnl": round(gross - total_cost, 2),
        "legs_priced": priced,
        "legs_total": len(legs),
        "complete": priced == len(legs),
    }


def open_structure(
    *, index_name: str, legs: list[dict], lot_size: int,
    signal_id: str | None = None, notes: str = "",
    db_path: str | Path = DEFAULT_LEDGER_PATH,
) -> str:
    """Record a multi-leg position. Returns its ``position_id``.

    Each entry in ``legs`` needs ``option_type``, ``strike``, ``action`` and
    ``entry_premium``; ``lots`` defaults to 1 and ``option_token`` is optional
    but wanted, because without it the leg cannot be priced from the tick feed.
    """
    if not legs:
        raise ValueError("A structure needs at least one leg.")
    if lot_size <= 0:
        raise ValueError("lot_size must be positive.")

    parsed: list[StoredLeg] = []
    for i, raw in enumerate(legs):
        action = str(raw["action"]).upper()
        if action not in ("BUY", "SELL"):
            raise ValueError(f"Leg {i}: action must be BUY or SELL, not {action!r}")
        option_type = str(raw["option_type"]).upper()
        if option_type not in ("CE", "PE"):
            raise ValueError(f"Leg {i}: option_type must be CE or PE.")
        premium = float(raw["entry_premium"])
        if premium <= 0:
            raise ValueError(
                f"Leg {i}: entry_premium must be positive. A sold leg is "
                f"recorded with its own positive premium and action=SELL; the "
                f"sign comes from the action, never from the price."
            )
        parsed.append(StoredLeg(
            leg_index=i, option_type=option_type, strike=float(raw["strike"]),
            action=action, lots=int(raw.get("lots", 1)), entry_premium=premium,
            option_token=str(raw["option_token"]) if raw.get("option_token") else None,
        ))

    net_premium = sum(leg.sign * leg.entry_premium * leg.lots for leg in parsed)
    primary = max(parsed, key=lambda leg: (leg.sign, leg.entry_premium))

    position_id = uuid.uuid4().hex
    opened_at = now_ist().isoformat(timespec="seconds")
    session_date = now_ist().date().isoformat()

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
                position_id, opened_at, session_date, index_name.upper(), MULTI,
                primary.strike, int(lot_size), max(leg.lots for leg in parsed),
                round(net_premium, 4),
                # A structure's exit is decided by its own payoff, not by a
                # percentage of a premium that may be negative. Stored as zero
                # so nothing downstream invents a level from them.
                0.0, 0.0,
                primary.option_token, signal_id, notes,
            ),
        )
        conn.executemany(
            """
            INSERT INTO position_legs (
                position_id, leg_index, option_type, strike, action, lots,
                entry_premium, option_token
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(position_id, leg.leg_index, leg.option_type, leg.strike,
              leg.action, leg.lots, leg.entry_premium, leg.option_token)
             for leg in parsed],
        )
        conn.commit()
    return position_id


def _load(row, conn) -> StoredStructure:
    legs = [
        StoredLeg(
            leg_index=int(r["leg_index"]), option_type=r["option_type"],
            strike=float(r["strike"]), action=r["action"], lots=int(r["lots"]),
            entry_premium=float(r["entry_premium"]),
            exit_premium=(float(r["exit_premium"])
                          if r["exit_premium"] is not None else None),
            option_token=r["option_token"],
        )
        for r in conn.execute(
            "SELECT * FROM position_legs WHERE position_id = ? ORDER BY leg_index",
            (row["position_id"],),
        ).fetchall()
    ]
    return StoredStructure(
        position_id=row["position_id"], index_name=row["index_name"],
        lot_size=int(row["lot_size"]), legs=legs,
        net_entry_premium=float(row["entry_premium"]),
        status=row["status"], opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        net_pnl=float(row["net_pnl"]) if row["net_pnl"] is not None else None,
        notes=row["notes"] or "",
    )


def load_open_structures(
    db_path: str | Path = DEFAULT_LEDGER_PATH,
) -> list[StoredStructure]:
    """Open multi-leg positions only. Single-leg positions are not returned."""
    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' AND option_type = ? "
            "ORDER BY opened_at ASC",
            (MULTI,),
        ).fetchall()
        return [_load(row, conn) for row in rows]


def load_structure(
    position_id: str, db_path: str | Path = DEFAULT_LEDGER_PATH,
) -> StoredStructure | None:
    init_ledger(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        return _load(row, conn) if row else None


def evaluate_structure(
    structure: StoredStructure, current: dict[int, float], *,
    costs: OptionsCostModel | None = None,
) -> dict:
    """Live P&L at the current leg premiums, without closing anything.

    A structure priced on only some of its legs is reported as incomplete
    rather than as a smaller P&L: a two-leg spread valued on one leg is a
    naked option, and showing that number as the spread's would be the most
    dangerous possible kind of wrong.
    """
    result = structure_pnl(structure.legs, structure.lot_size, current,
                           costs=costs)
    if not result["complete"]:
        result["warning"] = (
            f"Only {result['legs_priced']} of {result['legs_total']} legs have "
            f"a price. A partially priced structure is not this structure -- "
            f"treat the P&L as unknown, not as smaller."
        )
    return result


def close_structure(
    position_id: str, exits: dict[int, float], *, exit_reason: str = "manual",
    costs: OptionsCostModel | None = None,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
) -> dict | None:
    """Close every leg and record the net result.

    Refuses a partial close: a structure is one position, and closing half of
    it leaves an exposure nothing in this ledger models.
    """
    structure = load_structure(position_id, db_path=db_path)
    if structure is None:
        return None
    if structure.status != "OPEN":
        return None

    missing = [leg.leg_index for leg in structure.legs
               if exits.get(leg.leg_index) is None]
    if missing:
        return {
            "ok": False,
            "error": (
                f"Legs {missing} have no exit price. Closing part of a "
                f"structure leaves an exposure this ledger does not model, so "
                f"it is refused rather than half-recorded."
            ),
        }

    result = structure_pnl(structure.legs, structure.lot_size, exits, costs=costs)
    closed_at = now_ist().isoformat(timespec="seconds")
    net_exit = sum(
        leg.sign * float(exits[leg.leg_index]) * leg.lots
        for leg in structure.legs
    )

    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE positions
               SET status = 'CLOSED', closed_at = ?, exit_premium = ?,
                   exit_reason = ?, net_pnl = ?
             WHERE position_id = ? AND status = 'OPEN'
            """,
            (closed_at, round(net_exit, 4), exit_reason, result["net_pnl"],
             position_id),
        )
        for leg in structure.legs:
            conn.execute(
                "UPDATE position_legs SET exit_premium = ? "
                "WHERE position_id = ? AND leg_index = ?",
                (float(exits[leg.leg_index]), position_id, leg.leg_index),
            )
        conn.commit()

    return {
        "ok": True,
        "position_id": position_id,
        "net_entry_premium": structure.net_entry_premium,
        "net_exit_premium": round(net_exit, 4),
        **result,
        "closed_at": closed_at,
    }
