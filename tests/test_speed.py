"""The desk has to keep up with the market it is watching.

Measured on 2026-11-17: a snapshot took 17 to 87 seconds against a 15-second
refresh, so the chart, the prices and every stop alarm ran a minute behind.
Three causes, each pinned here so none can quietly return:

* the option chain was fetched eight times per snapshot;
* every indicator was recomputed on candles that had not changed;
* the indicator scorecard (about 73 seconds) ran inline every five minutes.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest


# ============================================================ one chain fetch ==

def test_the_chain_is_fetched_once_for_callers_seconds_apart(monkeypatch):
    from intraday_advisor import nse_option_chain as chain

    calls = []

    def fake(symbol, spot=0.0, expiry=None):
        calls.append((symbol, expiry))
        return {"df": pd.DataFrame({"strike": [25300.0]}), "atm": 25300.0}

    monkeypatch.setattr(chain, "_suggest_option_trade", fake)
    chain._CHAIN_CACHE.clear()
    for _ in range(8):                      # what one snapshot used to do
        chain.suggest_option_trade("NIFTY", spot=25300.0)
    assert len(calls) == 1


def test_a_different_expiry_or_index_is_a_different_chain(monkeypatch):
    from intraday_advisor import nse_option_chain as chain

    calls = []
    monkeypatch.setattr(chain, "_suggest_option_trade",
                        lambda s, spot=0.0, expiry=None: calls.append((s, expiry)) or {})
    chain._CHAIN_CACHE.clear()
    chain.suggest_option_trade("NIFTY", expiry="23NOV2026")
    chain.suggest_option_trade("NIFTY", expiry="01DEC2026")
    chain.suggest_option_trade("BANKNIFTY", expiry="23NOV2026")
    assert len(calls) == 3


def test_the_chain_is_refetched_once_it_is_stale(monkeypatch):
    from intraday_advisor import nse_option_chain as chain

    calls = []
    monkeypatch.setattr(chain, "_suggest_option_trade",
                        lambda s, spot=0.0, expiry=None: calls.append(s) or {})
    monkeypatch.setattr(chain, "_CHAIN_TTL", 0.0)
    chain._CHAIN_CACHE.clear()
    chain.suggest_option_trade("NIFTY")
    chain.suggest_option_trade("NIFTY")
    assert len(calls) == 2, "quotes must not be served stale"


def test_one_caller_cannot_change_what_the_next_one_sees(monkeypatch):
    from intraday_advisor import nse_option_chain as chain

    monkeypatch.setattr(chain, "_suggest_option_trade",
                        lambda s, spot=0.0, expiry=None: {"bias": "NEUTRAL"})
    chain._CHAIN_CACHE.clear()
    first = chain.suggest_option_trade("NIFTY")
    first["bias"] = "tampered"
    assert chain.suggest_option_trade("NIFTY")["bias"] == "NEUTRAL"


# ======================================================= unchanged candles ==

def _candles(n=260, seed=3):
    rng = np.random.default_rng(seed)
    close = 25_000 * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
    index = pd.date_range("2026-11-16 09:15", periods=n, freq="5min")
    return pd.DataFrame({"Open": close, "High": close * 1.0006,
                         "Low": close * 0.9994, "Close": close,
                         "Volume": 1000.0}, index=index)


def test_unchanged_candles_are_not_reanalysed(monkeypatch):
    from intraday_advisor import index_options

    calls = []
    real = index_options._analyse_underlying
    monkeypatch.setattr(index_options, "_analyse_underlying",
                        lambda df: calls.append(1) or real(df))
    index_options._UNDERLYING_CACHE.clear()
    frame = _candles()
    first = index_options.analyse_underlying(frame)
    second = index_options.analyse_underlying(frame.copy())
    assert len(calls) == 1
    assert second[1] == first[1]


def test_a_new_candle_is_analysed_afresh(monkeypatch):
    from intraday_advisor import index_options

    calls = []
    real = index_options._analyse_underlying
    monkeypatch.setattr(index_options, "_analyse_underlying",
                        lambda df: calls.append(1) or real(df))
    index_options._UNDERLYING_CACHE.clear()
    frame = _candles()
    index_options.analyse_underlying(frame.iloc[:-1])
    index_options.analyse_underlying(frame)
    assert len(calls) == 2


def test_a_cached_feature_row_cannot_be_mutated_by_a_caller():
    from intraday_advisor import index_options

    index_options._UNDERLYING_CACHE.clear()
    frame = _candles()
    _, row, _ = index_options.analyse_underlying(frame)
    row["Close"] = -1
    assert index_options.analyse_underlying(frame)[1].get("Close") != -1


# ============================================================== scorecard ==

def test_the_scorecard_never_blocks_a_snapshot(monkeypatch, tmp_path):
    """It took about 73 seconds and ran inline every five minutes, freezing
    the chart and every stop alarm for the duration."""
    import intraday_advisor.indicator_scorecard as cards
    from intraday_advisor.engine import Engine

    def slow(**kwargs):
        time.sleep(1.5)
        return {"trusted": [], "unproven": [], "sessions": 7, "note": "done"}

    monkeypatch.setattr(cards, "scorecard", slow)
    engine = Engine(available_capital=20_000, record=False,
                    db_path=str(tmp_path / "ledger.sqlite"))
    started = time.time()
    first = engine._scorecard()
    assert time.time() - started < 0.5, "must return at once"
    assert "being computed" in first["note"]

    deadline = time.time() + 6
    while engine._scorecard().get("note") != "done" and time.time() < deadline:
        time.sleep(0.1)
    assert engine._scorecard()["note"] == "done", "the result arrives later"


# ============================================================ polled spots ==

def test_spots_are_polled_when_the_tick_stream_is_off(monkeypatch):
    from intraday_advisor import index_ticks, nse_option_chain

    class Smart:
        calls = 0

        def getMarketData(self, mode, tokens):
            Smart.calls += 1
            return {"status": True, "data": {"fetched": [
                {"symbolToken": "99926000", "ltp": 25380.5},
                {"symbolToken": "99926009", "ltp": 61900.0}]}}

    monkeypatch.setattr(nse_option_chain, "_angel_session", lambda *a, **k: Smart())
    monkeypatch.setattr("intraday_advisor.engine.is_market_open", lambda *a, **k: True)
    monkeypatch.setattr(index_ticks, "_TOKENS",
                        {"NIFTY": "99926000", "BANKNIFTY": "99926009"})
    monkeypatch.setattr(index_ticks, "_POLL", {"at": 0.0, "prices": {}})

    prices = index_ticks.poll_spots(["NIFTY", "BANKNIFTY"])
    assert prices["NIFTY"]["price"] == 25380.5
    assert prices["BANKNIFTY"]["price"] == 61900.0

    index_ticks.poll_spots(["NIFTY", "BANKNIFTY"])      # inside the interval
    assert Smart.calls == 1, "rate-limited: one request, not one per tick"


def test_no_polling_outside_market_hours(monkeypatch):
    from intraday_advisor import index_ticks

    monkeypatch.setattr("intraday_advisor.engine.is_market_open", lambda *a, **k: False)
    monkeypatch.setattr(index_ticks, "_POLL", {"at": 0.0, "prices": {}})
    assert index_ticks.poll_spots(["NIFTY"]) == {}


def test_a_failing_quote_never_raises(monkeypatch):
    from intraday_advisor import index_ticks, nse_option_chain

    def broken(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(nse_option_chain, "_angel_session", broken)
    monkeypatch.setattr("intraday_advisor.engine.is_market_open", lambda *a, **k: True)
    monkeypatch.setattr(index_ticks, "_POLL", {"at": 0.0, "prices": {}})
    assert index_ticks.poll_spots(["NIFTY"]) == {}


def test_the_index_token_has_a_fixed_fallback(monkeypatch):
    from intraday_advisor import index_ticks, nse_option_chain

    def broken():
        raise RuntimeError("download hung")

    monkeypatch.setattr(nse_option_chain, "_load_instruments", broken)
    monkeypatch.setattr(index_ticks, "_TOKENS", {})
    assert index_ticks.index_token("NIFTY") == "99926000"
    assert index_ticks.index_token("BANKNIFTY") == "99926009"


# ======================================================= instrument master ==

def test_todays_instrument_file_is_used_instead_of_a_download(monkeypatch, tmp_path):
    import json

    from intraday_advisor import nse_option_chain as chain

    path = tmp_path / "instrument_master.json"
    rows = [{"token": str(i), "symbol": f"S{i}", "name": "NIFTY",
             "exch_seg": "NFO", "pad": "x" * 600} for i in range(2000)]
    path.write_text(json.dumps(rows), encoding="utf-8")

    def forbidden(*a, **k):
        raise AssertionError("must not download when today's file exists")

    monkeypatch.setattr(chain, "INSTRUMENT_FILE", str(path))
    monkeypatch.setattr(chain, "_INSTRUMENT_CACHE", None)
    monkeypatch.setattr(chain.requests, "get", forbidden)
    assert len(chain._load_instruments()) == 2000


def test_a_saved_file_is_used_when_the_download_fails(monkeypatch, tmp_path):
    import json
    import os

    from intraday_advisor import nse_option_chain as chain

    path = tmp_path / "instrument_master.json"
    rows = [{"token": str(i), "pad": "x" * 600} for i in range(2000)]
    path.write_text(json.dumps(rows), encoding="utf-8")
    old = time.time() - 3 * 86400
    os.utime(path, (old, old))                 # three days stale

    def down(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(chain, "INSTRUMENT_FILE", str(path))
    monkeypatch.setattr(chain, "_INSTRUMENT_CACHE", None)
    monkeypatch.setattr(chain.requests, "get", down)
    assert len(chain._load_instruments()) == 2000


# =========================================================== default expiry ==

def test_the_default_expiry_skips_a_contract_expiring_today(monkeypatch, tmp_path):
    """On an expiry day the engine analysed the contract its own picker labels
    "do not buy", so the whole session read DO NOT TAKE -- and a restart
    silently dropped the desk back onto it."""
    from intraday_advisor import nse_option_chain
    from intraday_advisor.engine import Engine

    monkeypatch.setattr(nse_option_chain, "list_expiries", lambda index, limit=8: [
        {"label": "17NOV2026", "days": 0}, {"label": "23NOV2026", "days": 6}])
    engine = Engine(available_capital=20_000, record=False,
                    db_path=str(tmp_path / "l.sqlite"))
    assert engine._expiry_for("NIFTY") == "23NOV2026"


def test_a_normal_day_keeps_the_chains_own_default(monkeypatch, tmp_path):
    from intraday_advisor import nse_option_chain
    from intraday_advisor.engine import Engine

    monkeypatch.setattr(nse_option_chain, "list_expiries", lambda index, limit=8: [
        {"label": "23NOV2026", "days": 6}, {"label": "01DEC2026", "days": 14}])
    engine = Engine(available_capital=20_000, record=False,
                    db_path=str(tmp_path / "l.sqlite"))
    assert engine._expiry_for("NIFTY") is None


def test_an_explicit_choice_always_wins(monkeypatch, tmp_path):
    from intraday_advisor import nse_option_chain
    from intraday_advisor.engine import Engine

    monkeypatch.setattr(nse_option_chain, "list_expiries", lambda index, limit=8: [
        {"label": "17NOV2026", "days": 0}, {"label": "23NOV2026", "days": 6}])
    engine = Engine(available_capital=20_000, record=False,
                    db_path=str(tmp_path / "l.sqlite"))
    engine.expiries["NIFTY"] = "17NOV2026"
    assert engine._expiry_for("NIFTY") == "17NOV2026"


# ========================================================= worker process ==

def _square(x):
    return x * x


def _boom():
    raise ValueError("job failed")


def test_slow_work_runs_in_another_process():
    """A background THREAD doing pandas still holds the interpreter lock and
    starved the snapshot loop (44-51s). A process has its own."""
    import os

    from intraday_advisor import offload

    assert offload.call(_square, 12) == 144
    assert offload.call(os.getpid) != os.getpid(), "must not be this process"


def test_a_job_that_fails_in_the_worker_still_raises_its_own_error():
    from intraday_advisor import offload

    with pytest.raises(ValueError, match="job failed"):
        offload.call(_boom)
    assert offload.call(_square, 3) == 9, "the pool recovers afterwards"


def test_without_a_pool_the_work_still_gets_done(monkeypatch):
    from intraday_advisor import offload

    monkeypatch.setattr(offload, "_BROKEN", True)
    monkeypatch.setattr(offload, "_POOL", None)
    assert offload.call(_square, 5) == 25
