"""Tests for expiry selection, alternative contracts, and tick-level pricing."""

from __future__ import annotations

import time

import pandas as pd
import pytest

from intraday_advisor.candidates import (
    MAX_ITM_STEPS,
    MAX_OTM_STEPS,
    _moneyness,
    build_candidates,
)


def chain(atm: float = 23000, step: float = 50, n: int = 9,
          expiry: str = "19OCT2026") -> dict:
    """A synthetic live-looking chain centred on ``atm``."""
    rows = []
    for i in range(-(n // 2), n // 2 + 1):
        strike = atm + i * step
        # Calls cheapen as strike rises, puts the reverse.
        rows.append({
            "strike": strike,
            "ce_ltp": max(2.0, 120 - i * 18),
            "pe_ltp": max(2.0, 120 + i * 18),
            "ce_oi": 100_000, "pe_oi": 100_000,
        })
    return {"df": pd.DataFrame(rows), "atm": atm, "expiry": expiry}


# ================================================================ moneyness ==

def test_moneyness_is_signed_in_the_options_own_direction():
    """A strike above spot is OTM for a call and ITM for a put; one raw number
    cannot describe both."""
    assert _moneyness("CE", 23100, 23000, 50) == ("OTM", 2)
    assert _moneyness("PE", 23100, 23000, 50) == ("ITM", -2)
    assert _moneyness("CE", 22900, 23000, 50) == ("ITM", -2)
    assert _moneyness("PE", 22900, 23000, 50) == ("OTM", 2)
    assert _moneyness("CE", 23000, 23000, 50) == ("ATM", 0)


def test_moneyness_handles_a_zero_step():
    assert _moneyness("CE", 100, 100, 0) == ("ATM", 0)


# =============================================================== candidates ==

def test_candidates_are_built_for_both_sides():
    out = build_candidates("NIFTY", chain(), direction="CALL",
                           available_capital=200_000, lot_size=65, step=50)
    assert out
    assert {c["option_type"] for c in out} == {"CE", "PE"}


def test_aligned_contracts_rank_above_counter_trend():
    out = build_candidates("NIFTY", chain(), direction="CALL",
                           available_capital=200_000, lot_size=65, step=50)
    best = out[0]
    assert best["aligned"] is True
    assert best["direction"] == "CALL"


def test_counter_trend_contracts_are_still_offered():
    """You may disagree with the engine; they are ranked last, not hidden."""
    out = build_candidates("NIFTY", chain(), direction="CALL",
                           available_capital=200_000, lot_size=65, step=50,
                           limit=20)
    assert any(not c["aligned"] for c in out)


def test_far_otm_and_deep_itm_are_excluded():
    """Beyond a few strikes an option is a lottery ticket or dead capital."""
    out = build_candidates("NIFTY", chain(n=25), direction="CALL",
                           available_capital=500_000, lot_size=65, step=50,
                           limit=50)
    for candidate in out:
        assert candidate["steps_from_atm"] <= MAX_OTM_STEPS
        assert candidate["steps_from_atm"] >= -MAX_ITM_STEPS


def test_synthetic_floor_prices_are_rejected():
    """0.5 is this project's synthetic placeholder, not a tradeable price.
    Offering it as a contract would invite a trade at a fictional premium."""
    frame = pd.DataFrame([
        {"strike": 23000, "ce_ltp": 0.5, "pe_ltp": 0.5, "ce_oi": 1, "pe_oi": 1},
        {"strike": 23050, "ce_ltp": 95.0, "pe_ltp": 0.5, "ce_oi": 1, "pe_oi": 1},
    ])
    out = build_candidates("NIFTY", {"df": frame, "atm": 23000},
                           direction="CALL", available_capital=200_000,
                           lot_size=65, step=50)
    assert len(out) == 1
    assert out[0]["premium"] == 95.0


def test_unaffordable_contracts_rank_below_affordable_ones():
    """A contract you cannot buy is information, not an option."""
    out = build_candidates("NIFTY", chain(), direction="CALL",
                           available_capital=7_000, lot_size=65, step=50,
                           limit=20)
    affordable = [i for i, c in enumerate(out) if c["affordable"]]
    unaffordable = [i for i, c in enumerate(out) if not c["affordable"]]
    if affordable and unaffordable:
        assert max(affordable) < min(unaffordable)


def test_each_candidate_is_priced_at_the_real_capital():
    out = build_candidates("NIFTY", chain(), direction="CALL",
                           available_capital=50_000, lot_size=65, step=50)
    for candidate in out:
        assert candidate["breakeven"] > candidate["premium"], "costs must be included"
        assert candidate["lot_size"] == 65
        if candidate["affordable"]:
            assert candidate["lots"] >= 1
            assert candidate["risk_pct"] > 0


def test_otm_contracts_buy_more_lots_than_atm():
    """The practical reason to offer alternatives at all."""
    out = build_candidates("NIFTY", chain(), direction="CALL",
                           available_capital=50_000, lot_size=65, step=50,
                           limit=20)
    calls = {c["steps_from_atm"]: c for c in out if c["option_type"] == "CE"}
    if 0 in calls and 2 in calls:
        assert calls[2]["lots"] >= calls[0]["lots"]
        assert calls[2]["capital_per_lot"] < calls[0]["capital_per_lot"]


def test_wait_signal_still_lists_contracts_unaligned():
    out = build_candidates("NIFTY", chain(), direction="WAIT",
                           available_capital=200_000, lot_size=65, step=50)
    assert out
    assert all(c["aligned"] is False for c in out)
    assert any("WAIT" in note for c in out for note in c["notes"])


def test_empty_chain_returns_nothing():
    assert build_candidates("NIFTY", {}, direction="CALL",
                            available_capital=100, lot_size=65) == []
    assert build_candidates("NIFTY", {"df": pd.DataFrame()}, direction="CALL",
                            available_capital=100, lot_size=65) == []


# ================================================================ live ticks ==

def test_live_price_returns_nothing_without_a_stream():
    from intraday_advisor import live_ticks

    assert live_ticks.live_price(None) == (None, None)
    assert live_ticks.live_price("12345")[0] is None


def test_stale_ticks_are_refused_rather_than_served(monkeypatch):
    """A frozen price is worse than no price: the caller would stop falling
    back to REST and keep valuing a position off a number that stopped
    updating."""
    from intraday_advisor import live_ticks

    class FrozenCache:
        def get(self, token):
            return {"ltp": 500.0, "ts": time.time() - 120, "oi": None}

    class FakeStreamer:
        cache = FrozenCache()

    monkeypatch.setitem(live_ticks._STATE, "streamer", FakeStreamer())
    price, age = live_ticks.live_price("1")

    assert price is None
    assert age is not None and age > live_ticks.STALE_AFTER_SECONDS


def test_fresh_ticks_are_served_with_their_age(monkeypatch):
    from intraday_advisor import live_ticks

    class FreshCache:
        def get(self, token):
            return {"ltp": 512.25, "ts": time.time() - 0.4, "oi": None}

    class FakeStreamer:
        cache = FreshCache()

    monkeypatch.setitem(live_ticks._STATE, "streamer", FakeStreamer())
    price, age = live_ticks.live_price("1")

    assert price == 512.25
    assert age is not None and age < 2


def test_zero_or_negative_tick_prices_are_refused(monkeypatch):
    from intraday_advisor import live_ticks

    class ZeroCache:
        def get(self, token):
            return {"ltp": 0.0, "ts": time.time(), "oi": None}

    class FakeStreamer:
        cache = ZeroCache()

    monkeypatch.setitem(live_ticks._STATE, "streamer", FakeStreamer())
    assert live_ticks.live_price("1") == (None, None)


def test_status_is_honest_when_nothing_is_running(monkeypatch):
    from intraday_advisor import live_ticks

    monkeypatch.setitem(live_ticks._STATE, "streamer", None)
    status = live_ticks.status()

    assert status["connected"] is False
    # The note must not promise millisecond updates; ~1/second is the ceiling.
    assert "tick" in status["note"].lower()
    assert "millisecond" not in status["note"].lower()


# ================================================= subscribe race (regression)

def test_tokens_queue_until_the_socket_is_open():
    """connect() returns before the handshake completes, so an immediate
    subscribe hit a closed socket and logged
    'Error occurred during subscribe: socket is already closed'."""
    from intraday_advisor.tick_stream import AngelTickStreamer

    streamer = AngelTickStreamer()
    sent: list[list[str]] = []
    streamer._send_subscribe = lambda tokens: sent.append(list(tokens))
    streamer._ws = object()          # a socket exists...
    streamer._open = False           # ...but is not open yet

    streamer.subscribe(["111", "222"])
    assert sent == [], "nothing should be sent before the socket opens"
    assert streamer._pending == {"111", "222"}

    streamer._on_open(None)
    assert sent and set(sent[0]) == {"111", "222"}
    assert streamer._pending == set()


def test_a_close_requeues_the_watch_list():
    from intraday_advisor.tick_stream import AngelTickStreamer

    streamer = AngelTickStreamer()
    streamer._send_subscribe = lambda tokens: None
    streamer._ws = object()
    streamer._open = True
    streamer.subscribe(["333"])

    streamer._on_close(None)
    assert "333" in streamer._pending, "the subscription must survive a reconnect"
