"""Long option against credit spread, on the same signal at the same moment.

Whatever the signal's quality, it is identical for both arms, so the difference
between them is the structure and nothing else. These tests pin the parts that
would silently bias that comparison if they were wrong: which spread expresses
the same view, when each arm exits, and how the result is counted.
"""

from __future__ import annotations

import pytest

import experiment as ex


def ladder(spot=25350.0, step=50.0, n=6):
    base = round(spot / step) * step
    rows = []
    for i in range(-n, n + 1):
        strike = base + i * step
        rows.append({"strike": strike,
                     "ce": max(5.0, 190.0 - i * 28.0),
                     "pe": max(5.0, 180.0 + i * 27.0)})
    return rows


def signal(**over):
    base = {"index": "NIFTY", "direction": "CALL", "option_type": "CE",
            "strike": 25350.0, "entry_premium": 189.3, "is_live": True,
            "spot": 25354.5, "lot_size": 65, "expiry": "23NOV2026",
            "bar_time": "b1", "ladder": ladder(),
            "thesis": {"blockers": []}, "agent": {"concerns": []}}
    base.update(over)
    return base


def allowed(sig, hhmm="10:00", open_=True, mode="LIVE"):
    return ex.entry_allowed(sig, now_hhmm=hhmm, market_open=open_, mode=mode)


# =================================================================== entry ==

def test_a_clean_live_signal_opens_a_pair():
    assert allowed(signal()) == (True, "ok")


def test_a_wait_bar_opens_nothing():
    assert allowed(signal(direction="WAIT"))[0] is False


def test_a_closed_market_opens_nothing():
    assert allowed(signal(), open_=False)[0] is False
    assert allowed(signal(), mode="ESTIMATED")[0] is False


def test_no_entries_late_in_the_session():
    assert allowed(signal(), hhmm="14:45")[0] is False


def test_the_expectancy_veto_is_the_only_blocker_overridden():
    """That veto IS the long-option record under re-measurement; obeying it
    would mean the test never runs."""
    sig = signal(thesis={"blockers": ["Risk checklist"]},
                 agent={"concerns": ["Measured expectancy is Rs -300 per lot."]})
    assert allowed(sig)[0] is True


def test_any_other_veto_still_stands():
    sig = signal(thesis={"blockers": ["Risk checklist"]},
                 agent={"concerns": ["Expiry day. This contract expires today."]})
    ok, reason = allowed(sig)
    assert ok is False and "vetoed" in reason


def test_a_contract_level_blocker_stops_both_arms():
    ok, reason = allowed(signal(thesis={"blockers": ["Costs"]}))
    assert ok is False and "Costs" in reason


def test_a_synthetic_floor_premium_is_not_traded():
    assert allowed(signal(entry_premium=0.5))[0] is False


# ================================================================== spread ==

def test_a_call_signal_becomes_a_bull_put_spread_below_spot():
    spread = ex.pick_spread(ladder(), 25354.5, "CALL")
    assert spread["option_type"] == "PE"
    assert spread["short_strike"] == 25350.0, "first strike below spot"
    assert spread["long_strike"] == 25250.0, "two strikes further out"
    assert spread["short_strike"] > spread["long_strike"]
    assert spread["credit"] > 0


def test_a_put_signal_becomes_a_bear_call_spread_above_spot():
    spread = ex.pick_spread(ladder(), 25354.5, "PUT")
    assert spread["option_type"] == "CE"
    assert spread["short_strike"] == 25400.0
    assert spread["long_strike"] == 25500.0
    assert spread["credit"] > 0


def test_the_loss_is_capped_by_the_bought_wing():
    spread = ex.pick_spread(ladder(), 25354.5, "CALL")
    assert spread["max_loss_per_unit"] == pytest.approx(
        spread["width"] - spread["credit"])
    assert spread["max_loss_per_unit"] > 0


def test_no_spread_when_the_ladder_cannot_supply_both_legs():
    assert ex.pick_spread(ladder(n=1), 25354.5, "CALL") is None
    assert ex.pick_spread([], 25354.5, "CALL") is None
    assert ex.pick_spread(ladder(), 25354.5, "WAIT") is None


def test_no_spread_that_pays_nothing_to_open():
    flat = [{"strike": 25000.0 + i * 50, "ce": 50.0, "pe": 50.0}
            for i in range(14)]
    assert ex.pick_spread(flat, 25354.5, "CALL") is None


# ==================================================================== exits ==

def test_the_long_arm_exits_at_target_stop_or_square_off():
    assert ex.long_exit(100.0, 125.0, now_hhmm="11:00") == "target"
    assert ex.long_exit(100.0, 80.0, now_hhmm="11:00") == "stop"
    assert ex.long_exit(100.0, 104.0, now_hhmm="11:00") is None
    assert ex.long_exit(100.0, 104.0, now_hhmm="15:10") == "square-off"


def test_an_unknown_price_closes_nothing():
    assert ex.long_exit(100.0, None, now_hhmm="15:10") is None
    assert ex.spread_exit(40.0, None, now_hhmm="15:10") is None


def test_the_spread_keeps_half_the_credit_or_loses_one_credit():
    assert ex.spread_exit(40.0, 20.0, now_hhmm="11:00") == "target"
    assert ex.spread_exit(40.0, 80.0, now_hhmm="11:00") == "stop"
    assert ex.spread_exit(40.0, 35.0, now_hhmm="11:00") is None
    assert ex.spread_exit(40.0, 35.0, now_hhmm="15:10") == "square-off"


# ============================================================ ledger + report ==

@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "experiment.sqlite")


def _prices(rows, **changes):
    out = [dict(r) for r in rows]
    for row in out:
        for (strike, key), value in changes.items():
            if row["strike"] == strike:
                row[key] = value
    return out


def test_a_pair_opens_both_arms_and_closes_each_on_its_own_rule(db):
    sig = signal()
    spread = ex.pick_spread(sig["ladder"], sig["spot"], "CALL")
    ex.open_pair(sig, spread, db_path=db)

    # Index rallies: the call hits its target, the put spread decays to target.
    rallied = [dict(r) for r in sig["ladder"]]
    for row in rallied:
        if row["strike"] == 25350.0:
            row["ce"], row["pe"] = 240.0, 20.0
        if row["strike"] == 25250.0:
            row["pe"] = 8.0
    lines = ex.manage({"NIFTY": rallied}, now_hhmm="11:00", db_path=db)
    assert len(lines) == 2
    assert any(l.startswith("LONG") and "target" in l for l in lines)
    assert any(l.startswith("SPREAD") and "target" in l for l in lines)

    result = ex.results(db_path=db)
    assert result["arms"]["long"]["trades"] == 1
    assert result["arms"]["spread"]["trades"] == 1
    assert result["arms"]["long"]["net"] > 0
    assert result["arms"]["spread"]["net"] > 0
    assert result["open"] == 0


def test_a_spread_missing_a_leg_price_is_held_not_half_closed(db):
    sig = signal()
    spread = ex.pick_spread(sig["ladder"], sig["spot"], "CALL")
    ex.open_pair(sig, spread, db_path=db)
    partial = [r for r in sig["ladder"] if r["strike"] != 25250.0]
    lines = ex.manage({"NIFTY": partial}, now_hhmm="15:12", db_path=db)
    assert not any(l.startswith("SPREAD") for l in lines), (
        "a two-leg spread valued on one leg is a naked option")


def test_the_report_refuses_to_call_a_winner_on_a_few_sessions(db):
    sig = signal()
    spread = ex.pick_spread(sig["ladder"], sig["spot"], "CALL")
    ex.open_pair(sig, spread, db_path=db)
    ex.manage({"NIFTY": sig["ladder"]}, now_hhmm="15:12", db_path=db)
    text = ex.render(ex.results(db_path=db))
    assert "not established" in text
    assert str(ex.MIN_SESSIONS) in text


def test_an_empty_ledger_reports_plainly(tmp_path):
    text = ex.render(ex.results(db_path=str(tmp_path / "none.sqlite")))
    assert "No closed pairs yet" in text


def test_a_restart_remembers_todays_entries(db):
    """Kept in the ledger, not in memory: a restart mid-session must not forget
    the cooldown and open a second pair on the same signal."""
    from intraday_advisor.ledger.recorder import now_ist

    sig = signal()
    ex.open_pair(sig, ex.pick_spread(sig["ladder"], sig["spot"], "CALL"),
                 db_path=db)
    pairs, latest = ex.todays_entries(db_path=db)
    assert pairs == 1, "one pair, not two rows"
    now = now_ist()
    assert abs(latest["NIFTY"] - (now.hour * 60 + now.minute)) <= 1


def test_another_days_entries_do_not_count(db):
    sig = signal()
    ex.open_pair(sig, ex.pick_spread(sig["ladder"], sig["spot"], "CALL"),
                 db_path=db)
    assert ex.todays_entries(db_path=db, today="2020-01-01") == (0, {})
