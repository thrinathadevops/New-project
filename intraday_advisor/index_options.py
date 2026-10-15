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

import pandas as pd

from intraday_advisor.data import drop_forming_candle
from intraday_advisor.indicators import add_indicators
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy, ema_swing_breakout_decision
from intraday_advisor.smart_money import analyze_smart_money
from intraday_advisor.scoring import ScoreWeights, score_signal
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
    is_live: bool = False

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


def _underlying_bias(df: pd.DataFrame) -> tuple[str, int, list[str], list[str]]:
    """Return (bias, confidence, reasons, warnings) for the index underlying.

    Uses the last CLOSED candle only. ``confidence`` is the unified score's
    distance from neutral, expressed 0-100.
    """
    enriched = apply_ema_swing_breakout_strategy(add_indicators(df))
    usable = enriched.dropna(subset=["Close", "EMA9", "EMA21", "ATR14"])
    if usable.empty:
        return "neutral", 0, [], ["Not enough candles for underlying trend."]

    last = usable.iloc[-1]
    decision = ema_swing_breakout_decision(enriched.index.name or "INDEX", enriched)
    smart = analyze_smart_money(enriched)

    row = {
        "Bullish_OB": bool(last.get("Bullish_OB", False)),
        "Bearish_OB": bool(last.get("Bearish_OB", False)),
        "Bullish_FVG": bool(last.get("Bullish_FVG", False)),
        "Bearish_FVG": bool(last.get("Bearish_FVG", False)),
        "Liquidity_Sweep_Bullish": bool(last.get("Liquidity_Sweep_Bullish", False)),
        "Liquidity_Sweep_Bearish": bool(last.get("Liquidity_Sweep_Bearish", False)),
        "SuperTrend_Dir": int(last.get("SuperTrend_Dir", 0)),
        "Adaptive_MACD_Hist": float(last.get("Adaptive_MACD_Hist", 0)),
    }
    scored = score_signal(
        row, decision_signal=decision.signal, vwap_relation=smart.vwap_relation,
        weights=ScoreWeights.load(),
    )

    if scored.signal_type == "BUY":
        bias = "bullish"
    elif scored.signal_type == "SELL":
        bias = "bearish"
    else:
        bias = "neutral"
    # Confidence as distance from the neutral 50 baseline, scaled to 0-100.
    confidence = int(min(100, abs(scored.score - 50) * 2))
    reasons = [f"Underlying: {bias} (score {scored.score})"] + scored.bull + scored.bear
    return bias, confidence, reasons, list(smart.warnings)


def analyze_index_option(
    index: str,
    *,
    candles: pd.DataFrame | None = None,
    chain: dict | None = None,
    interval: str = "5m",
    period: str = "5d",
    target_pct: float = DEFAULT_TARGET_PCT,
    stop_pct: float = DEFAULT_STOP_PCT,
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

    underlying_bias = "neutral"
    underlying_conf = 0
    if candles is not None and not candles.empty:
        closed = drop_forming_candle(candles, interval)
        underlying_bias, underlying_conf, u_reasons, u_warn = _underlying_bias(closed)
        reasons.extend(u_reasons)
        warnings.extend(u_warn)
    else:
        warnings.append("Underlying trend unavailable; relying on option chain only.")

    # --- 2. option chain (reuse a pre-fetched one if supplied) ---
    spot = float(candles["Close"].iloc[-1]) if (candles is not None and not candles.empty) else 0.0
    if chain is None:
        chain = suggest_option_trade(index, spot=spot)
    if chain.get("error"):
        warnings.append(chain["error"])
    chain_bias = chain.get("bias", "NEUTRAL")
    data_source = chain.get("data_source", "unknown")
    is_live = "live" in data_source.lower()
    reasons.extend(chain.get("reasons", []))

    # --- 3. confluence: price AND chain must agree ---
    direction = "WAIT"
    if underlying_bias == "bullish" and chain_bias in ("CALL", "NEUTRAL"):
        direction = "CALL"
    elif underlying_bias == "bearish" and chain_bias in ("PUT", "NEUTRAL"):
        direction = "PUT"
    elif chain_bias == "CALL" and underlying_bias != "bearish":
        direction = "CALL"
    elif chain_bias == "PUT" and underlying_bias != "bullish":
        direction = "PUT"
    else:
        if underlying_bias != "neutral" and chain_bias not in ("NEUTRAL",):
            warnings.append(
                f"Conflict: underlying is {underlying_bias} but option chain says {chain_bias} → WAIT."
            )

    # --- 4. confidence: reward agreement, penalise single-sided / conflict ---
    agree = (
        (direction == "CALL" and underlying_bias == "bullish" and chain_bias == "CALL")
        or (direction == "PUT" and underlying_bias == "bearish" and chain_bias == "PUT")
    )
    if direction == "WAIT":
        confidence = min(40, underlying_conf)
    elif agree:
        confidence = min(95, 55 + underlying_conf // 2)
    else:
        confidence = min(70, 45 + underlying_conf // 3)
    confidence = max(0, confidence - len(warnings) * 3)

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
        is_live=is_live,
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
