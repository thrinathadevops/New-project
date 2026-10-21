"""The data contract the on-chart trade ticket depends on.

The ticket and the level overlay are drawn in canvas from fields on the signal
dict. Canvas drawing cannot be unit-tested here, but the contract can: if a
refactor renames or drops one of these fields the chart silently loses the
instruction, with no error anywhere. These tests fail instead.
"""

from __future__ import annotations

import re
import pathlib

import pytest

APP_JS = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "app.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


# ------------------------------------------------- fields the overlay reads --

#: Index-point levels the overlay draws as lines, and where they come from.
LEVEL_FIELDS = ("strike", "pe_wall", "ce_wall")
#: Premium fields the ticket prints (never drawn as lines -- wrong units).
PREMIUM_FIELDS = ("entry_premium", "stop_premium", "target_premium")
#: Plan fields the ticket prints.
PLAN_FIELDS = ("lots", "lot_size", "deployed_capital", "risk_pct_of_capital",
               "breakeven", "capital_per_lot", "affordable",
               "spendable_capital")


def test_the_engine_publishes_every_level_the_overlay_draws():
    from intraday_advisor.index_options import IndexOptionSignal

    published = set(IndexOptionSignal.__dataclass_fields__)
    for field in LEVEL_FIELDS + PREMIUM_FIELDS:
        assert field in published, f"the chart ticket reads signal.{field}"


def test_the_plan_publishes_every_field_the_ticket_prints():
    from intraday_advisor.ledger.sizing import PositionPlan

    published = set(PositionPlan.__dataclass_fields__)
    for field in PLAN_FIELDS:
        assert field in published, f"the chart ticket reads plan.{field}"


def test_the_regime_publishes_prev_close():
    """The overlay marks yesterday's close, the line the day is measured
    against."""
    from intraday_advisor.regime import key_levels
    import pandas as pd

    daily = pd.DataFrame(
        {"Open": [100.0, 102.0], "High": [103.0, 104.0],
         "Low": [99.0, 101.0], "Close": [102.0, 103.0], "Volume": [1, 1]},
        index=pd.date_range("2026-10-20", periods=2, freq="D"),
    )
    assert "prev_close" in key_levels(daily, None)


# ------------------------------------------- units discipline on the canvas --

def test_premiums_are_never_drawn_as_price_lines(app_js: str):
    """The y-axis is the INDEX; entry/stop/target are option PREMIUMS. Putting
    a Rs 180 premium on a 22,750-point axis is a category error, so only
    overlayLevels() may feed the line drawing."""
    body = _function_body(app_js, "overlayLevels")
    for field in PREMIUM_FIELDS:
        assert field not in body, (
            f"overlayLevels must not place {field} on the index axis"
        )


def test_the_overlay_draws_the_levels_it_promises(app_js: str):
    body = _function_body(app_js, "overlayLevels")
    for field in LEVEL_FIELDS:
        assert field in body, f"overlayLevels should mark signal.{field}"


# --------------------------------------------------- accessibility contract --

def test_the_instruction_exists_as_text_not_only_pixels(app_js: str):
    """The chart is <canvas role="img">, so a pixels-only instruction is
    invisible to screen readers, to the table view and to anyone who cannot
    separate the colours."""
    assert "function chartGuideText" in app_js
    assert "setAttribute('aria-label'" in app_js
    guide = _function_body(app_js, "chartGuideText")
    assert "What to buy" in guide
    assert "overlayLevels(signal)" in guide, \
        "the text twin must list the same levels the chart draws"


def test_canvas_text_alignment_is_set_explicitly(app_js: str):
    """textAlign is canvas-global and drawGrid leaves it on 'right'; save()
    preserves the inherited value rather than resetting it. Both overlay
    painters must set it or their labels run off the left edge -- which is
    exactly what shipped the first time."""
    for name in ("drawLevels", "drawTicket"):
        body = _function_body(app_js, name)
        assert "textAlign = 'left'" in body, f"{name} must set textAlign"


def test_the_ticket_does_not_restate_the_risk_the_plan_already_shows(app_js: str):
    """The agent verdict is cached per candle while the plan re-prices every
    snapshot, so the agent's own risk percentage goes stale. The panel showed
    one fact twice with two different numbers."""
    body = _function_body(app_js, "ticketContent")
    assert "account|capital" in body, \
        "stale duplicate risk claims must be filtered out of the ticket"


# ------------------------------------------------------------------ helpers --

def _function_body(source: str, name: str) -> str:
    """The text of a top-level `function name(...) { ... }`, brace-matched."""
    match = re.search(rf"\bfunction {re.escape(name)}\s*\(", source)
    assert match, f"function {name} not found in app.js"
    start = source.index("{", match.end() - 1)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_the_helper_can_find_a_known_function(app_js: str):
    """A guard on the guard: if _function_body silently returned '' every
    assertion above would pass vacuously."""
    body = _function_body(app_js, "overlayLevels")
    assert len(body) > 200
    assert body.startswith("{") and body.endswith("}")
