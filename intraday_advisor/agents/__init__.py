"""Agent layer: a second opinion that can only ever hold a trade back.

Configure with environment variables (all optional)::

    AGENT_PROVIDER=rules      # default. Free, offline, deterministic.
    AGENT_PROVIDER=ollama     # free local LLM, plus the rule checks
    AGENT_PROVIDER=groq       # free tier, plus the rule checks
    AGENT_PROVIDER=gemini     # free tier, plus the rule checks
    AGENT_PROVIDER=none       # disable entirely

    AGENT_MODEL=llama3.1:8b
    AGENT_BASE_URL=http://localhost:11434/v1
    AGENT_API_KEY=...

The rule agent **always runs**, whatever else is configured. An LLM is an
addition to the checklist, never a replacement for it: it is slower, it is
non-deterministic, and it is the component most likely to be unavailable
exactly when the market is busy.

Verdicts combine pessimistically — one objection is enough. See
:mod:`intraday_advisor.agents.base` for why an agent may never approve or
upgrade a trade, only hold it back.
"""

from __future__ import annotations

import os

from intraday_advisor.agents.base import (
    CAUTION,
    PROCEED,
    VETO,
    Agent,
    AgentVerdict,
    TradeBrief,
    brief_from_snapshot,
    combine,
)
from intraday_advisor.agents.llm import PRESETS, LLMAgent
from intraday_advisor.agents.rules import RuleAgent

__all__ = [
    "PROCEED", "CAUTION", "VETO", "PRESETS",
    "Agent", "AgentVerdict", "TradeBrief", "LLMAgent", "RuleAgent",
    "brief_from_snapshot", "combine", "build_agents", "review_signal",
    "agent_status",
]


def build_agents(
    provider: str | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> list[Agent]:
    """Build the configured agent stack.

    The rule agent is always included unless the provider is explicitly
    ``none`` — it costs nothing, cannot fail, and catches the mistakes that
    actually drain accounts.
    """
    choice = (provider or os.getenv("AGENT_PROVIDER", "rules")).strip().lower()
    if choice in ("none", "off", "disabled"):
        return []

    agents: list[Agent] = [RuleAgent()]
    if choice in ("rules", "", "checklist"):
        return agents

    agents.append(LLMAgent(
        provider=choice,
        model=model or os.getenv("AGENT_MODEL"),
        base_url=base_url or os.getenv("AGENT_BASE_URL"),
        api_key=api_key or os.getenv("AGENT_API_KEY"),
    ))
    return agents


def review_signal(
    signal: dict,
    snapshot: dict,
    regime: dict | None = None,
    *,
    agents: list[Agent] | None = None,
) -> dict:
    """Run the agent stack over one signal and return a combined verdict dict.

    Never raises: an agent that throws becomes a CAUTION naming the failure,
    because a review layer that can crash the dashboard is worse than none.
    """
    stack = agents if agents is not None else build_agents()
    if not stack:
        return AgentVerdict(
            stance=PROCEED, summary="Agent review disabled.", provider="none"
        ).as_dict()

    brief = brief_from_snapshot(signal, snapshot, regime)
    verdicts = []
    for agent in stack:
        try:
            verdicts.append(agent.review(brief))
        except Exception as exc:  # noqa: BLE001
            verdicts.append(AgentVerdict(
                stance=CAUTION, provider=getattr(agent, "name", "unknown"),
                summary=f"Agent raised: {exc}", degraded=True, error=str(exc),
                concerns=[f"{getattr(agent, 'name', 'agent')} failed: {exc}"],
            ).clamped(tradable=brief.tradable))

    return combine(verdicts).as_dict()


def agent_status(provider: str | None = None) -> dict:
    """What is configured, whether it can run, and how to set it up.

    Surfaced in the dashboard so a misconfigured agent is visible rather than
    silently absent.
    """
    choice = (provider or os.getenv("AGENT_PROVIDER", "rules")).strip().lower()
    status = {
        "provider": choice,
        "rules_active": choice not in ("none", "off", "disabled"),
        "llm_active": False,
        "llm_ready": False,
        "model": None,
        "detail": "",
        "presets": {
            name: {"note": spec["note"], "needs_key": spec["needs_key"]}
            for name, spec in PRESETS.items()
        },
    }

    if choice in ("none", "off", "disabled"):
        status["detail"] = "Agent review is disabled (AGENT_PROVIDER=none)."
        return status
    if choice in ("rules", "", "checklist"):
        status["detail"] = (
            "Rule checklist only — free, offline, deterministic. Set "
            "AGENT_PROVIDER=ollama for a free local LLM on top."
        )
        return status

    agent = LLMAgent(
        provider=choice,
        model=os.getenv("AGENT_MODEL"),
        base_url=os.getenv("AGENT_BASE_URL"),
        api_key=os.getenv("AGENT_API_KEY"),
    )
    ready, why = agent.available()
    status.update({
        "llm_active": True, "llm_ready": ready, "model": agent.model,
        "detail": (
            f"Rule checklist + {choice} ({agent.model})." if ready
            else f"Rule checklist only — {why}"
        ),
    })
    return status
