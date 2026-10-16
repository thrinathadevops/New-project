"""LLM agent over any OpenAI-compatible endpoint — the free options first.

One class covers every provider worth trying for free, because they all speak
the OpenAI chat-completions shape:

===============  =========================================  ==================
Provider         Base URL                                   Cost
===============  =========================================  ==================
Ollama (local)   http://localhost:11434/v1                  free, offline
LM Studio        http://localhost:1234/v1                   free, offline
Groq             https://api.groq.com/openai/v1             free tier, fast
Google Gemini    https://generativelanguage.googleapis       free tier
                 .com/v1beta/openai/
Together, etc.   provider's own                             varies
===============  =========================================  ==================

Configure with environment variables::

    AGENT_PROVIDER=ollama          # or groq / gemini / openai_compat / rules
    AGENT_MODEL=llama3.1:8b
    AGENT_BASE_URL=http://localhost:11434/v1
    AGENT_API_KEY=...              # not needed for Ollama / LM Studio

Design notes that matter more than the plumbing
-----------------------------------------------

* **The model is given no power to approve.** Its stance is clamped on the way
  out (see :mod:`~intraday_advisor.agents.base`), so the worst a hallucinating
  model can do is block a good trade — annoying, never expensive. The reverse
  arrangement would make an unmeasured model the real strategy.

* **A failure is a CAUTION, not a PROCEED.** If the endpoint is down or the
  reply is unparseable, the verdict says so and degrades to caution. Silent
  success on failure is how a safety layer becomes decoration.

* **Short timeout.** This runs inside a refresh loop during market hours; an
  agent that takes 30 seconds to answer is worse than no agent, so it is given
  a few seconds and otherwise abandoned.

* **Temperature 0.** Not for quality, but so the same brief tends to produce
  the same verdict. A risk check that changes its mind at random cannot be
  trusted or tested.
"""

from __future__ import annotations

import json
import os
import re
import time

from intraday_advisor.agents.base import (
    CAUTION,
    AgentVerdict,
    TradeBrief,
    timed,
)

# Known free/low-cost endpoints, so a user only has to name one.
PRESETS = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.1:8b",
        "needs_key": False,
        "note": "Local, free, offline. Install from ollama.com, then: ollama pull llama3.1:8b",
    },
    "lmstudio": {
        "base_url": "http://localhost:1234/v1",
        "model": "local-model",
        "needs_key": False,
        "note": "Local, free. Start the LM Studio server.",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        # ":free" models cost nothing. The exact ids churn, so this is only a
        # starting guess -- discover_model() confirms against live pricing.
        # OpenRouter's auto-router across free models: it picks whichever is
        # up, which is the right default when every hardcoded id in this
        # file had already been retired within weeks of being written.
        "model": "openrouter/free",
        "needs_key": True,
        "note": "One key, many models, several genuinely free. Key from openrouter.ai/keys",
        # OpenRouter asks callers to identify themselves; it is also how your
        # usage shows up on their dashboard.
        "headers": {
            "HTTP-Referer": "https://github.com/local/intraday-advisor",
            "X-Title": "Intraday Options Desk",
        },
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-20b",
        "needs_key": True,
        "note": "Free tier, very fast. Key from console.groq.com",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-flash-lite-latest",
        "needs_key": True,
        "note": "Free tier. Key from aistudio.google.com",
    },
}


def resolve_api_key(provider: str) -> str:
    """Find the key for one provider, so several can be configured at once.

    Order: ``AGENT_API_KEY`` (an explicit override for whichever provider is
    active) then ``<PROVIDER>_API_KEY``. Keeping per-provider names means
    GROQ_API_KEY and GEMINI_API_KEY can both sit in .env and switching provider
    is a one-word change rather than a copy-paste of secrets.
    """
    from intraday_advisor.envfile import load_env

    load_env()
    explicit = os.getenv("AGENT_API_KEY")
    if explicit:
        return explicit
    return os.getenv(f"{provider.upper()}_API_KEY", "")



# Preferred chat models per provider, best-first. Hosted providers retire model
# names without warning -- a stale default is how this integration broke the
# first time -- so when the configured model is rejected the agent asks the
# endpoint what it actually has and picks the first match from here.

_SUGGESTED = re.compile(r"use\s+(?:models/)?([A-Za-z0-9._\-]+)", re.I)


def _suggested_model(error_body: str) -> str | None:
    """Pull the replacement model name out of a provider's own error message."""
    if not error_body:
        return None
    match = _SUGGESTED.search(error_body)
    if not match:
        return None
    candidate = match.group(1).strip().rstrip(".,")
    # Guard against matching ordinary prose like "use the Interactions API".
    return candidate if any(c.isdigit() for c in candidate) else None


MODEL_PREFERENCES = {
    # OpenRouter: free tiers only. Ordered by how well they follow a
    # "return only JSON" instruction, which is the whole job here.
    "openrouter": (
        "openrouter/free",                          # auto-router, survives churn
        "google/gemma-4-31b-it:free",               # instruction-tuned
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3.5-lightning:free",       # fast
        "thinkingmachines/inkling:free",
    ),
    "groq": ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"),
    # flash-lite first on purpose: the larger flash models return 503
    # ("experiencing high demand") on the free tier often enough that they are
    # not dependable for a check that runs every refresh. A structured risk
    # verdict does not need the bigger model; it needs an answer.
    "gemini": ("gemini-flash-lite-latest", "gemini-3.8-flash", "gemini-flash-latest"),
    "ollama": ("llama3.1:8b", "llama3:8b", "qwen2.5:7b"),
    "lmstudio": (),
}

# Model ids are not chat models just because they are listed.
_NON_CHAT = (
    "whisper", "tts", "embed", "guard", "orpheus", "image", "vision-only",
    # Free does not mean suitable: these are audio, classifier or code-only
    # models that will not produce a sensible risk verdict.
    "lyria", "content-safety", "clip", "-code", "laguna", "moderation", "rerank",
)


def discover_model(base_url: str, api_key: str, provider: str,
                   timeout: float = 8.0, *, free_only: bool | None = None) -> str | None:
    """Ask the endpoint which models exist and choose a usable chat model.

    On OpenRouter this defaults to **free models only**. The catalogue is
    hundreds of models, most of them paid, and silently falling back to a
    billed model because a free id was retired is not an acceptable failure
    mode for a risk checker that runs all day.
    """
    if free_only is None:
        free_only = provider == "openrouter"

    try:
        import requests
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        response = requests.get(f"{base_url}/models", headers=headers, timeout=timeout)
        response.raise_for_status()
        entries = response.json().get("data", [])
    except Exception:  # noqa: BLE001 - discovery is best-effort by definition
        return None

    available: set[str] = set()
    for entry in entries:
        model_id = str(entry.get("id", "")).removeprefix("models/")
        if not model_id:
            continue
        if free_only and not _is_free(entry, model_id):
            continue
        available.add(model_id)

    for preferred in MODEL_PREFERENCES.get(provider, ()):
        if preferred in available:
            return preferred
    # Nothing preferred: take any plausible chat model rather than give up.
    for candidate in sorted(available):
        if not any(bad in candidate.lower() for bad in _NON_CHAT):
            return candidate
    return None


def _is_free(entry: dict, model_id: str) -> bool:
    """True when a catalogue entry costs nothing to call.

    Trusts the published pricing over the ``:free`` suffix, because the suffix
    is a naming convention and the pricing is the contract.
    """
    pricing = entry.get("pricing") or {}
    # Absent pricing must NOT read as free. Defaulting the lookup to "0" made
    # any model with no pricing block look free, which is the precise failure
    # this function exists to prevent. With no pricing, trust only the suffix.
    if "prompt" not in pricing and "completion" not in pricing:
        return model_id.endswith(":free")
    try:
        prompt = float(pricing.get("prompt") or 0)
        completion = float(pricing.get("completion") or 0)
    except (TypeError, ValueError):
        return model_id.endswith(":free")
    return prompt == 0 and completion == 0


SYSTEM_PROMPT = """\
You are a risk reviewer for an intraday Indian index-options trading system.

Your ONLY job is to decide whether a proposed trade should be held back. You
have no power to approve, improve or create trades, and no power to change the
direction, size, target or stop. The system has already decided those.

Return exactly one JSON object, no other text:

{
  "stance": "PROCEED" | "CAUTION" | "VETO",
  "confidence": <integer 0-100>,
  "summary": "<one sentence, max 160 characters>",
  "concerns": ["<specific, checkable concern>", ...],
  "supports": ["<specific supporting fact>", ...]
}

Rules:
- VETO only when the trade is structurally unsound: it cannot profit after
  costs, the account cannot survive the loss, or it fights a strongly aligned
  market trend.
- CAUTION when it is takeable but something real is working against it.
- PROCEED when you have no substantive objection.
- Cite the specific numbers you are reasoning from. Never invent data that is
  not in the brief.
- Never predict direction or price. You are not forecasting; you are checking
  whether this trade makes sense given what is already known.
- Be brief. At most four concerns."""


def _format_brief(brief: TradeBrief) -> str:
    """Render the brief as compact text. Readable by a model and by a human
    reading the log, which matters when auditing why something was vetoed."""
    lines = [
        f"TRADE: {brief.direction} {brief.index} {brief.contract}",
        f"  entry ₹{brief.entry_premium:.2f} → target ₹{brief.target_premium:.2f} "
        f"/ stop ₹{brief.stop_premium:.2f}",
        f"  engine confluence score: {brief.score}/100",
    ]
    if brief.lots is not None:
        lines.append(
            f"  size {brief.lots} lot(s); one stop-out = ₹{brief.risk_amount:,.0f} "
            f"= {(brief.risk_pct_of_capital or 0) * 100:.1f}% of ₹"
            f"{brief.available_capital or 0:,.0f} capital"
        )
    if brief.net_rr is not None:
        lines.append(
            f"  net reward:risk after costs {brief.net_rr:.2f}:1; "
            f"breakeven ₹{brief.breakeven:.2f}; "
            f"cost drag {(brief.cost_drag or 0) * 100:.2f}% of position"
        )

    lines.append("")
    lines.append("MARKET CONTEXT:")
    lines.append(
        f"  regime {brief.regime}, bias {brief.regime_bias} "
        f"({brief.regime_conviction}% of inputs agree)"
    )
    if brief.vix is not None:
        lines.append(f"  India VIX {brief.vix} ({brief.vix_level})")
    if brief.breadth_pct is not None:
        lines.append(
            f"  breadth {brief.breadth_pct * 100:.0f}% of heavyweights above their "
            f"average{' — DIVERGING from the index' if brief.breadth_divergence else ''}"
        )
    lines.append(f"  global markets lean {brief.global_lean}")
    if brief.range_position is not None:
        lines.append(
            f"  price sits at {brief.range_position * 100:.0f}% of today's range"
        )
    if brief.minutes_to_square_off is not None:
        lines.append(f"  {brief.minutes_to_square_off} minutes to square-off")
    lines.append(
        f"  data is {'LIVE' if brief.data_is_live else 'ESTIMATED (not tradable)'}"
    )

    lines.append("")
    lines.append(
        f"MEASURED EDGE: {brief.graded_trades} graded trades"
        + (f", expectancy ₹{brief.measured_expectancy:.0f}/lot"
           if brief.measured_expectancy is not None else " — edge not yet measured")
    )

    if brief.readings:
        lines.append("")
        lines.append("INDICATORS:")
        for reading in brief.readings:
            lines.append(
                f"  [{reading.get('state')}] {reading.get('label')}: "
                f"{reading.get('value')}"
            )
    if brief.warnings:
        lines.append("")
        lines.append("ENGINE WARNINGS:")
        lines.extend(f"  - {w}" for w in brief.warnings[:6])
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a reply.

    Small models wrap JSON in prose or fences however firmly they are told not
    to, so this is forgiving about the surroundings and strict about the
    content.
    """
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    for index, char in enumerate(cleaned[start:], start=start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start:index + 1])
                except json.JSONDecodeError:
                    return None
    return None



def chat_completion(agent, system: str, user: str, *,
                    max_tokens: int = 1500) -> str:
    """One chat call, with every provider quirk handled in one place.

    Shared by the risk reviewer and the pre-market brief so a fix lands once.
    What it handles:

    * ``reasoning_effort="low"`` -- Groq's gpt-oss family spends completion
      tokens on hidden reasoning BEFORE answering. On a long prompt it spent
      1598 of 1600 and returned an empty string with ``finish_reason=length``,
      which looks like a parse failure rather than a truncation. Capping the
      effort fixes it and cuts token spend about four-fold.
    * Providers that reject the parameter -- retried once without it.
    * A rejected model id -- re-discovered and retried.
    * Overload (429/503) -- retried once.
    * Empty content with a populated ``reasoning`` field -- read from there.
    """
    import requests

    headers = {"Content-Type": "application/json",
               **getattr(agent, "extra_headers", {})}
    if agent.api_key:
        headers["Authorization"] = f"Bearer {agent.api_key}"

    payload = {
        "model": agent.model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "reasoning_effort": "low",
    }

    def post(body: dict):
        return requests.post(f"{agent.base_url}/chat/completions",
                             headers=headers, json=body, timeout=agent.timeout)

    response = post(payload)

    # Some providers 400 on an unknown parameter rather than ignoring it.
    if response.status_code == 400 and "reasoning_effort" in response.text:
        payload.pop("reasoning_effort", None)
        response = post(payload)

    if response.status_code in (400, 404):
        replacement = (_suggested_model(response.text)
                       or discover_model(agent.base_url, agent.api_key, agent.provider))
        if replacement and replacement != agent.model:
            agent.model = replacement
            payload["model"] = replacement
            response = post(payload)

    if response.status_code in (429, 503):
        time.sleep(1.5)
        response = post(payload)
        if response.status_code in (429, 503):
            for alternative in MODEL_PREFERENCES.get(agent.provider, ()):
                if alternative == agent.model:
                    continue
                payload["model"] = alternative
                response = post(payload)
                if response.ok:
                    agent.model = alternative
                    break

    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:280]}")

    choice = response.json()["choices"][0]
    message = choice.get("message", {})
    content = (message.get("content") or "").strip()
    if not content:
        content = (message.get("reasoning") or "").strip()
    if not content:
        raise RuntimeError(
            f"Empty reply (finish_reason={choice.get('finish_reason')}). "
            f"If 'length', the budget went to reasoning before any answer."
        )
    return content


class LLMAgent:
    """Risk review via any OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        provider: str = "ollama",
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 12.0,
    ) -> None:
        preset = PRESETS.get(provider, {})
        self.provider = provider
        self.name = f"llm:{provider}"
        self.base_url = (base_url or preset.get("base_url") or "").rstrip("/")
        self.model = model or preset.get("model") or "unknown"
        self.api_key = api_key or resolve_api_key(provider)
        self.timeout = timeout
        self.needs_key = bool(preset.get("needs_key", True))
        self.extra_headers = dict(preset.get("headers") or {})

    def available(self) -> tuple[bool, str]:
        """Whether this agent can run, and why not if it cannot."""
        if not self.base_url:
            return False, "No base URL configured."
        if self.needs_key and not self.api_key:
            return False, f"{self.provider} needs an API key (AGENT_API_KEY)."
        return True, ""

    @timed
    def review(self, brief: TradeBrief) -> AgentVerdict:
        verdict = AgentVerdict(provider=self.name, model=self.model)

        ok, why = self.available()
        if not ok:
            verdict.stance = CAUTION
            verdict.degraded = True
            verdict.error = why
            verdict.summary = f"Agent unavailable: {why}"
            verdict.concerns = [
                "The LLM reviewer did not run, so this trade has had only the "
                "rule checks applied."
            ]
            return verdict

        try:
            import requests
        except ImportError:  # pragma: no cover
            verdict.stance = CAUTION
            verdict.degraded = True
            verdict.error = "requests not installed"
            return verdict

        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _format_brief(brief)},
            ],
            # Zero so the same brief tends to produce the same verdict: a risk
            # check that changes its mind at random cannot be tested.
            "temperature": 0,
            # Generous because reasoning models (Groq's gpt-oss family) spend
            # completion tokens on hidden reasoning BEFORE emitting content. At
            # 400 the whole budget went to reasoning and the reply came back
            # empty, which looked like a parse failure rather than a truncation.
            "max_tokens": 1500,
        }

        def call(model_name: str):
            payload["model"] = model_name
            return requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers, json=payload, timeout=self.timeout,
            )

        try:
            response = call(self.model)

            # 404/400 here usually means the model id is wrong, not that the
            # service is down. Ask what exists and retry once: hosted providers
            # retire model names regularly and a stale default should not take
            # the reviewer offline.
            if response.status_code in (400, 404):
                # Providers often name the successor in the error body
                # ("no longer available ... please use models/X"). Believe them
                # before falling back to listing everything.
                found = _suggested_model(response.text) or discover_model(
                    self.base_url, self.api_key, self.provider)
                if found and found != self.model:
                    logger_note = f"model {self.model!r} rejected; using {found!r}"
                    self.model = found
                    verdict.model = found
                    response = call(found)
                    verdict.supports.append(logger_note)

            # 429/503 are load, not misconfiguration. Free tiers hit them
            # routinely, so one short retry is worth it -- but only one: a
            # reviewer that stalls the refresh loop is worse than one that
            # abstains.
            if response.status_code in (429, 503):
                time.sleep(1.5)
                response = call(self.model)

                # Still saturated: try the next preferred model rather than
                # abstaining. Free tiers overload per-model, not per-account.
                if response.status_code in (429, 503):
                    for alternative in MODEL_PREFERENCES.get(self.provider, ()):
                        if alternative == self.model:
                            continue
                        response = call(alternative)
                        if response.ok:
                            verdict.supports.append(
                                f"{self.model!r} was overloaded; used "
                                f"{alternative!r} instead"
                            )
                            self.model = alternative
                            verdict.model = alternative
                            break

            if not response.ok:
                # The body explains the failure; status alone does not.
                raise RuntimeError(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
            body = response.json()
            choice = body["choices"][0]
            message = choice.get("message", {})
            content = (message.get("content") or "").strip()
            if not content:
                # Reasoning models put their chain of thought in a separate
                # field; if the answer is empty the JSON may still be in there.
                content = (message.get("reasoning") or "").strip()
            if not content:
                raise RuntimeError(
                    f"Empty reply (finish_reason={choice.get('finish_reason')}). "
                    f"If this says 'length', the token budget was spent on "
                    f"reasoning before any answer was produced."
                )
        except Exception as exc:  # noqa: BLE001 - every failure degrades the same way
            verdict.stance = CAUTION
            verdict.degraded = True
            verdict.error = f"{type(exc).__name__}: {exc}"
            verdict.summary = "LLM reviewer unreachable — rule checks only."
            verdict.concerns = [f"Agent call failed: {exc}"]
            return verdict

        parsed = _extract_json(content)
        if not parsed:
            verdict.stance = CAUTION
            verdict.degraded = True
            verdict.error = "Reply was not valid JSON"
            verdict.summary = "LLM reply could not be parsed — treated as caution."
            verdict.concerns = [f"Unparseable reply: {content[:180]}"]
            return verdict

        verdict.stance = parsed.get("stance", CAUTION)
        verdict.confidence = parsed.get("confidence", 0)
        verdict.summary = str(parsed.get("summary", ""))[:200]
        verdict.concerns = [str(c)[:300] for c in (parsed.get("concerns") or [])][:6]
        verdict.supports = [str(s)[:300] for s in (parsed.get("supports") or [])][:6]
        return verdict
