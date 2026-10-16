"""Index-options decision engine for NIFTY & BANKNIFTY intraday CALL/PUT.

This is the brain behind the clean options dashboard. For each index it:

  1. Reads the UNDERLYING trend/momentum from 5-min candles (the same
     indicators + unified :mod:`scoring` engine used everywhere else), acting
     only on the last CLOSED candle (no repaint).
  2. Reads the OPTION CHAIN (PCR, max-pain, OI walls) via
     :func:`suggest_option_trade`.
  3. Requires the two to AGREE before it calls CALL or PUT. Disagreement →
     WAIT. This confluence is the whole point: price says "up" AND the option
     chain says "up" before you buy a CALL.

It then turns the decision into an actionable option plan on the PREMIUM:
entry = current ATM premium, target = +``target_pct``, stop = -``stop_pct``.

HONESTY: this ranks probability, it does not predict the future. WAIT is a
valid, frequent output and means "no clean edge right now" — that is the signal
working, not failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from intraday_advisor.data import drop_forming_candle
from intraday_advisor.indicators import add_indicators
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy, ema_swing_breakout_decision
from intraday_advisor.smart_money import analyze_smart_money
from intraday_advisor.scoring import (
    OptionScore,
    ScoreWeights,
    SignalScore,
    score_option_signal,
    score_signal,
)
from intraday_advisor.nse_option_chain import suggest_option_trade

INDEX_CONFIG = {
    "NIFTY": {"yahoo": "^NSEI", "step": 50},
    "BANKNIFTY": {"yahoo": "^NSEBANK", "step": 100},
}

DEFAULT_TARGET_PCT = 0.25
DEFAULT_STOP_PCT = 0.20


@dataclass
class IndexOptionSignal:
    index: str
    direction: str               # CALL / PUT / WAIT
    confidence: int              # 0-100
    spot: float
    strike: float | int
    option_type: str             # CE / PE / —
    entry_premium: float
    target_premium: float
    stop_premium: float
    target_pct: float
    stop_pct: float
    underlying_bias: str         # bullish / bearish / neutral
    chain_bias: str              # CALL / PUT / NEUTRAL
    pcr: float
    max_pain: float
    ce_wall: float
    pe_wall: float
    data_source: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timestamp: str = ""
    # Timestamp of the CLOSED candle this decision was computed from. Distinct
    # from ``timestamp``, which is wall-clock emission time. The ledger grades
    # against this one: grading against emission time would let the grader read
    # prices from inside the signal bar, which is look-ahead.
    bar_time: str = ""
    is_live: bool = False
    # Full traceability: every signed contribution to the score, the raw
    # feature values it was computed from, and how price and chain related.
    # The dashboard renders these so a score is never an unexplained number.
    score_components: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    agreement: str = ""
    underlying_score: int = 0
    #: Which expiry this plan is quoted against, and what else is available.
    expiry: str | None = None
    expiries: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


def _num(value, default: float = 0.0) -> float:
    """Float that is never NaN or infinite, for values destined for JSON."""
    try:
        if value is None or pd.isna(value):
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if (result != result or result in (float("inf"), float("-inf"))) else result


def analyse_underlying(df: pd.DataFrame) -> tuple[SignalScore | None, dict, list[str]]:
    """Score the index underlying from its last CLOSED candle.

    Returns ``(score, feature_row, warnings)``. The feature row is handed
    straight to the ledger, so what gets graded is exactly what was scored.
    ``None`` means there were not enough candles to form a view.
    """
    enriched = apply_ema_swing_breakout_strategy(add_indicators(df))
    usable = enriched.dropna(subset=["Close", "EMA9", "EMA21", "ATR14"])
    if usable.empty:
        return None, {}, ["Not enough candles for underlying trend."]

    last = usable.iloc[-1]
    decision = ema_swing_breakout_decision(enriched.index.name or "INDEX", enriched)
    smart = analyze_smart_money(enriched)

    row = {}
    for col, val in last.items():
        if isinstance(val, (bool, np.bool_)):
            row[col] = bool(val)
        elif isinstance(val, (int, np.integer)):
            row[col] = int(val)
        elif isinstance(val, (float, np.floating)):
            row[col] = _num(val)
        else:
            row[col] = val
    scored = score_signal(
        row, decision_signal=decision.signal, vwap_relation=smart.vwap_relation,
        weights=ScoreWeights.load(),
    )
    return scored, row, list(smart.warnings)


def analyze_index_option(
    index: str,
    *,
    candles: pd.DataFrame | None = None,
    chain: dict | None = None,
    interval: str = "5m",
    period: str = "5d",
    target_pct: float = DEFAULT_TARGET_PCT,
    stop_pct: float = DEFAULT_STOP_PCT,
    expiry: str | None = None,
) -> IndexOptionSignal:
    """Full CALL/PUT/WAIT decision for one index.

    ``candles`` may be injected (tests / a custom feed); otherwise underlying
    candles are pulled from Yahoo (``^NSEI`` / ``^NSEBANK``).
    ``chain`` may be a pre-fetched ``suggest_option_trade`` result so callers
    that already have the option chain (e.g. the strike scanner) don't trigger a
    second broker login.
    """
    index = index.upper()
    if index not in INDEX_CONFIG:
        raise ValueError(f"Unsupported index '{index}'. Use one of {list(INDEX_CONFIG)}.")

    reasons: list[str] = []
    warnings: list[str] = []

    # --- 1. underlying candles + trend ---
    if candles is None:
        try:
            from intraday_advisor.data import fetch_yahoo_ohlcv
            candles = fetch_yahoo_ohlcv(INDEX_CONFIG[index]["yahoo"], period=period, interval=interval)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not fetch underlying candles: {exc}")
            candles = None

    bar_time = ""
    underlying_score: SignalScore | None = None
    feature_row: dict = {}
    if candles is not None and not candles.empty:
        closed = drop_forming_candle(candles, interval)
        if closed is not None and not closed.empty:
            bar_time = str(closed.index[-1])
        underlying_score, feature_row, u_warn = analyse_underlying(closed)
        warnings.extend(u_warn)
    else:
        warnings.append("Underlying trend unavailable; relying on option chain only.")

    # --- 2. option chain (reuse a pre-fetched one if supplied) ---
    spot = float(candles["Close"].iloc[-1]) if (candles is not None and not candles.empty) else 0.0
    if chain is None:
        chain = suggest_option_trade(index, spot=spot, expiry=expiry)
    if chain.get("error"):
        warnings.append(chain["error"])
    # The chain corrects the spot from live option prices (put-call parity);
    # the candle close is delayed. Adopt the corrected figure so the card, the
    # OI-wall distances in scoring and the chosen strike all agree.
    corrected = chain.get("spot")
    if corrected and "live" in str(chain.get("data_source", "")).lower():
        try:
            corrected = float(corrected)
            if corrected > 0 and (not spot or abs(corrected - spot) < spot * 0.02):
                spot = corrected
        except (TypeError, ValueError):
            pass

    chain_bias = chain.get("bias", "NEUTRAL")
    data_source = chain.get("data_source", "unknown")
    is_live = "live" in data_source.lower()
    reasons.extend(chain.get("reasons", []))

    # --- 3 & 4. direction + score, both from the ONE scoring engine ---
    # Previously this block ran its own confluence logic and its own confidence
    # arithmetic, making it a fourth scorer nothing could trace or calibrate.
    # Both now come from scoring.score_option_signal, which records every
    # contribution in ``components``.
    if underlying_score is None:
        underlying_score = score_signal({}, weights=ScoreWeights.load())

    option_score = score_option_signal(
        underlying_score,
        chain_bias=chain_bias,
        pcr=float(chain.get("pcr", 0) or 0) or None,
        spot=spot or None,
        ce_wall=float(chain.get("max_ce_wall", 0) or 0) or None,
        pe_wall=float(chain.get("max_pe_wall", 0) or 0) or None,
        warnings=warnings,
        weights=ScoreWeights.load(),
    )
    direction = option_score.direction
    confidence = option_score.score
    underlying_bias = option_score.underlying_bias
    warnings = list(option_score.warnings)
    reasons = (
        [f"Underlying: {underlying_bias} (price score {underlying_score.score})"]
        + option_score.bull
        + option_score.bear
        + reasons
    )

    # --- 5. option plan on the premium ---
    atm = chain.get("atm", round(spot))
    if direction == "CALL":
        option_type, strike, entry = "CE", chain.get("rec_strike", atm), float(chain.get("atm_ce_ltp", 0) or 0)
    elif direction == "PUT":
        option_type, strike, entry = "PE", chain.get("rec_strike", atm), float(chain.get("atm_pe_ltp", 0) or 0)
    else:
        option_type, strike, entry = "—", atm, 0.0

    # Round entry first, then derive target/stop from it so the three numbers
    # are always internally consistent with what the user sees.
    entry = round(entry, 2)
    target = round(entry * (1 + target_pct), 2) if entry > 0 else 0.0
    stop = round(entry * (1 - stop_pct), 2) if entry > 0 else 0.0
    if direction in ("CALL", "PUT") and entry > 0:
        reasons.append(
            f"Plan: buy {index} {strike} {option_type} near ₹{entry:.2f} → "
            f"target ₹{target:.2f} (+{target_pct*100:.0f}%), stop ₹{stop:.2f} (-{stop_pct*100:.0f}%)."
        )

    return IndexOptionSignal(
        index=index,
        direction=direction,
        confidence=int(confidence),
        spot=round(spot, 2),
        strike=strike,
        option_type=option_type,
        entry_premium=entry,
        target_premium=target,
        stop_premium=stop,
        target_pct=target_pct,
        stop_pct=stop_pct,
        underlying_bias=underlying_bias,
        chain_bias=chain_bias,
        pcr=float(chain.get("pcr", 0) or 0),
        max_pain=float(chain.get("max_pain", 0) or 0),
        ce_wall=float(chain.get("max_ce_wall", 0) or 0),
        pe_wall=float(chain.get("max_pe_wall", 0) or 0),
        data_source=data_source,
        reasons=reasons,
        warnings=warnings,
        timestamp=datetime.now().isoformat(timespec="seconds"),
        bar_time=bar_time,
        is_live=is_live,
        expiry=chain.get("expiry"),
        expiries=list(chain.get("expiries") or []),
        score_components=dict(option_score.components),
        features=dict(feature_row),
        agreement=option_score.agreement,
        underlying_score=underlying_score.score,
    )


def get_option_premium(index: str, strike: float | int, option_type: str, spot: float = 0.0) -> tuple[float, str]:
    """Live premium for a specific strike/type of a held position.

    Returns ``(premium, data_source)``. Falls back to the ATM premium if the
    exact strike is missing from the chain. Used by the position monitor on
    every refresh to compute current P&L.
    """
    chain = suggest_option_trade(index.upper(), spot=spot)
    col = "ce_ltp" if str(option_type).upper() == "CE" else "pe_ltp"
    df = chain.get("df")
    if df is not None and not df.empty and "strike" in df.columns:
        match = df[df["strike"] == strike]
        if not match.empty and col in match.columns:
            return float(match[col].iloc[0]), chain.get("data_source", "unknown")
    atm_col = "atm_ce_ltp" if str(option_type).upper() == "CE" else "atm_pe_ltp"
    return float(chain.get(atm_col, 0) or 0), chain.get("data_source", "unknown")


def scan_indices(
    *,
    target_pct: float = DEFAULT_TARGET_PCT,
    stop_pct: float = DEFAULT_STOP_PCT,
) -> list[IndexOptionSignal]:
    """Analyse every supported index, best (highest-confidence) trade first."""
    signals = []
    for index in INDEX_CONFIG:
        try:
            signals.append(analyze_index_option(index, target_pct=target_pct, stop_pct=stop_pct))
        except Exception as exc:  # noqa: BLE001 - one bad index shouldn't kill the scan
            signals.append(IndexOptionSignal(
                index=index, direction="WAIT", confidence=0, spot=0.0, strike=0, option_type="—",
                entry_premium=0.0, target_premium=0.0, stop_premium=0.0, target_pct=target_pct,
                stop_pct=stop_pct, underlying_bias="neutral", chain_bias="NEUTRAL", pcr=0.0,
                max_pain=0.0, ce_wall=0.0, pe_wall=0.0, data_source="error",
                reasons=[], warnings=[f"Analysis failed: {exc}"],
                timestamp=datetime.now().isoformat(timespec="seconds"),
            ))
    # Actionable trades first, then by confidence.
    signals.sort(key=lambda s: (s.direction != "WAIT", s.confidence), reverse=True)
    return signals
