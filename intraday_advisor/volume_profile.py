"""Where the volume actually traded, rather than where the price went.

The gap this closes
-------------------
The indicator set has 39 engines and every one of them reads price *through
time*: an EMA, an ADX, a SuperTrend, a candle pattern. None of them asks the
question a market-profile trader asks first -- **at which prices did business
actually get done?**

That is a different axis. A session that opens at 23,000, spikes to 23,200 on
four thousand contracts and spends four hours grinding at 23,050 on two million
has a high of 23,200 that almost nobody traded. Every high/low-based level in
this repo -- ORB boundaries, previous-day high/low, Donchian, structure levels
-- treats that spike as equal in standing to the shelf. Volume profile does not,
and that difference is the whole point of it.

What it produces
----------------
* **POC** (point of control) -- the single price with the most volume. The
  session's centre of gravity, and the level price returns to most often.
* **Value area** (VAH / VAL) -- the narrowest price band containing 70% of the
  session's volume. Inside it, the market has agreed; outside it, it has not.
* **Low-volume nodes** -- prices that were crossed quickly. Price tends to move
  through them fast rather than stall, which makes them poor targets and poor
  stops.

Why 70%
-------
It is one standard deviation of a normal distribution, and the convention is
inherited from Market Profile rather than derived from Indian data. It is a
parameter here (``value_area_pct``) and labelled as a convention, because an
inherited constant presented as a law is how this project ended up with a flat
0.25% spread assumption it never questioned.

The honest limit, stated up front
---------------------------------
**Index candles carry no real volume.** ``^NSEI`` and ``^NSEBANK`` report
volume as 0 or as a broken figure from Yahoo, which is exactly why
``research_score_predictive_power.py`` passes ``vwap_relation=None`` for the
indices and why the VWAP/MFI/OBV components contribute nothing there. A profile
built from those bars would be a profile of nothing.

So :func:`build_profile` **refuses** to produce a profile when volume is absent
or constant, rather than returning a shape computed from a column of zeros.
Where it is genuinely useful: the 50 Nifty constituents (100% volume coverage,
per the daily-equity study), and option contracts via the tick feed.

This module computes levels. It does **not** score them. Whether value-area
rules carry an edge on NSE is an empirical question, and
``strategies/catalogue.py`` registers them so that question gets measured
through ``validation.validate_rule`` instead of assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: Share of session volume defining the value area. A Market Profile
#: convention (one standard deviation), not a measured property of NSE.
DEFAULT_VALUE_AREA_PCT = 0.70

#: Price buckets the session range is divided into. Too few and the POC is
#: meaningless; too many and every bucket holds one bar. 50 keeps a NIFTY
#: session's ~300-point range in roughly 6-point buckets.
DEFAULT_BINS = 50

#: Below this many bars there is no distribution to speak of.
MIN_BARS = 10


@dataclass
class VolumeProfile:
    """A session's traded-volume distribution and the levels it implies."""

    poc: float | None = None
    value_area_high: float | None = None
    value_area_low: float | None = None
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT
    total_volume: float = 0.0
    bins: int = 0
    bucket_width: float = 0.0
    #: (price, volume) per bucket, low price first.
    histogram: list = field(default_factory=list)
    #: Buckets with unusually little volume -- price crossed them quickly.
    low_volume_nodes: list = field(default_factory=list)
    usable: bool = False
    notes: list[str] = field(default_factory=list)

    def position_of(self, price: float) -> dict:
        """Where a price sits relative to the value area.

        This is the read that matters: inside the value area the market has
        agreed and mean reversion is the base case; outside it, either the
        market is discovering a new level or the move is about to fail back in.
        Which of those it is cannot be told from the profile alone, and this
        says so rather than guessing.
        """
        if not self.usable or self.value_area_high is None:
            return {"zone": "unknown", "note": "No usable profile."}

        if price > self.value_area_high:
            return {
                "zone": "above_value",
                "distance": round(price - self.value_area_high, 2),
                "note": (
                    f"Price is above the value area high "
                    f"({self.value_area_high:,.0f}). Either value is migrating "
                    f"up or this is an excursion that fails back inside. The "
                    f"profile cannot tell you which; acceptance (time spent "
                    f"out here) is what distinguishes them."
                ),
            }
        if price < self.value_area_low:
            return {
                "zone": "below_value",
                "distance": round(self.value_area_low - price, 2),
                "note": (
                    f"Price is below the value area low "
                    f"({self.value_area_low:,.0f}). Same ambiguity as above, "
                    f"mirrored."
                ),
            }
        above_poc = self.poc is not None and price > self.poc
        return {
            "zone": "inside_value",
            "distance": round(abs(price - self.poc), 2) if self.poc else None,
            "note": (
                f"Price is inside the value area "
                f"({self.value_area_low:,.0f}-{self.value_area_high:,.0f}), "
                f"{'above' if above_poc else 'below'} the POC at "
                f"{self.poc:,.0f}. Inside value, the POC is the magnet and a "
                f"breakout is the less likely of the two outcomes."
            ),
        }

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _volume_is_real(volume: "pd.Series") -> tuple[bool, str]:
    """Is this a volume column, or a column of zeros wearing the name?

    Index candles from Yahoo report volume as 0 or as a constant, and a profile
    built from that is a profile of the price range alone -- which looks
    plausible, carries no information, and cannot be told apart from a real one
    by eye. That is the failure this guard exists for.
    """
    clean = volume.dropna()
    if clean.empty:
        return False, "The Volume column is entirely missing or NaN."
    total = float(clean.sum())
    if total <= 0:
        return False, (
            "Total volume is zero. Index candles (^NSEI / ^NSEBANK) report no "
            "volume, so there is no distribution to build -- this is the same "
            "reason the VWAP and MFI components contribute nothing on indices."
        )
    if float(clean.nunique()) <= 1:
        return False, (
            f"Every bar reports the same volume ({clean.iloc[0]:,.0f}), which "
            f"is a placeholder rather than a measurement. A profile from it "
            f"would just redraw the price range."
        )
    non_zero = float((clean > 0).mean())
    if non_zero < 0.5:
        return False, (
            f"Only {non_zero:.0%} of bars carry volume. Too sparse for a "
            f"distribution."
        )
    return True, ""


def build_profile(
    frame: pd.DataFrame, *, bins: int = DEFAULT_BINS,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
) -> VolumeProfile:
    """Build the traded-volume distribution for ``frame``.

    Each bar's volume is spread **evenly across its own high-low range** rather
    than dumped at its close. A 5-minute bar that travelled 40 points did not
    trade all its volume at one price, and assigning it there would invent a
    spike at every close. Even spreading is still an approximation -- only tick
    data knows the truth -- but it is the approximation that does not
    manufacture structure.
    """
    profile = VolumeProfile(value_area_pct=float(value_area_pct))

    needed = ("High", "Low", "Volume")
    missing = [c for c in needed if c not in frame.columns]
    if missing:
        profile.notes.append(f"Missing columns: {missing}.")
        return profile
    if len(frame) < MIN_BARS:
        profile.notes.append(
            f"{len(frame)} bars is below the {MIN_BARS} needed for a "
            f"distribution."
        )
        return profile
    if not 0.3 <= value_area_pct <= 0.95:
        raise ValueError("value_area_pct must be between 0.3 and 0.95")
    if bins < 5:
        raise ValueError("bins must be at least 5")

    real, why = _volume_is_real(frame["Volume"])
    if not real:
        profile.notes.append(why)
        return profile

    highs = frame["High"].astype(float).to_numpy()
    lows = frame["Low"].astype(float).to_numpy()
    volumes = frame["Volume"].astype(float).fillna(0.0).to_numpy()

    top, bottom = float(np.nanmax(highs)), float(np.nanmin(lows))
    if not np.isfinite(top) or not np.isfinite(bottom) or top <= bottom:
        profile.notes.append(
            "The session has no range (high equals low), so there is nothing "
            "to distribute."
        )
        return profile

    edges = np.linspace(bottom, top, bins + 1)
    centres = (edges[:-1] + edges[1:]) / 2.0
    buckets = np.zeros(bins, dtype=float)
    width = float(edges[1] - edges[0])

    for high, low, volume in zip(highs, lows, volumes):
        if not (np.isfinite(high) and np.isfinite(low)) or volume <= 0:
            continue
        # Which buckets this bar touched, and how much of each.
        first = int(np.clip(np.searchsorted(edges, low, "right") - 1, 0, bins - 1))
        last = int(np.clip(np.searchsorted(edges, high, "left") - 1, 0, bins - 1))
        if last < first:
            first, last = last, first
        touched = last - first + 1
        buckets[first:last + 1] += volume / touched

    total = float(buckets.sum())
    if total <= 0:
        profile.notes.append("No volume landed in any bucket.")
        return profile

    profile.bins = bins
    profile.bucket_width = round(width, 4)
    profile.total_volume = round(total, 2)
    profile.histogram = [
        (round(float(p), 2), round(float(v), 2))
        for p, v in zip(centres, buckets)
    ]

    poc_index = int(np.argmax(buckets))
    profile.poc = round(float(centres[poc_index]), 2)

    # --- the value area ---------------------------------------------------- #
    # Grown outward from the POC, taking whichever neighbouring bucket holds
    # more volume, until the target share is covered. This is the standard
    # Market Profile construction and it matters that it grows from the POC
    # rather than taking the densest buckets anywhere: the value area is a
    # contiguous band by definition, and the discontiguous version would
    # produce a "range" with holes in it.
    target = total * value_area_pct
    low_i = high_i = poc_index
    covered = buckets[poc_index]
    while covered < target and (low_i > 0 or high_i < bins - 1):
        below = buckets[low_i - 1] if low_i > 0 else -1.0
        above = buckets[high_i + 1] if high_i < bins - 1 else -1.0
        if above >= below:
            high_i += 1
            covered += buckets[high_i]
        else:
            low_i -= 1
            covered += buckets[low_i]

    profile.value_area_low = round(float(edges[low_i]), 2)
    profile.value_area_high = round(float(edges[high_i + 1]), 2)

    # --- low-volume nodes -------------------------------------------------- #
    # Prices crossed quickly. Useful as the places NOT to put a target or a
    # stop: price does not linger there, so a limit order resting in one is
    # likely to be passed rather than filled on a retest.
    occupied = buckets[buckets > 0]
    if occupied.size:
        threshold = float(np.median(occupied)) * 0.25
        profile.low_volume_nodes = [
            round(float(centres[i]), 2)
            for i in range(bins)
            if 0 < buckets[i] < threshold
        ]

    profile.usable = True
    share_in_va = float(buckets[low_i:high_i + 1].sum() / total)
    profile.notes.append(
        f"POC {profile.poc:,.2f}; value area "
        f"{profile.value_area_low:,.2f}-{profile.value_area_high:,.2f} holds "
        f"{share_in_va:.0%} of volume across {high_i - low_i + 1} of {bins} "
        f"buckets ({profile.bucket_width:.2f} wide each)."
    )
    if share_in_va > value_area_pct + 0.08:
        profile.notes.append(
            "The value area overshoots its target share because one bucket is "
            "large relative to the total -- a thin session, or too few bins "
            "for this range."
        )
    return profile


def session_profiles(
    frame: pd.DataFrame, *, bins: int = DEFAULT_BINS,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
) -> dict:
    """One profile per calendar session, newest last.

    A profile is a *session* object. Pooling several days produces a composite
    that describes none of them and smears the POC across whatever range the
    week happened to cover -- and the levels other traders are watching are
    yesterday's, not last week's average.
    """
    if not isinstance(frame.index, pd.DatetimeIndex):
        return {"sessions": {}, "note": "Index is not datetime; cannot split."}

    out: dict = {"sessions": {}}
    for day, chunk in frame.groupby(pd.DatetimeIndex(frame.index).date):
        profile = build_profile(chunk, bins=bins, value_area_pct=value_area_pct)
        out["sessions"][str(day)] = profile
    usable = [p for p in out["sessions"].values() if p.usable]
    out["usable_sessions"] = len(usable)
    out["note"] = (
        f"{len(usable)} of {len(out['sessions'])} sessions produced a usable "
        f"profile."
        + ("" if usable else
           " None did -- most likely an index series, which carries no volume.")
    )
    return out


def naked_poc_levels(frame: pd.DataFrame, *, lookback_sessions: int = 10,
                     bins: int = DEFAULT_BINS) -> dict:
    """Past POCs that price has not since traded back to.

    A "naked" or virgin POC is a prior session's point of control that has
    never been revisited. The reasoning traders give is that the level holds
    unfinished business and acts as a magnet; whether it does on NSE is exactly
    the kind of claim this project measures rather than repeats, so this returns
    the levels and no signal.
    """
    profiles = session_profiles(frame, bins=bins)
    sessions = profiles.get("sessions", {})
    if not sessions:
        return {"levels": [], "note": profiles.get("note", "No sessions.")}

    ordered = sorted(sessions.items())[-(lookback_sessions + 1):]
    if len(ordered) < 2:
        return {"levels": [], "note": "Need at least two sessions."}

    naked = []
    for i, (day, profile) in enumerate(ordered[:-1]):
        if not profile.usable or profile.poc is None:
            continue
        # Did any LATER session trade through this POC?
        later = ordered[i + 1:]
        touched = False
        for later_day, _ in later:
            chunk = frame[pd.DatetimeIndex(frame.index).date ==
                          pd.Timestamp(later_day).date()]
            if chunk.empty:
                continue
            if (float(chunk["Low"].min()) <= profile.poc
                    <= float(chunk["High"].max())):
                touched = True
                break
        if not touched:
            naked.append({"session": day, "poc": profile.poc})

    return {
        "levels": naked,
        "note": (
            f"{len(naked)} naked POC(s) in the last {len(ordered) - 1} "
            f"sessions. Levels only -- whether they attract price is "
            f"unmeasured, and strategies/catalogue.py registers the claim so "
            f"it can be tested."
        ),
    }
