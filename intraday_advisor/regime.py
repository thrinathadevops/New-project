"""Is the market going up or down? — the honest version of that question.

What this does NOT do
---------------------
It does not predict. Nothing here, and nothing anyone sells, knows whether the
index will be higher or lower this afternoon. Any module claiming otherwise is
selling you the shape of a forecast without its substance.

What it does instead
--------------------
It answers the question you can actually act on: **what state is the market in
right now, how much do the different timeframes agree, and what would have to
happen for that read to be wrong?** A trade taken with four timeframes aligned
and falling volatility is a different proposition from the same trade taken
against a daily downtrend with VIX spiking — even though the 5-minute chart
looks identical in both cases.

Six inputs, each independently checkable:

1. **Multi-timeframe trend** (5m / 15m / 1h / 1d). Alignment is the single most
   useful context there is: when every timeframe points the same way, a move has
   somewhere to go. When they conflict, the short timeframe is usually noise
   inside a bigger range.
2. **Volatility regime** (India VIX). Rising VIX means falling markets far more
   often than not — fear and selling arrive together. Low, falling VIX is the
   grind-up regime where trend-following works and option buying bleeds.
3. **Breadth**: how many large caps are above their own 20-bar average. The
   index is cap-weighted, so a handful of heavyweights can carry it higher while
   most stocks fall. That divergence is the classic late-move warning.
4. **Global cues**: what the US did overnight and what Asia is doing now. The
   Indian open follows them more often than it follows yesterday's Indian close.
5. **Key levels**: previous day high/low/close and where price sits in today's
   range. Levels are where other traders have resting orders.
6. **Position in range**: whether price is at an extreme or mid-range, which
   decides whether a breakout or a fade is the sane trade.

The output names a regime, a directional lean, a conviction number, and --
the part most systems omit -- **what would change its mind**. A read you cannot
falsify is not analysis, it is a horoscope.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

# Timeframes read, fastest to slowest. Each carries a weight in the alignment
# score: slower frames describe the context the faster ones move inside, so they
# count for more.
TIMEFRAMES = (
    {"interval": "5m", "period": "5d", "label": "5 minutes", "weight": 1.0},
    {"interval": "15m", "period": "1mo", "label": "15 minutes", "weight": 1.5},
    {"interval": "1h", "period": "3mo", "label": "1 hour", "weight": 2.0},
    {"interval": "1d", "period": "1y", "label": "Daily", "weight": 3.0},
)

INDEX_SYMBOLS = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
VIX_SYMBOL = "^INDIAVIX"

# Global markets that lead the Indian open, with how much weight to give each.
GLOBAL_CUES = (
    {"symbol": "^GSPC", "name": "S&P 500", "weight": 1.0, "region": "US"},
    {"symbol": "^IXIC", "name": "Nasdaq", "weight": 1.0, "region": "US"},
    {"symbol": "^N225", "name": "Nikkei", "weight": 0.6, "region": "Asia"},
    {"symbol": "^HSI", "name": "Hang Seng", "weight": 0.6, "region": "Asia"},
)

# A spread of Nifty heavyweights across sectors, for the breadth check. Not the
# full index: a batched download of ~12 names is quick and the signal (what
# fraction are above their own average) saturates well before 50 names.
BREADTH_BASKET = (
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
    "ITC.NS", "LT.NS", "AXISBANK.NS", "SBIN.NS", "BHARTIARTL.NS",
    "KOTAKBANK.NS", "HINDUNILVR.NS",
)

# India VIX bands. Levels are historically grounded but not laws of nature.
VIX_CALM = 13.0
VIX_ELEVATED = 18.0
VIX_HIGH = 25.0


@dataclass
class TimeframeRead:
    interval: str
    label: str
    trend: str            # up / down / flat
    strength: float       # 0-1, how far the averages are apart
    close: float
    ema_fast: float
    ema_slow: float
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketRegime:
    """The current state of one index, and what would falsify that read."""

    index: str
    regime: str           # TRENDING_UP / TRENDING_DOWN / RANGE / VOLATILE / UNKNOWN
    bias: str             # UP / DOWN / NEUTRAL
    conviction: int       # 0-100: how much the evidence agrees, NOT a probability
    alignment: float      # -1..1, weighted timeframe agreement
    spot: float
    timeframes: list[dict] = field(default_factory=list)
    volatility: dict = field(default_factory=dict)
    breadth: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)
    global_cues: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    invalidation: list[str] = field(default_factory=list)
    headline: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=1).mean()


def read_timeframe(
    frame: pd.DataFrame, interval: str, label: str, *, fast: int = 9, slow: int = 21
) -> TimeframeRead | None:
    """Classify one timeframe's trend from a fast/slow EMA pair."""
    if frame is None or frame.empty or "Close" not in frame or len(frame) < slow:
        return None

    close = frame["Close"].astype(float)
    ema_fast = float(_ema(close, fast).iloc[-1])
    ema_slow = float(_ema(close, slow).iloc[-1])
    last = float(close.iloc[-1])
    if ema_slow == 0:
        return None

    separation = (ema_fast - ema_slow) / ema_slow
    # 0.3% apart counts as a fully-developed trend on any timeframe; beyond that
    # the extra distance says "extended", not "more trend".
    strength = min(1.0, abs(separation) / 0.003)

    if abs(separation) < 0.0004:
        trend, note = "flat", "Averages are intertwined — no trend on this timeframe."
    elif separation > 0:
        trend = "up"
        note = f"Fast average {separation * 100:+.2f}% above slow — buyers in control."
    else:
        trend = "down"
        note = f"Fast average {separation * 100:+.2f}% below slow — sellers in control."

    return TimeframeRead(
        interval=interval, label=label, trend=trend, strength=round(strength, 3),
        close=round(last, 2), ema_fast=round(ema_fast, 2),
        ema_slow=round(ema_slow, 2), note=note,
    )


def alignment_score(reads: list[TimeframeRead]) -> float:
    """Weighted agreement across timeframes, -1 (all down) to +1 (all up).

    Weighted because a daily downtrend is context for a 5-minute uptrend, not
    the other way round. An unweighted average lets noise outvote structure.
    """
    if not reads:
        return 0.0
    weights = {spec["interval"]: spec["weight"] for spec in TIMEFRAMES}
    total = 0.0
    weight_sum = 0.0
    for read in reads:
        weight = weights.get(read.interval, 1.0)
        direction = 1.0 if read.trend == "up" else -1.0 if read.trend == "down" else 0.0
        total += direction * read.strength * weight
        weight_sum += weight
    return round(total / weight_sum, 3) if weight_sum else 0.0


def classify_volatility(vix: float | None, vix_change_pct: float | None) -> dict:
    """India VIX into a regime, with what it implies for buying options."""
    if vix is None:
        return {
            "vix": None, "level": "unknown", "change_pct": None,
            "meaning": "India VIX unavailable — volatility context missing.",
            "implication": "Size more conservatively when you cannot see the fear gauge.",
        }

    if vix < VIX_CALM:
        level = "calm"
        meaning = (
            f"VIX {vix:.1f} is low. Markets are complacent and daily ranges are "
            f"narrow."
        )
        implication = (
            "Grind-up conditions. Trends persist but option premiums decay "
            "quickly — a bought option needs the move to come fast."
        )
    elif vix < VIX_ELEVATED:
        level = "normal"
        meaning = f"VIX {vix:.1f} is in its usual band."
        implication = "Ordinary conditions. Normal position sizing applies."
    elif vix < VIX_HIGH:
        level = "elevated"
        meaning = f"VIX {vix:.1f} is elevated — the market is pricing bigger swings."
        implication = (
            "Options are expensive and stops get hit by noise. Take fewer lots "
            "than usual, and expect whipsaws."
        )
    else:
        level = "high"
        meaning = f"VIX {vix:.1f} is high — this is a fear regime."
        implication = (
            "Large, fast moves in both directions. Premiums are rich, so a "
            "correct direction can still lose if the move is slow. Many "
            "profitable traders simply stand aside here."
        )

    if vix_change_pct is not None:
        if vix_change_pct > 5:
            meaning += (
                f" It is also RISING ({vix_change_pct:+.1f}%), which more often "
                f"accompanies falling markets than rising ones."
            )
        elif vix_change_pct < -5:
            meaning += (
                f" It is FALLING ({vix_change_pct:+.1f}%), which usually "
                f"accompanies a steadying or rising market."
            )

    return {
        "vix": round(vix, 2), "level": level,
        "change_pct": round(vix_change_pct, 2) if vix_change_pct is not None else None,
        "meaning": meaning, "implication": implication,
    }


def classify_breadth(above: int, total: int) -> dict:
    """What fraction of heavyweights are above their own 20-bar average."""
    if not total:
        return {
            "above": 0, "total": 0, "pct": None, "state": "unknown",
            "meaning": "Breadth unavailable.", "divergence": False,
        }
    pct = above / total
    if pct >= 0.7:
        state, meaning = "strong", "Most heavyweights are trending up — a broad advance."
    elif pct >= 0.55:
        state, meaning = "firm", "More heavyweights up than down."
    elif pct >= 0.45:
        state, meaning = "mixed", "Heavyweights are split — no broad direction."
    elif pct >= 0.3:
        state, meaning = "weak", "Most heavyweights are below their averages."
    else:
        state, meaning = "very weak", "Nearly all heavyweights are trending down."
    return {
        "above": above, "total": total, "pct": round(pct, 3),
        "state": state, "meaning": meaning, "divergence": False,
    }


def summarise_global(cues: list[dict]) -> dict:
    """Weighted lean from overnight and regional markets."""
    usable = [c for c in cues if c.get("change_pct") is not None]
    if not usable:
        return {"lean": "unknown", "score": None, "cues": cues,
                "meaning": "Global cues unavailable."}

    score = sum(c["change_pct"] * c.get("weight", 1.0) for c in usable)
    score /= sum(c.get("weight", 1.0) for c in usable)

    if score > 0.4:
        lean, meaning = "UP", "Global markets are firmly positive."
    elif score > 0.1:
        lean, meaning = "UP", "Global markets are mildly positive."
    elif score < -0.4:
        lean, meaning = "DOWN", "Global markets are firmly negative."
    elif score < -0.1:
        lean, meaning = "DOWN", "Global markets are mildly negative."
    else:
        lean, meaning = "NEUTRAL", "Global markets are flat — no external push."

    return {
        "lean": lean, "score": round(score, 3), "cues": cues,
        "meaning": meaning + (
            " The Indian open tracks these more often than it tracks "
            "yesterday's Indian close."
        ),
    }


def key_levels(daily: pd.DataFrame | None, intraday: pd.DataFrame | None) -> dict:
    """Previous day's high/low/close and where price sits in today's range."""
    levels: dict[str, Any] = {}
    if daily is not None and len(daily) >= 2:
        previous = daily.iloc[-2]
        levels.update({
            "prev_high": round(float(previous["High"]), 2),
            "prev_low": round(float(previous["Low"]), 2),
            "prev_close": round(float(previous["Close"]), 2),
        })
    if intraday is not None and not intraday.empty:
        today = intraday.tail(78)          # roughly one session of 5-minute bars
        high = float(today["High"].max())
        low = float(today["Low"].min())
        last = float(today["Close"].iloc[-1])
        span = high - low
        position = (last - low) / span if span > 0 else 0.5
        levels.update({
            "day_high": round(high, 2), "day_low": round(low, 2),
            "last": round(last, 2), "range_position": round(position, 3),
            "range_note": (
                "At the top of the day's range — breakout or exhaustion."
                if position > 0.8 else
                "At the bottom of the day's range — breakdown or exhaustion."
                if position < 0.2 else
                "Mid-range, where breakouts fail most often."
            ),
        })
    return levels


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def build_regime(
    index: str,
    *,
    frames: dict[str, pd.DataFrame],
    vix: float | None = None,
    vix_change_pct: float | None = None,
    breadth_above: int = 0,
    breadth_total: int = 0,
    global_cues: list[dict] | None = None,
) -> MarketRegime:
    """Assemble the regime from already-fetched inputs.

    Inputs are injected rather than fetched here so the whole classification is
    testable offline and deterministic — the part that talks to the network is
    :func:`fetch_regime_inputs`.
    """
    reads = []
    for spec in TIMEFRAMES:
        read = read_timeframe(frames.get(spec["interval"]), spec["interval"], spec["label"])
        if read:
            reads.append(read)

    alignment = alignment_score(reads)
    volatility = classify_volatility(vix, vix_change_pct)
    breadth = classify_breadth(breadth_above, breadth_total)
    glob = summarise_global(global_cues or [])
    levels = key_levels(frames.get("1d"), frames.get("5m"))

    spot = reads[0].close if reads else 0.0
    reasons: list[str] = []
    warnings: list[str] = []
    invalidation: list[str] = []

    up_votes = sum(1 for r in reads if r.trend == "up")
    down_votes = sum(1 for r in reads if r.trend == "down")

    # --- regime and bias ---
    if not reads:
        regime, bias, conviction = "UNKNOWN", "NEUTRAL", 0
        warnings.append("No usable price data — no read is possible.")
    else:
        if volatility["level"] == "high":
            regime = "VOLATILE"
        elif alignment >= 0.35:
            regime = "TRENDING_UP"
        elif alignment <= -0.35:
            regime = "TRENDING_DOWN"
        else:
            regime = "RANGE"

        bias = "UP" if alignment > 0.15 else "DOWN" if alignment < -0.15 else "NEUTRAL"
        # Conviction is agreement among independent inputs, NOT a probability of
        # being right. Nothing here knows that number.
        conviction = int(min(100, abs(alignment) * 100))
        reasons.append(
            f"{up_votes} of {len(reads)} timeframes up, {down_votes} down "
            f"(weighted alignment {alignment:+.2f})."
        )

    # --- agreement and conflict between the inputs ---
    if glob["lean"] != "unknown" and bias != "NEUTRAL":
        if glob["lean"] == bias:
            conviction = min(100, conviction + 10)
            reasons.append(f"Global cues agree ({glob['lean']}).")
        elif glob["lean"] != "NEUTRAL":
            conviction = max(0, conviction - 15)
            warnings.append(
                f"Global cues lean {glob['lean']} against a {bias} local read — "
                f"expect a gap or a reversal near the open."
            )

    if breadth["pct"] is not None:
        if bias == "UP" and breadth["pct"] < 0.45:
            breadth["divergence"] = True
            conviction = max(0, conviction - 15)
            warnings.append(
                f"Breadth divergence: the index leans up but only "
                f"{breadth['above']}/{breadth['total']} heavyweights are above "
                f"their averages. Narrow advances fail more often than broad ones."
            )
        elif bias == "DOWN" and breadth["pct"] > 0.55:
            breadth["divergence"] = True
            conviction = max(0, conviction - 15)
            warnings.append(
                "Breadth divergence: the index leans down but most heavyweights "
                "are still holding up. The fall may be narrow and short-lived."
            )
        else:
            reasons.append(f"Breadth is {breadth['state']} ({breadth['meaning']}).")

    if volatility["level"] in ("elevated", "high"):
        warnings.append(volatility["implication"])
    elif volatility["level"] != "unknown":
        reasons.append(volatility["meaning"])

    if regime == "RANGE":
        warnings.append(
            "Timeframes disagree, so this is a range, not a trend. Breakout "
            "trades fail most often here; the edge is in fading the extremes "
            "or standing aside."
        )

    # --- what would change the read: the part that makes it falsifiable ---
    slow_frames = [r for r in reads if r.interval in ("1h", "1d")]
    if slow_frames:
        anchor = slow_frames[-1]
        invalidation.append(
            f"{anchor.label} trend flips: its {anchor.ema_fast:,.0f} / "
            f"{anchor.ema_slow:,.0f} average pair crossing the other way ends "
            f"this read."
        )
    if levels.get("prev_high") and levels.get("prev_low"):
        if bias == "UP":
            invalidation.append(
                f"Price losing yesterday's low ({levels['prev_low']:,.0f}) "
                f"invalidates the upward read."
            )
        elif bias == "DOWN":
            invalidation.append(
                f"Price reclaiming yesterday's high ({levels['prev_high']:,.0f}) "
                f"invalidates the downward read."
            )
    if volatility.get("vix"):
        invalidation.append(
            f"India VIX jumping above {max(VIX_ELEVATED, volatility['vix'] * 1.2):.0f} "
            f"would change the regime to volatile regardless of direction."
        )

    headline = {
        "TRENDING_UP": f"{index}: trending up",
        "TRENDING_DOWN": f"{index}: trending down",
        "RANGE": f"{index}: range-bound, no trend",
        "VOLATILE": f"{index}: high-volatility regime",
        "UNKNOWN": f"{index}: no read available",
    }[regime]
    if regime in ("TRENDING_UP", "TRENDING_DOWN"):
        headline += f" ({conviction}% of inputs agree)"

    return MarketRegime(
        index=index, regime=regime, bias=bias, conviction=conviction,
        alignment=alignment, spot=spot,
        timeframes=[r.as_dict() for r in reads],
        volatility=volatility, breadth=breadth, levels=levels,
        global_cues=glob, reasons=reasons, warnings=warnings,
        invalidation=invalidation, headline=headline,
    )


# --------------------------------------------------------------------------- #
# Fetching (the only part that touches the network)
# --------------------------------------------------------------------------- #

def _flatten(frame: pd.DataFrame) -> pd.DataFrame:
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.copy()
        frame.columns = frame.columns.get_level_values(0)
    return frame


def fetch_regime_inputs(index: str, *, with_breadth: bool = True) -> dict:
    """Pull everything :func:`build_regime` needs. Never raises.

    Each piece degrades independently: a failed VIX fetch costs the volatility
    read, not the whole analysis.
    """
    import yfinance as yf

    symbol = INDEX_SYMBOLS.get(index.upper())
    if symbol is None:
        raise ValueError(f"Unsupported index {index!r}; known: {sorted(INDEX_SYMBOLS)}")

    inputs: dict[str, Any] = {
        "frames": {}, "vix": None, "vix_change_pct": None,
        "breadth_above": 0, "breadth_total": 0, "global_cues": [],
    }

    for spec in TIMEFRAMES:
        try:
            frame = yf.download(
                symbol, period=spec["period"], interval=spec["interval"],
                progress=False, auto_adjust=True, threads=False,
            )
            if frame is not None and not frame.empty:
                inputs["frames"][spec["interval"]] = _flatten(frame)
        except Exception:  # noqa: BLE001 - one timeframe failing is survivable
            continue

    try:
        vix_frame = yf.download(VIX_SYMBOL, period="5d", interval="1d",
                                progress=False, auto_adjust=True, threads=False)
        if vix_frame is not None and not vix_frame.empty:
            vix_frame = _flatten(vix_frame)
            closes = vix_frame["Close"].astype(float)
            inputs["vix"] = float(closes.iloc[-1])
            if len(closes) > 1 and float(closes.iloc[-2]):
                inputs["vix_change_pct"] = (
                    float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1
                ) * 100
    except Exception:  # noqa: BLE001
        pass

    for cue in GLOBAL_CUES:
        entry = {"name": cue["name"], "symbol": cue["symbol"],
                 "region": cue["region"], "weight": cue["weight"],
                 "change_pct": None, "close": None}
        try:
            frame = yf.download(cue["symbol"], period="5d", interval="1d",
                                progress=False, auto_adjust=True, threads=False)
            if frame is not None and not frame.empty:
                frame = _flatten(frame)
                closes = frame["Close"].astype(float)
                entry["close"] = round(float(closes.iloc[-1]), 2)
                if len(closes) > 1 and float(closes.iloc[-2]):
                    entry["change_pct"] = round(
                        (float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1) * 100, 2
                    )
        except Exception:  # noqa: BLE001
            pass
        inputs["global_cues"].append(entry)

    if with_breadth:
        try:
            # One batched request rather than twelve: faster and far less
            # likely to be rate-limited.
            basket = yf.download(
                list(BREADTH_BASKET), period="2mo", interval="1d",
                progress=False, auto_adjust=True, threads=False, group_by="ticker",
            )
            above = 0
            total = 0
            for ticker in BREADTH_BASKET:
                try:
                    closes = (basket[ticker]["Close"] if ticker in basket.columns.get_level_values(0)
                              else None)
                    if closes is None:
                        continue
                    closes = closes.dropna().astype(float)
                    if len(closes) < 20:
                        continue
                    total += 1
                    if float(closes.iloc[-1]) > float(_ema(closes, 20).iloc[-1]):
                        above += 1
                except Exception:  # noqa: BLE001
                    continue
            inputs["breadth_above"] = above
            inputs["breadth_total"] = total
        except Exception:  # noqa: BLE001
            pass

    return inputs


def analyse_market(index: str = "NIFTY", *, with_breadth: bool = True) -> MarketRegime:
    """Fetch and classify in one call. The entry point the engine uses."""
    inputs = fetch_regime_inputs(index, with_breadth=with_breadth)
    return build_regime(index.upper(), **inputs)
