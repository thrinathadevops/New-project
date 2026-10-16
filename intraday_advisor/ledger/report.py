"""Turn graded signals into the only numbers that matter.

This module answers three questions and refuses to answer them when the data
cannot support an answer:

1. **Does the strategy make money after costs?**  :func:`expectancy_report`
2. **Does a higher score actually mean a better trade?**  :func:`score_band_report`
3. **Where does the money come from?**  :func:`by_dimension`

Why there is so much statistics in here
---------------------------------------
A 60% win rate over 20 trades is consistent with a true win rate anywhere from
36% to 80%. Acting on it is indistinguishable from guessing. Every proportion
reported here carries a **Wilson score interval**, and every expectancy carries
a standard error, because a point estimate with no interval is what convinces
people to scale up a strategy that never worked.

The honest summary line is produced by :func:`verdict`, which will say
"not enough data" for as long as that is the truth -- which, starting from an
empty ledger, is a few hundred trades. There is no way to shorten that except
to replay history through the same grader.

Win = positive NET P&L
----------------------
Not "target touched". A target hit on a cheap contract whose round-trip costs
exceed the gross gain is a loss, and is counted as one.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from intraday_advisor.ledger.grader import RESOLVED, STOP, TARGET, TIMEOUT
from intraday_advisor.ledger.schema import DEFAULT_LEDGER_PATH, connect, init_ledger

# Below this many resolved trades, report numbers but refuse to draw a
# conclusion. 30 is the bare statistical minimum for a usable interval; 200+ is
# where a win-rate estimate becomes tight enough to size on.
MIN_SAMPLE_FOR_VERDICT = 30

# Trades needed per fitted weight before calibration is anything but curve
# fitting. 15 is already generous for financial data, where the signal-to-noise
# ratio is far worse than the textbook examples this rule of thumb comes from.
TRADES_PER_WEIGHT = 15


def _min_sample_for_calibration() -> int:
    """Scale the calibration gate with the number of weights being fitted.

    This was a flat 200, set when the scorer had 7 weights -- about 29 trades
    per parameter. The indicator set has since grown to 37 weights, which at
    200 trades is 5 per parameter: enough to fit the noise in those 200 trades
    beautifully and nothing else. Deriving the gate means adding indicators
    automatically raises the evidence bar instead of quietly lowering it.
    """
    from intraday_advisor.scoring import ScoreWeights

    weights = len(ScoreWeights().__dataclass_fields__)
    return max(200, weights * TRADES_PER_WEIGHT)


MIN_SAMPLE_FOR_CALIBRATION = _min_sample_for_calibration()

# Default score buckets. Deliberately coarse: finer bands just slice an already
# small sample into noise.
DEFAULT_BANDS = (0, 50, 60, 70, 80, 90, 101)

Z_95 = 1.959963984540054



def _weight_count() -> int:
    from intraday_advisor.scoring import ScoreWeights

    return len(ScoreWeights().__dataclass_fields__)


def wilson_interval(successes: int, total: int, z: float = Z_95) -> tuple[float, float]:
    """95% Wilson score interval for a proportion.

    Preferred over the normal approximation because it stays inside [0, 1] and
    behaves sensibly at small n and at proportions near 0 or 1 -- exactly the
    regime a new trading ledger lives in.
    """
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def load_graded(
    *,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
    engine_version: str | None = None,
    grader_version: str | None = None,
    session_from: str | None = None,
    session_to: str | None = None,
    index_name: str | None = None,
    resolved_only: bool = True,
) -> pd.DataFrame:
    """Load the ``graded_signals`` view as a DataFrame.

    Synthetic rows are already excluded by the view. ``resolved_only`` drops
    NO_DATA / NOT_FILLED rows from statistics -- but they are still counted in
    :func:`data_quality`, because a strategy whose signals are mostly ungradeable
    has a data problem masquerading as a strategy.
    """
    where: list[str] = []
    params: list[object] = []
    if engine_version:
        where.append("engine_version = ?")
        params.append(engine_version)
    if grader_version:
        where.append("grader_version = ?")
        params.append(grader_version)
    if session_from:
        where.append("session_date >= ?")
        params.append(session_from)
    if session_to:
        where.append("session_date <= ?")
        params.append(session_to)
    if index_name:
        where.append("index_name = ?")
        params.append(index_name.upper())
    if resolved_only:
        placeholders = ", ".join("?" for _ in RESOLVED)
        where.append(f"outcome IN ({placeholders})")
        params.extend(RESOLVED)

    sql = "SELECT * FROM graded_signals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY bar_time ASC"

    init_ledger(db_path)
    with connect(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def _stats(frame: pd.DataFrame) -> dict:
    """Core performance statistics for a set of resolved trades."""
    n = int(len(frame))
    if n == 0:
        return {
            "trades": 0, "win_rate": None, "win_rate_low": None, "win_rate_high": None,
            "wins": 0, "losses": 0, "expectancy": None, "expectancy_se": None,
            "expectancy_low": None, "expectancy_high": None, "avg_win": None,
            "avg_loss": None, "profit_factor": None, "total_net": 0.0,
            "avg_r": None, "target_rate": None, "stop_rate": None, "timeout_rate": None,
            "best": None, "worst": None,
        }

    pnl = pd.to_numeric(frame["net_pnl_per_lot"], errors="coerce").dropna()
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    win_count = int(len(wins))
    low, high = wilson_interval(win_count, len(pnl)) if len(pnl) else (None, None)

    mean = float(pnl.mean()) if len(pnl) else None
    # Standard error of the mean: with n this small the interval is the finding.
    se = float(pnl.std(ddof=1) / math.sqrt(len(pnl))) if len(pnl) > 1 else None
    exp_low = mean - Z_95 * se if (mean is not None and se is not None) else None
    exp_high = mean + Z_95 * se if (mean is not None and se is not None) else None

    gross_profit = float(wins.sum())
    gross_loss = abs(float(losses.sum()))
    outcomes = frame["outcome"].value_counts()
    r_values = pd.to_numeric(frame.get("r_multiple"), errors="coerce").dropna()

    return {
        "trades": n,
        "wins": win_count,
        "losses": int(len(losses)),
        "win_rate": round(win_count / len(pnl), 4) if len(pnl) else None,
        "win_rate_low": round(low, 4) if low is not None else None,
        "win_rate_high": round(high, 4) if high is not None else None,
        "expectancy": round(mean, 2) if mean is not None else None,
        "expectancy_se": round(se, 2) if se is not None else None,
        "expectancy_low": round(exp_low, 2) if exp_low is not None else None,
        "expectancy_high": round(exp_high, 2) if exp_high is not None else None,
        "avg_win": round(float(wins.mean()), 2) if win_count else None,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "total_net": round(float(pnl.sum()), 2),
        "avg_r": round(float(r_values.mean()), 3) if len(r_values) else None,
        "target_rate": round(int(outcomes.get(TARGET, 0)) / n, 4),
        "stop_rate": round(int(outcomes.get(STOP, 0)) / n, 4),
        "timeout_rate": round(int(outcomes.get(TIMEOUT, 0)) / n, 4),
        "best": round(float(pnl.max()), 2) if len(pnl) else None,
        "worst": round(float(pnl.min()), 2) if len(pnl) else None,
    }


def expectancy_report(
    *, db_path: str | Path = DEFAULT_LEDGER_PATH, **filters
) -> dict:
    """Headline performance, per lot, net of costs.

    ``expectancy`` is the average net rupees per lot per trade. It is the single
    number that decides whether the strategy is worth trading: positive means
    each signal is worth taking, negative means every trade is a donation.
    """
    frame = load_graded(db_path=db_path, **filters)
    stats = _stats(frame)
    stats["verdict"] = verdict(stats)
    if not frame.empty:
        stats["sessions"] = int(frame["session_date"].nunique())
        stats["first_session"] = str(frame["session_date"].min())
        stats["last_session"] = str(frame["session_date"].max())
        per_session = frame.groupby("session_date")["net_pnl_per_lot"].sum()
        stats["profitable_sessions"] = int((per_session > 0).sum())
        stats["total_sessions"] = int(len(per_session))
        stats["avg_session_net"] = round(float(per_session.mean()), 2)
        stats["worst_session_net"] = round(float(per_session.min()), 2)
    return stats


def verdict(stats: dict) -> str:
    """One honest sentence about what the data does and does not support."""
    n = stats.get("trades", 0)
    if n == 0:
        return "No graded trades yet. Record signals, then grade them after the close."
    if n < MIN_SAMPLE_FOR_VERDICT:
        return (
            f"Only {n} graded trades - far too few to conclude anything. "
            f"Need {MIN_SAMPLE_FOR_VERDICT}+ for a usable interval, "
            f"{MIN_SAMPLE_FOR_CALIBRATION}+ before changing weights."
        )

    low = stats.get("expectancy_low")
    high = stats.get("expectancy_high")
    expectancy = stats.get("expectancy")
    if low is None or high is None:
        return f"{n} trades, expectancy Rs {expectancy}/lot; interval unavailable."

    if low > 0:
        return (
            f"Positive expectancy: Rs {expectancy}/lot "
            f"(95% CI Rs {low} to Rs {high}) over {n} trades. "
            f"The whole interval is above zero, so this is unlikely to be luck."
        )
    if high < 0:
        return (
            f"NEGATIVE expectancy: Rs {expectancy}/lot "
            f"(95% CI Rs {low} to Rs {high}) over {n} trades. "
            f"The whole interval is below zero - this strategy loses money after costs. "
            f"Do not trade it."
        )
    return (
        f"Inconclusive: expectancy Rs {expectancy}/lot "
        f"(95% CI Rs {low} to Rs {high}) over {n} trades. "
        f"The interval straddles zero, so no edge is demonstrated yet."
    )


def score_band_report(
    *,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
    bands: tuple[int, ...] = DEFAULT_BANDS,
    **filters,
) -> pd.DataFrame:
    """Performance by score band -- the test of whether the score means anything.

    If win rate and expectancy do not rise with the score band, the scorer is
    not ranking trades, and a "confidence 85" signal deserves no more capital
    than a "confidence 60" one. That finding is the point of the whole ledger.
    """
    frame = load_graded(db_path=db_path, **filters)
    if frame.empty:
        return pd.DataFrame(columns=["band", "trades", "win_rate", "expectancy"])

    frame = frame.copy()
    frame["band"] = pd.cut(
        pd.to_numeric(frame["score"], errors="coerce"),
        bins=list(bands), right=False,
        labels=[f"{bands[i]}-{bands[i + 1] - 1}" for i in range(len(bands) - 1)],
    )

    rows = []
    for band, group in frame.groupby("band", observed=True):
        stats = _stats(group)
        rows.append({
            "band": str(band),
            "trades": stats["trades"],
            "win_rate": stats["win_rate"],
            "win_rate_low": stats["win_rate_low"],
            "win_rate_high": stats["win_rate_high"],
            "expectancy": stats["expectancy"],
            "expectancy_low": stats["expectancy_low"],
            "expectancy_high": stats["expectancy_high"],
            "avg_r": stats["avg_r"],
            "profit_factor": stats["profit_factor"],
            "total_net": stats["total_net"],
        })
    return pd.DataFrame(rows)


def score_rank_correlation(
    *, db_path: str | Path = DEFAULT_LEDGER_PATH, **filters
) -> dict:
    """Spearman rank correlation between score and net P&L.

    Computed from ranks with no scipy dependency. The interpretation that
    matters: a value near zero means the score carries no information about
    outcome, however plausible its components look on screen.
    """
    frame = load_graded(db_path=db_path, **filters)
    if frame.empty or len(frame) < 10:
        return {"n": int(len(frame)), "spearman": None,
                "interpretation": "Need at least 10 graded trades."}

    scores = pd.to_numeric(frame["score"], errors="coerce")
    pnl = pd.to_numeric(frame["net_pnl_per_lot"], errors="coerce")
    valid = scores.notna() & pnl.notna()
    scores, pnl = scores[valid], pnl[valid]
    if len(scores) < 10 or scores.nunique() < 2:
        return {"n": int(len(scores)), "spearman": None,
                "interpretation": "Not enough variation in score to correlate."}

    rho = float(scores.rank().corr(pnl.rank()))
    if abs(rho) < 0.1:
        reading = ("No relationship: the score does not rank trades. "
                   "Treat all signals as equal until the weights are calibrated.")
    elif rho < 0:
        reading = ("NEGATIVE: higher-scored signals did worse. "
                   "The scorer is actively misleading and must be re-fitted or ignored.")
    elif rho < 0.3:
        reading = "Weak positive: the score ranks trades slightly better than chance."
    else:
        reading = "Positive: higher scores really did produce better trades."

    return {"n": int(len(scores)), "spearman": round(rho, 3), "interpretation": reading}


def by_dimension(
    dimension: str,
    *,
    db_path: str | Path = DEFAULT_LEDGER_PATH,
    min_trades: int = 5,
    **filters,
) -> pd.DataFrame:
    """Break performance down by any recorded dimension.

    Useful ones: ``index_name``, ``option_type``, ``direction``, ``session_date``,
    ``underlying_bias``, ``chain_bias``, and the synthetic ``hour`` /
    ``moneyness_steps`` columns added below.

    Groups thinner than ``min_trades`` are returned but should be read as
    anecdotes, not evidence.
    """
    frame = load_graded(db_path=db_path, **filters)
    if frame.empty:
        return pd.DataFrame()

    frame = frame.copy()
    if dimension == "hour":
        frame["hour"] = pd.to_datetime(
            frame["bar_time"], errors="coerce", format="mixed"
        ).dt.hour
    if dimension not in frame.columns:
        raise ValueError(
            f"Unknown dimension {dimension!r}. Available: {sorted(frame.columns)}"
        )

    rows = []
    for key, group in frame.groupby(dimension, observed=True, dropna=False):
        stats = _stats(group)
        rows.append({
            dimension: key,
            "trades": stats["trades"],
            "win_rate": stats["win_rate"],
            "expectancy": stats["expectancy"],
            "expectancy_low": stats["expectancy_low"],
            "total_net": stats["total_net"],
            "avg_r": stats["avg_r"],
            "thin_sample": stats["trades"] < min_trades,
        })
    out = pd.DataFrame(rows)
    return out.sort_values("expectancy", ascending=False, na_position="last")


def data_quality(*, db_path: str | Path = DEFAULT_LEDGER_PATH, **filters) -> dict:
    """Health of the measurement itself, before trusting any performance number.

    Three things here silently invalidate results elsewhere:

      * ``ungradeable_rate`` -- signals with no option token or no premium data.
        High values mean the sample is whatever happened to be gradeable.
      * ``median_fill_slippage`` -- how far the premium on screen was from a
        fill. Persistently positive means the dashboard quotes fantasy prices.
      * ``same_bar_conflict_rate`` -- how often stop and target fell in one bar
        and were resolved pessimistically. High values mean the bar interval is
        too coarse for the stop distance and the grade is a coin flip.
    """
    filters.pop("resolved_only", None)
    everything = load_graded(db_path=db_path, resolved_only=False, **filters)
    if everything.empty:
        return {"graded": 0, "note": "Nothing graded yet."}

    total = int(len(everything))
    resolved = everything[everything["outcome"].isin(RESOLVED)]
    slippage = pd.to_numeric(resolved.get("fill_slippage"), errors="coerce").dropna()
    conflicts = pd.to_numeric(resolved.get("same_bar_conflict"), errors="coerce").fillna(0)

    quality = {
        "graded": total,
        "resolved": int(len(resolved)),
        "ungradeable": total - int(len(resolved)),
        "ungradeable_rate": round((total - len(resolved)) / total, 4),
        "outcome_counts": {str(k): int(v) for k, v in
                           everything["outcome"].value_counts().items()},
        "median_fill_slippage": round(float(slippage.median()), 2) if len(slippage) else None,
        "mean_fill_slippage": round(float(slippage.mean()), 2) if len(slippage) else None,
        "same_bar_conflict_rate": round(float(conflicts.mean()), 4) if len(conflicts) else None,
        "price_sources": {str(k): int(v) for k, v in
                          resolved.get("price_source", pd.Series(dtype=str))
                          .value_counts().items()},
    }

    flags = []
    if quality["ungradeable_rate"] > 0.2:
        flags.append(
            f"{quality['ungradeable_rate'] * 100:.0f}% of signals could not be graded - "
            f"performance stats cover a biased subset. Fix option-token resolution first."
        )
    if quality["median_fill_slippage"] is not None and quality["median_fill_slippage"] > 0:
        flags.append(
            f"Median fill was Rs {quality['median_fill_slippage']:.2f} worse than the "
            f"quoted premium - the screen is showing prices you cannot get."
        )
    if quality["same_bar_conflict_rate"] and quality["same_bar_conflict_rate"] > 0.15:
        flags.append(
            f"{quality['same_bar_conflict_rate'] * 100:.0f}% of trades hit stop and target "
            f"in the same bar. Use a finer interval or a wider stop; these grades are "
            f"effectively coin flips resolved as losses."
        )
    quality["flags"] = flags
    return quality


def summary(*, db_path: str | Path = DEFAULT_LEDGER_PATH, **filters) -> str:
    """Everything above as one printable block. The daily read."""
    from intraday_advisor.ledger.schema import ledger_stats

    stats = ledger_stats(db_path)
    performance = expectancy_report(db_path=db_path, **filters)
    quality = data_quality(db_path=db_path, **filters)
    correlation = score_rank_correlation(db_path=db_path, **filters)

    lines = [
        "=" * 72,
        "LEDGER",
        "=" * 72,
        f"  Signals recorded   {stats['real_signals']:>8}  "
        f"({stats['synthetic_signals']} synthetic, excluded)",
        f"  Graded             {stats['graded']:>8}   Ungraded {stats['ungraded']}",
        f"  Sessions           {stats['sessions']:>8}  "
        f"({stats['first_session']} to {stats['last_session']})",
        "",
        "PERFORMANCE (per lot, net of all costs)",
        "-" * 72,
    ]

    if performance["trades"] == 0:
        lines.append("  Nothing resolved yet.")
    else:
        lines += [
            f"  Resolved trades    {performance['trades']:>8}",
            f"  Win rate           {performance['win_rate'] * 100:>7.1f}%  "
            f"(95% CI {performance['win_rate_low'] * 100:.1f}% - "
            f"{performance['win_rate_high'] * 100:.1f}%)",
            f"  Expectancy         Rs {performance['expectancy']:>7,.2f}/lot"
            + (f"  (95% CI {performance['expectancy_low']:,.2f} to "
               f"{performance['expectancy_high']:,.2f})"
               if performance["expectancy_low"] is not None else ""),
            f"  Avg win / loss     Rs {performance['avg_win'] or 0:>7,.2f} / "
            f"Rs {performance['avg_loss'] or 0:,.2f}",
            f"  Profit factor      {performance['profit_factor'] or 0:>8.2f}",
            f"  Total net          Rs {performance['total_net']:>7,.2f}",
            f"  Outcomes           target {performance['target_rate'] * 100:.0f}%  "
            f"stop {performance['stop_rate'] * 100:.0f}%  "
            f"timeout {performance['timeout_rate'] * 100:.0f}%",
        ]

    lines += ["", "SCORE QUALITY", "-" * 72,
              f"  Spearman(score, net P&L) = {correlation.get('spearman')}",
              f"  {correlation.get('interpretation')}", "",
              "DATA QUALITY", "-" * 72]
    if quality.get("graded"):
        lines.append(
            f"  Ungradeable {quality['ungradeable']}/{quality['graded']} "
            f"({quality['ungradeable_rate'] * 100:.0f}%)   "
            f"median fill slippage Rs {quality.get('median_fill_slippage')}"
        )
    for flag in quality.get("flags", []):
        lines.append(f"  ! {flag}")

    lines += ["", "VERDICT", "-" * 72]
    lines += [f"  {line}" for line in _wrap(performance["verdict"], 68)]
    lines.append("=" * 72)
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def can_calibrate(*, db_path: str | Path = DEFAULT_LEDGER_PATH, **filters) -> dict:
    """Whether there is enough data to re-fit the score weights.

    Deliberately conservative. Fitting seven weights on 50 trades produces a
    model that describes the noise in those 50 trades and nothing else -- it
    will look excellent in-sample and fail live. This gate exists so that
    ``ScoreWeights.load()`` is never handed numbers fitted on nothing.
    """
    frame = load_graded(db_path=db_path, **filters)
    n = int(len(frame))
    sessions = int(frame["session_date"].nunique()) if not frame.empty else 0
    min_sessions = max(20, MIN_SAMPLE_FOR_CALIBRATION // 10)
    ready = n >= MIN_SAMPLE_FOR_CALIBRATION and sessions >= min_sessions
    return {
        "ready": ready,
        "trades": n,
        "trades_needed": max(0, MIN_SAMPLE_FOR_CALIBRATION - n),
        "sessions": sessions,
        "sessions_needed": max(0, min_sessions - sessions),
        "reason": (
            "Enough data to attempt weight calibration with walk-forward validation."
            if ready else
            f"Need {MIN_SAMPLE_FOR_CALIBRATION}+ trades across {min_sessions}+ "
            f"sessions to fit {_weight_count()} weights at "
            f"{TRADES_PER_WEIGHT} trades each; have {n} across {sessions}. "
            f"Fitting now would fit noise."
        ),
    }
