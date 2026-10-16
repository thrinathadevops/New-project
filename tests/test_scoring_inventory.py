"""Keep the scorer, the ledger and the weights in sync.

The failure these guard against already happened once: the indicator set grew
from 5 engines to 39 and the scorer grew to read 89 columns, while the ledger
went on recording a hand-maintained list of 17. Everything *looked* fine --
scores moved, components appeared on screen -- but 32 of the 37 weights could
never be fitted, because their inputs were never written down. A weight you
cannot calibrate is a guess you are permanently stuck with.

Nothing warns you about that at runtime. These tests do.
"""

from __future__ import annotations

import pathlib
import re

from intraday_advisor.ledger.recorder import FEATURE_KEYS
from intraday_advisor.scoring import SCORED_FEATURES, ScoreWeights

SCORING_SOURCE = pathlib.Path(
    __file__
).resolve().parent.parent / "intraday_advisor" / "scoring.py"


def features_read_by_the_source() -> set[str]:
    """Re-derive, from the code itself, every raw column the scorer reads."""
    source = SCORING_SOURCE.read_text(encoding="utf-8")
    # Only the body, so the SCORED_FEATURES literal cannot validate itself.
    body = source.split("SCORED_FEATURES")[0]
    return set(
        re.findall(r'_truthy\(row,\s*"([A-Za-z_0-9]+)"\)', body)
        + re.findall(r'row\.get\(\s*"([A-Za-z_0-9]+)"', body)
    )


def test_scored_features_matches_the_code():
    """If someone adds a feature to the scorer, it must be declared here too --
    otherwise it silently becomes uncalibratable."""
    actual = features_read_by_the_source()
    declared = set(SCORED_FEATURES)

    undeclared = actual - declared
    stale = declared - actual

    assert not undeclared, (
        f"These columns are scored but missing from SCORED_FEATURES, so the "
        f"ledger will not record them and their weights can never be fitted: "
        f"{sorted(undeclared)}"
    )
    assert not stale, (
        f"These are declared in SCORED_FEATURES but no longer read by the "
        f"scorer: {sorted(stale)}"
    )


def test_every_scored_feature_is_recorded():
    """The point of the ledger is to measure what the scorer scored."""
    unrecorded = set(SCORED_FEATURES) - set(FEATURE_KEYS)
    assert not unrecorded, (
        f"Scored but not recorded, so not calibratable: {sorted(unrecorded)}"
    )


def test_the_inventory_actually_grew_past_the_original_seventeen():
    """A regression guard with a number in it, because the original bug was
    exactly 'the list stopped being updated'."""
    assert len(SCORED_FEATURES) > 50
    assert len(FEATURE_KEYS) > 50


def test_context_columns_are_recorded_even_though_unscored():
    """Price, volume and volatility are needed to interpret any later finding,
    even where no weight reads them."""
    for column in ("Close", "Volume", "ATR14", "VWAP", "ADTV20"):
        assert column in FEATURE_KEYS


def test_no_duplicate_feature_keys():
    assert len(FEATURE_KEYS) == len(set(FEATURE_KEYS))


def test_weight_count_is_plausible_for_the_feature_count():
    """A sanity check on proportion: dozens of features scored through a
    handful of weights would mean most indicators are decorative."""
    weights = list(ScoreWeights().__dataclass_fields__)
    assert len(weights) >= 30, "weights have not kept up with the indicator set"


def test_calibration_gate_scales_with_the_weight_count():
    """More weights need more data, not the same amount.

    Fitting 37 weights on the 200-trade minimum is roughly 5 trades per
    parameter, which overfits. This records the expectation so the gate is
    revisited deliberately rather than forgotten.
    """
    from intraday_advisor.ledger.report import MIN_SAMPLE_FOR_CALIBRATION

    weights = len(ScoreWeights().__dataclass_fields__)
    trades_per_weight = MIN_SAMPLE_FOR_CALIBRATION / weights
    assert trades_per_weight >= 5, (
        f"Only {trades_per_weight:.1f} graded trades per weight at the current "
        f"gate of {MIN_SAMPLE_FOR_CALIBRATION}. With {weights} weights that is "
        f"curve-fitting, not calibration."
    )
