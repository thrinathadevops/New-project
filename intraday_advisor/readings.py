"""Plain-language interpretation of what the indicators currently say.

Why this exists
---------------
A dashboard that prints ``EMA9 55780 · EMA21 55812 · RSI 41`` tells you nothing
unless you already know what those mean, and a static glossary tells you what
they mean but not what they are saying *right now*. This module does both: for
each indicator it gives the definition, the current reading, and what that
reading implies for taking or avoiding the trade.

It performs no analysis of its own and never contradicts the scorer — it reads
the same feature values :mod:`intraday_advisor.scoring` scored and puts them
into words.

A caution that belongs here
---------------------------
Indicators describe what price has already done. They do not predict. Every
"bullish" reading below is a statement about the past that is merely *more
often* followed by an up-move — and "more often" is precisely what the ledger
exists to measure, because nobody actually knows the number until they do.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Reading:
    """One indicator, explained and interpreted at the current value."""

    key: str
    label: str
    what: str          # what the indicator is, in one sentence
    value: str         # the current numbers
    state: str         # bullish / bearish / neutral / caution
    meaning: str       # what this particular reading says
    action: str        # what it implies for the trade

    def as_dict(self) -> dict:
        return asdict(self)


def _fmt(value, decimals: int = 2) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _has(features: dict, *keys: str) -> bool:
    return all(
        features.get(key) is not None and features.get(key) != 0 for key in keys
    )


def ema_reading(features: dict) -> Reading | None:
    """EMA 9 against EMA 21 — the short-term trend."""
    if not _has(features, "EMA9", "EMA21"):
        return None
    ema9 = float(features["EMA9"])
    ema21 = float(features["EMA21"])
    gap_pct = (ema9 - ema21) / ema21 * 100 if ema21 else 0.0

    what = (
        "Two averages of recent price. EMA 9 tracks roughly the last 9 bars, "
        "EMA 21 the last 21, and both weight recent bars more heavily. The one "
        "that matters is which is on top."
    )
    value = f"EMA 9 {_fmt(ema9)} vs EMA 21 {_fmt(ema21)} ({gap_pct:+.2f}%)"

    if ema9 > ema21:
        state = "bullish"
        meaning = (
            "The short average is ABOVE the medium one, so recent prices are "
            "higher than the prices before them: buyers have been in control."
        )
        action = (
            "Supports a CALL. A PUT here is fighting the trend — possible, but "
            "it needs a much better reason."
        )
        if abs(gap_pct) < 0.05:
            state = "neutral"
            meaning += (
                " The two are almost touching, though, which means the trend is "
                "weak and could flip on the next bar."
            )
            action = "Too close to call. Waiting costs nothing."
    else:
        state = "bearish"
        meaning = (
            "The short average is BELOW the medium one: recent prices are lower "
            "than the prices before them, so sellers have been in control."
        )
        action = (
            "Supports a PUT. A CALL here is counter-trend and needs a much "
            "better reason."
        )
        if abs(gap_pct) < 0.05:
            state = "neutral"
            meaning += " They are nearly touching, so the trend is weak."
            action = "Too close to call. Waiting costs nothing."

    return Reading("ema", "EMA 9 vs EMA 21", what, value, state, meaning, action)


VWAP_WHAT = (
    "Volume Weighted Average Price: the average price everyone actually "
    "traded at today, weighted by how much traded there. Large funds use it "
    "as the benchmark for a fair fill, so it acts as a magnet and as "
    "support or resistance."
)


def vwap_reading(features: dict) -> Reading | None:
    """Price against VWAP — are buyers or sellers paying up today?"""
    if not _has(features, "VWAP", "Close"):
        # Say so rather than silently omitting it. VWAP needs volume, and the
        # free index feed reports none for NIFTY/BANKNIFTY, so the figure
        # cannot be computed from this data source at all. An indicator that
        # just disappears looks like a bug and teaches nothing.
        return Reading(
            "vwap", "Price vs VWAP", VWAP_WHAT,
            "unavailable on this feed", "neutral",
            "VWAP needs traded volume, and the free index feed reports no "
            "volume for NIFTY and BANKNIFTY, so it cannot be calculated here.",
            "Not contributing to the score either way. A broker feed with "
            "volume (Angel One during market hours) restores it; until then "
            "lean on the EMAs and SuperTrend for direction.",
        )
    close = float(features["Close"])
    vwap = float(features["VWAP"])
    distance = (close - vwap) / vwap * 100 if vwap else 0.0

    what = VWAP_WHAT
    value = f"Price {_fmt(close)} vs VWAP {_fmt(vwap)} ({distance:+.2f}%)"

    if close > vwap:
        state = "bullish"
        meaning = (
            "Price is ABOVE the day's average traded price: buyers are willing "
            "to pay more than the average participant did. VWAP below often "
            "acts as a floor on pullbacks."
        )
        action = (
            "Supports a CALL. A common entry is a dip back toward VWAP that "
            "holds — rather than chasing an extended move."
        )
    else:
        state = "bearish"
        meaning = (
            "Price is BELOW the day's average traded price: sellers are "
            "accepting less than the average participant paid. VWAP above often "
            "acts as a ceiling on bounces."
        )
        action = (
            "Supports a PUT. A common entry is a bounce back up to VWAP that "
            "fails."
        )

    if abs(distance) > 1.0:
        state = "caution"
        action += (
            f" But price is {abs(distance):.1f}% away from VWAP — stretched. "
            f"Entering here means a wide stop and poor reward for the risk."
        )
    return Reading("vwap", "Price vs VWAP", what, value, state, meaning, action)


def rsi_reading(features: dict) -> Reading | None:
    """RSI 14 — how hard the recent move has pushed."""
    rsi = features.get("RSI14")
    if rsi is None or rsi == 0:
        return None
    rsi = float(rsi)
    what = (
        "Relative Strength Index: how much of the last 14 bars' movement was "
        "upward, on a 0-100 scale. Around 50 is balanced; above 70 means the "
        "move up has been relentless, below 30 the same downward."
    )
    value = f"RSI {rsi:.0f}"

    if rsi > 70:
        state, meaning = "caution", (
            "Overbought. The move up has been one-sided, which means most of "
            "the easy part is behind you and a pullback is overdue."
        )
        action = "A poor place to BUY a CALL — you are paying the top of the move."
    elif rsi < 30:
        state, meaning = "caution", (
            "Oversold. The fall has been one-sided and a bounce is overdue."
        )
        action = "A poor place to BUY a PUT — you are paying the bottom of the move."
    elif rsi >= 55:
        state, meaning = "bullish", "Momentum leans up without being stretched."
        action = "A healthy zone for a CALL."
    elif rsi <= 45:
        state, meaning = "bearish", "Momentum leans down without being stretched."
        action = "A healthy zone for a PUT."
    else:
        state, meaning = "neutral", "Balanced — no momentum edge either way."
        action = "Neither side has an advantage from momentum alone."

    return Reading("rsi", "RSI 14 (momentum)", what, value, state, meaning, action)


def supertrend_reading(features: dict) -> Reading | None:
    """SuperTrend — which regime price is in."""
    direction = features.get("SuperTrend_Dir")
    if direction is None:
        return None
    direction = int(direction)
    what = (
        "A volatility-based trend filter. It flips between 'up regime' and "
        "'down regime' and tends to stay flipped, so it is a slower, steadier "
        "read than the EMAs."
    )
    if direction == 1:
        return Reading(
            "supertrend", "SuperTrend", what, "Up regime", "bullish",
            "The filter says the prevailing trend is up and has not yet broken.",
            "Supports a CALL and argues against shorting into strength.",
        )
    if direction == -1:
        return Reading(
            "supertrend", "SuperTrend", what, "Down regime", "bearish",
            "The filter says the prevailing trend is down and has not yet broken.",
            "Supports a PUT and argues against buying the dip.",
        )
    return Reading(
        "supertrend", "SuperTrend", what, "No regime", "neutral",
        "No clear regime — price is choppy.", "Trend-following setups are weak here.",
    )


def atr_reading(features: dict) -> Reading | None:
    """ATR 14 — how far price typically travels, which sets the stop."""
    atr = features.get("ATR14")
    close = features.get("Close")
    if not atr or not close:
        return None
    atr = float(atr)
    close = float(close)
    atr_pct = atr / close * 100 if close else 0.0

    what = (
        "Average True Range: how far price moves in a typical bar. It is the "
        "honest unit for a stop — a stop tighter than ATR gets hit by ordinary "
        "noise rather than by being wrong."
    )
    value = f"ATR {_fmt(atr)} ({atr_pct:.2f}% of price)"

    if atr_pct < 0.08:
        state = "caution"
        meaning = "Unusually quiet. Price is barely moving."
        action = (
            "The underlying may not travel far enough to cover the option's "
            "cost. Low volatility also bleeds premium while you wait."
        )
    elif atr_pct > 0.45:
        state = "caution"
        meaning = "Unusually volatile — large, fast swings."
        action = (
            "A normal-sized stop will be hit by noise. Either take fewer lots "
            "or stand aside."
        )
    else:
        state = "neutral"
        meaning = "Normal movement for this instrument."
        action = "Stop distances of a usual size are reasonable."

    return Reading("atr", "ATR 14 (volatility)", what, value, state, meaning, action)


def chain_reading(signal: dict) -> Reading | None:
    """Option-chain positioning — PCR, max pain and the OI walls."""
    pcr = signal.get("pcr")
    if not pcr:
        return None
    pcr = float(pcr)
    max_pain = float(signal.get("max_pain") or 0)
    spot = float(signal.get("spot") or 0)
    ce_wall = float(signal.get("ce_wall") or 0)
    pe_wall = float(signal.get("pe_wall") or 0)

    what = (
        "What other option traders have positioned for. PCR is puts open "
        "versus calls open; the 'walls' are the strikes with the most open "
        "contracts, which tend to act as magnets and barriers into expiry."
    )
    value = (
        f"PCR {pcr:.2f} · max pain {max_pain:,.0f} · "
        f"call wall {ce_wall:,.0f} · put wall {pe_wall:,.0f}"
    )

    if pcr >= 1.3:
        state, meaning = "bullish", (
            "More puts open than calls. Heavy put writing usually means sellers "
            "expect the index to stay above those strikes."
        )
        action = "Mildly supports a CALL."
    elif pcr <= 0.7:
        state, meaning = "bearish", (
            "More calls open than puts, which usually means sellers expect the "
            "index to stay below those strikes."
        )
        action = "Mildly supports a PUT."
    else:
        state, meaning = "neutral", "Positioning is balanced."
        action = "The chain gives no directional edge right now."

    if spot and ce_wall and 0 < (ce_wall - spot) / spot < 0.004:
        state = "caution"
        action += (
            f" Note the call wall at {ce_wall:,.0f} sits just overhead — a lot "
            f"of open interest there tends to cap a move."
        )
    elif spot and pe_wall and 0 < (spot - pe_wall) / spot < 0.004:
        state = "caution"
        action += (
            f" Note the put wall at {pe_wall:,.0f} sits just below — that tends "
            f"to cushion a fall."
        )
    return Reading("chain", "Option chain", what, value, state, meaning, action)


def cpr_reading(features: dict) -> Reading | None:
    if not _has(features, "CPR_Pivot", "Close"):
        return None
    pivot = float(features["CPR_Pivot"])
    tc = float(features.get("CPR_TC", pivot))
    bc = float(features.get("CPR_BC", pivot))
    close = float(features["Close"])
    width_pct = float(features.get("CPR_Width_Pct", 0) or 0)

    what = (
        "Central Pivot Range (CPR): calculated from yesterday's High, Low, and Close. "
        "TC (Top Central) and BC (Bottom Central) form a support/resistance ribbon."
    )
    value = f"CPR TC {_fmt(tc)} · Pivot {_fmt(pivot)} · BC {_fmt(bc)} (Width: {width_pct:.2f}%)"
    if close > tc:
        state = "bullish"
        meaning = "Price is trading comfortably above CPR Top Central (TC) — buyers dominate the session."
        action = "Favors CALL entries. Pullbacks into TC often offer dynamic support."
    elif close < bc:
        state = "bearish"
        meaning = "Price is trading below CPR Bottom Central (BC) — sellers dominate the session."
        action = "Favors PUT entries. Rallies into BC often offer dynamic resistance."
    else:
        state = "caution"
        meaning = "Price is trapped INSIDE the CPR ribbon — a high-risk churn zone where breakouts frequently fail."
        action = "Avoid initiating breakout trades inside CPR; wait for an exit outside TC or BC."

    return Reading("cpr", "Central Pivot Range (CPR)", what, value, state, meaning, action)


def orb_reading(features: dict) -> Reading | None:
    if not _has(features, "ORB_15_High", "ORB_15_Low"):
        return None
    orb_h = float(features["ORB_15_High"])
    orb_l = float(features["ORB_15_Low"])
    close = float(features.get("Close", 0))

    what = (
        "Opening Range Breakout (ORB 15m): the high and low established during the initial "
        "15 minutes of trading. A break outside this boundary often establishes the session's direction."
    )
    value = f"15m High {_fmt(orb_h)} · 15m Low {_fmt(orb_l)}"
    if features.get("ORB_Breakout_Bull"):
        state = "bullish"
        meaning = "Bullish breakout above 15m Opening Range High with confirmed volume surge."
        action = "High momentum CALL setup. Stop placed just inside the ORB high."
    elif features.get("ORB_Breakdown_Bear"):
        state = "bearish"
        meaning = "Bearish breakdown below 15m Opening Range Low with confirmed volume surge."
        action = "High momentum PUT setup. Stop placed just inside the ORB low."
    elif features.get("ORB_Trap_Bull"):
        state = "caution"
        meaning = "Bull Trap detected: price poked above 15m High but fell back inside the range."
        action = "Counter-trend PUT alert or exit long calls immediately."
    elif features.get("ORB_Trap_Bear"):
        state = "caution"
        meaning = "Bear Trap detected: price dipped below 15m Low but snapped back inside the range."
        action = "Counter-trend CALL alert or exit short puts immediately."
    elif close > orb_h:
        state = "bullish"
        meaning = "Price is sustaining above the 15m Opening Range High."
        action = "Bullish bias intact."
    elif close < orb_l:
        state = "bearish"
        meaning = "Price is sustaining below the 15m Opening Range Low."
        action = "Bearish bias intact."
    else:
        state = "neutral"
        meaning = "Price is oscillating inside the 15m Opening Range."
        action = "Rangebound; wait for a definitive volume breakout."

    return Reading("orb", "Opening Range (15m ORB)", what, value, state, meaning, action)


def heikin_ashi_reading(features: dict) -> Reading | None:
    if "HA_Color" not in features:
        return None
    color = int(features.get("HA_Color", 0))
    strong_bull = bool(features.get("HA_Strong_Bull", False))
    strong_bear = bool(features.get("HA_Strong_Bear", False))
    exhaustion = bool(features.get("HA_Exhaustion_Alert", False))
    streak = int(features.get("HA_Streak", 0))

    what = (
        "Heikin-Ashi: filtered candle calculation removing market noise to display trend conviction."
    )
    value = f"Streak: {streak:+d} bars · Strong Bull: {strong_bull} · Strong Bear: {strong_bear}"
    if exhaustion:
        state = "caution"
        meaning = "Exhaustion Doji printed after an extended trend streak — momentum may be rolling over."
        action = "Tighten trailing stops or consider taking partial profits."
    elif strong_bull:
        state = "bullish"
        meaning = "Strong bull candle with flat bottom (no lower shadow) — buyers in full control."
        action = "Ideal trend-following environment for CALLs."
    elif strong_bear:
        state = "bearish"
        meaning = "Strong bear candle with flat top (no upper shadow) — sellers in full control."
        action = "Ideal trend-following environment for PUTs."
    elif color > 0:
        state = "bullish"
        meaning = "Green Heikin-Ashi candle, moderate upward bias."
        action = "Mild bullish confirmation."
    else:
        state = "bearish"
        meaning = "Red Heikin-Ashi candle, moderate downward bias."
        action = "Mild bearish confirmation."

    return Reading("heikin_ashi", "Heikin-Ashi Trend", what, value, state, meaning, action)


def build_readings(signal: dict) -> list[dict]:
    """Every available reading for one signal, in the order worth reading."""
    features = signal.get("features") or {}
    builders = (
        ema_reading(features),
        cpr_reading(features),
        orb_reading(features),
        vwap_reading(features),
        supertrend_reading(features),
        heikin_ashi_reading(features),
        rsi_reading(features),
        atr_reading(features),
        chain_reading(signal),
    )
    return [reading.as_dict() for reading in builders if reading is not None]


def build_playbook(signal: dict) -> dict:
    """Entry trigger, invalidation and exits, in the operator's own numbers.

    Deliberately concrete. "Buy on strength" is advice nobody can follow or
    check afterwards; "enter at ₹26.85, out at ₹21.48, and if the index crosses
    back above VWAP your reason is gone" is a rule that can be obeyed and then
    graded.
    """
    direction = signal.get("direction", "WAIT")
    if direction not in ("CALL", "PUT"):
        return {
            "tradable": False,
            "headline": "No trade right now.",
            "why": (
                "Price and the option chain do not agree, or neither has a view. "
                "WAIT is a real output, not a failure — most of the session has "
                "no edge in it, and the cost of waiting is zero."
            ),
            "steps": [
                "Do nothing. There is no setup to act on.",
                "Watch for the score to rise and the direction badge to change.",
                "The alert will tell you the moment it does.",
            ],
        }

    entry = float(signal.get("entry_premium") or 0)
    target = float(signal.get("target_premium") or 0)
    stop = float(signal.get("stop_premium") or 0)
    plan = signal.get("plan") or {}
    features = signal.get("features") or {}
    vwap = features.get("VWAP")
    index = signal.get("index", "")
    contract = f"{signal.get('strike')} {signal.get('option_type')}"
    side = "upward" if direction == "CALL" else "downward"

    steps = [
        f"ENTRY — buy {index} {contract} near ₹{entry:.2f}. "
        f"If the premium has already run well past this, the move is gone; "
        f"skip it rather than chase.",
        f"SIZE — {plan.get('lots', '?')} lot(s). One stop-out costs "
        f"₹{plan.get('risk_amount', 0):,.0f}, which is "
        f"{(plan.get('risk_pct_of_capital') or 0) * 100:.1f}% of your capital.",
        f"STOP — exit at ₹{stop:.2f} ({(signal.get('stop_pct') or 0) * 100:.0f}% "
        f"below entry). Set it the moment you are filled, not later.",
        f"TARGET — book at ₹{target:.2f} "
        f"({(signal.get('target_pct') or 0) * 100:.0f}% up). "
        f"Taking the profit is the whole job; a winner that round-trips to the "
        f"stop is a loss.",
    ]

    if vwap:
        steps.append(
            f"INVALIDATION — your reason was a {side} move. If the index closes "
            f"back {'below' if direction == 'CALL' else 'above'} VWAP "
            f"({float(vwap):,.0f}), the reason is gone. Exit then, even without "
            f"the stop being hit."
        )
    steps.append(
        "TIME — intraday options are squared off at 15:15. An option held into "
        "the close bleeds time value whether or not the index moves, so a trade "
        "that has not worked by about 14:45 is usually not going to."
    )

    breakeven = plan.get("breakeven")
    if breakeven:
        steps.append(
            f"COSTS — you are flat at ₹{breakeven:.2f}, not at your ₹{entry:.2f} "
            f"entry. Anything below that is a loss even though the premium is up."
        )

    return {
        "tradable": True,
        "headline": f"{direction} on {index} {contract}",
        "why": (
            f"Price action and the option chain both lean {side}, and the "
            f"contract is affordable at your capital."
            if plan.get("affordable")
            else f"The read leans {side}, but one lot costs more than you have "
                 f"available — this is information, not a trade."
        ),
        "steps": steps,
    }


# What each glossary term means, for the "learn" panel. Kept separate from the
# live readings so it can be shown even when there is no signal.
GLOSSARY = [
    {
        "term": "EMA 9 / EMA 21",
        "plain": "Fast and slow average price.",
        "detail": (
            "When the fast one (9 bars) is above the slow one (21 bars), recent "
            "prices are higher than earlier ones — an uptrend. When it crosses "
            "below, the short-term trend has turned down. The crossover is the "
            "signal; the gap between them is how strong it is."
        ),
        "use": "Direction. CALL when 9 is above 21, PUT when below.",
    },
    {
        "term": "VWAP",
        "plain": "The day's average traded price, weighted by volume.",
        "detail": (
            "Institutions benchmark their fills against it, so price tends to "
            "return to it and then react. Above VWAP the day belongs to buyers; "
            "below it, to sellers. It resets every morning."
        ),
        "use": (
            "Entry timing and invalidation. Buying a dip back to VWAP that holds "
            "beats chasing; a close back through VWAP kills the trade's reason."
        ),
    },
    {
        "term": "RSI 14",
        "plain": "How one-sided the last 14 bars were, 0-100.",
        "detail": (
            "Above 70 the move up has been relentless and is due a pause; below "
            "30 the same downward. The middle band is where trends actually run."
        ),
        "use": "A filter against chasing. Do not buy a CALL into RSI 75+.",
    },
    {
        "term": "SuperTrend",
        "plain": "A slower up-or-down regime flag.",
        "detail": (
            "Built from volatility, it flips rarely and stays flipped, so it is "
            "a steadier read than the EMAs and disagrees with them at turns."
        ),
        "use": "Confirmation. Best setups have SuperTrend and the EMAs agreeing.",
    },
    {
        "term": "ATR 14",
        "plain": "How far price moves in a typical bar.",
        "detail": (
            "The honest unit for a stop. A stop closer than one ATR will be hit "
            "by ordinary noise rather than because you were wrong."
        ),
        "use": "Stop distance and whether the move can even cover your costs.",
    },
    {
        "term": "PCR and OI walls",
        "plain": "What other option traders are positioned for.",
        "detail": (
            "Put-Call Ratio above ~1.3 leans bullish, below ~0.7 bearish. The "
            "strikes with the most open contracts act as magnets and barriers, "
            "especially near expiry."
        ),
        "use": "Confluence. The engine requires price and chain to agree.",
    },
    {
        "term": "Breakeven premium",
        "plain": "The price you must sell at just to get your money back.",
        "detail": (
            "Always above your entry, because you pay brokerage, STT, exchange "
            "charges, GST and the bid-ask spread on the way in AND out. On a ₹10 "
            "premium that gap is about 7.5%; on a ₹200 premium under 1%."
        ),
        "use": (
            "The single most useful number for rejecting a trade. If breakeven "
            "is near your target, the trade cannot pay even when it works."
        ),
    },
    {
        "term": "Net R:R",
        "plain": "Rupees made per rupee risked, after costs.",
        "detail": (
            "Below 1.0 you are risking more than you stand to make, which needs "
            "a win rate above 50% just to break even."
        ),
        "use": "Reject setups under about 1.2 unless the win rate justifies it.",
    },
]
