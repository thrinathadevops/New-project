"""Tests for the market-regime read and the agent layer.

The agent tests matter most. An agent that can approve or upgrade a trade
becomes the system's real strategy — unmeasured, unbacktested and
non-deterministic. The clamp that prevents that is checked from several angles,
including the case where the agent returns outright garbage.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.agents import build_agents, review_signal
from intraday_advisor.agents.base import (
    CAUTION,
    PROCEED,
    VETO,
    AgentVerdict,
    TradeBrief,
    brief_from_snapshot,
    combine,
)
from intraday_advisor.agents.llm import LLMAgent, _extract_json
from intraday_advisor.agents.rules import RuleAgent
from intraday_advisor.regime import (
    alignment_score,
    build_regime,
    classify_breadth,
    classify_volatility,
    key_levels,
    read_timeframe,
    summarise_global,
)


# =================================================================== regime ==

def trending_frame(direction: int, bars: int = 120, start: float = 100.0) -> pd.DataFrame:
    """A cleanly trending series, so the EMA read is unambiguous."""
    drift = 0.002 * direction
    closes = start * np.exp(np.cumsum(np.full(bars, drift)))
    index = pd.date_range("2026-01-01 09:15", periods=bars, freq="5min")
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.001, "Low": closes * 0.999,
         "Close": closes, "Volume": 1000},
        index=index,
    )


def flat_frame(bars: int = 120) -> pd.DataFrame:
    closes = np.full(bars, 100.0)
    index = pd.date_range("2026-01-01 09:15", periods=bars, freq="5min")
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes,
         "Volume": 1000},
        index=index,
    )


def test_read_timeframe_detects_direction():
    assert read_timeframe(trending_frame(1), "5m", "5 minutes").trend == "up"
    assert read_timeframe(trending_frame(-1), "5m", "5 minutes").trend == "down"
    assert read_timeframe(flat_frame(), "5m", "5 minutes").trend == "flat"


def test_read_timeframe_needs_enough_bars():
    assert read_timeframe(trending_frame(1, bars=5), "5m", "5 minutes") is None
    assert read_timeframe(pd.DataFrame(), "5m", "5 minutes") is None


def test_alignment_weights_slower_timeframes_more():
    """A daily downtrend is the context a 5-minute uptrend moves inside, not
    the other way round. Unweighted, noise outvotes structure."""
    fast_up = read_timeframe(trending_frame(1), "5m", "5 minutes")
    slow_down = read_timeframe(trending_frame(-1), "1d", "Daily")

    score = alignment_score([fast_up, slow_down])
    assert score < 0           # the daily wins


def test_alignment_all_agreeing_is_near_one():
    reads = [
        read_timeframe(trending_frame(1), interval, label)
        for interval, label in (("5m", "5m"), ("15m", "15m"), ("1h", "1h"), ("1d", "1d"))
    ]
    assert alignment_score(reads) > 0.9
    assert alignment_score([]) == 0.0


@pytest.mark.parametrize("vix,level", [
    (10.0, "calm"), (15.0, "normal"), (20.0, "elevated"), (30.0, "high"),
])
def test_volatility_bands(vix, level):
    assert classify_volatility(vix, None)["level"] == level


def test_rising_vix_is_called_out():
    result = classify_volatility(20.0, 12.0)
    assert "RISING" in result["meaning"]
    assert "falling markets" in result["meaning"]


def test_missing_vix_degrades_safely():
    result = classify_volatility(None, None)
    assert result["level"] == "unknown"
    assert "conservatively" in result["implication"]


def test_breadth_states():
    assert classify_breadth(10, 12)["state"] == "strong"
    assert classify_breadth(6, 12)["state"] == "mixed"
    assert classify_breadth(1, 12)["state"] == "very weak"
    assert classify_breadth(0, 0)["state"] == "unknown"


def test_global_lean_is_weighted():
    cues = [
        {"name": "S&P 500", "change_pct": -1.5, "weight": 1.0},
        {"name": "Nikkei", "change_pct": 0.2, "weight": 0.6},
    ]
    assert summarise_global(cues)["lean"] == "DOWN"
    assert summarise_global([])["lean"] == "unknown"


def test_key_levels_reports_range_position():
    daily = pd.DataFrame({
        "Open": [100, 101], "High": [102, 103], "Low": [99, 100], "Close": [101, 102],
    }, index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    intraday = trending_frame(1, bars=40)

    levels = key_levels(daily, intraday)
    assert levels["prev_high"] == 102
    assert levels["prev_low"] == 99
    assert 0 <= levels["range_position"] <= 1
    assert "range" in levels["range_note"]


def test_aligned_uptrend_is_classified_as_trending_up():
    frames = {tf: trending_frame(1) for tf in ("5m", "15m", "1h", "1d")}
    regime = build_regime("NIFTY", frames=frames, vix=13.5, vix_change_pct=-3,
                          breadth_above=10, breadth_total=12)

    assert regime.regime == "TRENDING_UP"
    assert regime.bias == "UP"
    assert regime.conviction > 60
    assert regime.invalidation, "a read with no falsifier is a horoscope"


def test_conflicting_timeframes_are_a_range_not_a_trend():
    frames = {"5m": trending_frame(1), "15m": trending_frame(1),
              "1h": trending_frame(-1), "1d": trending_frame(-1)}
    regime = build_regime("NIFTY", frames=frames)

    assert regime.regime == "RANGE"
    assert any("range, not a trend" in w for w in regime.warnings)


def test_high_vix_overrides_trend_into_volatile():
    frames = {tf: trending_frame(1) for tf in ("5m", "15m", "1h", "1d")}
    regime = build_regime("NIFTY", frames=frames, vix=32.0, vix_change_pct=20)

    assert regime.regime == "VOLATILE"


def test_breadth_divergence_cuts_conviction():
    frames = {tf: trending_frame(1) for tf in ("5m", "15m", "1h", "1d")}
    broad = build_regime("NIFTY", frames=frames, breadth_above=10, breadth_total=12)
    narrow = build_regime("NIFTY", frames=frames, breadth_above=3, breadth_total=12)

    assert narrow.conviction < broad.conviction
    assert narrow.breadth["divergence"] is True
    assert any("Breadth divergence" in w for w in narrow.warnings)


def test_global_cues_against_the_local_read_cut_conviction():
    frames = {tf: trending_frame(1) for tf in ("5m", "15m", "1h", "1d")}
    agreeing = build_regime("NIFTY", frames=frames, global_cues=[
        {"name": "S&P 500", "change_pct": 1.2, "weight": 1.0}])
    opposing = build_regime("NIFTY", frames=frames, global_cues=[
        {"name": "S&P 500", "change_pct": -1.2, "weight": 1.0}])

    assert opposing.conviction < agreeing.conviction
    assert any("against" in w for w in opposing.warnings)


def test_no_data_is_unknown_not_a_guess():
    regime = build_regime("NIFTY", frames={})
    assert regime.regime == "UNKNOWN"
    assert regime.bias == "NEUTRAL"
    assert regime.conviction == 0


# =================================================================== agents ==

def a_brief(**overrides) -> TradeBrief:
    base = dict(
        index="NIFTY", direction="CALL", score=72, contract="23000 CE",
        entry_premium=100.0, target_premium=125.0, stop_premium=80.0,
        lots=2, risk_amount=3_100.0, risk_pct_of_capital=0.03, net_rr=1.3,
        breakeven=101.3, cost_drag=0.013, affordable=True,
        available_capital=100_000.0, regime="TRENDING_UP", regime_bias="UP",
        regime_conviction=70, vix=14.0, vix_level="normal", breadth_pct=0.7,
        global_lean="UP", minutes_to_square_off=180, market_open=True,
        data_is_live=True, graded_trades=120, measured_expectancy=250.0,
    )
    base.update(overrides)
    return TradeBrief(**base)


def test_clean_trade_passes():
    verdict = RuleAgent().review(a_brief())
    assert verdict.stance == PROCEED
    assert verdict.concerns == []


@pytest.mark.parametrize("overrides,reason", [
    ({"breakeven": 130.0}, "cannot profit after costs"),
    ({"net_rr": 0.8}, "risk exceeds reward"),
    ({"cost_drag": 0.09}, "costs eat the position"),
    ({"risk_pct_of_capital": 0.40}, "account cannot survive it"),
    ({"affordable": False}, "cannot afford one lot"),
    ({"regime_bias": "DOWN", "regime_conviction": 70}, "fights an aligned trend"),
    ({"data_is_live": False}, "estimated data is not tradable"),
    ({"minutes_to_square_off": 0}, "no time left"),
    ({"measured_expectancy": -180.0}, "measured to lose money"),
])
def test_structural_problems_are_vetoed(overrides, reason):
    verdict = RuleAgent().review(a_brief(**overrides))
    assert verdict.stance == VETO, f"should veto: {reason}"
    assert verdict.concerns


@pytest.mark.parametrize("overrides", [
    {"risk_pct_of_capital": 0.12},
    {"minutes_to_square_off": 10},
    {"graded_trades": 3, "measured_expectancy": None},
    {"regime": "RANGE"},
    {"vix_level": "high", "vix": 30.0},
    {"breadth_divergence": True},
    {"global_lean": "DOWN"},
    {"score": 30},
])
def test_contextual_problems_caution_rather_than_block(overrides):
    verdict = RuleAgent().review(a_brief(**overrides))
    assert verdict.stance == CAUTION
    assert verdict.concerns


def test_wait_signals_are_not_reviewed():
    verdict = RuleAgent().review(a_brief(direction="WAIT"))
    assert verdict.stance == PROCEED
    assert "nothing to check" in verdict.summary.lower()


def test_rule_agent_is_deterministic():
    """A risk check that changes its mind at random cannot be trusted."""
    brief = a_brief(risk_pct_of_capital=0.12)
    first = RuleAgent().review(brief)
    second = RuleAgent().review(brief)
    assert (first.stance, first.concerns) == (second.stance, second.concerns)


# ------------------------------------------------------- authority clamping --

def test_an_agent_cannot_approve_its_way_past_the_schema():
    """Any stance outside the three allowed values becomes CAUTION, never
    PROCEED — a broken agent must not read as approval."""
    verdict = AgentVerdict(stance="STRONG BUY", confidence=99).clamped(tradable=True)
    assert verdict.stance == CAUTION
    assert any("unrecognised stance" in c for c in verdict.concerns)


def test_veto_on_a_non_trade_collapses_to_proceed():
    verdict = AgentVerdict(stance=VETO).clamped(tradable=False)
    assert verdict.stance == PROCEED


def test_confidence_is_clamped_to_range():
    assert AgentVerdict(stance=PROCEED, confidence=500).clamped(tradable=True).confidence == 100
    assert AgentVerdict(stance=PROCEED, confidence=-5).clamped(tradable=True).confidence == 0


def test_combine_takes_the_most_cautious_stance():
    """One objection is enough. Averaging would let a confident-but-wrong model
    talk over a correct objection."""
    merged = combine([
        AgentVerdict(stance=PROCEED, provider="a", confidence=90),
        AgentVerdict(stance=VETO, provider="b", summary="unsafe", confidence=40),
    ])
    assert merged.stance == VETO
    assert merged.summary == "unsafe"


def test_combine_with_nothing_is_proceed():
    assert combine([]).stance == PROCEED


# ------------------------------------------------------------- LLM plumbing --

def test_llm_without_a_key_degrades_to_caution_not_proceed(monkeypatch):
    """A silently-succeeding safety layer is decoration.

    Env is cleared explicitly: once real keys live in .env this test would
    otherwise pass or fail depending on the developer's machine, which is no
    test at all.
    """
    # The loader must be stubbed as well as the vars cleared: resolve_api_key
    # calls load_env(), which would read the real .env straight back in.
    monkeypatch.setattr("intraday_advisor.envfile.load_env", lambda *a, **k: False)
    for name in ("AGENT_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    agent = LLMAgent(provider="groq")
    verdict = agent.review(a_brief())

    assert verdict.stance == CAUTION
    assert verdict.degraded is True
    assert "key" in (verdict.error or "").lower()


def test_api_key_resolves_per_provider(monkeypatch):
    """Several providers can be configured at once, so each reads its own key
    and AGENT_API_KEY overrides whichever is active."""
    from intraday_advisor.agents.llm import resolve_api_key

    monkeypatch.setattr("intraday_advisor.envfile.load_env", lambda *a, **k: False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")

    assert resolve_api_key("groq") == "groq-key"
    assert resolve_api_key("gemini") == "gemini-key"
    assert resolve_api_key("nosuch") == ""

    monkeypatch.setenv("AGENT_API_KEY", "override")
    assert resolve_api_key("groq") == "override"


def test_llm_unreachable_endpoint_degrades_to_caution():
    agent = LLMAgent(provider="ollama", base_url="http://127.0.0.1:1", timeout=1.0)
    verdict = agent.review(a_brief())

    assert verdict.stance == CAUTION
    assert verdict.degraded is True


def test_extract_json_handles_fences_and_prose():
    assert _extract_json('{"stance":"VETO"}')["stance"] == "VETO"
    assert _extract_json('```json\n{"stance":"PROCEED"}\n```')["stance"] == "PROCEED"
    assert _extract_json('Sure! {"stance":"CAUTION"} hope that helps')["stance"] == "CAUTION"
    assert _extract_json("no json here") is None
    assert _extract_json("") is None


def test_extract_json_handles_nested_objects():
    parsed = _extract_json('prose {"stance":"VETO","meta":{"a":1}} trailing')
    assert parsed["meta"]["a"] == 1


# ------------------------------------------------------------ orchestration --

def test_build_agents_defaults_to_rules_only():
    agents = build_agents("rules")
    assert len(agents) == 1
    assert agents[0].name == "rules"


def test_build_agents_adds_llm_but_keeps_rules():
    """The rule checklist always runs: it is free, cannot fail, and catches the
    mistakes that actually drain accounts."""
    agents = build_agents("ollama")
    assert [a.name for a in agents] == ["rules", "llm:ollama"]


def test_agents_can_be_disabled():
    assert build_agents("none") == []


def test_review_signal_never_raises_on_a_broken_agent():
    class Exploding:
        name = "boom"

        def review(self, brief):
            raise RuntimeError("kaboom")

    verdict = review_signal(
        {"direction": "CALL", "plan": {}}, {}, None, agents=[Exploding()]
    )
    assert verdict["stance"] == CAUTION
    assert "kaboom" in verdict["summary"]


def test_review_with_agents_disabled_proceeds():
    verdict = review_signal({"direction": "CALL"}, {}, None, agents=[])
    assert verdict["stance"] == PROCEED


def test_brief_from_snapshot_pulls_the_right_fields():
    signal = {
        "index": "NIFTY", "direction": "CALL", "score": 71, "strike": 23000,
        "option_type": "CE", "entry_premium": 100.0, "target_premium": 125.0,
        "stop_premium": 80.0,
        "plan": {"lots": 2, "risk_amount": 3100.0, "risk_pct_of_capital": 0.031,
                 "net_rr": 1.25, "breakeven": 101.3, "cost_drag": 0.013,
                 "affordable": True},
        "readings": [{"label": "EMA", "state": "bullish"}],
        "warnings": ["something"],
    }
    snapshot = {
        "mode": "LIVE", "market_open": True, "minutes_to_square_off": 120,
        "settings": {"available_capital": 100000},
        "ledger": {"performance": {"trades": 50, "expectancy": 180.0}},
    }
    regime = {"regime": "TRENDING_UP", "bias": "UP", "conviction": 65,
              "volatility": {"vix": 14.0, "level": "normal"},
              "breadth": {"pct": 0.7, "divergence": False},
              "global_cues": {"lean": "UP"}, "levels": {"range_position": 0.6}}

    brief = brief_from_snapshot(signal, snapshot, regime)
    assert brief.tradable is True
    assert brief.contract == "23000 CE"
    assert brief.regime_bias == "UP"
    assert brief.vix == 14.0
    assert brief.graded_trades == 50
    assert brief.measured_expectancy == 180.0
    assert brief.data_is_live is True


# ----------------------------------------------------------- OpenRouter ----

def test_openrouter_preset_is_configured_for_free_use():
    from intraday_advisor.agents.llm import MODEL_PREFERENCES, PRESETS

    preset = PRESETS["openrouter"]
    assert preset["base_url"] == "https://openrouter.ai/api/v1"
    # OpenRouter asks callers to identify themselves.
    assert "HTTP-Referer" in preset["headers"]
    # The default is the auto-router, which survives model retirement.
    assert preset["model"] == "openrouter/free"
    assert MODEL_PREFERENCES["openrouter"][0] == "openrouter/free"


def test_free_detection_trusts_pricing_over_the_name():
    """The ':free' suffix is a naming convention; pricing is the contract.
    Billing a user because a suffix was missing is not acceptable."""
    from intraday_advisor.agents.llm import _is_free

    assert _is_free({"pricing": {"prompt": "0", "completion": "0"}}, "x/y") is True
    assert _is_free({"pricing": {"prompt": "0.0000012", "completion": "0"}},
                    "x/y:free") is False
    # No pricing at all: fall back to the suffix.
    assert _is_free({}, "x/y:free") is True
    assert _is_free({}, "x/y") is False


def test_discovery_skips_free_but_unsuitable_models(monkeypatch):
    """Free does not mean usable: audio, classifier and code-only models must
    not be chosen for a risk verdict."""
    import intraday_advisor.agents.llm as llm_module

    catalogue = {"data": [
        {"id": "google/lyria-3-pro-preview", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "nvidia/nemotron-3.5-content-safety:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "cohere/north-mini-code:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "some/good-model:free", "pricing": {"prompt": "0", "completion": "0"}},
    ]}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return catalogue

    class FakeRequests:
        @staticmethod
        def get(*args, **kwargs):
            return FakeResponse()

    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)
    chosen = llm_module.discover_model("http://x", "", "openrouter")
    assert chosen == "some/good-model:free"


def test_discovery_excludes_paid_models_on_openrouter(monkeypatch):
    """Silently falling back to a billed model is not an acceptable failure
    mode for a checker that runs all day."""
    import intraday_advisor.agents.llm as llm_module

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [
                {"id": "expensive/model", "pricing": {"prompt": "0.00003", "completion": "0.00006"}},
            ]}

    class FakeRequests:
        @staticmethod
        def get(*args, **kwargs):
            return FakeResponse()

    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)
    assert llm_module.discover_model("http://x", "", "openrouter") is None


# --------------------------------------------------- provider fallback ----

def test_available_providers_requires_a_key_or_a_live_endpoint(monkeypatch):
    """A keyless local provider passes the key check trivially; claiming it is
    available with nothing listening makes the status panel lie."""
    import intraday_advisor.agents.llm as llm_module

    monkeypatch.setattr(llm_module, "_reachable", lambda *a, **k: False)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)

    found = llm_module.available_providers()
    assert "groq" in found
    assert "ollama" not in found, "unreachable local provider must not be listed"


def test_chain_falls_through_to_the_next_provider():
    """One provider failing must not drop the whole LLM layer. Free tiers
    rate-limit constantly, so depending on exactly one is a design flaw."""
    from intraday_advisor.agents.llm import ChainAgent

    chain = ChainAgent([], timeout=1.0)

    class Dead:
        provider = "dead"
        name = "llm:dead"

        def review(self, brief):
            verdict = AgentVerdict(provider="dead", stance=CAUTION, degraded=True)
            verdict.error = "boom"
            return verdict

    class Alive:
        provider = "alive"
        name = "llm:alive"

        def review(self, brief):
            return AgentVerdict(provider="alive", stance=VETO, summary="no",
                                confidence=80)

    chain.agents = [Dead(), Alive()]
    verdict = chain.review(a_brief())

    assert verdict.stance == VETO
    assert verdict.degraded is False
    assert any("after 1 failed" in s for s in verdict.supports)


def test_chain_remembers_which_provider_last_worked():
    """A dead provider should not be re-probed at the front of the queue on
    every single call."""
    from intraday_advisor.agents.llm import ChainAgent

    calls = []

    class Recorder:
        def __init__(self, name, ok):
            self.provider = name
            self.name = f"llm:{name}"
            self.ok = ok

        def review(self, brief):
            calls.append(self.provider)
            if self.ok:
                return AgentVerdict(provider=self.provider, stance=PROCEED)
            verdict = AgentVerdict(provider=self.provider, stance=CAUTION,
                                   degraded=True)
            verdict.error = "down"
            return verdict

    chain = ChainAgent([], timeout=1.0)
    chain.agents = [Recorder("dead", False), Recorder("alive", True)]

    chain.review(a_brief())
    assert calls == ["dead", "alive"]

    calls.clear()
    chain.review(a_brief())
    assert calls == ["alive"], "the known-good provider should be tried first"


def test_chain_with_every_provider_down_is_caution_not_proceed():
    from intraday_advisor.agents.llm import ChainAgent

    chain = ChainAgent([], timeout=1.0)

    class Dead:
        provider = "dead"
        name = "llm:dead"

        def review(self, brief):
            verdict = AgentVerdict(provider="dead", stance=CAUTION, degraded=True)
            verdict.error = "down"
            return verdict

    chain.agents = [Dead(), Dead()]
    verdict = chain.review(a_brief())

    assert verdict.stance == CAUTION
    assert verdict.degraded is True
    assert "failed" in verdict.summary


def test_chain_with_no_providers_says_so():
    from intraday_advisor.agents.llm import ChainAgent

    chain = ChainAgent([], timeout=1.0)
    ready, why = chain.available()
    assert ready is False
    assert "OPENROUTER_API_KEY" in why

    verdict = chain.review(a_brief())
    assert verdict.stance == CAUTION
    assert verdict.degraded is True


def test_auto_builds_a_chain_and_keeps_the_rules(monkeypatch):
    # ChainAgent resolves providers through the llm module, so patch it there.
    monkeypatch.setattr("intraday_advisor.agents.llm.available_providers",
                        lambda: ["groq", "gemini"])
    agents = build_agents("auto")

    assert agents[0].name == "rules"
    assert len(agents) == 2
    assert "groq" in agents[1].name and "gemini" in agents[1].name


def test_explicit_comma_separated_order_is_honoured():
    agents = build_agents("gemini,groq")
    assert agents[0].name == "rules"
    assert agents[1].providers == ["gemini", "groq"]


def test_single_provider_status_warns_about_the_single_point_of_failure(monkeypatch):
    # Patched where it is USED, not where it is defined: agents/__init__ binds
    # the name at import, so patching the llm module leaves that binding alone.
    monkeypatch.setattr("intraday_advisor.agents.available_providers",
                        lambda: ["groq", "gemini"])
    from intraday_advisor.agents import agent_status

    status = agent_status("groq")
    assert "auto" in (status.get("advice") or "")
    assert "gemini" in status["advice"]
