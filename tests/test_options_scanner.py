"""Tests for the all-strikes option scanner. Fully offline via injected chains."""

import pandas as pd

from intraday_advisor.data import generate_sample_ohlcv
from intraday_advisor.options_scanner import (
    DEFAULT_LOT_SIZES,
    OptionCandidate,
    _score_contract,
    scan_option_contracts,
)


def _fake_chain(spot: float, step: int) -> dict:
    atm = round(spot / step) * step
    strikes = [int(atm + i * step) for i in range(-3, 4)]
    rows = []
    for s in strikes:
        depth = abs(s - atm) // step
        rows.append({
            "strike": s,
            "ce_ltp": 50.0 + (atm - s) / step * 5,   # always > 0 for this range
            "pe_ltp": 50.0 + (s - atm) / step * 5,
            "ce_oi": 1000 * (4 - depth),
            "pe_oi": 1000 * (4 - depth),
        })
    return {
        "df": pd.DataFrame(rows), "atm": atm, "spot": spot, "bias": "CALL",
        "rec_strike": atm, "atm_ce_ltp": 50.0, "atm_pe_ltp": 50.0, "pcr": 1.1,
        "max_pain": atm, "max_ce_wall": atm + step * 2, "max_pe_wall": atm - step * 2,
        "data_source": "Synthetic (test)", "reasons": [], "error": None,
    }


def _run_scan(**kw):
    candles = {"NIFTY": generate_sample_ohlcv(seed=7), "BANKNIFTY": generate_sample_ohlcv(seed=11)}
    chains = {"NIFTY": _fake_chain(25000, 50), "BANKNIFTY": _fake_chain(50000, 100)}
    return scan_option_contracts(candles_by_index=candles, chains_by_index=chains, **kw)


# --------------------------------------------------------------------------- #
# pure scoring
# --------------------------------------------------------------------------- #
def test_aligned_call_beats_counter_put_at_same_strike():
    common = dict(strike=25000, premium=50.0, oi=1000, oi_rank=0.8, atm=25000, step=50)
    call_aligned, *_ = _score_contract(opt="CE", index_direction="CALL", **common)
    put_counter, *_ = _score_contract(opt="PE", index_direction="CALL", **common)
    assert call_aligned > put_counter


def test_moneyness_classification_for_calls_and_puts():
    _, m_atm, *_ = _score_contract(opt="CE", strike=25000, premium=50, oi=0, oi_rank=0, atm=25000, step=50, index_direction="CALL")
    _, m_otm, *_ = _score_contract(opt="CE", strike=25100, premium=50, oi=0, oi_rank=0, atm=25000, step=50, index_direction="CALL")
    _, m_itm, *_ = _score_contract(opt="CE", strike=24900, premium=50, oi=0, oi_rank=0, atm=25000, step=50, index_direction="CALL")
    assert (m_atm, m_otm, m_itm) == ("ATM", "OTM", "ITM")
    # puts invert
    _, p_otm, *_ = _score_contract(opt="PE", strike=24900, premium=50, oi=0, oi_rank=0, atm=25000, step=50, index_direction="PUT")
    assert p_otm == "OTM"


def test_far_otm_scores_lower_than_atm():
    atm_score, *_ = _score_contract(opt="CE", strike=25000, premium=50, oi=0, oi_rank=0, atm=25000, step=50, index_direction="CALL")
    far_score, *_ = _score_contract(opt="CE", strike=25300, premium=50, oi=0, oi_rank=0, atm=25000, step=50, index_direction="CALL")
    assert far_score < atm_score


def test_higher_open_interest_raises_score():
    low, *_ = _score_contract(opt="CE", strike=25000, premium=50, oi=100, oi_rank=0.1, atm=25000, step=50, index_direction="CALL")
    high, *_ = _score_contract(opt="CE", strike=25000, premium=50, oi=9999, oi_rank=0.95, atm=25000, step=50, index_direction="CALL")
    assert high > low


# --------------------------------------------------------------------------- #
# full scan
# --------------------------------------------------------------------------- #
def test_scan_returns_scored_candidates():
    out = _run_scan()
    assert len(out) > 0
    assert all(isinstance(c, OptionCandidate) for c in out)
    assert all(0 <= c.score <= 100 for c in out)


def test_scan_rupee_math_is_consistent():
    out = _run_scan(target_pct=0.25, stop_pct=0.20)
    c = out[0]
    assert c.capital_per_lot == round(c.premium * c.lot_size, 2)
    assert c.target_premium == round(c.premium * 1.25, 2)
    assert c.stop_premium == round(c.premium * 0.80, 2)
    assert c.profit_per_lot == round((c.target_premium - c.premium) * c.lot_size, 2)
    assert c.loss_per_lot == round((c.premium - c.stop_premium) * c.lot_size, 2)


def test_scan_sorts_aligned_first():
    out = _run_scan()
    flags = [c.aligned for c in out]
    assert flags == sorted(flags, reverse=True)


def test_affordable_lots_respects_budget():
    out = _run_scan()
    c = out[0]
    budget = c.capital_per_lot * 3 + 1
    assert c.affordable_lots(budget) == 3
    assert c.affordable_lots(0) == 0


def test_lot_size_override_is_applied():
    out = _run_scan(lot_sizes={"NIFTY": 50})
    nifty = [c for c in out if c.index == "NIFTY"]
    assert nifty and all(c.lot_size == 50 for c in nifty)


def test_default_lot_sizes_present():
    assert "NIFTY" in DEFAULT_LOT_SIZES and "BANKNIFTY" in DEFAULT_LOT_SIZES
