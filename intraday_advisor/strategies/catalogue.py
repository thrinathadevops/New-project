"""The strategies themselves.

Each one is the standard market formulation, written so it can be measured
rather than argued about. Where a strategy is usually described with
discretionary wording ("wait for confirmation", "if it looks strong"), the
mechanical reading is used and the choice is stated -- a rule that needs a
human to interpret it cannot be backtested, and cannot be shown to be wrong.

Every ``signal`` function is **causal**: it may read bar *i* and earlier only.
Anywhere a rolling window is used it is closed at bar *i*, and anywhere a
session statistic is used it is the session-to-date, never the full session.
That is the one invariant that matters here, because breaking it produces a
beautiful backtest and no money.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import Strategy, register


# --------------------------------------------------------------------------- #
# helpers -- all strictly backward-looking
# --------------------------------------------------------------------------- #

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(frame: pd.DataFrame, window: int = 14) -> pd.Series:
    high, low, close = frame["High"], frame["Low"], frame["Close"]
    previous = close.shift(1)
    spread = pd.concat([
        high - low, (high - previous).abs(), (low - previous).abs()
    ], axis=1).max(axis=1)
    return spread.rolling(window).mean()


def _session_key(frame: pd.DataFrame):
    return pd.DatetimeIndex(frame.index).date


def _session_cumulative(frame: pd.DataFrame, column: str, how: str):
    """Session-to-date statistic, shifted so bar *i* never sees its own bar.

    The shift is the whole point. An opening-range high that includes the
    current bar's high is a level price has by definition already reached, so a
    "breakout above it" triggers on the bar that set it -- which backtests as a
    perfect momentum signal and is unachievable live.
    """
    grouped = frame.groupby(_session_key(frame))[column]
    running = grouped.cummax() if how == "max" else grouped.cummin()
    return running.shift(1)


def _bars_into_session(frame: pd.DataFrame) -> pd.Series:
    return frame.groupby(_session_key(frame)).cumcount()


def _zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    sd = series.rolling(window).std(ddof=1)
    return (series - mean) / sd.replace(0.0, np.nan)


# ============================================================ trend following ==

def _ema_cross(frame: pd.DataFrame) -> pd.Series:
    fast, slow = _ema(frame["Close"], 9), _ema(frame["Close"], 21)
    return pd.Series(np.where(fast > slow, 1.0, -1.0), index=frame.index)


register(Strategy(
    name="ema_cross_9_21",
    family="trend",
    thesis=(
        "The classic moving-average cross: hold long while the fast EMA is "
        "above the slow one. Included as the CONTROL. It is the cheapest "
        "possible trend rule, so any trend strategy that cannot beat it is "
        "adding complexity without information -- and the scorer is largely a "
        "weighted blend of rules like this one."
    ),
    signal=_ema_cross,
    horizon=12,
    falsified_by=(
        "A negative lag-1 autocorrelation of returns, which is what was "
        "measured intraday (about -0.09): strength followed by weakness means "
        "every momentum rule is fighting the series."
    ),
    source="standard",
))


def _adx_filtered_trend(frame: pd.DataFrame) -> pd.Series:
    """Trend, but only while the trend is strong enough to be worth following."""
    close = frame["Close"]
    fast, slow = _ema(close, 9), _ema(close, 21)
    atr = _atr(frame, 14)
    # A cheap trend-strength proxy: how far the EMAs have separated relative to
    # the bar's own range. Using the real ADX would import the indicator stack;
    # the point here is the FILTER, not which strength measure implements it.
    strength = (fast - slow).abs() / atr.replace(0.0, np.nan)
    direction = np.where(fast > slow, 1.0, -1.0)
    return pd.Series(np.where(strength > 0.5, direction, 0.0), index=frame.index)


register(Strategy(
    name="trend_with_strength_filter",
    family="trend",
    thesis=(
        "The same cross, standing aside when the EMAs have barely separated. "
        "Most of a trend rule's losses come from the chop it trades through "
        "while waiting, so the filter is where the value is claimed to be. "
        "Measuring it against the unfiltered control says whether that is true "
        "or merely plausible."
    ),
    signal=_adx_filtered_trend,
    horizon=12,
    falsified_by=(
        "The filtered version failing to beat ema_cross_9_21 net of costs -- "
        "it takes fewer trades, so it must earn more per trade to be worth it."
    ),
    source="standard",
))


# ============================================================ mean reversion ==

def _zscore_fade(frame: pd.DataFrame) -> pd.Series:
    z = _zscore(frame["Close"], 48)
    return pd.Series(
        np.where(z <= -2.0, 1.0, np.where(z >= 2.0, -1.0, 0.0)),
        index=frame.index,
    )


register(Strategy(
    name="zscore_fade_2sigma",
    family="mean_reversion",
    thesis=(
        "Buy two standard deviations below the recent mean, sell two above. "
        "The direct expression of the one statistical property this project "
        "actually measured in the index: lag-1 autocorrelation of -0.088 "
        "(NIFTY) and -0.099 (BANKNIFTY) at 5 minutes, against a sample-aware "
        "threshold of 0.030. If anything directional works here, it is shaped "
        "like this."
    ),
    signal=_zscore_fade,
    horizon=12,
    falsified_by=(
        "The measured reversion is real and TINY -- about 0.1% per trade "
        "against a 0.19% equity and 0.50%+ option round trip. This is expected "
        "to be significant and unprofitable, which is a different failure from "
        "being wrong."
    ),
    source="standard; the autocorrelation is measured in validation.py",
))


def _vwap_reversion(frame: pd.DataFrame) -> pd.Series:
    """Fade distance from the session VWAP. Needs real volume."""
    volume = frame.get("Volume")
    if volume is None or float(pd.Series(volume).fillna(0).sum()) <= 0:
        return pd.Series(0.0, index=frame.index)
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3.0
    key = _session_key(frame)
    cum_pv = (typical * volume).groupby(key).cumsum()
    cum_v = pd.Series(volume, index=frame.index).groupby(key).cumsum()
    vwap = cum_pv / cum_v.replace(0.0, np.nan)
    atr = _atr(frame, 14)
    gap = (frame["Close"] - vwap) / atr.replace(0.0, np.nan)
    return pd.Series(
        np.where(gap <= -1.5, 1.0, np.where(gap >= 1.5, -1.0, 0.0)),
        index=frame.index,
    )


register(Strategy(
    name="vwap_reversion",
    family="mean_reversion",
    thesis=(
        "Fade price when it stretches more than an ATR from the session VWAP. "
        "VWAP is the benchmark institutional execution is measured against, so "
        "the argument is that flow is drawn back to it -- a reason rooted in "
        "who is trading rather than in the shape of a line."
    ),
    signal=_vwap_reversion,
    horizon=12,
    instrument="equity",
    requires=("Volume",),
    falsified_by=(
        "Index candles carry no volume, so this returns flat on ^NSEI and "
        "^NSEBANK by construction and can only be measured on constituents."
    ),
    source="standard",
))


# =================================================================== breakout ==

def _opening_range_breakout(frame: pd.DataFrame, bars: int = 3) -> pd.Series:
    """Break of the first ``bars`` of the session, measured without lookahead."""
    key = _session_key(frame)
    position = _bars_into_session(frame)
    high = frame.groupby(key)["High"].cummax().shift(1)
    low = frame.groupby(key)["Low"].cummin().shift(1)
    # Freeze the range at the end of the opening window: after that the levels
    # must stop updating, or "the breakout level" keeps running away from price
    # and the rule can never trigger.
    opening_high = high.where(position == bars).groupby(key).transform("max")
    opening_low = low.where(position == bars).groupby(key).transform("min")
    close = frame["Close"]
    direction = np.where(
        (position > bars) & (close > opening_high), 1.0,
        np.where((position > bars) & (close < opening_low), -1.0, 0.0),
    )
    return pd.Series(direction, index=frame.index)


register(Strategy(
    name="opening_range_breakout_15m",
    family="breakout",
    thesis=(
        "Trade the break of the first fifteen minutes. The most widely traded "
        "intraday rule on NSE, and already present inside the scorer as "
        "orb_breakout / orb_retest / orb_trap components -- which have never "
        "been measured on their own because the score sums them with 29 others."
    ),
    signal=_opening_range_breakout,
    horizon=12,
    falsified_by=(
        "NSE's own microstructure work puts spreads WIDEST at the open, so "
        "this family trades in the most expensive window of the day. It can be "
        "right about direction and still lose -- measure it on futures and on "
        "a one-lot option and the two verdicts will differ."
    ),
    source="standard; see microstructure.SPREAD_SHAPE_BY_HOUR",
))


def _orb_fade(frame: pd.DataFrame, bars: int = 3) -> pd.Series:
    return -_opening_range_breakout(frame, bars)


register(Strategy(
    name="opening_range_fade",
    family="mean_reversion",
    thesis=(
        "The opposite trade: the opening break usually fails, so fade it. "
        "Registered explicitly because the breakout and the fade cannot both "
        "be right, and running both through one evaluator settles which -- or "
        "shows that neither clears costs, which is the likeliest outcome and "
        "still worth knowing."
    ),
    signal=_orb_fade,
    horizon=12,
    falsified_by=(
        "It is the exact negation of opening_range_breakout_15m, so their "
        "measured means must be equal and opposite. If both look profitable, "
        "the harness is broken -- which makes this pair a useful self-check on "
        "the harness itself."
    ),
    source="standard",
))


def _range_compression_breakout(frame: pd.DataFrame) -> pd.Series:
    """Break out of a volatility squeeze."""
    atr = _atr(frame, 14)
    baseline = atr.rolling(96).median()
    squeezed = atr < baseline * 0.7
    high = frame["High"].rolling(24).max().shift(1)
    low = frame["Low"].rolling(24).min().shift(1)
    close = frame["Close"]
    return pd.Series(
        np.where(squeezed & (close > high), 1.0,
                 np.where(squeezed & (close < low), -1.0, 0.0)),
        index=frame.index,
    )


register(Strategy(
    name="squeeze_breakout",
    family="breakout",
    thesis=(
        "Volatility clusters, so a quiet stretch is followed by a loud one. "
        "Unlike the directional rules this one rests on a property that IS "
        "measurable and persistent -- variance, not direction -- and only "
        "borrows a direction from which side of the range breaks."
    ),
    signal=_range_compression_breakout,
    horizon=24,
    falsified_by=(
        "The volatility premise can hold while the direction is a coin flip, "
        "in which case this loses the spread every time. If that is what the "
        "measurement shows, the right expression is a long straddle "
        "(spreads.straddle), not a directional break."
    ),
    source="standard; Bollinger/Keltner squeeze",
))


# ================================================================= volatility ==

def _vol_expansion(frame: pd.DataFrame) -> pd.Series:
    """Long volatility when recent realised vol sits well below its own norm.

    Direction-free: the signal is +1 meaning "own movement", and the harness
    scores it against the ABSOLUTE forward move rather than the signed one.
    """
    atr = _atr(frame, 14)
    baseline = atr.rolling(96).median()
    ratio = atr / baseline.replace(0.0, np.nan)
    return pd.Series(np.where(ratio < 0.7, 1.0, 0.0), index=frame.index)


register(Strategy(
    name="volatility_expansion",
    family="volatility",
    thesis=(
        "Buy movement, not direction, when realised volatility is compressed "
        "well below its own recent norm. This is the one prediction problem "
        "this project's own measurements call tractable: returns are close to "
        "unforecastable because a predictable return is an arbitrage, while a "
        "predictable VARIANCE is not -- knowing tomorrow is turbulent does not "
        "say which way, so nothing arbitrages the persistence away. See "
        "vol_forecast.py (HAR-RV)."
    ),
    signal=_vol_expansion,
    horizon=24,
    instrument="option_spread",
    falsified_by=(
        "A long straddle pays two premiums and two round trips, so the "
        "expansion has to beat roughly twice the cost. Compare the expected "
        "move against volatility.implied_move_from_straddle: when compression "
        "is visible to you it is visible in the option price too, and the "
        "straddle is already cheap for a reason."
    ),
    source="standard; Corsi (2009) HAR-RV for the forecast",
))


# ==================================================================== premium ==

def _short_premium_when_iv_rich(frame: pd.DataFrame) -> pd.Series:
    """Sell premium while implied sits above realised. Needs an IV column."""
    if "implied_vol_pct" not in frame.columns:
        return pd.Series(0.0, index=frame.index)
    from ..volatility import BARS_PER_YEAR
    returns = np.log(frame["Close"] / frame["Close"].shift(1))
    realised = returns.rolling(48).std(ddof=1) * np.sqrt(
        BARS_PER_YEAR["5m"]) * 100
    premium = frame["implied_vol_pct"] - realised
    return pd.Series(np.where(premium > 3.0, 1.0, 0.0), index=frame.index)


register(Strategy(
    name="short_premium_iv_over_rv",
    family="premium",
    thesis=(
        "Sell option premium while implied volatility exceeds realised by more "
        "than three points. Measured on this data: NIFTY realised 12.3% and "
        "BANKNIFTY 13.8% (Yang-Zhang, 30 sessions) against India VIX near "
        "15.9 -- a premium of +3.6 and +2.1 points, on the SELLER's side. "
        "Every option in this ledger was bought into that headwind."
    ),
    signal=_short_premium_when_iv_rich,
    horizon=24,
    instrument="option_spread",
    requires=("implied_vol_pct",),
    falsified_by=(
        "Already falsified as a retail trade at one lot, and the arithmetic is "
        "in research_variance_premium.py: the premium is worth 0.27-0.46% of "
        "the underlying over six days against a measured ~1.25% option round "
        "trip. Also a win rate says almost nothing about it -- small wins "
        "often, rare large losses -- so it may only ever be sold inside a "
        "defined-risk structure."
    ),
    source="Carr & Wu (2008); NIFTY-specific studies 2025-2026",
))


# ============================================================= relative value ==

def _pair_ratio_reversion(frame: pd.DataFrame) -> pd.Series:
    """Fade the NIFTY/BANKNIFTY ratio away from its own recent mean."""
    if "pair_close" not in frame.columns:
        return pd.Series(0.0, index=frame.index)
    ratio = frame["Close"] / frame["pair_close"].replace(0.0, np.nan)
    z = _zscore(ratio, 96)
    # z high = this leg is rich against its pair -> short it.
    return pd.Series(
        np.where(z >= 2.0, -1.0, np.where(z <= -2.0, 1.0, 0.0)),
        index=frame.index,
    )


register(Strategy(
    name="index_pair_ratio_reversion",
    family="relative_value",
    thesis=(
        "Trade NIFTY against BANKNIFTY when their ratio stretches from its own "
        "mean, rather than either against cash. The two share most of their "
        "risk -- banks are roughly a third of NIFTY -- so the ratio removes "
        "the market move and leaves the relative one, which is a different and "
        "much smaller bet. This family had no representation at all: every "
        "trade the app could express was outright directional."
    ),
    signal=_pair_ratio_reversion,
    horizon=24,
    instrument="futures",
    requires=("pair_close",),
    falsified_by=(
        "Two cost problems a single-leg trade does not have. It pays TWO round "
        "trips, so the spread bill doubles while the edge shrinks. And "
        "BANKNIFTY lost its weekly options in November 2024, so the cheap leg "
        "is a monthly or a future -- which also makes the lot notional very "
        "large. Neutral is not the same as cheap."
    ),
    source="standard statistical arbitrage",
))


# ================================================================== structure ==

def _value_area_fade(frame: pd.DataFrame) -> pd.Series:
    """Fade excursions outside the PREVIOUS session's value area."""
    needed = ("prev_value_area_high", "prev_value_area_low")
    if any(c not in frame.columns for c in needed):
        return pd.Series(0.0, index=frame.index)
    close = frame["Close"]
    return pd.Series(
        np.where(close > frame["prev_value_area_high"], -1.0,
                 np.where(close < frame["prev_value_area_low"], 1.0, 0.0)),
        index=frame.index,
    )


register(Strategy(
    name="value_area_fade",
    family="structure",
    thesis=(
        "Sell above and buy below the previous session's value area -- the "
        "band holding 70% of yesterday's traded volume. The premise is that "
        "price leaving an agreed range without acceptance returns to it. This "
        "asks a question no indicator in the repo asks: at WHICH PRICES did "
        "business actually get done, rather than where did price go through "
        "time. See volume_profile.py."
    ),
    signal=_value_area_fade,
    horizon=24,
    instrument="equity",
    requires=("prev_value_area_high", "prev_value_area_low"),
    falsified_by=(
        "It cannot distinguish rejection from acceptance, which is the whole "
        "difficulty: an excursion that holds is value MIGRATING, and fading "
        "that is standing in front of a trend. If the measurement is a wash, "
        "that ambiguity is the reason."
    ),
    source="Market Profile (Steidlmayer); CBOT",
))


def _poc_reversion(frame: pd.DataFrame) -> pd.Series:
    if "prev_poc" not in frame.columns:
        return pd.Series(0.0, index=frame.index)
    atr = _atr(frame, 14)
    gap = (frame["Close"] - frame["prev_poc"]) / atr.replace(0.0, np.nan)
    return pd.Series(
        np.where(gap >= 1.5, -1.0, np.where(gap <= -1.5, 1.0, 0.0)),
        index=frame.index,
    )


register(Strategy(
    name="poc_reversion",
    family="structure",
    thesis=(
        "Price is drawn back to the previous session's point of control -- the "
        "single price at which the most volume changed hands. A tighter claim "
        "than the value area version: one level, not a band."
    ),
    signal=_poc_reversion,
    horizon=24,
    instrument="equity",
    requires=("prev_poc",),
    falsified_by=(
        "The POC is where volume was heaviest, which also makes it where "
        "opinion was most balanced. A magnet and a battleground are the same "
        "place, and only measurement separates them."
    ),
    source="Market Profile",
))


# ======================================================================= flow ==

def _oi_wall_avoidance(frame: pd.DataFrame) -> pd.Series:
    """Trade away from the nearest heavy open-interest wall, never through it."""
    needed = ("call_wall", "put_wall")
    if any(c not in frame.columns for c in needed):
        return pd.Series(0.0, index=frame.index)
    close = frame["Close"]
    to_call = (frame["call_wall"] - close).abs()
    to_put = (close - frame["put_wall"]).abs()
    # Nearer the call wall above -> the path up is defended, so lean down.
    return pd.Series(
        np.where(to_call < to_put * 0.5, -1.0,
                 np.where(to_put < to_call * 0.5, 1.0, 0.0)),
        index=frame.index,
    )


register(Strategy(
    name="oi_wall_avoidance",
    family="flow",
    thesis=(
        "Lean away from whichever open-interest wall is closer, because that "
        "is the level writers are defending. This is the formalisation of a "
        "lesson already paid for: on 2026-10-21 the 22500 PE was offered as a "
        "candidate and credited +5 for 'heavy open interest - liquid' when "
        "buying it bet on the most defended support on the chain breaking. "
        "High OI means liquid AND defended, and which applies depends on "
        "direction."
    ),
    signal=_oi_wall_avoidance,
    horizon=12,
    requires=("call_wall", "put_wall"),
    falsified_by=(
        "Open interest says where contracts are open, not who is long them. A "
        "wall of calls written by a hedger behaves nothing like one written by "
        "a speculator, and the chain cannot tell them apart -- NSE's "
        "participant data is end-of-day only, so it cannot help intraday."
    ),
    source="standard; the 2026-10-21 session finding",
))


def _pin_fade(frame: pd.DataFrame) -> pd.Series:
    """Trade back toward a pinned strike."""
    if "pin_strike" not in frame.columns:
        return pd.Series(0.0, index=frame.index)
    pin = frame["pin_strike"]
    atr = _atr(frame, 14)
    gap = (frame["Close"] - pin) / atr.replace(0.0, np.nan)
    valid = pin.notna() & (pin > 0)
    return pd.Series(
        np.where(valid & (gap >= 1.0), -1.0,
                 np.where(valid & (gap <= -1.0), 1.0, 0.0)),
        index=frame.index,
    )


register(Strategy(
    name="pin_reversion",
    family="flow",
    thesis=(
        "When the call wall, the put wall and max pain all sit on one strike, "
        "both sides of the chain are defending it and price is pulled back "
        "rather than trending away. options_math.detect_pin has identified "
        "this state since 2026-10-21 and the app has had no way to trade it -- "
        "it could only warn you off a directional trade through one. The "
        "defined-risk expression is an iron butterfly at the pin."
    ),
    signal=_pin_fade,
    horizon=12,
    instrument="option_spread",
    requires=("pin_strike",),
    falsified_by=(
        "A pin holds until it does not, and it breaks on the days that matter "
        "most. Expect a high win rate with a fat left tail, which is precisely "
        "the shape a win rate misrepresents -- judge it on mean P&L, never on "
        "the hit rate."
    ),
    source="standard; options_math.detect_pin",
))


# ====================================================================== event ==

def _expiry_day_decay(frame: pd.DataFrame) -> pd.Series:
    """Short premium on expiry day. Needs a flag for which day that is."""
    if "is_expiry_day" not in frame.columns:
        return pd.Series(0.0, index=frame.index)
    position = _bars_into_session(frame)
    flag = frame["is_expiry_day"].fillna(False).astype(bool)
    # Enter after the opening hour, when the widest spreads have passed.
    return pd.Series(np.where(flag & (position >= 12), 1.0, 0.0),
                     index=frame.index)


register(Strategy(
    name="expiry_day_theta",
    family="event",
    thesis=(
        "Sell premium on expiry day, when the remaining time value decays to "
        "nothing within hours. The most popular systematic trade on NSE, and "
        "the one with the most seductive statistics: vendor backtests quote a "
        "68% win rate on an ATM short straddle."
    ),
    signal=_expiry_day_decay,
    horizon=24,
    instrument="option_spread",
    requires=("is_expiry_day",),
    falsified_by=(
        "The same vendor backtest shows its worst single day losing over four "
        "times the average winning day, and Zerodha's 45-DTE study had a trade "
        "lose 1,000+ points THROUGH a stop on a gap. A 68% win rate on an "
        "undefined-risk structure is not evidence of profit. Also note the "
        "instrument changed under this trade: BANKNIFTY weeklies were "
        "withdrawn in November 2024 and NIFTY expiry moved to Tuesday in "
        "September 2025, so any backtest spanning those dates is measuring "
        "more than one contract."
    ),
    source="widely traded; TradingTick 2020-2024 backtest",
))


def _event_blackout(frame: pd.DataFrame) -> pd.Series:
    """Stand aside around scheduled events. A strategy whose output is flat."""
    if "event_near" not in frame.columns:
        return pd.Series(0.0, index=frame.index)
    near = frame["event_near"].fillna(False).astype(bool)
    return pd.Series(np.where(near, 0.0, 0.0), index=frame.index)


register(Strategy(
    name="event_blackout",
    family="event",
    thesis=(
        "Take nothing into a scheduled catalyst -- RBI policy, a heavyweight "
        "result, inflation. Registered as a strategy because it is a decision "
        "with a measurable cost, and because the catalogue should contain the "
        "option of NOT trading. event_calendar.py supplies the dates; its own "
        "docstring is careful that only the DATE is trustworthy, since Yahoo "
        "returns Indian earnings timestamps in a US timezone."
    ),
    signal=_event_blackout,
    horizon=12,
    requires=("event_near",),
    falsified_by=(
        "It takes no positions, so it cannot be measured by this harness at "
        "all -- it produces no P&L series. The right test is the opposite one: "
        "measure every OTHER strategy with and without the blackout applied "
        "and see whether removing those bars helps. screen_all reports it as "
        "'no positions taken' rather than pretending to a verdict."
    ),
    source="standard risk practice",
))
