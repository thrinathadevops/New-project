"""Open positions on the fast tick clock.

The point of this path is that a stop alarm should not wait out a 10-20 second
snapshot interval. The risks it introduces are what these tests pin down: that
it must not hit the network, must not blank a row it has no tick for, must not
announce the same alarm twice, and must agree with the snapshot about what a
stop-out is.
"""

from __future__ import annotations

import pytest

from intraday_advisor import live_ticks
from intraday_advisor.engine import Engine
from intraday_advisor.positions import open_position


@pytest.fixture
def engine(tmp_path):
    return Engine(db_path=str(tmp_path / "ledger.sqlite"), record=False)


@pytest.fixture
def held(engine):
    """One open NIFTY position, entry 100, target +40%, stop -20%."""
    return open_position(
        index_name="NIFTY", option_type="CE", strike=23000.0,
        entry_premium=100.0, lots=1, lot_size=65,
        target_pct=0.40, stop_pct=0.20,
        option_token="99999", db_path=engine.db_path,
    )


@pytest.fixture
def ticking(monkeypatch):
    """Control what the tick cache reports, with no socket involved."""
    prices: dict[str, tuple[float | None, float | None]] = {}
    monkeypatch.setattr(live_ticks, "watch", lambda tokens: None)
    monkeypatch.setattr(
        live_ticks, "live_price",
        lambda token: prices.get(str(token), (None, None)),
    )
    return prices


# ==================================================================== basics ==

def test_no_open_positions_reports_an_empty_list_not_silence(engine, ticking):
    """An empty list is a real state -- all closed -- and the browser has to
    clear the table rather than keep showing rows that are gone."""
    result = engine.tick_positions()
    assert result["positions"] == []
    assert result["total"] == 0


def test_a_tick_prices_the_position(engine, held, ticking):
    ticking["99999"] = (120.0, 0.4)

    result = engine.tick_positions()
    row = result["positions"][0]

    assert row["current_premium"] == 120.0
    assert row["premium_source"].startswith("live tick")
    assert row["net_pnl"] > 0
    assert result["priced"] == 1


def test_net_pnl_is_net_of_exit_costs(engine, held, ticking):
    ticking["99999"] = (120.0, 0.4)
    row = engine.tick_positions()["positions"][0]

    gross = (120.0 - 100.0) * 65
    assert row["gross_pnl"] == pytest.approx(gross)
    assert row["net_pnl"] < row["gross_pnl"], "exiting costs money"


# ============================================================== no network ===

def test_the_tick_path_never_calls_rest(engine, held, ticking, monkeypatch):
    """A premium fetch every 1.5s would be rate-limited within minutes. The
    snapshot may fall back to REST; this path may not."""
    import intraday_advisor.engine as engine_module

    def forbidden(*args, **kwargs):
        raise AssertionError("tick path must not fetch premiums over REST")

    monkeypatch.setattr(engine_module, "get_option_premium", forbidden)
    engine.tick_positions()          # no tick available, must still not fetch


def test_a_row_without_a_tick_keeps_the_last_known_price(engine, held, ticking):
    """Otherwise the row flickers between a REST price and a dash every 1.5s,
    which is worse than showing the stale number with its source named."""
    engine._last_premiums[held] = (105.0, "option chain")

    row = engine.tick_positions()["positions"][0]
    assert row["current_premium"] == 105.0
    assert row["premium_source"] == "option chain"
    assert engine.tick_positions()["priced"] == 0


def test_with_neither_tick_nor_history_the_row_says_so(engine, held, ticking):
    row = engine.tick_positions()["positions"][0]
    assert row["current_premium"] is None
    assert row["status"] == "NO_DATA"
    assert row["net_pnl"] == 0.0


# ================================================================== alarms ===

def test_a_stop_breach_raises_an_alarm_on_the_tick_clock(engine, held, ticking):
    ticking["99999"] = (79.0, 0.3)          # below the 80.0 stop

    result = engine.tick_positions()
    assert result["positions"][0]["status"] == "STOP_HIT"
    kinds = [e["kind"] for e in result["events"]]
    assert "POSITION_STOP" in kinds
    assert result["events"][0]["sticky"] is True, "a stop must be acknowledged"


def test_an_alarm_is_announced_once_not_on_every_tick(engine, held, ticking):
    ticking["99999"] = (79.0, 0.3)

    first = engine.tick_positions()
    second = engine.tick_positions()

    assert first["events"], "the breach must be announced"
    assert second["events"] == [], "a 1.5s loop must not re-toast it"
    assert second["positions"][0]["status"] == "STOP_HIT"


def test_the_snapshot_does_not_re_announce_what_a_tick_already_did(
    engine, held, ticking
):
    """Both paths share one dedupe set, so whichever sees the breach first
    announces it -- which is the entire point of running this often."""
    ticking["99999"] = (79.0, 0.3)
    assert engine.tick_positions()["events"]

    rows = engine.tick_positions()["positions"]
    assert engine._position_events(rows) == []


def test_a_target_hit_is_announced_too(engine, held, ticking):
    ticking["99999"] = (141.0, 0.2)         # above the 140.0 target
    result = engine.tick_positions()
    assert result["positions"][0]["status"] == "TARGET_HIT"
    assert [e["kind"] for e in result["events"]] == ["POSITION_TARGET"]


def test_an_on_track_position_raises_nothing(engine, held, ticking):
    ticking["99999"] = (104.0, 0.2)
    result = engine.tick_positions()
    assert result["events"] == []


# =============================================== agreement with the snapshot ==

def test_tick_and_snapshot_agree_on_the_same_price(engine, held, ticking):
    """One copy of the status arithmetic. If these ever diverge, the ledger
    records one answer and the screen shows another."""
    ticking["99999"] = (79.0, 0.3)

    tick_row = engine.tick_positions()["positions"][0]
    engine._announced.clear()
    snapshot_row = engine._positions(
        [{"index": "NIFTY", "direction": "CALL", "spot": 23000}]
    )[0]

    for field in ("status", "action", "net_pnl", "pnl_pct", "is_alarm"):
        assert tick_row[field] == snapshot_row[field], field


def test_underlying_direction_carries_over_from_the_last_snapshot(
    engine, held, ticking
):
    """The direction only changes on a closed candle, so re-deriving it every
    1.5s would be work for an answer that cannot have moved."""
    ticking["99999"] = (104.0, 0.2)
    engine._last_directions = {"NIFTY": "PUT"}

    row = engine.tick_positions()["positions"][0]
    assert row["status"] in ("WARNING", "ON_TRACK")
    assert engine._last_directions["NIFTY"] == "PUT"
