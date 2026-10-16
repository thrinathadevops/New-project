"""Tests for the intraday equity screener.

The theme: equities are not options with a different ticker. Shares are
divisible, there is no time decay, and the cost structure is different -- so
the sizing and the cost model must differ too, or the numbers lie.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.config import CostModel
from intraday_advisor.nse_watchlist import get_scan_universe
from intraday_advisor.stock_screener import (
    MIN_BARS,
    WARMUP_BARS,
    StockRow,
    _equity_round_trip,
    screen_one,
)


def candles(bars: int = 300, start: float = 1000.0, drift: float = 0.0008,
            volatility: float = 0.004) -> pd.DataFrame:
    """A trending OHLCV series long enough for the 200-period indicators."""
    rng = np.random.default_rng(7)
    returns = rng.normal(drift, volatility, size=bars)
    close = start * np.exp(np.cumsum(returns))
    spread = np.abs(rng.normal(0, volatility, size=bars)) * close
    index = pd.date_range("2026-10-16 09:15", periods=bars, freq="5min")
    return pd.DataFrame({
        "Open": close * (1 + rng.normal(0, 0.0005, size=bars)),
        "High": close + spread,
        "Low": close - spread,
        "Close": close,
        "Volume": rng.integers(100_000, 900_000, size=bars),
    }, index=index)


# ===================================================================== costs ==

def test_equity_costs_charge_stt_on_the_sell_side_only():
    costs = CostModel()
    cost = _equity_round_trip(1000.0, 1100.0, 10, costs)
    assert cost > 0
    # STT applies to the sell leg: a higher exit costs more tax.
    higher_exit = _equity_round_trip(1000.0, 1200.0, 10, costs)
    assert higher_exit > cost


def test_equity_brokerage_is_the_lower_of_flat_and_percentage():
    """The real intraday structure, unlike options' flat per-order fee."""
    costs = CostModel()
    small = _equity_round_trip(100.0, 100.0, 10, costs)       # Rs 1,000 turnover
    large = _equity_round_trip(100.0, 100.0, 10_000, costs)   # Rs 1,000,000
    assert large > small
    # On a small position the percentage leg binds, so cost is well under the
    # Rs 20 flat fee on each side.
    assert small < costs.brokerage_flat * 2


def test_no_shares_costs_nothing():
    assert _equity_round_trip(100.0, 110.0, 0, CostModel()) == 0.0


def test_equity_costs_are_a_smaller_burden_than_option_costs():
    """The cost floor that makes cheap options unviable largely disappears:
    equity brokerage scales with turnover, options' does not."""
    from intraday_advisor.ledger.costs import cost_drag_pct

    equity = _equity_round_trip(100.0, 100.0, 100, CostModel()) / (100.0 * 100)
    option = cost_drag_pct(10.0, 65)          # a cheap option, one lot
    assert equity < option


# ================================================================ screen_one ==

def test_screen_one_produces_a_sized_plan():
    row = screen_one("TEST", candles(), capital=200_000, risk_pct=0.02)

    assert row.error is None
    assert row.price > 0
    assert 0 <= row.score <= 100
    assert row.signal in ("BUY", "SELL", "HOLD")
    assert row.stop < row.entry < row.target, "long plan must bracket the entry"
    assert row.shares > 0
    assert row.affordable is True


def test_shares_are_divisible_unlike_option_lots():
    """The practical reason a small account can trade equities at all: it can
    size to its risk budget instead of being forced into one whole lot."""
    big = screen_one("TEST", candles(), capital=500_000, risk_pct=0.02)
    small = screen_one("TEST", candles(), capital=50_000, risk_pct=0.02)

    assert big.shares > small.shares
    assert small.shares >= 1, "a share is divisible; a lot is not"


def test_risk_includes_costs_so_it_exceeds_the_textbook_figure():
    row = screen_one("TEST", candles(), capital=200_000, risk_pct=0.02)
    textbook = row.shares * (row.entry - row.stop)

    assert row.risk_amount > textbook
    assert row.cost_round_trip > 0


def test_breakeven_sits_above_the_entry():
    row = screen_one("TEST", candles(), capital=200_000, risk_pct=0.02)
    assert row.breakeven > row.entry


def test_insufficient_history_is_reported_not_guessed():
    row = screen_one("TEST", candles(bars=20), capital=100_000, risk_pct=0.02)
    assert row.error is not None
    assert "bars" in row.error
    assert row.score == 0


def test_garbage_input_does_not_raise():
    row = screen_one("TEST", pd.DataFrame({"nonsense": [1, 2, 3]}),
                     capital=100_000, risk_pct=0.02)
    assert row.error is not None
    assert row.signal == "HOLD"


def test_components_are_recorded_for_traceability():
    """Same requirement as the options path: a score with no breakdown is an
    unexplained number."""
    row = screen_one("TEST", candles(), capital=200_000, risk_pct=0.02)
    assert row.components
    assert row.bar_time


def test_tiny_capital_warns_rather_than_inventing_a_position():
    row = screen_one("TEST", candles(start=25_000.0), capital=5_000,
                     risk_pct=0.02)
    assert row.shares == 0
    assert row.affordable is False
    assert any("one share" in w or "capital" in w for w in row.warnings)


def test_quiet_stocks_are_flagged():
    flat = candles(drift=0.0, volatility=0.00005)
    row = screen_one("TEST", flat, capital=200_000, risk_pct=0.02)
    if row.error is None and row.atr_pct is not None:
        assert row.atr_pct < 0.2
        assert any("quiet" in w.lower() for w in row.warnings)


# ================================================================= universes ==

def test_universes_are_defined_and_distinct():
    n50 = get_scan_universe("nifty50")
    fno = get_scan_universe("fno")
    bank = get_scan_universe("banknifty")

    assert len(n50) >= 45
    assert len(bank) >= 8
    assert set(bank) != set(n50)
    assert get_scan_universe("all") != n50


def test_no_duplicate_symbols_in_the_combined_universe():
    combined = get_scan_universe("all")
    assert len(combined) == len(set(combined))


def test_the_delisted_tata_motors_ticker_is_gone():
    """A dead ticker returns zero bars, scores as HOLD and quietly vanishes
    from the ranking instead of announcing itself."""
    for mode in ("nifty50", "fno", "banknifty", "all"):
        assert "TATAMOTORS" not in get_scan_universe(mode)


# ================================================================== plumbing ==

def test_warmup_covers_the_slowest_indicator():
    """EMA200/SMA200 need 200 bars; fewer and the row is mostly NaN."""
    assert WARMUP_BARS >= 200
    assert MIN_BARS < WARMUP_BARS


def test_scan_async_returns_immediately_without_blocking(monkeypatch):
    """A dashboard refresh must never wait on a minute-long scan."""
    import intraday_advisor.stock_screener as screener

    monkeypatch.setitem(screener._SCAN_STATE, "result", None)
    monkeypatch.setitem(screener._SCAN_STATE, "running", True)   # pretend busy

    result = screener.scan_async("nifty50", capital=20_000)
    assert result["rows"] == []
    assert result["scanning"] is True
    assert "First scan" in result["note"]


def test_scan_async_serves_the_cached_result(monkeypatch):
    import intraday_advisor.stock_screener as screener
    import time as _time

    cached = {"rows": [{"symbol": "TEST"}], "mode": "nifty50", "scored": 1}
    monkeypatch.setitem(screener._SCAN_STATE, "result", cached)
    monkeypatch.setitem(screener._SCAN_STATE, "running", False)
    monkeypatch.setitem(screener._SCAN_STATE, "started_at", _time.time())

    result = screener.scan_async("nifty50", capital=20_000)
    assert result["rows"] == cached["rows"]
    assert result["scanning"] is False


def test_stock_row_serialises():
    row = StockRow(symbol="X", price=1.0, change_pct=0.0, score=50, signal="HOLD")
    data = row.as_dict()
    assert data["symbol"] == "X"
    assert "components" in data
