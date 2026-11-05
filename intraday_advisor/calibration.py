"""Turn stored data into answers. Every table in the archive is read here.

Storage without a consumer is a landfill, so each function below answers a
question that could not be answered before the archive existed.

| reads | answers |
|---|---|
| ``spread_samples`` | is the cost model running on the published prior or on this account's own book? |
| ``signals`` + ``signal_outcomes`` | **would the vetoed trades have made money?** |
| ``observations`` + ``daily_record`` | is conviction a probability, or just agreement among inputs? |
| ``predictions`` | when something says 47%, does 47% happen? |
| ``bars`` | what has realised volatility actually done, over a sample longer than the source serves? |

The second one is the one AGENTS.md calls "the only question that settles it",
and it has been answerable for weeks with nothing asking it.

One statistical rule runs through all of this
---------------------------------------------
**A session is the unit of observation, not a trade.** Trades inside one day
share that day's regime, so pooling them and dividing by the square root of the
count overstates precision badly -- this project has made that mistake twice,
once with overlapping forward returns and once with per-trade confidence
intervals. Every comparison here reports the session count beside the trade
count, and widens to session-level intervals where it matters.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

logger = logging.getLogger("calibration")

#: Spread samples an hour needs before its measured mean replaces the prior.
#: Matches ``microstructure.MIN_FILLS_PER_HOUR`` -- the calibration is
#: delegated there, and this constant exists so the threshold can be asked
#: about without importing the private detail.
MIN_SAMPLES_PER_HOUR = 10

#: Sessions a comparison needs before it is called anything. Below this the
#: spread between days dominates, which is the finding that keeps recurring.
MIN_SESSIONS = 3


# ================================================================= spreads ==

def _readable(db_path) -> bool:
    """Whether a ledger exists to read.

    Guard rather than convenience. ``init_ledger`` creates parent directories
    and an empty schema, so simply asking "what is the spread shape?" against a
    path that does not exist **created a database** -- a read with a side
    effect, which left a stray `no/such/` tree in the repository root when a
    test asked about a missing ledger. The cost model calls this on every
    verdict; it must look, not build.
    """
    from pathlib import Path

    try:
        return Path(db_path).exists()
    except Exception:  # noqa: BLE001
        return False


def spread_fills(*, db_path, session_date: str | None = None) -> list:
    """Stored spread samples in the shape ``calibrate_shape_from_fills`` wants."""
    from intraday_advisor.ledger.schema import connect, init_ledger

    if not _readable(db_path):
        return []
    init_ledger(db_path)
    sql = ("SELECT hour, half_spread_pct, premium, index_name, session_date "
           "FROM spread_samples WHERE hour IS NOT NULL")
    params: tuple = ()
    if session_date:
        sql += " AND session_date = ?"
        params = (session_date,)
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def spread_shape(*, db_path) -> dict:
    """Measured half-spread by hour against the published prior.

    Delegates the arithmetic and the sample-size rule to
    :func:`intraday_advisor.microstructure.calibrate_shape_from_fills`, which
    already normalises to a median of 1.0 and already refuses to calibrate an
    hour that has too few observations. This adds only the stored input and the
    per-hour comparison a reader needs to judge it.
    """
    from intraday_advisor.microstructure import (
        SPREAD_SHAPE_BY_HOUR,
        calibrate_shape_from_fills,
    )

    fills = spread_fills(db_path=db_path)
    result = calibrate_shape_from_fills(fills)
    counts = result.get("sample") or {}
    rows = []
    for hour in sorted(set(SPREAD_SHAPE_BY_HOUR) | set(counts)):
        samples = int(counts.get(hour, 0) or 0)
        rows.append({
            "hour": hour,
            "prior": SPREAD_SHAPE_BY_HOUR.get(hour),
            "measured": (round(float(result["shape"][hour]), 3)
                         if hour in result.get("shape", {}) else None),
            "samples": samples,
            "source": "measured" if samples >= MIN_SAMPLES_PER_HOUR else "prior",
        })
    return {
        "basis": result.get("basis", "prior"),
        "note": result.get("note", ""),
        "rows": rows,
        "total_samples": sum(r["samples"] for r in rows),
        "measured_hours": [r["hour"] for r in rows if r["source"] == "measured"],
    }


def measured_hours(*, db_path) -> list:
    """Hours whose spread is now taken from this account's own book.

    Kept separate and cheap because the live cost model calls it, and because
    the daily record stores it: the day an hour flips is the day every veto,
    breakeven and cost-drag figure for that hour changes, and that has to be
    traceable to a date rather than discovered later.
    """
    from intraday_advisor.ledger.schema import connect, init_ledger

    if not _readable(db_path):
        return []
    try:
        init_ledger(db_path)
        with connect(db_path) as conn:
            rows = conn.execute(
                "SELECT hour, COUNT(*) FROM spread_samples "
                "WHERE hour IS NOT NULL AND half_spread_pct > 0 "
                "GROUP BY hour HAVING COUNT(*) >= ?",
                (MIN_SAMPLES_PER_HOUR,)).fetchall()
        return sorted(int(r[0]) for r in rows)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Measured hours unavailable: %s", exc)
        return []


def render_spreads(result: dict) -> str:
    lines = ["=" * 68, "SPREAD SHAPE BY HOUR  (measured vs the published prior)",
             "=" * 68,
             f"  {'hour':>5} {'prior':>8} {'measured':>9} {'samples':>8}  source"]
    for row in result.get("rows", []):
        measured = f"{row['measured']:.3f}" if row["measured"] is not None else "-"
        prior = f"{row['prior']:.3f}" if row["prior"] is not None else "-"
        lines.append(f"  {row['hour']:5d} {prior:>8} {measured:>9} "
                     f"{row['samples']:8d}  {row['source']}")
    lines += ["", f"  basis         : {result.get('basis')}",
              f"  total samples : {result.get('total_samples', 0)}",
              f"  live hours    : {result.get('measured_hours') or 'none yet'}"]
    if result.get("note"):
        lines.append(f"    - {result['note']}")
    lines += ["", f"  An hour switches to measured at {MIN_SAMPLES_PER_HOUR} "
                  f"samples. Until then the prior stands,",
              "  because calibrating on a handful of observations fits noise.",
              "=" * 68]
    return "\n".join(lines)


# =========================================================== the agent layer ==

@dataclass
class StanceRecord:
    """One stance's track record."""

    stance: str = ""
    trades: int = 0
    sessions: int = 0
    mean_pnl: float | None = None
    hit_rate: float | None = None
    per_session: list = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def agent_scorecard(*, db_path) -> dict:
    """Would the vetoed trades have made money?

    AGENTS.md promises this question is answerable because "every vetoed setup
    is still recorded by the ledger". It is, and nothing asked. The answer is
    reported **per session as well as pooled**, because a stance that looks
    good pooled and flips between days has not been shown to work.
    """
    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.agent_stance stance, s.session_date session, "
            "o.net_pnl_per_lot pnl, o.outcome outcome "
            "FROM signals s JOIN signal_outcomes o ON o.signal_id = s.signal_id "
            "WHERE s.is_synthetic = 0 AND o.net_pnl_per_lot IS NOT NULL")]

    if not rows:
        return {"records": [], "sessions": 0, "trades": 0,
                "verdict": "No graded trades yet — the agent has no record."}

    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row["stance"] or "UNRULED", []).append(row)

    records = []
    for stance, items in grouped.items():
        by_session: dict = {}
        for item in items:
            by_session.setdefault(item["session"], []).append(item["pnl"])
        record = StanceRecord(
            stance=stance,
            trades=len(items),
            sessions=len(by_session),
            mean_pnl=round(sum(i["pnl"] for i in items) / len(items), 2),
            hit_rate=round(
                sum(1 for i in items if i["outcome"] == "TARGET") / len(items), 4),
            per_session=[
                {"session": day, "trades": len(values),
                 "mean_pnl": round(sum(values) / len(values), 2)}
                for day, values in sorted(by_session.items())
            ],
        )
        records.append(record)

    records.sort(key=lambda r: r.trades, reverse=True)
    sessions = len({row["session"] for row in rows})
    result = {
        "records": [r.as_dict() for r in records],
        "trades": len(rows),
        "sessions": sessions,
        "notes": [],
    }

    vetoed = next((r for r in records if r.stance == "VETO"), None)
    allowed = [r for r in records if r.stance in ("PROCEED", "CAUTION")]

    if vetoed and allowed:
        allowed_trades = sum(r.trades for r in allowed)
        allowed_mean = sum((r.mean_pnl or 0) * r.trades for r in allowed) / allowed_trades
        gap = (vetoed.mean_pnl or 0) - allowed_mean
        if gap < 0:
            result["verdict"] = (
                f"The veto is earning its place: vetoed trades averaged "
                f"Rs {vetoed.mean_pnl:,.0f} against Rs {allowed_mean:,.0f} "
                f"for the ones it allowed."
            )
        else:
            result["verdict"] = (
                f"The veto is blocking the better trades: vetoed averaged "
                f"Rs {vetoed.mean_pnl:,.0f} against Rs {allowed_mean:,.0f} "
                f"allowed. The thresholds need revisiting."
            )
    elif vetoed:
        result["verdict"] = (
            f"Every graded signal was vetoed ({vetoed.trades} trades across "
            f"{vetoed.sessions} session(s), averaging "
            f"Rs {vetoed.mean_pnl:,.0f}). There is no approved group to "
            f"compare against, so the veto cannot be scored yet — only "
            f"observed to have been pointed at losing trades."
        )
    else:
        result["verdict"] = (
            "No vetoed trades are graded, so the agent layer has no record."
        )

    if sessions < MIN_SESSIONS:
        result["notes"].append(
            f"{sessions} session(s) of evidence. A number from one or two days "
            f"describes those days."
        )
    result["notes"].append(
        "Sessions are reported separately because trades inside a day share "
        "that day's regime and are not independent draws."
    )
    return result


def render_agent(result: dict) -> str:
    lines = ["=" * 68, "AGENT SCORECARD  (would the vetoed trades have made money?)",
             "=" * 68,
             f"  {'stance':10} {'trades':>7} {'sessions':>9} {'mean P&L':>11} {'target':>8}"]
    for record in result.get("records", []):
        hit = f"{record['hit_rate'] * 100:.0f}%" if record["hit_rate"] is not None else "-"
        mean = f"Rs {record['mean_pnl']:,.0f}" if record["mean_pnl"] is not None else "-"
        lines.append(f"  {record['stance']:10} {record['trades']:7} "
                     f"{record['sessions']:9} {mean:>11} {hit:>8}")
    lines += ["", f"  VERDICT: {result.get('verdict', '')}"]
    for note in result.get("notes", []):
        lines.append(f"    - {note}")
    lines.append("=" * 68)
    return "\n".join(lines)


# ============================================================== conviction ==

def _wilson(successes: int, total: int, z: float = 1.96) -> tuple:
    """Wilson score interval. Correct at small n, where normal approximation is not."""
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
              / denominator)
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def conviction_calibration(*, db_path, buckets=(0, 40, 60, 80, 101)) -> dict:
    """Is conviction a probability, or only agreement among inputs?

    AGENTS.md is explicit that conviction "is agreement among inputs, not a
    probability of being right. Nothing here knows that number; only the ledger
    can estimate it." This estimates it: bucket every observation by the
    conviction it stated, then ask what the index actually did over the rest of
    that session.

    "Right" means the day finished in the direction the regime leaned. That is a
    crude target and deliberately so -- a conviction figure that cannot beat a
    coin flip on the easiest possible question has not earned a finer one.
    """
    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT o.session_date session, o.conviction, o.regime, "
            "d.spot_move_pct "
            "FROM observations o "
            "JOIN daily_record d ON d.session_date = o.session_date "
            "WHERE o.conviction IS NOT NULL AND o.regime IS NOT NULL "
            "AND d.spot_move_pct IS NOT NULL")]

    if not rows:
        return {"buckets": [], "observations": 0,
                "note": ("No observation joins a daily record yet. Conviction "
                         "cannot be calibrated until days are recorded.")}

    #: A move smaller than this counts as flat. A day that finished where it
    #: started did not confirm or refute a directional call, and scoring it as
    #: a miss punished the regime for a day the market did nothing -- which is
    #: how both conviction bands first read 0.0%.
    flat = 0.05

    def correct(row) -> bool | None:
        regime = (row["regime"] or "").upper()
        move = row["spot_move_pct"]
        if move is None or abs(float(move)) < flat:
            return None      # flat: no directional evidence either way
        if "UP" in regime:
            return move > 0
        if "DOWN" in regime:
            return move < 0
        return None          # RANGE / VOLATILE make no directional claim

    out = []
    for low, high in zip(buckets, buckets[1:]):
        chunk = [r for r in rows
                 if low <= float(r["conviction"]) < high and correct(r) is not None]
        if not chunk:
            continue
        hits = sum(1 for r in chunk if correct(r))
        lower, upper = _wilson(hits, len(chunk))
        out.append({
            "band": f"{low}-{high - 1}",
            "observations": len(chunk),
            "sessions": len({r["session"] for r in chunk}),
            "stated": round(sum(float(r["conviction"]) for r in chunk) / len(chunk), 1),
            "realised_pct": round(hits / len(chunk) * 100, 1),
            "ci_low": round(lower * 100, 1),
            "ci_high": round(upper * 100, 1),
        })

    sessions = len({r["session"] for r in rows})
    result = {"buckets": out, "observations": len(rows), "sessions": sessions,
              "notes": []}
    if not out:
        # An empty report has to say WHY. "Nothing to report" with no reason is
        # indistinguishable from "nothing happened", and that ambiguity is the
        # failure mode this project guards against everywhere else.
        scored = sum(1 for r in rows if correct(r) is not None)
        result["note"] = (
            f"{len(rows)} observation(s), none scoreable: "
            f"{len(rows) - scored} were a flat day or a non-directional regime, "
            f"which confirm nothing either way."
        )
    if sessions < MIN_SESSIONS:
        result["notes"].append(
            f"{sessions} session(s). Observations within a day are not "
            f"independent, so the interval above is optimistic."
        )
    directional = [b for b in out if b["observations"] >= 20]
    rates = [b["realised_pct"] for b in directional]
    if len(directional) >= 2 and len(set(rates)) > 1:
        rising = all(rates[i] <= rates[i + 1] for i in range(len(rates) - 1))
        result["notes"].append(
            "Higher conviction did go with a higher hit rate."
            if rising else
            "Higher conviction did NOT go with a higher hit rate, so the "
            "number is not behaving like a probability."
        )
    elif len(directional) >= 2:
        # "0% <= 0%" is monotonic and says nothing. Claiming a trend from it
        # would be the kind of vacuous finding this project keeps withdrawing.
        result["notes"].append(
            f"Every band read {rates[0]:.0f}%, so there is no ordering to "
            f"judge yet -- not evidence that conviction works or does not."
        )
    result["notes"].append(
        "Only TRENDING_UP and TRENDING_DOWN are scored; RANGE and VOLATILE "
        "make no directional claim and are excluded rather than counted as "
        "wrong."
    )
    return result


def render_conviction(result: dict) -> str:
    lines = ["=" * 68, "CONVICTION CALIBRATION  (is it a probability?)", "=" * 68,
             f"  {'band':>8} {'obs':>6} {'sess':>5} {'stated':>7} "
             f"{'realised':>9}  95% interval"]
    for row in result.get("buckets", []):
        lines.append(
            f"  {row['band']:>8} {row['observations']:6} {row['sessions']:5} "
            f"{row['stated']:7.1f} {row['realised_pct']:8.1f}%  "
            f"{row['ci_low']:.0f}% to {row['ci_high']:.0f}%")
    if not result.get("buckets"):
        lines.append(f"  {result.get('note', 'nothing to report')}")
    for note in result.get("notes", []):
        lines.append(f"    - {note}")
    lines.append("=" * 68)
    return "\n".join(lines)


# ============================================================== reliability ==

def odds_reliability(*, db_path, kind: str = "forward_odds",
                     buckets=(0.0, 0.3, 0.45, 0.55, 0.7, 1.01)) -> dict:
    """When a prediction says 47%, does 47% happen?

    Reads :mod:`intraday_advisor.archive`'s ``predictions`` table, where a
    stated probability sits beside what actually happened. Unresolved rows are
    counted and excluded, never treated as misses.
    """
    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT predicted, realised, session_date FROM predictions "
            "WHERE kind = ?", (kind,))]

    resolved = [r for r in rows if r["realised"] is not None]
    pending = len(rows) - len(resolved)
    if not resolved:
        return {"kind": kind, "buckets": [], "resolved": 0, "pending": pending,
                "note": (f"{len(rows)} {kind} prediction(s) stored, none "
                         f"resolved yet. Reliability needs outcomes.")}

    out = []
    for low, high in zip(buckets, buckets[1:]):
        chunk = [r for r in resolved if low <= float(r["predicted"]) < high]
        if not chunk:
            continue
        hits = sum(1 for r in chunk if float(r["realised"]) >= 0.5)
        lower, upper = _wilson(hits, len(chunk))
        out.append({
            "band": f"{low:.0%}-{high:.0%}",
            "n": len(chunk),
            "predicted": round(sum(float(r["predicted"]) for r in chunk) / len(chunk) * 100, 1),
            "realised": round(hits / len(chunk) * 100, 1),
            "ci_low": round(lower * 100, 1),
            "ci_high": round(upper * 100, 1),
        })

    result = {"kind": kind, "buckets": out, "resolved": len(resolved),
              "pending": pending,
              "sessions": len({r["session_date"] for r in resolved}),
              "notes": []}
    miscalibrated = [b for b in out
                     if b["n"] >= 20 and not (b["ci_low"] <= b["predicted"] <= b["ci_high"])]
    if miscalibrated:
        result["notes"].append(
            f"{len(miscalibrated)} band(s) have the stated probability outside "
            f"their own interval, so the number is not calibrated there."
        )
    elif out:
        result["notes"].append(
            "Every band with enough observations contains its stated "
            "probability in its interval — consistent with being calibrated, "
            "which is not the same as proven."
        )
    return result


def render_reliability(result: dict) -> str:
    lines = ["=" * 68,
             f"RELIABILITY: {result.get('kind')}  (does a stated % happen?)",
             "=" * 68,
             f"  {'band':>10} {'n':>5} {'said':>7} {'happened':>9}  95% interval"]
    for row in result.get("buckets", []):
        lines.append(f"  {row['band']:>10} {row['n']:5} {row['predicted']:6.1f}% "
                     f"{row['realised']:8.1f}%  {row['ci_low']:.0f}% to "
                     f"{row['ci_high']:.0f}%")
    if not result.get("buckets"):
        lines.append(f"  {result.get('note', 'nothing to report')}")
    lines.append(f"  resolved {result.get('resolved', 0)}, "
                 f"awaiting outcome {result.get('pending', 0)}")
    for note in result.get("notes", []):
        lines.append(f"    - {note}")
    lines.append("=" * 68)
    return "\n".join(lines)


# ========================================================= volatility history ==

def volatility_history(*, db_path, symbol: str | None = None) -> dict:
    """Daily realised variance from the archive, and a HAR-RV forecast.

    The point of storing bars: ``vol_forecast`` recomputed from a 60-day Yahoo
    window every call, which is too short to fit a model that needs daily,
    weekly and monthly terms. This feeds it whatever the archive holds.
    """
    from intraday_advisor.archive import stored_bars
    from intraday_advisor.index_options import INDEX_CONFIG

    name = symbol or INDEX_CONFIG["NIFTY"]["yahoo"]
    frame = stored_bars(name, db_path=db_path)
    if frame is None or frame.empty:
        return {"symbol": name, "sessions": 0,
                "note": "Nothing archived yet; run `ledger_cli archive`."}

    from intraday_advisor.vol_forecast import (
        fit_har,
        forecast_next,
        realised_variance_series,
    )

    try:
        variance = realised_variance_series(frame)
    except Exception as exc:  # noqa: BLE001
        return {"symbol": name, "sessions": 0,
                "note": f"Could not build a variance series: {exc}"}

    daily = variance.resample("1D").sum()
    daily = daily[daily > 0]
    result = {"symbol": name, "sessions": int(len(daily)),
              "latest": round(float(daily.iloc[-1]), 8) if len(daily) else None,
              "notes": []}
    try:
        model = fit_har(daily)
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"HAR-RV could not be fitted: {exc}"
        return result

    result["fit_rows"] = getattr(model, "rows", None)
    if not getattr(model, "fitted", False):
        # Surface the model's own reason. The previous gate let a sample
        # through that `fit_har` then rejected, and reported a bare "Model is
        # not fitted" instead of the explanation it had already written: the
        # daily/weekly/monthly lags consume the start of the series, so 60
        # sessions yield only 38 usable rows against the 100 a four-parameter
        # fit needs.
        reason = "; ".join(getattr(model, "notes", []) or []) or "not fitted"
        result["note"] = (
            f"{len(daily)} archived session(s) is not yet enough: {reason} "
            f"Keep archiving — this is the sample that grows."
        )
        return result

    result["forecast"] = forecast_next(daily, model)
    result["r_squared"] = getattr(model, "r_squared", None)
    result["notes"].append(
        "Fitted on archived bars, so this sample grows past what Yahoo serves "
        "instead of sliding with it."
    )
    return result
