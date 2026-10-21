"""Realised volatility, and the one question the chain cannot answer alone.

Why this module exists
----------------------
``options_math.py`` computes implied volatility and compares it to India VIX.
That answers "dear or cheap *relative to other options*". It cannot answer the
question that decides whether to **buy or sell** premium:

    is the market's expected movement larger than the movement that
    actually happens?

Implied volatility is a forecast. Realised volatility is the outcome. The gap
between them is the **variance risk premium**, and its sign tells you which
side of an option trade is being paid and which is paying. The app has spent
every session so far on the paying side -- ``trading-strategy-framework``
records it plainly: *"Puts were the dear side on both and I was buying them."*
Nothing on the dashboard could have said so, because realised volatility was
never computed anywhere in this codebase.

Why this is worth adding when more indicators are not
-----------------------------------------------------
``scoring.py`` carries 37 weights across 32 price components -- SuperTrend,
PSAR, MACD, ADX, ribbons, Chandelier, candle patterns. Every one of them is a
function of the same OHLC series, so they are not 32 pieces of evidence; they
are 32 views of one piece. That is why the score saturates, why ranking was a
30-way tie, and why ``validation.py`` measured no usable edge in it. Adding a
33rd function of the same prices adds nothing.

Realised volatility is also computed from price -- but the comparison against
*implied* volatility brings in information from outside the price series
entirely: what other participants are willing to pay. That makes the premium
genuinely orthogonal to the scorer, which is the only reason it earns a place.

What the research says, including the part that hurts
-----------------------------------------------------
The variance risk premium is well documented and positive in developed markets,
and a 2025 study of five years of NIFTY 50 and India VIX data reports implied
variance sitting above realised variance with a statistically significant
positive premium.

**And a 2026 SSRN backtest of the same premium on NIFTY concludes it cannot be
captured by a retail trader under current market microstructure.**

That second finding is not a footnote, it is the whole situation this project
is in. ``microstructure.py`` measures why: a two-rupee spread on a Rs 160
premium is 2.5% round trip, and a few volatility points of premium is worth
less than that. So this module is built to *report the premium honestly*, not
to generate a signal from it -- :func:`variance_risk_premium` names the cost
that must be cleared and refuses to call the premium an opportunity on its own.

Estimators, and why there is more than one
------------------------------------------
Close-to-close volatility throws away the high and the low -- every bar's
entire range reduced to its last print. The range-based estimators recover it:
Parkinson is roughly **5x** as efficient as close-to-close and Garman-Klass
roughly **7.4x**, meaning a 10-bar range estimate carries about as much
information as a 50-bar close-only one. On a 58-day sample of 5-minute bars
that is the difference between an estimate you can use intraday and one that
is still mostly noise by the close.

The catch: Parkinson and Garman-Klass assume continuous trading and **no
gaps**, so they understate volatility on a series with jumps. Within a session
that assumption is nearly true, which is exactly why they suit intraday bars.
Across sessions it is false -- the overnight move never enters the formula --
so daily bars want Rogers-Satchell (drift-independent) or Yang-Zhang (which
adds the overnight term back explicitly).

Annualisation, and the error it is easy to make here
----------------------------------------------------
``options_math.py`` prices with ``years = days_to_expiry / 365.0`` -- calendar
time. Realised volatility accrues only while the market is open, so the two
must be reconciled deliberately rather than by habit.

The reconciliation is that **252 trading days span one calendar year**, so
annualising a per-trading-bar volatility by the number of bars in a trading
year yields a figure directly comparable to that implied volatility. For NSE
5-minute bars: a 09:15-15:30 session is 375 minutes, so 75 bars a day and
18,900 a year. Using 365 in place of 252 here would overstate realised
volatility by about 20% and make every option look cheap.

Two honest limits on the intraday figure
----------------------------------------
**Microstructure noise.** At 5 minutes, bid-ask bounce inflates realised
volatility: a price alternating between bid and ask moves without the
underlying moving. Given the spreads ``microstructure.py`` measures, this is
not negligible, and it biases the premium *downwards* -- making options look
fairer than they are. The estimate is therefore conservative in the direction
that matters.

**The intraday shape.** Volatility is not flat across the session any more than
spreads are, so a realised figure measured over the first hour and annualised
describes a day made entirely of first hours. :func:`realized_volatility`
reports the window it used so this is visible rather than implicit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: Bars in a trading year on NSE, by bar size.
#:
#: The session runs 09:15-15:30 IST = 375 minutes. Trading days per year are
#: taken as 252, which is the figure that makes an annualised realised
#: volatility comparable to a Black-Scholes implied volatility quoted in
#: calendar years -- see the module docstring.
TRADING_DAYS_PER_YEAR = 252
SESSION_MINUTES = 375

BARS_PER_YEAR = {
    "1m": 375 * TRADING_DAYS_PER_YEAR,
    "3m": 125 * TRADING_DAYS_PER_YEAR,
    "5m": 75 * TRADING_DAYS_PER_YEAR,
    "15m": 25 * TRADING_DAYS_PER_YEAR,
    "30m": 12.5 * TRADING_DAYS_PER_YEAR,
    "60m": 6.25 * TRADING_DAYS_PER_YEAR,
    "1h": 6.25 * TRADING_DAYS_PER_YEAR,
    "1d": TRADING_DAYS_PER_YEAR,
}

#: Documented efficiency of each estimator relative to close-to-close, used
#: only to explain a choice in words -- never to weight anything.
_EFFICIENCY = {
    "close_to_close": 1.0,
    "parkinson": 5.0,
    "garman_klass": 7.4,
    "rogers_satchell": 6.0,
    "yang_zhang": 7.4,
}

#: Below this many bars a volatility estimate is not a measurement. Chosen so
#: that even the most efficient estimator has enough range observations to be
#: worth printing: 20 bars at 7.4x efficiency is comparable to ~150 closes.
MIN_BARS = 20


@dataclass
class RealizedVol:
    """A realised volatility estimate, with everything needed to doubt it."""

    method: str = ""
    annualized_pct: float | None = None
    per_bar_pct: float | None = None
    bars: int = 0
    bar_size: str = ""
    bars_per_year: float = 0.0
    window_start: object = None
    window_end: object = None
    #: Relative to close-to-close, from the literature. Context, not a weight.
    efficiency_vs_close: float = 1.0
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.annualized_pct is not None and self.bars >= MIN_BARS

    def as_dict(self) -> dict:
        from dataclasses import asdict
        out = asdict(self)
        out["window_start"] = str(self.window_start) if self.window_start is not None else None
        out["window_end"] = str(self.window_end) if self.window_end is not None else None
        out["usable"] = self.usable
        return out


def _logs(frame: pd.DataFrame) -> dict:
    """Log price relations, with non-positive prices removed rather than nan'd.

    A zero or negative price is bad data, and ``log`` of it is a warning plus a
    nan that then silently propagates through a mean. Dropping the row and
    saying how many were dropped is the honest handling.
    """
    needed = ("Open", "High", "Low", "Close")
    missing = [c for c in needed if c not in frame.columns]
    if missing:
        raise KeyError(f"frame is missing {missing}; need Open/High/Low/Close")

    data = frame[list(needed)].astype(float)
    valid = (data > 0).all(axis=1)
    dropped = int((~valid).sum())
    data = data[valid]

    return {
        "open": data["Open"].to_numpy(),
        "high": data["High"].to_numpy(),
        "low": data["Low"].to_numpy(),
        "close": data["Close"].to_numpy(),
        "index": data.index,
        "dropped": dropped,
    }


def _session_mask(index) -> np.ndarray:
    """True where a bar's previous bar belongs to the same session.

    Close-to-close returns that straddle an overnight gap are a different
    animal from intraday ones: they carry a whole night of news in one
    observation. Including them in an intraday estimate is what makes a
    5-minute volatility suddenly look like a crisis every morning at 09:15.
    """
    try:
        dates = pd.DatetimeIndex(index).normalize()
    except (TypeError, ValueError):
        return np.ones(max(0, len(index) - 1), dtype=bool)
    # Comparing two DatetimeIndex slices yields a plain ndarray, not a pandas
    # object, so asarray rather than .to_numpy().
    return np.asarray(dates[1:] == dates[:-1], dtype=bool)


def realized_volatility(
    frame: pd.DataFrame, *, method: str = "garman_klass",
    bar_size: str = "5m", session_aware: bool = True,
    bars_per_year: float | None = None,
) -> RealizedVol:
    """Annualised realised volatility, as a percentage.

    ``method`` is one of ``close_to_close``, ``parkinson``, ``garman_klass``,
    ``rogers_satchell`` or ``yang_zhang``.

    ``session_aware`` drops returns that span an overnight gap, which matters
    for every close-to-close term. Leave it on for intraday bars: an overnight
    jump annualised as if it were a 5-minute move is the single biggest way to
    get a nonsense number out of this function. Turn it off for daily bars,
    where the gap *is* part of the return you are measuring.
    """
    if method not in _EFFICIENCY:
        raise ValueError(
            f"unknown method {method!r}; expected one of "
            f"{sorted(_EFFICIENCY)}"
        )

    per_year = bars_per_year if bars_per_year is not None else BARS_PER_YEAR.get(bar_size)
    if per_year is None:
        raise ValueError(
            f"unknown bar_size {bar_size!r} and no bars_per_year given; "
            f"known sizes are {sorted(BARS_PER_YEAR)}"
        )

    result = RealizedVol(
        method=method, bar_size=bar_size, bars_per_year=float(per_year),
        efficiency_vs_close=_EFFICIENCY[method],
    )

    try:
        parts = _logs(frame)
    except KeyError as exc:
        result.notes.append(str(exc))
        return result

    o, h, l, c = parts["open"], parts["high"], parts["low"], parts["close"]
    n = len(c)
    result.bars = n
    if n:
        result.window_start, result.window_end = parts["index"][0], parts["index"][-1]
    if parts["dropped"]:
        result.notes.append(
            f"Dropped {parts['dropped']} bar(s) with a non-positive price -- "
            f"bad data, not low volatility."
        )

    if n < MIN_BARS:
        result.notes.append(
            f"{n} bars is below the {MIN_BARS} needed for a volatility "
            f"estimate to be a measurement rather than a guess."
        )
        return result

    with np.errstate(divide="ignore", invalid="ignore"):
        hl = np.log(h / l)
        co = np.log(c / o)

        if method == "close_to_close":
            returns = np.log(c[1:] / c[:-1])
            if session_aware:
                keep = _session_mask(parts["index"])
                if keep.size == returns.size:
                    dropped_gaps = int((~keep).sum())
                    returns = returns[keep]
                    if dropped_gaps:
                        result.notes.append(
                            f"Excluded {dropped_gaps} overnight return(s); an "
                            f"overnight gap is not a {bar_size} move."
                        )
            if returns.size < 2:
                result.notes.append("Not enough same-session returns to measure.")
                return result
            # Zero-mean: intraday drift over a few hours is noise, and
            # subtracting an estimated mean costs a degree of freedom for it.
            variance = float(np.nanmean(returns ** 2))

        elif method == "parkinson":
            variance = float(np.nanmean(hl ** 2) / (4.0 * math.log(2.0)))

        elif method == "garman_klass":
            variance = float(np.nanmean(
                0.5 * hl ** 2 - (2.0 * math.log(2.0) - 1.0) * co ** 2
            ))

        elif method == "rogers_satchell":
            hc, ho = np.log(h / c), np.log(h / o)
            lc, lo = np.log(l / c), np.log(l / o)
            variance = float(np.nanmean(hc * ho + lc * lo))

        else:  # yang_zhang
            if n < 3:
                result.notes.append("Yang-Zhang needs at least 3 bars.")
                return result
            overnight = np.log(o[1:] / c[:-1])
            open_close = co[1:]
            hc, ho = np.log(h[1:] / c[1:]), np.log(h[1:] / o[1:])
            lc, lo = np.log(l[1:] / c[1:]), np.log(l[1:] / o[1:])
            rs = np.nanmean(hc * ho + lc * lo)
            m = overnight.size
            var_o = float(np.nanvar(overnight, ddof=1)) if m > 1 else 0.0
            var_c = float(np.nanvar(open_close, ddof=1)) if m > 1 else 0.0
            k = 0.34 / (1.34 + (m + 1) / (m - 1)) if m > 1 else 0.34
            variance = var_o + k * var_c + (1 - k) * float(rs)
            if session_aware:
                result.notes.append(
                    "Yang-Zhang includes the overnight term by construction, "
                    "so session_aware does not apply to it. Use it on daily "
                    "bars, where that term is the point."
                )

    # Garman-Klass and Rogers-Satchell can go negative on a handful of bars:
    # both subtract terms, and on a tiny or degenerate sample the subtraction
    # can win. That is a sample problem, and reporting sqrt of a negative as
    # nan -- or worse, as zero -- would hide it.
    if not np.isfinite(variance):
        result.notes.append(
            f"The {method} variance came out non-finite, which means the "
            f"input has gaps or degenerate bars rather than that volatility "
            f"is unknowable. Check the frame."
        )
        return result
    if variance < 0:
        result.notes.append(
            f"The {method} estimator returned a negative variance "
            f"({variance:.3e}). It subtracts terms, so a short or degenerate "
            f"sample can do this; it is not a volatility of zero. Use more "
            f"bars or the parkinson estimator, which cannot go negative."
        )
        return result

    per_bar = math.sqrt(variance)
    result.per_bar_pct = round(per_bar * 100, 6)
    result.annualized_pct = round(per_bar * math.sqrt(per_year) * 100, 4)

    if bar_size in ("1m", "3m", "5m"):
        result.notes.append(
            "At this bar size bid-ask bounce inflates realised volatility, so "
            "this figure is biased slightly high -- which biases any variance "
            "premium computed from it slightly LOW. The premium is therefore "
            "conservative, not flattered."
        )
    return result


def volatility_cone(
    frame: pd.DataFrame, *, method: str = "garman_klass", bar_size: str = "5m",
    windows: tuple[int, ...] = (20, 60, 120, 240),
) -> dict:
    """Realised volatility over several lookbacks at once.

    One number cannot say whether volatility is high; it can only say what it
    is. A cone answers the question actually being asked -- *is the market
    moving more than it has been?* -- and that is what decides whether an
    implied volatility is expensive. The regime card reports India VIX and its
    direction for the same reason; this is the realised counterpart.
    """
    out: dict = {"method": method, "bar_size": bar_size, "windows": {}}
    estimates = []
    for window in windows:
        if len(frame) < window:
            out["windows"][window] = {"usable": False, "reason": "not enough bars"}
            continue
        estimate = realized_volatility(
            frame.tail(window), method=method, bar_size=bar_size)
        out["windows"][window] = estimate.as_dict()
        if estimate.usable:
            estimates.append((window, estimate.annualized_pct))

    if len(estimates) < 2:
        out["note"] = (
            "Fewer than two usable windows, so no term structure can be read."
        )
        return out

    shortest, short_value = estimates[0]
    longest, long_value = estimates[-1]
    out["short_window"], out["short_pct"] = shortest, short_value
    out["long_window"], out["long_pct"] = longest, long_value
    out["ratio"] = round(short_value / long_value, 3) if long_value else None

    if out["ratio"] and out["ratio"] >= 1.3:
        out["state"] = "expanding"
        out["note"] = (
            f"Recent {shortest}-bar volatility ({short_value:.1f}%) is "
            f"{out['ratio']:.2f}x the {longest}-bar figure "
            f"({long_value:.1f}%). Volatility is expanding, which widens "
            f"spreads and takes out stops that were sized for the calmer "
            f"regime."
        )
    elif out["ratio"] and out["ratio"] <= 0.75:
        out["state"] = "contracting"
        out["note"] = (
            f"Recent volatility ({short_value:.1f}%) is well below the "
            f"{longest}-bar figure ({long_value:.1f}%). Options priced off "
            f"the longer window are dear relative to what is happening now -- "
            f"and a contraction is where breakout components generate their "
            f"false signals."
        )
    else:
        out["state"] = "stable"
        out["note"] = (
            f"Volatility is steady across windows ({short_value:.1f}% over "
            f"{shortest} bars against {long_value:.1f}% over {longest})."
        )
    return out


@dataclass
class VariancePremium:
    """Implied against realised, and whether the gap survives the spread."""

    implied_pct: float | None = None
    realized_pct: float | None = None
    premium_points: float | None = None
    premium_ratio: float | None = None
    side_being_paid: str = "unknown"
    verdict: str = "unknown"
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def variance_risk_premium(
    implied_pct: float | None, realized: "RealizedVol | float | None", *,
    days_to_expiry: float | None = None,
    round_trip_cost_pct: float | None = None,
) -> VariancePremium:
    """Implied minus realised volatility, and who is being paid for it.

    ``implied_pct`` and ``realized`` are annualised volatilities in percent
    (15.2 means 15.2%). ``realized`` also accepts a :class:`RealizedVol`.

    ``round_trip_cost_pct`` is the cost of the structure as a percentage of the
    premium -- from ``microstructure.assess_tradeability``, not assumed. Supply
    it. Without it this function reports the premium and explicitly refuses to
    call it an opportunity, because that is the step where every option
    strategy in this project has previously gone wrong.

    The premium is positive far more often than not: that is the documented
    finding, and it is why systematically *buying* options is a losing posture
    before direction is even considered. It does not follow that selling them
    is a winning one -- a positive premium is compensation for taking crash
    risk, and the 2026 SSRN backtest on NIFTY concludes retail microstructure
    eats it. Both halves of that belong on screen together.
    """
    result = VariancePremium()

    realized_pct = None
    if isinstance(realized, RealizedVol):
        result.evidence["realized"] = realized.as_dict()
        if realized.usable:
            realized_pct = realized.annualized_pct
        else:
            result.verdict = "no realised estimate"
            result.reasons.append(
                "The realised volatility estimate is not usable: "
                + "; ".join(realized.notes or ["too few bars"])
            )
            return result
    elif realized is not None:
        realized_pct = float(realized)

    result.implied_pct = implied_pct
    result.realized_pct = realized_pct

    if implied_pct is None or realized_pct is None:
        result.verdict = "incomplete"
        result.reasons.append(
            "Both an implied and a realised volatility are needed. A missing "
            "implied volatility usually means a stale or wide quote, which "
            "options_math returns as None rather than as a confident wrong "
            "number -- and that absence is itself information about the "
            "contract's liquidity."
        )
        return result

    if realized_pct <= 0:
        result.verdict = "incomplete"
        result.reasons.append(
            "Realised volatility of zero is a data problem, not a calm "
            "market; the ratio against it is undefined."
        )
        return result

    points = implied_pct - realized_pct
    result.premium_points = round(points, 4)
    result.premium_ratio = round(implied_pct / realized_pct, 4)
    result.side_being_paid = (
        "seller" if points > 0 else "buyer" if points < 0 else "neither")

    if days_to_expiry:
        # What the premium is worth over the actual holding period, in the
        # units a trader sees: a percentage of the underlying, not an
        # annualised rate nobody holds for a year.
        horizon = math.sqrt(max(0.0, days_to_expiry) / 365.0)
        result.evidence["premium_over_horizon_pct"] = round(points * horizon, 4)
        result.evidence["days_to_expiry"] = days_to_expiry

    favours = "of premium to the seller" if points > 0 else "in the buyer's favour"
    result.reasons.append(
        f"Implied {implied_pct:.1f}% against realised {realized_pct:.1f}% -- "
        f"{abs(points):.1f} volatility point(s) {favours} "
        f"({result.premium_ratio:.2f}x)."
    )

    if round_trip_cost_pct is None:
        result.verdict = "premium measured, not assessed"
        result.reasons.append(
            "No measured round-trip cost supplied, so this is a measurement "
            "and not a recommendation. A few volatility points are routinely "
            "smaller than an option's spread: pass the figure from "
            "microstructure.assess_tradeability to find out which this is."
        )
        return result

    horizon_value = result.evidence.get("premium_over_horizon_pct")
    result.evidence["round_trip_cost_pct"] = round_trip_cost_pct

    if horizon_value is None:
        result.verdict = "premium measured, not assessed"
        result.reasons.append(
            "Without days_to_expiry the premium cannot be converted into the "
            "units the cost is quoted in, so the two are not comparable. An "
            "annualised volatility gap and a per-trade cost are different "
            "quantities and comparing them directly is meaningless."
        )
        return result

    # Both now in percent-of-underlying over the holding period. The premium is
    # a volatility edge, not a cash one, so the comparison is indicative --
    # said out loud rather than buried.
    if abs(horizon_value) <= round_trip_cost_pct:
        result.verdict = "premium smaller than costs"
        result.reasons.append(
            f"Over {result.evidence['days_to_expiry']:.0f} day(s) the premium "
            f"is worth about {abs(horizon_value):.3f}% of the underlying, "
            f"against a measured {round_trip_cost_pct:.3f}% round trip. The "
            f"gap is real and still too small to collect -- the same "
            f"arithmetic that defeated the +0.111% BANKNIFTY edge."
        )
    else:
        result.verdict = "premium exceeds measured costs"
        result.reasons.append(
            f"Over {result.evidence['days_to_expiry']:.0f} day(s) the premium "
            f"is worth about {abs(horizon_value):.3f}% against a "
            f"{round_trip_cost_pct:.3f}% round trip. It clears the cost on "
            f"this contract -- which makes it a candidate to test, not a "
            f"trade to place."
        )
        result.reasons.append(
            "Before acting: a positive premium is payment for bearing crash "
            "risk. Selling it earns small amounts often and loses large "
            "amounts rarely, so a win rate says almost nothing about it. Only "
            "ever sell inside a defined-risk structure "
            "(spreads.build_structure) and size for the tail, not the median."
        )
    return result


def implied_move_from_straddle(
    straddle_premium: float, spot: float, days_to_expiry: float,
) -> dict:
    """What the market is pricing as the move, straight off the straddle.

    Useful because it needs no volatility model at all. The ATM straddle is
    roughly what the market charges for movement in either direction, so it
    converts directly into an expected range -- and that range is the honest
    benchmark for a price target. When the dashboard's default +25% premium
    target implies a move larger than the straddle is pricing, the target is
    not ambitious, it is arithmetically unlikely.

    This is the same correction as setting the target from the option wall
    rather than the default, arrived at from the pricing side.
    """
    if spot <= 0 or straddle_premium <= 0 or days_to_expiry <= 0:
        return {
            "usable": False,
            "note": "Need a positive spot, straddle premium and time to expiry.",
        }

    move_pct = straddle_premium / spot * 100
    # The ATM straddle is worth about 0.8 * sigma * sqrt(T) * S under
    # Black-Scholes, so inverting that constant recovers the implied vol
    # without a solver. Approximate by construction, and labelled as such.
    years = days_to_expiry / 365.0
    implied = move_pct / (0.7979 * math.sqrt(years)) if years > 0 else None

    return {
        "usable": True,
        "straddle_premium": straddle_premium,
        "spot": spot,
        "days_to_expiry": days_to_expiry,
        "implied_move_pct": round(move_pct, 4),
        "implied_move_points": round(straddle_premium, 2),
        "approx_implied_vol_pct": round(implied, 2) if implied else None,
        "note": (
            f"The market is pricing a move of about {move_pct:.2f}% "
            f"({straddle_premium:,.0f} points) by expiry, in either "
            f"direction. A target beyond that is betting against the price of "
            f"the straddle, which is a position worth taking knowingly rather "
            f"than by leaving a default at +25%."
        ),
        "caveat": (
            "The 0.7979 constant is the Black-Scholes ATM straddle "
            "approximation (sqrt(2/pi)); it is exact only at zero rates and "
            "zero skew. Use options_math.implied_vol for a solved figure."
        ),
    }
