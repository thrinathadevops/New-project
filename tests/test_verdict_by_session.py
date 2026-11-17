"""The record is counted by session, and the banner shows what is current.

The banner read "Over 597 completed trades ... the entire statistical range
sits on the losing side. That is a measurement, not bad luck" -- the same
sentence every day. It was static because the record only moves when a session
is graded, and it was overstated because trades inside one day share that
day's market: 597 trades from nine days are not 597 independent results.

Counted by session the same ledger is "not proven, leaning negative": 3 of 9
sessions profitable, range roughly Rs -1,000 to +350. That difference decided
whether the checklist vetoed every trade forever.
"""

from __future__ import annotations

import pytest

from intraday_advisor.engine import _t95, plain_verdict
from intraday_advisor.ledger.schema import ENGINE_VERSION, connect, init_ledger


def ledger(tmp_path, sessions: dict) -> str:
    """``sessions`` maps a date to a list of per-lot results."""
    db = str(tmp_path / "ledger.sqlite")
    init_ledger(db)
    with connect(db) as conn:
        n = 0
        for day, results in sessions.items():
            for pnl in results:
                n += 1
                sid = f"s{n}"
                conn.execute(
                    "INSERT INTO signals (signal_id, dedupe_key, emitted_at, "
                    "bar_time, interval, session_date, index_name, direction, "
                    "option_type, strike, lot_size, entry_premium, "
                    "stop_premium, target_premium, score, is_live, "
                    "is_synthetic, engine_version) VALUES (?,?,?,?, '5m', ?, "
                    "'NIFTY','CALL','CE', ?, 65, 190, 152, 237, 50, 1, 0, ?)",
                    (sid, sid, f"{day}T10:00:00+05:30",
                     f"{day} 10:{n % 60:02d}:00+05:30", day, 25000 + n,
                     ENGINE_VERSION))
                conn.execute(
                    "INSERT INTO signal_outcomes (signal_id, outcome, "
                    "net_pnl_per_lot, graded_at, grader_version) "
                    "VALUES (?,?,?,?, 'v1')",
                    (sid, "TARGET" if pnl > 0 else "STOP", pnl,
                     f"{day}T16:00:00+05:30"))
        conn.commit()
    return db


def verdict(db: str) -> dict:
    from intraday_advisor.ledger.report import expectancy_report

    return plain_verdict(expectancy_report(db_path=db), db)


#: The real ledger's shape on 2026-11-17: nine sessions, three profitable.
REAL = {
    "2026-10-16": [-1222.0] * 8, "2026-10-21": [-1221.0] * 8,
    "2026-10-22": [488.0] * 8, "2026-10-23": [293.0] * 8,
    "2026-11-02": [-1026.0] * 8, "2026-11-03": [-467.0] * 8,
    "2026-11-04": [-740.0] * 8, "2026-11-05": [-393.0] * 8,
    "2026-11-06": [1309.0] * 8,
}


def test_nine_mixed_sessions_are_not_an_established_loss(tmp_path):
    """The headline case. Per trade this read "entire range on the losing
    side"; per session the range includes a gain."""
    result = verdict(ledger(tmp_path, REAL))
    assert result["state"] == "INCONCLUSIVE"
    assert result["established"] is False
    assert result["ci_low"] < 0 < result["ci_high"]
    assert "Not proven either way" in result["headline"]
    assert "leaning negative" in result["headline"]


def test_the_banner_names_what_is_current(tmp_path):
    result = verdict(ledger(tmp_path, REAL))
    assert "3 of 9 graded sessions made money" in result["detail"]
    assert "2026-11-06" in result["detail"], "the latest session is named"
    assert "made Rs 1,309" in result["detail"]
    assert result["last_session"] == "2026-11-06"
    assert [r["session"] for r in result["recent"]][-1] == "2026-11-06"
    assert len(result["recent"]) == 6


def test_it_does_not_call_an_unproven_record_an_edge(tmp_path):
    result = verdict(ledger(tmp_path, REAL))
    assert result["tradeable"] is False
    assert "not evidence of an edge" in result["what_would_change_it"]


def test_a_loss_on_every_session_is_established(tmp_path):
    losing = {f"2026-11-{d:02d}": [-800.0 - d * 10] * 6 for d in range(2, 12)}
    result = verdict(ledger(tmp_path, losing))
    assert result["state"] == "LOSING"
    assert result["established"] is True
    assert result["ci_high"] < 0
    assert "established" in result["headline"]


def test_a_gain_on_every_session_is_an_edge(tmp_path):
    winning = {f"2026-11-{d:02d}": [600.0 + d * 10] * 6 for d in range(2, 12)}
    result = verdict(ledger(tmp_path, winning))
    assert result["state"] == "EDGE"
    assert result["tradeable"] is True


def test_two_sessions_are_not_enough_to_say_anything(tmp_path):
    result = verdict(ledger(tmp_path, {"2026-11-05": [-500.0] * 15,
                                       "2026-11-06": [-600.0] * 15}))
    assert result["state"] == "UNMEASURED"


def test_the_win_rate_is_stated_as_it_is(tmp_path):
    """41% used to be worded "about one winner in every 2"."""
    mixed = {f"2026-11-{d:02d}": [500.0] * 4 + [-600.0] * 6 for d in range(2, 8)}
    assert "about 4 winners in every 10" in verdict(ledger(tmp_path, mixed))["detail"]


def test_few_sessions_use_a_wider_multiplier_than_a_large_sample():
    assert _t95(8) == pytest.approx(2.31)
    assert _t95(2) > _t95(8) > _t95(30) > 1.96
    assert _t95(500) == 1.96


# ============================================================ consequences ==

def brief(**over):
    from intraday_advisor.agents.base import TradeBrief

    base = dict(index="NIFTY", direction="CALL", score=60, contract="25350 CE",
                entry_premium=194.0, target_premium=242.0, stop_premium=155.0,
                lots=1, net_rr=1.2, cost_drag=0.01, data_is_live=True,
                market_open=True, minutes_to_square_off=120, days_to_expiry=4.0,
                graded_trades=597, measured_expectancy=-300.0)
    base.update(over)
    return TradeBrief(**base)


def test_an_unestablished_negative_average_cautions_instead_of_vetoing():
    """It used to veto every trade, every day, on nine days of evidence."""
    from intraday_advisor.agents.rules import RuleAgent

    result = RuleAgent().review(brief())
    assert result.stance == "CAUTION"
    assert any("not established" in c for c in result.concerns)


def test_an_established_loss_is_still_vetoed():
    from intraday_advisor.agents.rules import RuleAgent

    result = RuleAgent().review(brief(expectancy_established=True))
    assert result.stance == "VETO"


def test_the_brief_reports_not_proven_rather_than_lost():
    from intraday_advisor.thesis import NEUTRAL, build_thesis

    signal = {"index": "NIFTY", "direction": "CALL", "option_type": "CE",
              "strike": 25350.0, "entry_premium": 194.0,
              "target_premium": 242.5, "stop_premium": 155.2}
    advice = {"state": "INCONCLUSIVE", "sessions": 9, "positive_sessions": 3,
              "mean_per_signal": -331.0}
    result = build_thesis(signal, performance={"trades": 597,
                                               "expectancy": -300.0},
                          advice=advice, mode="LIVE")
    record = next(c for c in result["checks"] if c["key"] == "record")
    assert record["state"] == NEUTRAL
    assert "3 of 9 sessions" in record["detail"]
    assert "LOST" not in record["detail"]
