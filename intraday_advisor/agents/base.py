"""The agent contract, and the one rule that makes agents safe here.

**An agent may only hold a trade back. It can never create or improve one.**

That constraint is the whole design. An agent that can turn WAIT into BUY, or
raise a score, becomes the system's real strategy — an unmeasured, unbacktested,
non-deterministic one that happens to sound confident. Every verdict is
therefore clamped to one of:

* ``PROCEED``  — no objection. The engine's signal stands, unchanged.
* ``CAUTION``  — proceed only deliberately; the concerns are listed.
* ``VETO``     — do not take this trade.

A ``VETO`` on a WAIT is meaningless and collapses to ``PROCEED``; there is
nothing to stop. The clamp is enforced in :meth:`AgentVerdict.clamped` after
parsing, so a model that ignores its instructions cannot widen its own
authority.

Why run an agent at all, then? Because the engine scores a setup in isolation,
while the things that most often make a technically valid trade a bad idea are
contextual: the daily trend disagrees, VIX is spiking, breadth is diverging,
costs eat the target, the account cannot survive the position, it is 15:05. A
checklist-holder that refuses on those grounds is worth having — and the free
rule-based one does it deterministically, for nothing, offline.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

PROCEED = "PROCEED"
CAUTION = "CAUTION"
VETO = "VETO"

_ORDER = {PROCEED: 0, CAUTION: 1, VETO: 2}


@dataclass
class AgentVerdict:
    """One agent's opinion on one proposed trade."""

    stance: str = PROCEED
    confidence: int = 0                       # 0-100, the agent's own certainty
    summary: str = ""
    concerns: list[str] = field(default_factory=list)
    supports: list[str] = field(default_factory=list)
    provider: str = "none"
    model: str = ""
    latency_ms: int = 0
    error: str | None = None
    # True when the agent could not be reached and this is a safe default.
    degraded: bool = False

    def clamped(self, *, tradable: bool) -> "AgentVerdict":
        """Enforce the authority limits, whatever the agent actually said.

        An unparseable or out-of-range stance becomes CAUTION, never PROCEED:
        a broken agent must not read as approval.
        """
        stance = str(self.stance or "").strip().upper()
        if stance not in _ORDER:
            stance = CAUTION
            self.concerns.append(
                f"Agent returned an unrecognised stance {self.stance!r}; "
                f"treated as CAUTION."
            )
        # Vetoing a non-trade is noise.
        if not tradable and stance == VETO:
            stance = PROCEED
        self.stance = stance
        self.confidence = max(0, min(100, int(self.confidence or 0)))
        return self

    @property
    def blocks(self) -> bool:
        return self.stance == VETO

    def as_dict(self) -> dict:
        return asdict(self)


def combine(verdicts: list[AgentVerdict]) -> AgentVerdict:
    """Merge several agents' verdicts, taking the most cautious stance.

    Deliberately pessimistic: one agent objecting is enough. Averaging stances
    would let a confident-but-wrong model talk over a correct objection, which
    is precisely the failure an agent layer is supposed to prevent.
    """
    usable = [v for v in verdicts if v is not None]
    if not usable:
        return AgentVerdict(stance=PROCEED, summary="No agent ran.", provider="none")

    worst = max(usable, key=lambda v: _ORDER.get(v.stance, 1))
    merged = AgentVerdict(
        stance=worst.stance,
        confidence=max(v.confidence for v in usable),
        summary=worst.summary,
        concerns=[c for v in usable for c in v.concerns],
        supports=[s for v in usable for s in v.supports],
        provider="+".join(sorted({v.provider for v in usable})),
        model="+".join(sorted({v.model for v in usable if v.model})),
        latency_ms=sum(v.latency_ms for v in usable),
        degraded=any(v.degraded for v in usable),
    )
    errors = [v.error for v in usable if v.error]
    merged.error = "; ".join(errors) if errors else None
    return merged


@dataclass
class TradeBrief:
    """Everything an agent is allowed to see about one proposed trade.

    A flat, serialisable summary rather than the raw snapshot: it keeps an LLM
    prompt small and, more importantly, fixes exactly what the agent reasons
    over so two providers can be compared on identical input.
    """

    index: str
    direction: str
    score: int
    contract: str
    entry_premium: float
    target_premium: float
    stop_premium: float
    lots: int | None = None
    risk_amount: float | None = None
    risk_pct_of_capital: float | None = None
    net_rr: float | None = None
    breakeven: float | None = None
    cost_drag: float | None = None
    affordable: bool = True
    available_capital: float | None = None

    regime: str = "UNKNOWN"
    regime_bias: str = "NEUTRAL"
    regime_conviction: int = 0
    vix: float | None = None
    vix_level: str = "unknown"
    breadth_pct: float | None = None
    breadth_divergence: bool = False
    global_lean: str = "unknown"
    range_position: float | None = None

    minutes_to_square_off: int | None = None
    market_open: bool = False
    data_is_live: bool = False
    graded_trades: int = 0
    measured_expectancy: float | None = None

    readings: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def tradable(self) -> bool:
        return self.direction in ("CALL", "PUT")

    def as_dict(self) -> dict:
        return asdict(self)


class Agent(Protocol):
    """Anything that can review a brief and return a verdict."""

    name: str

    def review(self, brief: TradeBrief) -> AgentVerdict: ...


def timed(func):
    """Decorator recording how long a review took, for the UI."""
    def wrapper(self, brief: TradeBrief) -> AgentVerdict:
        started = time.perf_counter()
        verdict = func(self, brief)
        verdict.latency_ms = int((time.perf_counter() - started) * 1000)
        return verdict.clamped(tradable=brief.tradable)
    return wrapper


def brief_from_snapshot(
    signal: dict, snapshot: dict, regime: dict | None = None
) -> TradeBrief:
    """Build a brief from one signal inside an engine snapshot."""
    plan = signal.get("plan") or {}
    regime = regime or {}
    volatility = regime.get("volatility") or {}
    breadth = regime.get("breadth") or {}
    glob = regime.get("global_cues") or {}
    levels = regime.get("levels") or {}
    ledger = snapshot.get("ledger") or {}
    performance = ledger.get("performance") or {}
    settings = snapshot.get("settings") or {}

    return TradeBrief(
        index=signal.get("index", ""),
        direction=signal.get("direction", "WAIT"),
        score=int(signal.get("score") or 0),
        contract=f"{signal.get('strike')} {signal.get('option_type')}",
        entry_premium=float(signal.get("entry_premium") or 0),
        target_premium=float(signal.get("target_premium") or 0),
        stop_premium=float(signal.get("stop_premium") or 0),
        lots=plan.get("lots"),
        risk_amount=plan.get("risk_amount"),
        risk_pct_of_capital=plan.get("risk_pct_of_capital"),
        net_rr=plan.get("net_rr"),
        breakeven=plan.get("breakeven"),
        cost_drag=plan.get("cost_drag"),
        affordable=bool(plan.get("affordable", True)),
        available_capital=settings.get("available_capital"),
        regime=regime.get("regime", "UNKNOWN"),
        regime_bias=regime.get("bias", "NEUTRAL"),
        regime_conviction=int(regime.get("conviction") or 0),
        vix=volatility.get("vix"),
        vix_level=volatility.get("level", "unknown"),
        breadth_pct=breadth.get("pct"),
        breadth_divergence=bool(breadth.get("divergence")),
        global_lean=glob.get("lean", "unknown"),
        range_position=levels.get("range_position"),
        minutes_to_square_off=snapshot.get("minutes_to_square_off"),
        market_open=bool(snapshot.get("market_open")),
        data_is_live=snapshot.get("mode") == "LIVE",
        graded_trades=int(performance.get("trades") or 0),
        measured_expectancy=performance.get("expectancy"),
        readings=signal.get("readings") or [],
        warnings=list(signal.get("warnings") or []),
    )
