"""A score built from the components that survived, not from all of them.

What the measurement said
-------------------------
Every one of the 27 stored indicator components was tested against realised
P&L across 238 graded signals. **Ten were significant at p<0.05 against 1.4
expected by chance** -- there is real information in them. But they disagree:

    INVERTED (its "bullish" preceded worse)   WORKING AS INTENDED
      gap_signal       -0.396                   fvg            +0.204
      warnings         -0.245                   hma_trend      +0.186
      liquidity_sweep  -0.168                   bollinger      +0.182
      advanced_candle  -0.141                   oi_wall        +0.147
      squeeze          -0.129                   heikin_ashi    +0.133

``score_signal`` **adds all of them together**. One contributes +10, another
-10, and the sum is noise -- which is exactly why the total score correlates
-0.074 with outcome ("no relationship") while its own parts reach +/-0.40.

The indicators were never blind. The score was deaf.

Which ones survive a session split
----------------------------------
Pooled significance is not enough -- hour 9 looked like an opening edge and was
one session. Tested per session, only three keep their sign on all three:

    fvg       +0.517  +0.204  +0.069    consistent, positive
    oi_wall   +0.229  +0.294  +0.048    consistent, positive
    squeeze   -0.336  -0.010  -0.538    consistent, INVERTED

``gap_signal``, the strongest single number in the whole study at -0.396,
appears in **one session only** and is an anecdote. ``hma_trend``,
``heikin_ashi``, ``bollinger`` and ``warnings`` all flip sign between days, so
their pooled significance is a mirage.

**This is a candidate, not a result.**
--------------------------------------
Three components were chosen from 27 by looking at the same 238 trades this
will be measured against. That is textbook selection bias: pick the best of 27
and the winner is flattered by however much noise helped it. Three sessions
cannot fix that, and no amount of re-testing on these trades will either.

So this module exists to be measured **forward**, on sessions it has never
seen. :func:`compare` reports both scores side by side and labels in-sample
results as in-sample, every time, without being asked.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: The three that kept their sign across every session, and how to read them.
#: A negative weight means the component is INVERTED: its own "bullish"
#: contribution preceded worse outcomes, so it is subtracted.
SURVIVORS: dict[str, float] = {
    "fvg": +1.0,
    "oi_wall": +1.0,
    "squeeze": -1.0,
}

#: Rejected despite pooled significance, and why -- kept in the source because
#: the reasons are the point. Anything here is a trap for a future reader who
#: sees a big correlation and wants to use it.
REJECTED: dict[str, str] = {
    "gap_signal": "strongest single correlation (-0.396) but present in ONE "
                  "session; an anecdote about that day",
    "warnings": "flips sign between sessions (-0.229 / +0.281 / +0.085)",
    "hma_trend": "flips (+0.452 / +0.267 / -0.035)",
    "heikin_ashi": "flips (+0.447 / +0.222 / -0.134)",
    "bollinger": "flips (-0.151 / +0.138 / +0.139), and fires 14 times in 238",
    "liquidity_sweep": "flips (+0.038 / -0.121 / -0.204)",
    "advanced_candle": "flips (+0.292 / -0.131 / -0.271)",
}


@dataclass
class ScoreComparison:
    """Two scores measured against the same outcomes."""

    trades: int = 0
    full_spearman: float = 0.0
    focused_spearman: float = 0.0
    in_sample: bool = True
    per_session: list = field(default_factory=list)
    verdict: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def focused_score(components: dict) -> float:
    """A directional strength from the three surviving components only.

    Returns a signed number centred on zero: positive leans bullish, negative
    bearish, and the magnitude is how much the three agree. Deliberately NOT
    rescaled to 0-100 -- the 0-100 form is what saturated at its rails once a
    quarter of 193 points of weight fired, and there is no reason to repeat it
    with three components.
    """
    if not components:
        return 0.0
    total = 0.0
    for name, direction in SURVIVORS.items():
        contribution = components.get(name)
        if contribution is None:
            continue
        try:
            total += direction * float(contribution)
        except (TypeError, ValueError):
            continue
    return round(total, 3)


def _spearman(a, b) -> float:
    ra = pd.Series(a).rank().to_numpy(dtype=float)
    rb = pd.Series(b).rank().to_numpy(dtype=float)
    ra -= ra.mean()
    rb -= rb.mean()
    denominator = math.sqrt(float((ra ** 2).sum()) * float((rb ** 2).sum()))
    return float((ra * rb).sum() / denominator) if denominator else 0.0


def compare(
    *, db_path: str = "data/ledger.sqlite",
    held_out_sessions: "list[str] | None" = None,
    **filters,
) -> ScoreComparison:
    """Rank quality of the focused score against the full one.

    ``held_out_sessions`` restricts the comparison to sessions the survivors
    were NOT chosen from. Pass the sessions recorded after 2026-10-22 to get
    the only measurement that counts; omit it and the result is in-sample and
    says so.
    """
    from intraday_advisor.ledger.report import load_graded

    result = ScoreComparison()
    frame = load_graded(db_path=db_path, **filters)
    frame = frame.dropna(subset=["score_components", "net_pnl_per_lot"])
    if frame.empty:
        result.notes.append("No graded trades with stored components.")
        return result

    if held_out_sessions:
        frame = frame[frame["session_date"].isin(held_out_sessions)]
        result.in_sample = False
        if frame.empty:
            result.notes.append(
                "None of those sessions are graded yet, so there is nothing "
                "out of sample to measure."
            )
            return result

    parsed = [json.loads(x) for x in frame["score_components"]]
    focused = np.array([focused_score(c) for c in parsed], dtype=float)
    full = frame["score"].to_numpy(dtype=float)
    pnl = frame["net_pnl_per_lot"].to_numpy(dtype=float)

    result.trades = int(len(frame))
    result.full_spearman = round(_spearman(full, pnl), 3)
    result.focused_spearman = round(_spearman(focused, pnl), 3)

    for session, chunk in frame.groupby("session_date"):
        rows = [json.loads(x) for x in chunk["score_components"]]
        if len(chunk) < 10:
            continue
        result.per_session.append({
            "session": str(session),
            "trades": int(len(chunk)),
            "full": round(_spearman(chunk["score"].to_numpy(float),
                                    chunk["net_pnl_per_lot"].to_numpy(float)), 3),
            "focused": round(_spearman(
                np.array([focused_score(c) for c in rows], dtype=float),
                chunk["net_pnl_per_lot"].to_numpy(float)), 3),
        })

    better = abs(result.focused_spearman) > abs(result.full_spearman)
    signs = [row["focused"] for row in result.per_session]
    consistent = len(signs) >= 2 and (all(x > 0 for x in signs)
                                      or all(x < 0 for x in signs))

    if result.in_sample:
        result.verdict = "IN-SAMPLE ONLY — not evidence"
        result.notes.append(
            "The three components were chosen by looking at these same trades. "
            "Picking the best of 27 flatters the winner by however much noise "
            "helped it, so a good number here is expected whether or not the "
            "effect is real. Re-run with held_out_sessions once more sessions "
            "are graded."
        )
    elif better and consistent:
        result.verdict = "out-of-sample: focused ranks better, and consistently"
    elif better:
        result.verdict = "out-of-sample: focused ranks better but flips between sessions"
    else:
        result.verdict = "out-of-sample: no improvement over the full score"

    result.notes.append(
        f"full score {result.full_spearman:+.3f} vs focused "
        f"{result.focused_spearman:+.3f} over {result.trades} trades."
    )
    return result


def render(comparison: ScoreComparison) -> str:
    """Plain text, with the in-sample caveat impossible to miss."""
    lines = [
        "=" * 70,
        "FOCUSED SCORE vs FULL SCORE   (Spearman against realised P&L)",
        "=" * 70,
        f"  components used : {', '.join(f'{k}({v:+.0f})' for k, v in SURVIVORS.items())}",
        f"  trades          : {comparison.trades}",
        f"  full score      : {comparison.full_spearman:+.3f}",
        f"  focused score   : {comparison.focused_spearman:+.3f}",
    ]
    if comparison.per_session:
        lines += ["", f"  {'session':12} {'trades':>7} {'full':>8} {'focused':>9}"]
        for row in comparison.per_session:
            lines.append(
                f"  {row['session']:12} {row['trades']:7} {row['full']:+8.3f} "
                f"{row['focused']:+9.3f}"
            )
    lines += ["", f"  VERDICT: {comparison.verdict}"]
    for note in comparison.notes:
        lines.append(f"    - {note}")
    lines.append("=" * 70)
    return "\n".join(lines)
