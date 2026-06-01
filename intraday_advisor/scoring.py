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

    confidence = int(max(0, min(100, round(score))))
    if confidence >= buy_threshold:
        signal_type = "BUY"
    elif confidence <= sell_threshold:
        signal_type = "SELL"
    else:
        signal_type = "HOLD"

    return SignalScore(confidence, signal_type, bull, bear, components)
