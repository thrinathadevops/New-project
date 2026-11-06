"""A catalogue of market strategies, each one measurable on the same terms.

Why a registry and not more scoring components
----------------------------------------------
``scoring.py`` holds 37 weights over 32 price components and sums them into one
number. Measured over **7 sessions and 524 graded trades** on the repaired
ledger, ``indicator_scorecard`` finds the trusted list **empty**: every one of
the 37 components changes sign somewhere, ``fvg`` positive in 5 of 7 sessions,
``squeeze`` in 3 of 6, ``gap_signal`` in 1 of 4. ``focused_score``, built from
the three that looked best, was then **falsified out of sample** -- in-sample
+0.130 against the full score's +0.023, out-of-sample -0.026 against +0.052,
flipping sign session to session.

(An earlier cut of this study reported six trusted components with five of them
inverted and per-part correlations near +/-0.40. That sample was inflated: the
ledger had stored each bar twice under two timezone spellings, so a UTC and an
IST string for the same instant both passed a UNIQUE key built from the string.
A doubled sample halves every standard error. Those figures are superseded and
should not be requoted.)

So the components are not a set of signals waiting to be weighted correctly.
Adding a strategy to that sum would make it one term more of something already
measured to carry nothing.

So strategies are registered here instead, as **separately measurable objects**.
Each one declares what it bets on, produces a direction per bar, and is scored
by ``validation.evaluate_signal`` -- the same evaluator, with the same overlap
corrections, that withdrew this project's own headline finding. A strategy's
verdict is therefore comparable with every other strategy's, which is the whole
reason to have a catalogue rather than a pile.

**Nothing here is enabled by trading it.** :func:`screen_all` returns verdicts.
A strategy that passes ``validation.validate_rule`` has earned a forward test,
not capital.

What the families are, and which were missing
---------------------------------------------
Before this, every trade the app could express was *buy one option, because a
trend score was high*. One family, one structure, one direction. These are the
families a desk actually chooses between:

====================  ==============================================
family                what it bets on
====================  ==============================================
``trend``             the move continues
``mean_reversion``    the move is overdone
``breakout``          a range resolves, and holds
``volatility``        the size of the move, not its direction
``premium``           implied volatility exceeds what gets realised
``relative_value``    one instrument against another, market-neutral
``structure``         where business was done (volume profile, levels)
``flow``              positioning: open interest, depth, participants
``event``             a scheduled catalyst, or the absence of one
====================  ==============================================

``trend``, ``breakout`` and some of ``mean_reversion`` already existed inside
the scorer. ``volatility``, ``premium``, ``relative_value``, ``structure`` and
``flow`` had no representation at all -- and the measurements point at exactly
those: direction is close to unforecastable here (lag-1 autocorrelation about
-0.09; 41 of 50 names indistinguishable from random walks on daily bars) while
variance is persistent and forecastable, which is what ``vol_forecast`` is for.

The honest expectation
----------------------
Most of these will fail. The standing conclusion of this project is that **no
rule discovered in it clears its own transaction costs**, and a catalogue does
not change the arithmetic -- a 0.1% edge against a 0.19-1.25% round trip loses
whatever its name is. What a catalogue does is make the failures *legible*: a
strategy that fails on costs is a different problem from one that fails on
significance, and only the second is hopeless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

#: Families, in the order a desk would consider them.
FAMILIES = (
    "trend", "mean_reversion", "breakout", "volatility", "premium",
    "relative_value", "structure", "flow", "event",
)

#: What a strategy is traded with. This decides the cost gate, and the cost
#: gate decides nearly everything: the same signal clears on futures (0.03%)
#: and loses on a one-lot option (0.50-1.25%).
INSTRUMENTS = ("equity", "futures", "option_1lot", "option_spread")


@dataclass
class Strategy:
    """One strategy: what it bets on, how to read it, and what would kill it.

    ``signal`` takes a prepared frame and returns a per-bar direction
    (+1 long, -1 short, 0 flat). It must be **causal**: bar *i*'s value may
    only use information available at the close of bar *i*. Everything in this
    module is scored on forward returns measured from that close, so a signal
    that peeks forward will report a spectacular edge and is the single easiest
    way to fool this harness.
    """

    name: str
    family: str
    thesis: str
    #: The direction-per-bar function.
    signal: Callable[[pd.DataFrame], "pd.Series"]
    #: Bars held. The overlap correction scales with this, so a longer hold
    #: needs a proportionally larger sample to say anything.
    horizon: int = 12
    instrument: str = "option_1lot"
    #: Where this strategy is known or expected to fail. A strategy without a
    #: falsifier is a horoscope -- the same standard `regime.py` is held to.
    falsified_by: str = ""
    #: Set where the strategy needs data this project does not yet have, so the
    #: catalogue can list it honestly instead of silently returning no signal.
    requires: tuple = ()
    source: str = ""

    def __post_init__(self):
        if self.family not in FAMILIES:
            raise ValueError(
                f"{self.name}: unknown family {self.family!r}; "
                f"expected one of {FAMILIES}"
            )
        if self.instrument not in INSTRUMENTS:
            raise ValueError(
                f"{self.name}: unknown instrument {self.instrument!r}; "
                f"expected one of {INSTRUMENTS}"
            )
        if self.horizon < 1:
            raise ValueError(f"{self.name}: horizon must be at least 1 bar")
        if not self.falsified_by:
            raise ValueError(
                f"{self.name}: every strategy must declare what would falsify "
                f"it. A read you cannot falsify is a horoscope."
            )

    def as_dict(self) -> dict:
        return {
            "name": self.name, "family": self.family, "thesis": self.thesis,
            "horizon": self.horizon, "instrument": self.instrument,
            "falsified_by": self.falsified_by, "requires": list(self.requires),
            "source": self.source,
        }


#: name -> Strategy. Populated by ``catalogue.py`` at import.
REGISTRY: dict[str, Strategy] = {}


def register(strategy: Strategy) -> Strategy:
    """Add a strategy, refusing a silent overwrite.

    Two strategies under one name would mean the second quietly replaced the
    first and its measurement was attributed to the wrong thesis.
    """
    if strategy.name in REGISTRY:
        raise ValueError(
            f"{strategy.name!r} is already registered. Pick a distinct name -- "
            f"a replaced strategy would be measured under the other's thesis."
        )
    REGISTRY[strategy.name] = strategy
    return strategy


def load_catalogue() -> dict[str, Strategy]:
    """Import the catalogue and return the registry."""
    from . import catalogue  # noqa: F401  -- import registers
    return REGISTRY


def describe_catalogue(family: str | None = None) -> list[dict]:
    """The catalogue as plain data, for a report or the dashboard."""
    load_catalogue()
    rows = [s.as_dict() for s in REGISTRY.values()
            if family is None or s.family == family]
    return sorted(rows, key=lambda r: (FAMILIES.index(r["family"]), r["name"]))
