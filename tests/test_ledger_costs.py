"""Tests for the options cost model.

The point of these is that cost errors do not look like bugs, they look like
edge. A model that undercharges by 0.5% turns a losing strategy into a winning
backtest, so each statutory rule is pinned down individually.
"""

from __future__ import annotations

import pytest

from intraday_advisor.ledger.costs import (
    TICK_SIZE,
    OptionsCostModel,
    breakeven_premium,
    calibrate_from_fill,
    cost_drag_pct,
    describe_costs,
    leg_costs,
    net_pnl,
    round_trip_cost,
    slippage_per_unit,
)

NIFTY_LOT = 75


def test_stt_charged_on_sell_only():
    """STT is a sell-side tax on premium. Charging it on buys inflates costs,
    omitting it on sells understates every loss."""
    buy = leg_costs(100.0, NIFTY_LOT, is_sell=False)
    sell = leg_costs(100.0, NIFTY_LOT, is_sell=True)

    assert buy["stt"] == 0.0
    assert sell["stt"] == pytest.approx(100.0 * NIFTY_LOT * 0.001)


def test_stamp_duty_charged_on_buy_only():
    assert leg_costs(100.0, NIFTY_LOT, is_sell=False)["stamp_duty"] > 0
    assert leg_costs(100.0, NIFTY_LOT, is_sell=True)["stamp_duty"] == 0.0


def test_gst_excludes_stt_and_stamp_duty():
    """GST applies to services, not to taxes. Taxing STT would overstate costs."""
    costs = OptionsCostModel()
    sell = leg_costs(100.0, NIFTY_LOT, is_sell=True, costs=costs)
    taxable = sell["brokerage"] + sell["exchange_txn"] + sell["sebi"] + sell["ipft"]

    assert sell["gst"] == pytest.approx(taxable * costs.gst_pct)
    # Explicitly not including STT in the GST base.
    assert sell["gst"] < (taxable + sell["stt"]) * costs.gst_pct


def test_options_brokerage_is_flat_per_order():
    """F&O brokerage is Rs 20 per order regardless of premium size.

    Modelling it as a percentage (the equity structure) is the error that makes
    cheap contracts look cheap to trade.
    """
    costs = OptionsCostModel()
    assert costs.brokerage_pct == 0.0

    cheap = leg_costs(10.0, NIFTY_LOT, is_sell=False, costs=costs)
    rich = leg_costs(500.0, NIFTY_LOT, is_sell=False, costs=costs)
    assert cheap["brokerage"] == pytest.approx(20.0)
    assert rich["brokerage"] == pytest.approx(20.0)


def test_percentage_brokerage_applies_when_configured():
    """Brokers offering 'flat or %, whichever is lower' are still supported."""
    costs = OptionsCostModel(brokerage_pct=0.0003)
    small = leg_costs(20.0, NIFTY_LOT, is_sell=False, costs=costs)
    large = leg_costs(5000.0, NIFTY_LOT, is_sell=False, costs=costs)

    assert small["brokerage"] == pytest.approx(20.0 * NIFTY_LOT * 0.0003)
    assert large["brokerage"] == pytest.approx(20.0)  # flat cap binds


def test_cheap_premiums_suffer_far_worse_cost_drag():
    """The finding that justifies this whole module existing.

    Identical trade structure, 4x+ the drag, purely because flat brokerage does
    not scale down with premium. A percentage-only cost model cannot see this.
    """
    expensive = cost_drag_pct(100.0, NIFTY_LOT)
    cheap = cost_drag_pct(20.0, NIFTY_LOT)
    very_cheap = cost_drag_pct(10.0, NIFTY_LOT)

    assert cheap > expensive * 2.5
    assert very_cheap > expensive * 5
    assert expensive < 0.02        # ~1.3% round trip
    assert cheap > 0.03            # >3% round trip
    assert very_cheap > 0.06       # >6% round trip: a +25% target is a bad bet


def test_slippage_floors_at_one_tick():
    """A 5-paise premium cannot be crossed for 0.0125 paise."""
    costs = OptionsCostModel(half_spread_pct=0.0025, min_half_spread_ticks=1.0)
    # 0.25% of Rs 2.00 = Rs 0.005, far below one tick.
    assert slippage_per_unit(2.0, costs) == pytest.approx(TICK_SIZE)
    # At Rs 100 the percentage term dominates.
    assert slippage_per_unit(100.0, costs) == pytest.approx(0.25)


def test_slippage_can_be_disabled_for_comparison():
    with_slip = round_trip_cost(100.0, 110.0, NIFTY_LOT, include_slippage=True)
    without = round_trip_cost(100.0, 110.0, NIFTY_LOT, include_slippage=False)

    assert with_slip["slippage"] > 0
    assert without["slippage"] == 0.0
    assert with_slip["total"] > without["total"]


def test_net_pnl_is_gross_minus_all_costs():
    result = net_pnl(100.0, 125.0, NIFTY_LOT)

    assert result["gross_pnl"] == pytest.approx(25.0 * NIFTY_LOT)
    assert result["net_pnl"] == pytest.approx(result["gross_pnl"] - result["total_cost"])
    assert result["total_cost"] > 0


def test_a_flat_exit_is_a_loss():
    """Selling at your entry price loses money. Any model that returns zero
    here will report break-even trades as free."""
    assert net_pnl(100.0, 100.0, NIFTY_LOT)["net_pnl"] < 0


def test_breakeven_is_above_entry_and_nets_to_zero():
    be = breakeven_premium(100.0, NIFTY_LOT)

    assert be > 100.0
    assert net_pnl(100.0, be, NIFTY_LOT)["net_pnl"] == pytest.approx(0.0, abs=0.01)
    # Just below breakeven must still be a loss.
    assert net_pnl(100.0, be - 0.5, NIFTY_LOT)["net_pnl"] < 0


def test_breakeven_scales_badly_for_cheap_contracts():
    """On a Rs 10 premium, flat brokerage alone demands a multi-percent move."""
    cheap_move = breakeven_premium(10.0, NIFTY_LOT) / 10.0 - 1
    rich_move = breakeven_premium(200.0, NIFTY_LOT) / 200.0 - 1

    assert cheap_move > rich_move
    assert cheap_move > 0.03


def test_zero_and_negative_inputs_do_not_blow_up():
    assert leg_costs(0.0, NIFTY_LOT, is_sell=True)["total"] == 0.0
    assert leg_costs(100.0, 0, is_sell=True)["total"] == 0.0
    assert slippage_per_unit(-5.0, OptionsCostModel()) == 0.0
    assert cost_drag_pct(0.0, NIFTY_LOT) == 0.0
    assert breakeven_premium(0.0, NIFTY_LOT) == 0.0


def test_calibrate_from_fill_recovers_a_known_spread():
    """Feed in a cost produced by a known spread; get that spread back."""
    costs = OptionsCostModel(half_spread_pct=0.004, min_half_spread_ticks=0.0)
    actual = round_trip_cost(100.0, 110.0, NIFTY_LOT, costs=costs)["total"]

    fitted = calibrate_from_fill(100.0, 110.0, NIFTY_LOT, actual, costs=costs)
    assert fitted.half_spread_pct == pytest.approx(0.004, rel=1e-6)


def test_calibrate_from_fill_handles_costs_below_statutory():
    """A broker cheaper than the model implies zero measurable spread, not a
    negative one."""
    fitted = calibrate_from_fill(100.0, 110.0, NIFTY_LOT, 1.0)
    assert fitted.half_spread_pct == 0.0


def test_with_measured_spread_clamps_negatives():
    assert OptionsCostModel().with_measured_spread(-1.0).half_spread_pct == 0.0


def test_describe_costs_reports_every_component():
    text = describe_costs(100.0, 125.0, NIFTY_LOT)
    for label in ("Brokerage", "STT", "Exchange txn", "GST",
                  "Spread/slippage", "TOTAL COST", "Net P&L"):
        assert label in text
