"""Which indicators have earned trust, measured per session, updated daily.

The problem this answers
------------------------
``score_signal`` adds up to 37 component contributions into one number. Tested
against 325 graded trades, **the total correlates +0.02 with outcome** while
its own parts reach +/-0.40 -- because they disagree with each other and the
sum cancels:

    gap_signal       -0.396      fvg        +0.204
    warnings         -0.245      hma_trend  +0.186
    liquidity_sweep  -0.168      oi_wall    +0.147

A negative figure means that component's "bullish" contribution preceded
**worse** outcomes. Adding a reliable +10 to a reliably-wrong +10 produces
noise, and that is the whole explanation for a score that ranks nothing.

Why consistency and not size
----------------------------
The biggest single correlation in the study, ``gap_signal`` at -0.396,
appears in **one session**. So does the "opening edge" at hour 9. A number
computed from one day describes that day.

What separates a usable component from a lucky one is keeping its sign when
the market changes. Measured across four sessions:

    full score    -0.204  -0.175  +0.071  +0.219   FLIPS
    fvg           +0.517  +0.204  +0.403  (held)   consistent
    oi_wall       +0.229  +0.294  +0.048  (held)   consistent
    squeeze       -0.336  -0.010  -0.538  (held)   consistent, inverted

A component that ranks well some days and backwards on others cannot be
traded, because you do not know which kind of day you are in until afterwards.

**This recomputes from the ledger every time it is asked**, so the two lists
move as evidence arrives rather than being frozen to what three days happened
to show. Everything here is a running measurement, not a conclusion.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: A component must appear in at least this many sessions before its sign can
#: be called consistent. Below it, one day is doing all the talking.
MIN_SESSIONS = 3

#: And fire at least this many times overall, or a handful of coincidences
#: produce a correlation of 1.0.
MIN_OBSERVATIONS = 20

#: Per-session correlations below this are treated as "no opinion" rather than
#: as a sign, so a component is not disqualified by a session where it barely
#: moved.
FLAT = 0.02


@dataclass
class IndicatorRecord:
    """One component's track record."""

    name: str
    observations: int = 0
    sessions: int = 0
    overall: float = 0.0
    per_session: dict = field(default_factory=dict)
    consistent: bool = False
    direction: int = 0          # +1 reads correctly, -1 inverted, 0 unproven
    reason: str = ""

    @property
    def trusted(self) -> bool:
        return self.consistent and self.direction != 0

    def as_dict(self) -> dict:
        from dataclasses import asdict
        data = asdict(self)
        data["trusted"] = self.trusted
        return data


def _spearman(a, b) -> float:
    ra = pd.Series(a).rank().to_numpy(dtype=float)
    rb = pd.Series(b).rank().to_numpy(dtype=float)
    ra -= ra.mean()
    rb -= rb.mean()
    denominator = math.sqrt(float((ra ** 2).sum()) * float((rb ** 2).sum()))
    return float((ra * rb).sum() / denominator) if denominator else 0.0


def scorecard(*, db_path: str = "data/ledger.sqlite", **filters) -> dict:
    """Every component's record, split into trusted and unproven.

    Returns ``{"trusted": [...], "unproven": [...], "sessions": n, ...}`` with
    each entry an :class:`IndicatorRecord` dict, strongest first.
    """
    from intraday_advisor.ledger.report import load_graded

    frame = load_graded(db_path=db_path, **filters)
    frame = frame.dropna(subset=["score_components", "net_pnl_per_lot"])
    if frame.empty:
        return {"trusted": [], "unproven": [], "sessions": 0, "trades": 0,
                "note": "No graded trades yet — nothing has a record."}

    parsed = pd.DataFrame([json.loads(x) for x in frame["score_components"]])
    parsed = parsed.fillna(0.0)
    parsed["__session"] = frame["session_date"].to_numpy()
    parsed["__pnl"] = frame["net_pnl_per_lot"].to_numpy(dtype=float)
    sessions = sorted(parsed["__session"].dropna().unique())

    records: list[IndicatorRecord] = []
    for name in parsed.columns:
        if name.startswith("__"):
            continue
        values = parsed[name].to_numpy(dtype=float)
        fired = int((values != 0).sum())
        record = IndicatorRecord(name=name, observations=fired)
        if np.std(values) == 0:
            record.reason = "never varies in this sample"
            records.append(record)
            continue

        record.overall = round(_spearman(values, parsed["__pnl"].to_numpy()), 3)

        signs: list[int] = []
        for session in sessions:
            chunk = parsed[parsed["__session"] == session]
            column = chunk[name].to_numpy(dtype=float)
            if len(chunk) < 10 or np.std(column) == 0:
                continue
            rho = _spearman(column, chunk["__pnl"].to_numpy(dtype=float))
            record.per_session[str(session)] = round(rho, 3)
            if abs(rho) >= FLAT:
                signs.append(1 if rho > 0 else -1)

        record.sessions = len(record.per_session)

        if fired < MIN_OBSERVATIONS:
            record.reason = (
                f"fired only {fired} time(s); a handful of coincidences makes "
                f"any correlation"
            )
        elif record.sessions < MIN_SESSIONS:
            record.reason = (
                f"present in {record.sessions} session(s); a number from one "
                f"day describes that day"
            )
        elif not signs:
            record.reason = "contributes but never leans either way"
        elif all(x > 0 for x in signs):
            record.consistent = True
            record.direction = 1
            record.reason = (
                f"same sign in all {len(signs)} session(s) — reads correctly"
            )
        elif all(x < 0 for x in signs):
            record.consistent = True
            record.direction = -1
            record.reason = (
                f"same sign in all {len(signs)} session(s) — INVERTED, its "
                f"bullish reading preceded worse outcomes"
            )
        else:
            positives = sum(1 for x in signs if x > 0)
            record.reason = (
                f"flips: positive in {positives} of {len(signs)} session(s), "
                f"so it cannot be relied on in advance"
            )
        records.append(record)

    trusted = sorted(
        (r for r in records if r.trusted),
        key=lambda r: abs(r.overall), reverse=True,
    )
    unproven = sorted(
        (r for r in records if not r.trusted),
        key=lambda r: abs(r.overall), reverse=True,
    )
    return {
        "sessions": len(sessions),
        "session_dates": [str(s) for s in sessions],
        "trades": int(len(frame)),
        "trusted": [r.as_dict() for r in trusted],
        "unproven": [r.as_dict() for r in unproven],
        "note": (
            f"Measured over {len(frame)} graded trades across {len(sessions)} "
            f"session(s). A component is trusted only when it keeps the same "
            f"sign in every session it appears in."
        ),
    }


def live_readings(components: dict, card: dict) -> dict:
    """Pair this candle's contributions with each component's track record.

    ``components`` is the live ``score_components`` for one signal; ``card`` is
    a :func:`scorecard` result. Returns the same two groups, each entry
    carrying what the indicator is saying **now** and whether that reading has
    historically been worth anything.
    """
    contributions = {k: float(v) for k, v in (components or {}).items()}

    def decorate(entries: list) -> list:
        out = []
        for entry in entries:
            points = contributions.get(entry["name"])
            if points is None:
                continue
            direction = entry.get("direction") or 0
            # An inverted component's honest reading is the OPPOSITE of its
            # contribution: that is the whole finding, so it is applied here
            # rather than left for a reader to remember.
            effective = points * direction if direction else points
            out.append({
                **entry,
                "points": round(points, 2),
                "says": ("BULLISH" if points > 0 else
                         "BEARISH" if points < 0 else "neutral"),
                "reading": ("BULLISH" if effective > 0 else
                            "BEARISH" if effective < 0 else "neutral"),
                "inverted": direction == -1,
            })
        return out

    trusted = decorate(card.get("trusted", []))
    agree = sum(1 for e in trusted if e["reading"] == "BULLISH") - \
        sum(1 for e in trusted if e["reading"] == "BEARISH")
    return {
        "trusted": trusted,
        "unproven": decorate(card.get("unproven", [])),
        "trusted_lean": ("BULLISH" if agree > 0 else
                         "BEARISH" if agree < 0 else "split"),
        "trusted_net": agree,
    }
