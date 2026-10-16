"""The pre-market brief: what the news says, read by a model, once a day.

How it spends tokens
--------------------
Deliberately frugally, because this is the one place an LLM earns its cost here
and it is also the easiest place to waste it:

* It only runs inside the market window (see
  :mod:`intraday_advisor.market_calendar`). Outside it, the call is not made.
* It is cached per **(market, trading day)**. The brief is built once and
  re-served for the rest of the session. A refresh loop asking the same
  question every ten seconds would spend a free tier before the bell.
* Headlines are de-duplicated first, so the prompt carries ~30 distinct
  stories rather than 90 copies of nine.

What it is honest about
-----------------------
News is weak evidence about intraday direction. Everything in the brief is
public and already priced by the time you read it; the move usually happens on
the gap. The model is therefore instructed to describe **what the news implies
and how confident that is**, to use UNCLEAR freely, and to name what would
change the read. A brief that says "markets will rise today" is worse than
useless, and the prompt forbids it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

from intraday_advisor.market_calendar import get_market, session_state

logger = logging.getLogger("premarket")

UP = "LEAN_UP"
DOWN = "LEAN_DOWN"
UNSTABLE = "UNSTABLE"
UNCLEAR = "UNCLEAR"

SYSTEM_PROMPT = """\
You are a pre-market analyst for an intraday index-options trader. You are given
today's market news headlines before the session opens.

Produce ONE JSON object, nothing else:

{
  "lean": "LEAN_UP" | "LEAN_DOWN" | "UNSTABLE" | "UNCLEAR",
  "confidence": <integer 0-100>,
  "headline": "<one sentence, max 140 chars, what today looks like>",
  "drivers": [
    {"what": "<the specific news item>",
     "effect": "up" | "down" | "volatile",
     "why": "<one line on the mechanism>"}
  ],
  "watch": ["<a specific level, event or time to watch today>"],
  "caveats": ["<what would make this read wrong>"]
}

Rules you must follow:
- You are NOT predicting. You are describing what the news implies and how
  strongly. Use "UNCLEAR" whenever the news genuinely does not lean, which is
  most days. A confident answer on ambiguous news is a failure, not a success.
- "UNSTABLE" means expect large two-way moves without a clear direction - use it
  when the news is heavy but conflicting.
- Cite the SPECIFIC headline behind each driver. Never invent news that is not
  in the list.
- Remember everything in these headlines is already public and largely priced
  in; the move usually happens on the opening gap. Say so if the news is stale
  or already reflected.
- At most 4 drivers, 3 watch items, 2 caveats. Be concrete, not generic.
- Never give a target, a price, or a buy/sell instruction."""


@dataclass
class PremarketBrief:
    market: str
    market_name: str
    session_date: str
    built_at: str
    lean: str = UNCLEAR
    confidence: int = 0
    headline: str = ""
    drivers: list[dict] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    headlines_used: int = 0
    news_coverage: str = ""
    top_headlines: list[dict] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    degraded: bool = False
    error: str | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# (market, session_date) -> brief. Built once per trading day.
_CACHE: dict[tuple[str, str], PremarketBrief] = {}


def _extract_json(text: str) -> dict | None:
    from intraday_advisor.agents.llm import _extract_json as extract

    return extract(text)


def build_brief(
    market_key: str = "india",
    *,
    force: bool = False,
    now: datetime | None = None,
    news: dict | None = None,
) -> PremarketBrief:
    """Build (or re-serve) today's brief for one market.

    Returns a brief with ``note`` explaining itself in every path: outside the
    window, without an LLM configured, or on failure. A panel that silently
    shows nothing is indistinguishable from one that is broken.
    """
    market = get_market(market_key)
    state = session_state(market.key, now)
    session_date = state.local_time[:10]
    cache_key = (market.key, session_date)

    if not force and cache_key in _CACHE:
        return _CACHE[cache_key]

    # --- the token gate --------------------------------------------------- #
    if not state.llm_active and not force:
        brief = PremarketBrief(
            market=market.key, market_name=market.name, session_date=session_date,
            built_at=datetime.now().isoformat(timespec="seconds"),
            lean=UNCLEAR,
            note=(
                f"{state.llm_reason} The brief is built automatically "
                f"{market.llm_lead_minutes} minutes before the open."
            ),
        )
        return brief

    # --- gather --------------------------------------------------------- #
    from intraday_advisor.news import format_for_prompt, gather_news

    news = news or gather_news(market.key)
    brief = PremarketBrief(
        market=market.key, market_name=market.name, session_date=session_date,
        built_at=datetime.now().isoformat(timespec="seconds"),
        headlines_used=news.get("count", 0),
        news_coverage=news.get("coverage", ""),
        top_headlines=news.get("headlines", [])[:12],
    )
    if not news.get("headlines"):
        brief.note = "No headlines could be retrieved — news sources unreachable."
        brief.degraded = True
        _CACHE[cache_key] = brief
        return brief

    # --- read it ---------------------------------------------------------- #
    from intraday_advisor.agents import agent_status
    from intraday_advisor.agents.llm import LLMAgent

    status = agent_status()
    if not status.get("llm_ready"):
        brief.note = (
            "Headlines gathered, but no LLM is configured to read them. "
            "Set AGENT_PROVIDER (openrouter / groq / gemini / ollama) to get "
            "an interpretation. The headlines below are still worth a look."
        )
        brief.degraded = True
        _CACHE[cache_key] = brief
        return brief

    # "auto" and comma-lists name a CHAIN, not a provider. Building a single
    # LLMAgent from the literal string "auto" finds no preset, leaving
    # base_url empty -- which surfaced as
    # "MissingSchema: Invalid URL '/chat/completions'".
    chain_names = status.get("chain") or [status["provider"]]
    if len(chain_names) > 1:
        from intraday_advisor.agents.llm import ChainAgent

        agent = ChainAgent(chain_names, timeout=45.0).agents[0]
        fallbacks = ChainAgent(chain_names, timeout=45.0).agents[1:]
    else:
        agent = LLMAgent(provider=chain_names[0], timeout=45.0)
        fallbacks = []
    user_prompt = (
        f"Market: {market.name} ({market.exchange}), opens "
        f"{market.open_time:%H:%M} {market.timezone}.\n"
        f"Local time now: {state.local_time}.\n"
        f"Session state: {state.state}.\n\n"
        f"TODAY'S HEADLINES (newest first):\n{format_for_prompt(news)}"
    )

    started = datetime.now()
    content = None
    failures: list[str] = []
    for candidate in [agent, *fallbacks]:
        try:
            content = _chat(candidate, SYSTEM_PROMPT, user_prompt)
            agent = candidate
            break
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{candidate.provider}: {exc}")

    if content is None:
        brief.degraded = True
        brief.error = " | ".join(failures)[:400]
        brief.note = (
            f"The news could not be analysed ({len(failures)} provider(s) "
            f"failed); the headlines are still listed below."
        )
        _CACHE[cache_key] = brief
        return brief

    brief.latency_ms = int((datetime.now() - started).total_seconds() * 1000)
    brief.provider = agent.name
    brief.model = agent.model

    parsed = _extract_json(content)
    if not parsed:
        brief.degraded = True
        brief.error = "Reply was not valid JSON"
        brief.note = "The model's reply could not be parsed; headlines listed below."
        _CACHE[cache_key] = brief
        return brief

    lean = str(parsed.get("lean", UNCLEAR)).strip().upper()
    brief.lean = lean if lean in (UP, DOWN, UNSTABLE, UNCLEAR) else UNCLEAR
    try:
        brief.confidence = max(0, min(100, int(parsed.get("confidence", 0))))
    except (TypeError, ValueError):
        brief.confidence = 0
    brief.headline = str(parsed.get("headline", ""))[:200]
    brief.drivers = [
        {"what": str(d.get("what", ""))[:200],
         "effect": str(d.get("effect", ""))[:20],
         "why": str(d.get("why", ""))[:200]}
        for d in (parsed.get("drivers") or [])[:4]
        if isinstance(d, dict)
    ]
    brief.watch = [str(w)[:200] for w in (parsed.get("watch") or [])[:3]]
    brief.caveats = [str(c)[:200] for c in (parsed.get("caveats") or [])[:2]]
    brief.note = (
        "News is weak evidence about intraday direction and is largely priced "
        "into the opening gap. Treat this as context for sizing and caution, "
        "never as a reason to take a trade the system has not signalled."
    )

    _CACHE[cache_key] = brief
    return brief


def _chat(agent, system: str, user: str) -> str:
    """Delegates to the shared caller, so provider quirks are fixed once."""
    from intraday_advisor.agents.llm import chat_completion

    # Larger budget than a risk verdict: the brief has to cite drivers.
    return chat_completion(agent, system, user, max_tokens=2500)

def brief_notification(brief: PremarketBrief) -> dict | None:
    """Turn a brief into a dashboard alert, or None if there is nothing to say."""
    if brief.lean == UNCLEAR and not brief.headline:
        return None

    severity = {
        UP: "good", DOWN: "warn", UNSTABLE: "danger", UNCLEAR: "info",
    }[brief.lean]
    title = {
        UP: "Pre-market: news leans UP",
        DOWN: "Pre-market: news leans DOWN",
        UNSTABLE: "Pre-market: expect instability",
        UNCLEAR: "Pre-market: no clear lean",
    }[brief.lean]

    return {
        "kind": "PREMARKET_BRIEF",
        "severity": severity,
        "title": f"{title} ({brief.confidence}%)",
        "message": brief.headline or "Headlines gathered; no clear direction.",
        "subject": brief.market,
        "ts": brief.built_at,
        "sticky": brief.lean in (DOWN, UNSTABLE),
    }


def clear_cache() -> None:
    """Drop cached briefs. Used by tests and by a manual refresh."""
    _CACHE.clear()
