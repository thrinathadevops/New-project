"""The spread was assumed for six sessions. These tests measure it instead.

The gap these guard is not a crash, it is a silent understatement. A cost model
that reads 0.25% on a contract whose book is 1.25% wide does not fail -- it
produces confident verdicts that are wrong by a factor of five, on the one
number that decides whether any trade in this project can clear its costs.

Several of these tests exist because the *feed* is untrustworthy rather than the
arithmetic: Angel One's own SDK hands back buy and sell depth from each other's
parser output, and its binary feed reports paise where its REST endpoint reports
rupees. Getting either wrong scales every cost by 100 or inverts the book.
"""

from __future__ import annotations

import datetime as dt

import pytest

from intraday_advisor.ledger.costs import OptionsCostModel
from intraday_advisor.microstructure import (
    SPREAD_SHAPE_BY_HOUR,
    assess_tradeability,
    calibrate_shape_from_fills,
    chain_spread_report,
    cost_model_from_book,
    parse_book,
    spread_multiplier_at,
)


# ================================================================= parsing ==

def rest_payload(bid=160.0, ask=161.0, bid_qty=300, ask_qty=300):
    """The shape Angel's REST quote returns: rupees, under ``depth``."""
    return {
        "depth": {
            "buy": [{"price": bid, "quantity": bid_qty, "orders": 4},
                    {"price": bid - 0.5, "quantity": 500, "orders": 7}],
            "sell": [{"price": ask, "quantity": ask_qty, "orders": 3},
                     {"price": ask + 0.5, "quantity": 450, "orders": 6}],
        }
    }


def test_a_rest_quote_yields_a_measured_spread():
    book = parse_book(rest_payload(), symbol="NIFTY22700PE", reference_price=160.5)

    assert book.is_usable
    assert book.best_bid == 160.0
    assert book.best_ask == 161.0
    assert book.mid == pytest.approx(160.5)
    assert book.spread == pytest.approx(1.0)
    assert book.spread_ticks == pytest.approx(20.0)      # Rs 1.00 at a 0.05 tick
    # The number the cost model wants: half the spread against the mid.
    assert book.half_spread_pct == pytest.approx(1.0 / 2 / 160.5)


def test_the_measured_spread_is_five_times_the_assumed_constant():
    """The whole reason this module exists, as an assertion.

    A Rs 2 spread on a Rs 160 premium is routine on an index option and it is
    5x what ledger/costs.py assumed for every contract.
    """
    book = parse_book(rest_payload(bid=159.0, ask=161.0), reference_price=160.0)
    measured = book.half_spread_pct
    assumed = OptionsCostModel().half_spread_pct

    assert measured == pytest.approx(0.00625)            # 0.625% per leg
    assert measured / assumed == pytest.approx(2.5, abs=0.01)
    # Round trip: 1.25% of premium, against every edge this project measured
    # at ~0.1% of the index.
    assert book.relative_spread == pytest.approx(0.0125)


def test_websocket_paise_are_detected_and_rescaled():
    """The binary feed reports integers in paise. Guessing wrong scales every
    cost by 100 -- in the direction that makes a trade look impossible."""
    payload = {
        "best_5_buy_data": [{"flag": 0, "price": 16000, "quantity": 300,
                             "no of orders": 4}],
        "best_5_sell_data": [{"flag": 1, "price": 16100, "quantity": 300,
                              "no of orders": 3}],
    }
    book = parse_book(payload, reference_price=160.5)

    assert book.best_bid == pytest.approx(160.0)
    assert book.best_ask == pytest.approx(161.0)
    assert any("paise" in n for n in book.notes)


def test_without_a_reference_price_the_scale_is_flagged_not_guessed():
    payload = {
        "best_5_buy_data": [{"price": 16000, "quantity": 300}],
        "best_5_sell_data": [{"price": 16100, "quantity": 300}],
    }
    book = parse_book(payload)

    # Taken at face value, and SAID so, rather than silently rescaled.
    assert book.best_bid == 16000
    assert any("face value" in n for n in book.notes)


def test_a_crossed_book_is_corrected_because_markets_cannot_cross():
    """The installed SmartApi SDK assigns best_5_buy_data from its own parser's
    SELL list and vice versa. Rather than hard-code a fix to someone else's
    bug -- which inverts the day they fix it -- the parser tests the one
    invariant a real market cannot violate."""
    swapped = {
        "depth": {
            "buy": [{"price": 161.0, "quantity": 300}],   # the real asks
            "sell": [{"price": 160.0, "quantity": 300}],  # the real bids
        }
    }
    book = parse_book(swapped, reference_price=160.5)

    assert book.best_bid == 160.0, "bid must be the lower of the two"
    assert book.best_ask == 161.0
    assert book.spread > 0, "a corrected book is never crossed"
    assert any("swapped" in n for n in book.notes)


def test_a_correctly_ordered_book_is_left_alone():
    """The self-correction must not fire on a healthy book."""
    book = parse_book(rest_payload(), reference_price=160.5)
    assert not any("swapped" in n for n in book.notes)


def test_padded_empty_levels_are_dropped_not_read_as_free():
    payload = {
        "depth": {
            "buy": [{"price": 160.0, "quantity": 300},
                    {"price": 0.0, "quantity": 0}],       # padding
            "sell": [{"price": 161.0, "quantity": 300},
                     {"price": 0.0, "quantity": 0}],
        }
    }
    book = parse_book(payload, reference_price=160.5)

    assert len(book.bids) == 1 and len(book.asks) == 1
    assert book.best_bid == 160.0


def test_a_missing_book_is_unknown_never_tight():
    """The failure mode this project refuses everywhere: an absent measurement
    must not read as a favourable one."""
    book = parse_book({}, symbol="DEADSTRIKE")

    assert not book.is_usable
    assert book.half_spread_pct is None
    assert any("not tight" in n or "cannot be measured" in n for n in book.notes)


def test_a_one_sided_book_is_not_usable():
    payload = {"depth": {"buy": [{"price": 160.0, "quantity": 300}], "sell": []}}
    book = parse_book(payload, reference_price=160.0)

    assert not book.is_usable
    assert book.mid is None


# ========================================================= walking the book ==

def test_an_order_inside_the_touch_pays_only_the_touch():
    book = parse_book(rest_payload(ask_qty=300), reference_price=160.5)
    walk = book.walk("buy", 75)                 # one NIFTY lot

    assert walk["complete"] is True
    assert walk["average_price"] == pytest.approx(161.0)
    # Reported to 4 decimals, which is finer than any spread is meaningful.
    assert walk["slippage_vs_mid_pct"] == pytest.approx(0.5 / 160.5 * 100, abs=1e-4)


def test_an_order_larger_than_the_touch_walks_up_and_costs_more():
    """A percentage half-spread cannot express 'the best offer only shows 40 of
    the 75 you need'. This is why walk() exists."""
    book = parse_book(rest_payload(ask_qty=40), reference_price=160.5)
    walk = book.walk("buy", 75)

    assert walk["complete"] is True
    # 40 at 161.0 then 35 at 161.5
    assert walk["average_price"] == pytest.approx((40 * 161.0 + 35 * 161.5) / 75)
    assert walk["average_price"] > 161.0, "walking the book costs more than the touch"


def test_a_book_too_thin_to_fill_says_so_instead_of_inventing_a_price():
    book = parse_book(rest_payload(ask_qty=10), reference_price=160.5)
    walk = book.walk("buy", 5_000)

    assert walk["complete"] is False
    assert walk["filled"] < 5_000
    assert "no quoted price" in walk["note"]


def test_selling_consumes_the_bid_side():
    book = parse_book(rest_payload(), reference_price=160.5)
    walk = book.walk("sell", 75)

    assert walk["average_price"] == pytest.approx(160.0)
    # Selling below the mid is also a positive cost, not a negative one.
    assert walk["slippage_vs_mid_pct"] > 0


def test_a_nonsense_order_is_refused():
    book = parse_book(rest_payload(), reference_price=160.5)
    with pytest.raises(ValueError):
        book.walk("sideways", 10)
    with pytest.raises(ValueError):
        book.walk("buy", 0)


def test_depth_imbalance_is_reported_as_unvalidated():
    """It is orthogonal to the scorer, which makes it interesting and does not
    make it predictive. Scoring it would repeat the mistake that produced 32
    collinear components with no measured edge."""
    book = parse_book(rest_payload(bid_qty=800, ask_qty=200),
                      reference_price=160.5)
    read = book.depth_imbalance()

    assert read["imbalance"] > 0, "more size bid than offered"
    assert read["status"] == "unvalidated"
    assert "not committed size" in read["caveat"]


# ============================================================== cost model ==

def test_a_measured_book_replaces_the_assumed_constant():
    book = parse_book(rest_payload(bid=159.0, ask=161.0), reference_price=160.0)
    out = cost_model_from_book(book)

    assert out["measured"] is True
    assert out["cost_model"].half_spread_pct == pytest.approx(0.00625)
    assert out["ratio_to_assumed"] == pytest.approx(2.5, abs=0.01)
    assert "understating" in out["note"]


def test_an_unmeasurable_book_keeps_the_assumption_and_flags_it():
    """A missing book must never make a contract look cheap -- the same rule
    the agent layer applies when an LLM call fails and the verdict is CAUTION
    rather than a silent PROCEED."""
    out = cost_model_from_book(parse_book({}))

    assert out["measured"] is False
    assert out["cost_model"].half_spread_pct == OptionsCostModel().half_spread_pct
    assert "assumption, not a measurement" in out["note"]


def test_a_tight_book_is_allowed_to_be_cheaper_than_assumed():
    """The correction has to run both ways, or it is just a penalty."""
    book = parse_book(rest_payload(bid=160.45, ask=160.55),
                      reference_price=160.5)
    out = cost_model_from_book(book)

    assert out["measured"] is True
    assert out["cost_model"].half_spread_pct < OptionsCostModel().half_spread_pct
    assert out["ratio_to_assumed"] < 1


# ============================================================== time of day ==

def test_the_open_is_modelled_as_dearer_than_midday():
    """NSE's own microstructure work puts spreads widest at the open and close
    and narrowest around 11:00-14:00. The scorer's opening-range components
    fire in the widest window of the day."""
    open_hour = spread_multiplier_at(dt.time(9, 20))
    midday = spread_multiplier_at(dt.time(12, 0))
    close = spread_multiplier_at(dt.time(15, 10))

    assert open_hour["multiplier"] > midday["multiplier"]
    assert close["multiplier"] > midday["multiplier"]
    assert midday["multiplier"] == min(SPREAD_SHAPE_BY_HOUR.values())
    assert "opening-range" in open_hour["note"]


def test_the_shape_is_labelled_a_prior_not_a_measurement():
    """A number whose provenance is invisible is how the 0.25% constant
    survived six sessions unexamined."""
    assert spread_multiplier_at(dt.time(9, 20))["basis"] == "prior"


def test_outside_the_session_there_is_no_book_to_measure():
    read = spread_multiplier_at(dt.time(3, 0))
    assert read["in_session"] is False
    assert "outside" in read["note"]


def test_a_thin_calibration_sample_keeps_the_prior():
    """Calibrating an hour on three fills fits noise and then reports it as
    'calibrated', which is worse than an honest prior."""
    out = calibrate_shape_from_fills(
        [{"hour": 9, "half_spread_pct": 0.01}] * 3)

    assert out["basis"] == "prior"
    assert out["shape"] == SPREAD_SHAPE_BY_HOUR
    assert "noise" in out["note"]


def test_enough_fills_replace_the_prior_and_report_the_sample():
    fills = (
        [{"hour": 9, "half_spread_pct": 0.020}] * 12
        + [{"hour": 12, "half_spread_pct": 0.010}] * 12
    )
    out = calibrate_shape_from_fills(fills)

    assert out["basis"] == "calibrated"
    # Normalised to the median calibrated hour, so the OPEN stays the dear one.
    assert out["shape"][9] > out["shape"][12]
    assert out["calibrated_hours"] == {9: 12, 12: 12}


def test_no_fills_leaves_the_prior_untouched():
    out = calibrate_shape_from_fills([])
    assert out["basis"] == "prior" and out["shape"] == SPREAD_SHAPE_BY_HOUR


# ============================================================ tradeability ==

def test_an_absurd_spread_is_untradeable_whatever_the_signal_says():
    book = parse_book(rest_payload(bid=10.0, ask=20.0), symbol="FARWING",
                      reference_price=15.0)
    verdict = assess_tradeability(book, quantity=75, expected_move_pct=25.0)

    assert verdict.verdict == "untradeable"
    assert verdict.tradeable is False
    assert "absence of one" in verdict.reasons[0]


def test_a_target_smaller_than_the_round_trip_is_refused():
    """The arithmetic that lost Rs 395 on a CORRECT direction: the round trip
    cost more than the move earned."""
    book = parse_book(rest_payload(bid=155.0, ask=165.0), reference_price=160.0)
    verdict = assess_tradeability(book, quantity=75, expected_move_pct=5.0)

    assert verdict.verdict == "cost exceeds target"
    assert verdict.required_move_pct > 5.0
    assert "No directional read fixes this" in verdict.reasons[-1]


def test_a_tight_book_with_a_real_target_is_tradeable():
    book = parse_book(rest_payload(bid=160.4, ask=160.6), reference_price=160.5)
    verdict = assess_tradeability(book, quantity=75, expected_move_pct=55.0)

    assert verdict.tradeable is True
    assert verdict.required_move_pct < 55.0


def test_a_thin_margin_is_named_even_when_tradeable():
    book = parse_book(rest_payload(bid=158.5, ask=161.5), reference_price=160.0)
    verdict = assess_tradeability(book, quantity=75, expected_move_pct=6.0)

    if verdict.tradeable:
        assert any("Thin margin" in r for r in verdict.reasons)


def test_breakeven_is_reported_even_without_a_target():
    book = parse_book(rest_payload(), reference_price=160.5)
    verdict = assess_tradeability(book, quantity=75)

    assert verdict.verdict == "cost measured"
    assert verdict.required_move_pct > 0
    assert "Supply the target" in verdict.reasons[-1]


def test_the_required_move_includes_fees_not_just_the_spread():
    book = parse_book(rest_payload(), reference_price=160.5)
    verdict = assess_tradeability(book, quantity=75)

    assert verdict.required_move_pct > verdict.round_trip_spread_pct, (
        "statutory charges and flat brokerage must be in the breakeven too"
    )


def test_an_unquoted_contract_has_no_verdict_to_give():
    verdict = assess_tradeability(parse_book({}), quantity=75,
                                  expected_move_pct=25.0)
    assert verdict.verdict == "no price"
    assert verdict.tradeable is False


def test_a_lot_that_does_not_fill_at_the_touch_is_called_out():
    book = parse_book(rest_payload(ask_qty=20), reference_price=160.5)
    verdict = assess_tradeability(book, quantity=75, expected_move_pct=55.0)

    assert verdict.fills_at_touch is False
    assert any("does not fill at the touch" in r for r in verdict.reasons)


# ================================================================== a chain ==

def test_a_chain_report_ranks_by_what_you_can_afford_to_enter():
    """Open interest says a strike is DEFENDED. It does not say it is cheap to
    cross. The chain builder has only ever asked the first question."""
    rows = [
        {"symbol": "ATM", "ltp": 160.0,
         "payload": rest_payload(bid=159.9, ask=160.1)},
        {"symbol": "WING", "ltp": 12.0,
         "payload": rest_payload(bid=10.0, ask=14.0)},
        {"symbol": "DEAD", "ltp": 2.0, "payload": {}},
    ]
    report = chain_spread_report(rows)

    assert report["contracts"] == 2
    assert report["unusable"] == ["DEAD"]
    assert report["tightest"]["symbol"] == "ATM"
    assert report["widest"]["symbol"] == "WING"
    assert report["untradeable_count"] == 1
    assert "defended" in report["note"]


def test_a_chain_with_no_quotes_says_so_rather_than_reporting_zero_spread():
    report = chain_spread_report([{"symbol": "A", "payload": {}}])
    assert report["contracts"] == 0
    assert "no contract" in report["note"].lower()


# ============================================ the wiring into the live path ==
#
# A library the app never calls changes nothing. These assert that the measured
# spread actually reaches the contract ranking, which is where it alters a
# decision.

import pandas as pd  # noqa: E402

from intraday_advisor.candidates import build_candidates  # noqa: E402
from intraday_advisor.nse_option_chain import (  # noqa: E402
    _fetch_market_data_angel,
)


def chain_with_spreads(atm=23_000, step=50, n=5, *, wide_strike=None):
    """A chain carrying measured bid/ask, as the live fetch now returns."""
    rows = []
    for i in range(-(n // 2), n // 2 + 1):
        strike = atm + i * step
        ce = max(2.0, 120 - i * 18)
        pe = max(2.0, 120 + i * 18)
        row = {"strike": strike, "ce_ltp": ce, "pe_ltp": pe,
               "ce_oi": 100_000, "pe_oi": 100_000}
        # One tick wide by default; one named strike quoted brutally.
        for side, ltp in (("ce", ce), ("pe", pe)):
            half = 0.05 if strike != wide_strike else ltp * 0.10
            row[f"{side}_bid"] = ltp - half
            row[f"{side}_ask"] = ltp + half
            row[f"{side}_half_spread_pct"] = half / ltp
            row[f"{side}_ask_qty"] = 500
        rows.append(row)
    return {"df": pd.DataFrame(rows), "atm": atm, "expiry": "19OCT2026",
            "spot": atm}


def test_a_candidate_carries_the_measured_spread_not_an_assumption():
    out = build_candidates("NIFTY", chain_with_spreads(), direction="CALL",
                           available_capital=200_000)

    assert out, "expected candidates"
    top = out[0]
    assert top["spread_measured"] is True
    assert top["bid"] is not None and top["ask"] is not None
    assert top["spread_pct"] > 0


def test_a_chain_without_depth_still_works_and_says_it_was_not_measured():
    """The fallback has to be graceful: the chain predates this measurement and
    a feed can stop sending depth at any time."""
    plain = {"df": pd.DataFrame([
        {"strike": 23_000, "ce_ltp": 120.0, "pe_ltp": 120.0,
         "ce_oi": 100_000, "pe_oi": 100_000},
    ]), "atm": 23_000, "expiry": "19OCT2026"}

    out = build_candidates("NIFTY", plain, direction="CALL",
                           available_capital=200_000)

    assert out
    assert out[0]["spread_measured"] is False
    assert out[0]["spread_pct"] is None, (
        "an unmeasured spread must be None, never 0 -- zero reads as free"
    )


def test_a_brutally_quoted_strike_is_ranked_below_its_tight_neighbour():
    """The behaviour that matters: two contracts identical in every respect the
    old model could see, separated only by what it cost to enter them."""
    wide = 23_050
    out = build_candidates("NIFTY", chain_with_spreads(wide_strike=wide),
                           direction="CALL", available_capital=500_000,
                           limit=20)
    by_strike = {(c["strike"], c["option_type"]): c for c in out}

    wide_call = by_strike.get((wide, "CE"))
    tight_call = by_strike.get((23_000, "CE"))
    assert wide_call and tight_call

    assert wide_call["spread_pct"] > tight_call["spread_pct"]
    assert wide_call["score"] < tight_call["score"], (
        "a 20% round-trip spread must cost the contract its ranking"
    )
    assert any("Spread is" in n or "Wide book" in n for n in wide_call["notes"])


def test_the_measured_spread_reaches_the_cost_model_not_just_the_notes():
    """Scoring it is cosmetic; sizing with it is the point. A wider book must
    raise the breakeven the position has to clear."""
    tight = build_candidates("NIFTY", chain_with_spreads(), direction="CALL",
                             available_capital=500_000, limit=20)
    wide = build_candidates("NIFTY", chain_with_spreads(wide_strike=23_000),
                            direction="CALL", available_capital=500_000,
                            limit=20)

    def atm_call(rows):
        return next(c for c in rows
                    if c["strike"] == 23_000 and c["option_type"] == "CE")

    assert atm_call(wide)["breakeven"] > atm_call(tight)["breakeven"]
    assert atm_call(wide)["cost_drag"] > atm_call(tight)["cost_drag"]


def test_a_lot_larger_than_the_touch_is_penalised():
    chain = chain_with_spreads()
    chain["df"]["ce_ask_qty"] = 10          # far below a 75-unit NIFTY lot
    out = build_candidates("NIFTY", chain, direction="CALL",
                           available_capital=500_000, limit=20)
    call = next(c for c in out if c["option_type"] == "CE")

    assert any("walk the book" in n for n in call["notes"])


# ----------------------------------------------------- the feed parse itself --

class FakeSmart:
    """Stands in for SmartConnect, returning one FULL-mode payload."""

    def __init__(self, payload):
        self.payload = payload

    def getMarketData(self, mode, tokens):
        assert mode == "FULL"
        return {"status": True, "data": {"fetched": [self.payload]}}


def test_the_live_fetch_now_keeps_the_book_it_used_to_discard():
    smart = FakeSmart({
        "symbolToken": "12345", "ltp": 160.5, "opnInterest": 900_000,
        "depth": {
            "buy": [{"price": 160.0, "quantity": 300, "orders": 4}],
            "sell": [{"price": 161.0, "quantity": 250, "orders": 3}],
        },
    })
    out = _fetch_market_data_angel(["12345"], smart)

    assert out["12345"]["oi"] == 900_000
    assert out["12345"]["bid"] == 160.0
    assert out["12345"]["ask"] == 161.0
    assert out["12345"]["ask_qty"] == 250
    assert out["12345"]["half_spread_pct"] == pytest.approx(1.0 / 2 / 160.5)


def test_a_feed_that_stops_sending_depth_does_not_take_the_chain_down():
    smart = FakeSmart({"symbolToken": "9", "ltp": 12.0, "opnInterest": 5_000})
    out = _fetch_market_data_angel(["9"], smart)

    assert out["9"]["ltp"] == 12.0, "LTP and OI must survive a missing book"
    assert out["9"]["half_spread_pct"] is None
    assert out["9"]["bid"] is None


def test_unreadable_depth_is_recorded_rather_than_crashing():
    smart = FakeSmart({"symbolToken": "9", "ltp": 12.0,
                       "depth": {"buy": "not a list", "sell": None}})
    out = _fetch_market_data_angel(["9"], smart)

    assert out["9"]["ltp"] == 12.0
    assert out["9"]["half_spread_pct"] is None


def test_the_time_of_day_prior_is_applied_only_when_nothing_was_measured():
    """A measured book already IS the book at this hour. Multiplying it by a
    prior about what books look like at this hour charges the effect twice."""
    import datetime as _dt

    measured = cost_model_from_book(
        parse_book(rest_payload(bid=159.0, ask=161.0), reference_price=160.0),
        moment=_dt.time(9, 20))

    assert measured["measured"] is True
    # The hour is reported for comparison, and NOT multiplied in.
    assert measured["time_of_day"]["multiplier"] > 1
    assert measured["cost_model"].half_spread_pct == pytest.approx(0.00625)


def test_an_unmeasured_spread_is_at_least_put_in_the_right_hour():
    import datetime as _dt

    at_open = cost_model_from_book(parse_book({}), moment=_dt.time(9, 20))
    at_midday = cost_model_from_book(parse_book({}), moment=_dt.time(12, 0))
    flat = cost_model_from_book(parse_book({}))

    assert at_open["measured"] is False
    assert (at_open["cost_model"].half_spread_pct
            > at_midday["cost_model"].half_spread_pct)
    assert at_open["cost_model"].half_spread_pct > flat["cost_model"].half_spread_pct
    assert "Scaled to" in at_open["note"]
    assert "assumption, not a measurement" in at_open["note"]
