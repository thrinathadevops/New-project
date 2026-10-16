"""Single source of truth for signal confidence.

Before this module there were two disagreeing scoring systems: hand-coded
constants in ``strategy.py`` (92 / 76 / 68 ...) and a separate additive engine
in ``realtime_server.py`` (+15 / +10 / +5 ...). Two engines, two answers for the
same setup, and neither traceable to evidence.

This module centralises the weights so that:

  1. every surface (real-time server, dashboards) scores a setup identically;
  2. each feature's contribution is recorded for explainability; and
  3. the weights can later be *calibrated from the backtest* (see
     ``ScoreWeights.load``) instead of being guessed.

IMPORTANT: the default weights below are heuristic priors, NOT yet fitted to
your data. A score of 70 means "this many weighted confirmations lined up", not
"70% historical win rate" — until you run calibration. Treat it as a ranking
aid, not a probability.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ScoreWeights:
    """Per-feature weight applied to the neutral baseline (0-100 scale)."""

    # Smart-money structure
    order_block: float = 15.0
    liquidity_sweep: float = 15.0
    fvg: float = 10.0
    # Trend / momentum
    supertrend: float = 10.0
    adaptive_macd: float = 5.0
    # Legacy confluence
    ema_signal: float = 10.0
    vwap: float = 5.0

    # --- New indicator weights (added by strategy audit) ---
    # Momentum oscillators
    adx_trend: float = 5.0         # ADX confirms trending market
    stochastic: float = 3.0        # Stochastic overbought/oversold
    mfi: float = 3.0               # Money Flow Index
    # Volatility structure
    bollinger: float = 4.0         # Bollinger band position
    squeeze: float = 5.0           # TTM Squeeze breakout
    parabolic_sar: float = 4.0     # Parabolic SAR direction
    # Divergence (high weight — among the most reliable signals)
    rsi_divergence: float = 8.0    # RSI divergence
    macd_divergence: float = 7.0   # MACD divergence
    # Structure
    ichimoku: float = 5.0          # Ichimoku cloud position
    # Candlestick patterns
    candle_pattern: float = 4.0    # Multi-bar candlestick patterns
    # Volume flow
    volume_flow: float = 3.0       # CMF / Force Index
    # MA Ribbon
    ma_ribbon: float = 4.0         # Moving average ribbon alignment

    # --- Option-chain confluence (index options only) ---
    chain_agreement: float = 12.0
    chain_conflict: float = 20.0
    pcr_extreme: float = 5.0
    oi_wall: float = 5.0
    # Flat penalty per analysis warning, so data problems visibly cost score.
    warning_penalty: float = 3.0

    @classmethod
    def load(cls, path: str | None = None) -> "ScoreWeights":
        """Load calibrated weights from JSON if available, else defaults.

        Path resolution: explicit ``path`` → ``SCORE_WEIGHTS_FILE`` env var →
        built-in defaults. This is the hook a future backtest-calibration step
        writes to, so improved weights flow through without code changes.
        """
        target = path or os.getenv("SCORE_WEIGHTS_FILE")
        if not target or not os.path.exists(target):
            return cls()
        try:
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            known = {k: float(v) for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        except (OSError, ValueError, json.JSONDecodeError):
            return cls()  # fail safe to defaults rather than crash live trading


@dataclass(frozen=True)
class SignalScore:
    score: int                       # clamped 0-100
    signal_type: str                 # BUY / SELL / HOLD
    bull: list[str] = field(default_factory=list)
    bear: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)  # feature -> signed points

    def as_dict(self) -> dict:
        return asdict(self)


def _truthy(row, key: str) -> bool:
    val = row.get(key, False)
    try:
        return bool(val)
    except Exception:
        return False


def score_signal(
    row,
    *,
    decision_signal: str | None = None,
    vwap_relation: str | None = None,
    weights: ScoreWeights | None = None,
    baseline: float = 50.0,
    buy_threshold: float = 65.0,
    sell_threshold: float = 35.0,
) -> SignalScore:
    """Combine indicator/structure features into one 0-100 confidence score.

    ``row`` is a mapping of the latest **closed-candle** analysis values
    (e.g. ``Bullish_OB``, ``SuperTrend_Dir``, ``Adaptive_MACD_Hist``).
    ``decision_signal`` and ``vwap_relation`` come from the legacy strategy and
    smart-money reports. Every contribution is recorded in ``components`` so the
    UI can show *why* a score is what it is.
    """
    w = weights or ScoreWeights()
    score = baseline
    bull: list[str] = []
    bear: list[str] = []
    components: dict[str, float] = {}

    def add(name: str, points: float, bull_msg: str | None = None, bear_msg: str | None = None):
        nonlocal score
        score += points
        components[name] = components.get(name, 0.0) + points
        if points > 0 and bull_msg:
            bull.append(bull_msg)
        elif points < 0 and bear_msg:
            bear.append(bear_msg)

    # --- Smart-money structure ---
    if _truthy(row, "Bullish_OB"):
        add("order_block", w.order_block, bull_msg="Bullish order block formed")
    elif _truthy(row, "Bearish_OB"):
        add("order_block", -w.order_block, bear_msg="Bearish order block formed")

    if _truthy(row, "Liquidity_Sweep_Bullish"):
        add("liquidity_sweep", w.liquidity_sweep, bull_msg="Bullish liquidity sweep")
    elif _truthy(row, "Liquidity_Sweep_Bearish"):
        add("liquidity_sweep", -w.liquidity_sweep, bear_msg="Bearish liquidity sweep")

    if _truthy(row, "Bullish_FVG"):
        add("fvg", w.fvg, bull_msg="Bullish fair value gap")
    elif _truthy(row, "Bearish_FVG"):
        add("fvg", -w.fvg, bear_msg="Bearish fair value gap")

    # --- Trend / momentum ---
    st_dir = row.get("SuperTrend_Dir", 0)
    if st_dir == 1:
        add("supertrend", w.supertrend, bull_msg="SuperTrend bullish")
    elif st_dir == -1:
        add("supertrend", -w.supertrend, bear_msg="SuperTrend bearish")

    macd_hist = row.get("Adaptive_MACD_Hist", 0) or 0
    if macd_hist > 0:
        add("adaptive_macd", w.adaptive_macd, bull_msg="Adaptive MACD momentum positive")
    elif macd_hist < 0:
        add("adaptive_macd", -w.adaptive_macd, bear_msg="Adaptive MACD momentum negative")

    # --- Legacy confluence ---
    if decision_signal == "BUY":
        add("ema_signal", w.ema_signal, bull_msg="EMA swing-breakout BUY")
    elif decision_signal == "SELL":
        add("ema_signal", -w.ema_signal, bear_msg="EMA swing-breakout SELL")

    if vwap_relation == "above VWAP":
        add("vwap", w.vwap, bull_msg="Price above VWAP")
    elif vwap_relation == "below VWAP":
        add("vwap", -w.vwap, bear_msg="Price below VWAP")

    # --- ADX trend strength ---
    adx_val = row.get("ADX", 0) or 0
    plus_di = row.get("Plus_DI", 0) or 0
    minus_di = row.get("Minus_DI", 0) or 0
    if adx_val > 25:
        if plus_di > minus_di:
            add("adx_trend", w.adx_trend, bull_msg=f"ADX {adx_val:.0f} confirms bullish trend")
        elif minus_di > plus_di:
            add("adx_trend", -w.adx_trend, bear_msg=f"ADX {adx_val:.0f} confirms bearish trend")

    # --- Stochastic ---
    stoch_k = row.get("Stoch_K", 50) or 50
    stoch_d = row.get("Stoch_D", 50) or 50
    if stoch_k < 20 and stoch_k > stoch_d:
        add("stochastic", w.stochastic, bull_msg="Stochastic bullish cross in oversold")
    elif stoch_k > 80 and stoch_k < stoch_d:
        add("stochastic", -w.stochastic, bear_msg="Stochastic bearish cross in overbought")

    # --- MFI (volume-weighted RSI) ---
    mfi_val = row.get("MFI14", 50) or 50
    if mfi_val > 80:
        add("mfi", -w.mfi, bear_msg=f"MFI {mfi_val:.0f} overbought (distribution)")
    elif mfi_val < 20:
        add("mfi", w.mfi, bull_msg=f"MFI {mfi_val:.0f} oversold (accumulation)")

    # --- Bollinger Bands ---
    bb_pctb = row.get("BB_PctB")
    if bb_pctb is not None:
        if bb_pctb > 1.0:
            add("bollinger", -w.bollinger * 0.5, bear_msg="Price above upper Bollinger Band")
        elif bb_pctb < 0.0:
            add("bollinger", w.bollinger * 0.5, bull_msg="Price below lower Bollinger Band")

    # --- TTM Squeeze ---
    squeeze_on = row.get("Squeeze_On", False)
    squeeze_mom = row.get("Squeeze_Mom", 0) or 0
    if squeeze_on:
        # Squeeze is building — don't add direction yet
        pass
    elif squeeze_mom > 0:
        add("squeeze", w.squeeze, bull_msg="Squeeze fired bullish")
    elif squeeze_mom < 0:
        add("squeeze", -w.squeeze, bear_msg="Squeeze fired bearish")

    # --- Parabolic SAR ---
    psar_dir = row.get("PSAR_Dir", 0) or 0
    if psar_dir == 1:
        add("parabolic_sar", w.parabolic_sar, bull_msg="Parabolic SAR bullish")
    elif psar_dir == -1:
        add("parabolic_sar", -w.parabolic_sar, bear_msg="Parabolic SAR bearish")

    # --- RSI Divergence (high-probability reversal) ---
    if _truthy(row, "RSI_Div_Bull"):
        add("rsi_divergence", w.rsi_divergence, bull_msg="Bullish RSI divergence (reversal signal)")
    elif _truthy(row, "RSI_Div_Bear"):
        add("rsi_divergence", -w.rsi_divergence, bear_msg="Bearish RSI divergence (reversal signal)")
    elif _truthy(row, "RSI_Div_Hidden_Bull"):
        add("rsi_divergence", w.rsi_divergence * 0.6, bull_msg="Hidden bullish RSI divergence (continuation)")
    elif _truthy(row, "RSI_Div_Hidden_Bear"):
        add("rsi_divergence", -w.rsi_divergence * 0.6, bear_msg="Hidden bearish RSI divergence (continuation)")

    # --- MACD Divergence ---
    if _truthy(row, "MACD_Div_Bull"):
        add("macd_divergence", w.macd_divergence, bull_msg="Bullish MACD divergence")
    elif _truthy(row, "MACD_Div_Bear"):
        add("macd_divergence", -w.macd_divergence, bear_msg="Bearish MACD divergence")

    # --- Ichimoku Cloud ---
    if _truthy(row, "Ichimoku_Above_Cloud"):
        add("ichimoku", w.ichimoku, bull_msg="Price above Ichimoku cloud")
    elif _truthy(row, "Ichimoku_Below_Cloud"):
        add("ichimoku", -w.ichimoku, bear_msg="Price below Ichimoku cloud")
    if _truthy(row, "Ichimoku_TK_Cross_Bull"):
        add("ichimoku", w.ichimoku * 0.5, bull_msg="Ichimoku TK bullish cross")
    elif _truthy(row, "Ichimoku_TK_Cross_Bear"):
        add("ichimoku", -w.ichimoku * 0.5, bear_msg="Ichimoku TK bearish cross")

    # --- Multi-bar candlestick patterns ---
    if _truthy(row, "Morning_Star") or _truthy(row, "Three_White_Soldiers") or _truthy(row, "Three_Inside_Up"):
        add("candle_pattern", w.candle_pattern, bull_msg="Bullish multi-bar candle pattern")
    elif _truthy(row, "Evening_Star") or _truthy(row, "Three_Black_Crows") or _truthy(row, "Three_Inside_Down"):
        add("candle_pattern", -w.candle_pattern, bear_msg="Bearish multi-bar candle pattern")
    elif _truthy(row, "Tweezer_Bottom") or _truthy(row, "Bullish_Harami"):
        add("candle_pattern", w.candle_pattern * 0.5, bull_msg="Bullish reversal candle pattern")
    elif _truthy(row, "Tweezer_Top") or _truthy(row, "Bearish_Harami"):
        add("candle_pattern", -w.candle_pattern * 0.5, bear_msg="Bearish reversal candle pattern")

    # --- Volume flow (CMF + Force Index) ---
    cmf = row.get("CMF20", 0) or 0
    force = row.get("Force_Index", 0) or 0
    if cmf > 0.1 and force > 0:
        add("volume_flow", w.volume_flow, bull_msg="Buying pressure (CMF + Force Index positive)")
    elif cmf < -0.1 and force < 0:
        add("volume_flow", -w.volume_flow, bear_msg="Selling pressure (CMF + Force Index negative)")

    # --- MA Ribbon ---
    if _truthy(row, "Ribbon_Bullish"):
        add("ma_ribbon", w.ma_ribbon, bull_msg="MA Ribbon fully bullish (all 8 EMAs aligned)")
    elif _truthy(row, "Ribbon_Bearish"):
        add("ma_ribbon", -w.ma_ribbon, bear_msg="MA Ribbon fully bearish (all 8 EMAs aligned)")

    confidence = int(max(0, min(100, round(score))))
    if confidence >= buy_threshold:
        signal_type = "BUY"
    elif confidence <= sell_threshold:
        signal_type = "SELL"
    else:
        signal_type = "HOLD"

    return SignalScore(confidence, signal_type, bull, bear, components)

# --------------------------------------------------------------------------- #
# Index-option direction + score
# --------------------------------------------------------------------------- #

# PCR above this reads as bullish (puts being written), below the inverse as
# bearish. Conventional retail thresholds; calibratable like any other weight.
PCR_BULLISH = 1.3
PCR_BEARISH = 0.7


@dataclass(frozen=True)
class OptionScore:
    """A CALL / PUT / WAIT call with a fully traceable 0-100 score."""

    direction: str                   # CALL / PUT / WAIT
    score: int                       # 0-100
    underlying_score: int            # the price-only score it started from
    underlying_bias: str             # bullish / bearish / neutral
    chain_bias: str                  # CALL / PUT / NEUTRAL
    agreement: str                   # agree / chain-only / price-only / conflict
    bull: list[str] = field(default_factory=list)
    bear: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.direction in ("CALL", "PUT")

    def as_dict(self) -> dict:
        data = asdict(self)
        data["actionable"] = self.actionable
        return data


def score_option_signal(
    underlying: SignalScore,
    *,
    chain_bias: str = "NEUTRAL",
    pcr: float | None = None,
    spot: float | None = None,
    ce_wall: float | None = None,
    pe_wall: float | None = None,
    warnings: list[str] | None = None,
    weights: ScoreWeights | None = None,
) -> OptionScore:
    """Combine the price-only score with option-chain evidence.

    ``underlying`` is the result of :func:`score_signal` on the index's own
    candles. Everything the option chain contributes is added here as a named,
    signed component, so the dashboard can show precisely why a setup scored
    what it scored and a future calibration can re-fit each piece.

    Direction requires CONFLUENCE: price and chain must not contradict each
    other. A conflict returns WAIT however strong either side looks on its own,
    because the one thing worse than no signal is two disagreeing ones.
    """
    w = weights or ScoreWeights()
    warnings = list(warnings or [])
    chain_bias = (chain_bias or "NEUTRAL").upper()

    if underlying.signal_type == "BUY":
        bias = "bullish"
    elif underlying.signal_type == "SELL":
        bias = "bearish"
    else:
        bias = "neutral"

    # --- direction: confluence, or stand aside ---
    conflict = (
        (bias == "bullish" and chain_bias == "PUT")
        or (bias == "bearish" and chain_bias == "CALL")
    )
    if conflict:
        direction, agreement = "WAIT", "conflict"
        warnings.append(
            f"Conflict: price reads {bias} but the option chain reads {chain_bias}. "
            f"Standing aside is the signal working, not failing."
        )
    elif bias == "bullish" and chain_bias == "CALL":
        direction, agreement = "CALL", "agree"
    elif bias == "bearish" and chain_bias == "PUT":
        direction, agreement = "PUT", "agree"
    elif bias == "bullish":
        direction, agreement = "CALL", "price-only"
    elif bias == "bearish":
        direction, agreement = "PUT", "price-only"
    elif chain_bias == "CALL":
        direction, agreement = "CALL", "chain-only"
    elif chain_bias == "PUT":
        direction, agreement = "PUT", "chain-only"
    else:
        direction, agreement = "WAIT", "neither"

    # --- score: start from the price-only distance off neutral ---
    bull = list(underlying.bull)
    bear = list(underlying.bear)
    components = dict(underlying.components)
    # Directional magnitude of the underlying read, 0-50.
    score = abs(underlying.score - 50.0)

    def add(name: str, points: float, message: str | None = None) -> None:
        nonlocal score
        score += points
        components[name] = components.get(name, 0.0) + points
        if message:
            (bull if points > 0 else bear).append(message)

    if agreement == "agree":
        add("chain_agreement", w.chain_agreement,
            f"Option chain agrees with price ({chain_bias})")
    elif agreement == "conflict":
        add("chain_conflict", -w.chain_conflict,
            f"Option chain contradicts price ({bias} vs {chain_bias})")
    elif agreement in ("price-only", "chain-only"):
        bull.append(
            "Price leads; option chain is neutral" if agreement == "price-only"
            else "Option chain leads; price is neutral"
        )

    # --- PCR extremes, in the direction they support ---
    if pcr is not None and pcr > 0:
        if pcr >= PCR_BULLISH and direction == "CALL":
            add("pcr_extreme", w.pcr_extreme, f"PCR {pcr:.2f} supports upside")
        elif pcr <= PCR_BEARISH and direction == "PUT":
            add("pcr_extreme", w.pcr_extreme, f"PCR {pcr:.2f} supports downside")
        elif pcr >= PCR_BULLISH and direction == "PUT":
            add("pcr_extreme", -w.pcr_extreme, f"PCR {pcr:.2f} argues against a PUT")
        elif pcr <= PCR_BEARISH and direction == "CALL":
            add("pcr_extreme", -w.pcr_extreme, f"PCR {pcr:.2f} argues against a CALL")

    # --- OI walls: price running INTO a wall is resistance against you ---
    if spot and spot > 0:
        if direction == "CALL" and ce_wall and ce_wall > 0 and spot < ce_wall:
            distance = (ce_wall - spot) / spot
            if distance < 0.003:
                add("oi_wall", -w.oi_wall,
                    f"CALL wall at {ce_wall:.0f} sits just overhead")
            else:
                add("oi_wall", w.oi_wall * 0.5,
                    f"Room to the {ce_wall:.0f} CALL wall")
        elif direction == "PUT" and pe_wall and pe_wall > 0 and spot > pe_wall:
            distance = (spot - pe_wall) / spot
            if distance < 0.003:
                add("oi_wall", -w.oi_wall,
                    f"PUT wall at {pe_wall:.0f} sits just below")
            else:
                add("oi_wall", w.oi_wall * 0.5,
                    f"Room to the {pe_wall:.0f} PUT wall")

    # --- data problems cost score, visibly ---
    if warnings:
        add("warnings", -w.warning_penalty * len(warnings))

    if direction == "WAIT":
        # A WAIT still carries a score, but capped: it reflects how clear the
        # "no trade" read is, and must never look like a tradable setup.
        score = min(score, 40.0)

    return OptionScore(
        direction=direction,
        score=int(max(0, min(100, round(score)))),
        underlying_score=underlying.score,
        underlying_bias=bias,
        chain_bias=chain_bias,
        agreement=agreement,
        bull=bull,
        bear=bear,
        components=components,
        warnings=warnings,
    )
