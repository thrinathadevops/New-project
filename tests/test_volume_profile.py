"""Where volume traded, and the refusal to pretend when it did not.

The failure mode this guards is quiet. A volume profile built from a column of
zeros still produces a POC and a value area -- they just describe the price
range rather than any distribution, and nothing about the output looks wrong.
Index candles (``^NSEI`` / ``^NSEBANK``) report exactly that, which is why the
predictive study passes ``vwap_relation=None`` for indices and why VWAP, MFI
and OBV contribute nothing there.

So the first group of tests is about refusing, not computing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_advisor.volume_profile import (
    DEFAULT_VALUE_AREA_PCT,
    MIN_BARS,
    build_profile,
    naked_poc_levels,
    session_profiles,
)


def frame(volumes, highs=None, lows=None, start="2026-09-01 09:15"):
    n = len(volumes)
    base = np.linspace(23_000, 23_100, n)
    high = base + 5 if highs is None else np.asarray(highs, dtype=float)
    low = base - 5 if lows is None else np.asarray(lows, dtype=float)
    return pd.DataFrame(
        {"Open": base, "High": high, "Low": low, "Close": base,
         "Volume": np.asarray(volumes, dtype=float)},
        index=pd.date_range(start, periods=n, freq="5min"),
    )


def concentrated(n: int = 120, centre: float = 23_000.0,
                 spike_at: float = 23_200.0) -> pd.DataFrame:
    """Most volume at ``centre``, a thin excursion up to ``spike_at``.

    The shape the whole method exists for: a high nobody traded. Every
    high/low-based level in the repo treats that spike as equal in standing to
    the shelf; a profile does not.
    """
    rows = []
    for i in range(n):
        if i % 20 == 0:                       # brief excursion, tiny volume
            price, volume = spike_at, 100.0
        else:                                 # the shelf, heavy volume
            price, volume = centre + (i % 3) * 2.0, 50_000.0
        rows.append({"Open": price, "High": price + 2, "Low": price - 2,
                     "Close": price, "Volume": volume})
    return pd.DataFrame(
        rows, index=pd.date_range("2026-09-01 09:15", periods=n, freq="5min"))


# ================================================================== refusing ==

def test_zero_volume_is_refused_rather_than_profiled():
    profile = build_profile(frame([0.0] * 60))

    assert profile.usable is False
    assert profile.poc is None
    assert any("Index candles" in n for n in profile.notes)


def test_a_constant_volume_placeholder_is_refused():
    """A constant is a placeholder, not a measurement, and a profile from it
    would just redraw the price range."""
    profile = build_profile(frame([1_000.0] * 60))

    assert profile.usable is False
    assert any("placeholder" in n for n in profile.notes)


def test_a_mostly_empty_volume_column_is_refused():
    volumes = [0.0] * 50 + [5_000.0, 9_000.0] * 5
    profile = build_profile(frame(volumes))

    assert profile.usable is False
    assert any("sparse" in n for n in profile.notes)


def test_too_few_bars_is_refused():
    profile = build_profile(frame(list(np.linspace(1, 100, MIN_BARS - 1))))

    assert profile.usable is False
    assert any(str(MIN_BARS) in n for n in profile.notes)


def test_missing_columns_are_named():
    bare = pd.DataFrame({"Close": [1.0] * 40})
    profile = build_profile(bare)

    assert profile.usable is False
    assert any("Missing columns" in n for n in profile.notes)


def test_a_rangeless_session_is_refused():
    flat = frame(list(np.linspace(1_000, 9_000, 40)),
                 highs=[23_000.0] * 40, lows=[23_000.0] * 40)
    profile = build_profile(flat)

    assert profile.usable is False
    assert any("no range" in n for n in profile.notes)


def test_absurd_parameters_are_refused_loudly():
    good = concentrated()
    with pytest.raises(ValueError, match="value_area_pct"):
        build_profile(good, value_area_pct=0.99)
    with pytest.raises(ValueError, match="bins"):
        build_profile(good, bins=2)


# ================================================================= computing ==

def test_the_poc_lands_where_the_volume_is_not_where_the_high_is():
    """The whole point of the method, as one assertion."""
    profile = build_profile(concentrated(centre=23_000.0, spike_at=23_200.0))

    assert profile.usable
    assert profile.poc == pytest.approx(23_000, abs=25), (
        f"POC at {profile.poc} should sit on the heavy shelf, not the spike"
    )
    assert profile.value_area_high < 23_150, (
        "the thin excursion must fall OUTSIDE the value area"
    )


def test_the_value_area_holds_about_the_requested_share():
    profile = build_profile(concentrated(), value_area_pct=0.70)

    assert profile.usable
    total = sum(v for _, v in profile.histogram)
    inside = sum(
        v for p, v in profile.histogram
        if profile.value_area_low <= p <= profile.value_area_high
    )
    assert inside / total >= 0.70
    assert profile.value_area_pct == 0.70


def test_the_value_area_is_contiguous_by_construction():
    """A discontiguous "range" with holes in it is not a range. The value area
    grows outward from the POC for that reason."""
    profile = build_profile(concentrated())

    assert profile.value_area_low <= profile.poc <= profile.value_area_high


def test_a_wider_value_area_target_cannot_shrink_the_band():
    narrow = build_profile(concentrated(), value_area_pct=0.50)
    wide = build_profile(concentrated(), value_area_pct=0.90)

    narrow_width = narrow.value_area_high - narrow.value_area_low
    wide_width = wide.value_area_high - wide.value_area_low
    assert wide_width >= narrow_width


def test_volume_is_spread_across_a_bar_not_dumped_at_its_close():
    """A bar that travelled 40 points did not trade all its volume at one
    price, and assigning it there invents a spike at every close."""
    wide = pd.DataFrame(
        {"Open": [23_000.0] * 40, "High": [23_100.0] * 40,
         "Low": [22_900.0] * 40, "Close": [23_000.0] * 40,
         "Volume": np.linspace(1_000, 40_000, 40)},
        index=pd.date_range("2026-09-01 09:15", periods=40, freq="5min"))
    profile = build_profile(wide)

    assert profile.usable
    occupied = [v for _, v in profile.histogram if v > 0]
    assert len(occupied) > 10, (
        "a 200-point bar range must occupy many buckets, not one"
    )


def test_the_histogram_totals_the_session_volume():
    data = concentrated()
    profile = build_profile(data)

    assert profile.total_volume == pytest.approx(
        float(data["Volume"].sum()), rel=1e-6)


def test_low_volume_nodes_are_the_prices_crossed_quickly():
    profile = build_profile(concentrated(centre=23_000.0, spike_at=23_200.0))

    assert profile.low_volume_nodes, "the thin excursion should leave nodes"
    assert all(n > profile.value_area_high or n < profile.value_area_low
               for n in profile.low_volume_nodes), (
        "a low-volume node inside the value area is a contradiction"
    )


# ================================================================== position ==

def test_a_price_above_the_value_area_is_reported_as_such():
    profile = build_profile(concentrated())
    read = profile.position_of(profile.value_area_high + 100)

    assert read["zone"] == "above_value"
    assert read["distance"] == pytest.approx(100, abs=1)
    assert "acceptance" in read["note"]


def test_a_price_below_the_value_area_is_reported_as_such():
    profile = build_profile(concentrated())
    read = profile.position_of(profile.value_area_low - 50)

    assert read["zone"] == "below_value"
    assert read["distance"] == pytest.approx(50, abs=1)


def test_a_price_inside_the_value_area_names_the_poc_as_the_magnet():
    profile = build_profile(concentrated())
    read = profile.position_of(profile.poc)

    assert read["zone"] == "inside_value"
    assert "POC" in read["note"]


def test_an_unusable_profile_refuses_to_place_a_price():
    """It must not guess a zone from a profile it declined to build."""
    read = build_profile(frame([0.0] * 60)).position_of(23_000)

    assert read["zone"] == "unknown"


# ================================================================== sessions ==

def test_profiles_are_built_per_session_not_pooled():
    """A profile is a session object. Pooling smears the POC across whatever
    range the week happened to cover, and the levels other traders watch are
    yesterday's."""
    day_one = concentrated(60, centre=23_000.0)
    day_two = concentrated(60, centre=24_000.0)
    day_two.index = day_two.index + pd.Timedelta(days=1)
    both = pd.concat([day_one, day_two])

    out = session_profiles(both)
    assert out["usable_sessions"] == 2
    pocs = sorted(p.poc for p in out["sessions"].values() if p.usable)
    assert pocs[0] == pytest.approx(23_000, abs=50)
    assert pocs[1] == pytest.approx(24_000, abs=50)


def test_a_non_datetime_index_cannot_be_split_into_sessions():
    data = concentrated().reset_index(drop=True)
    out = session_profiles(data)

    assert out["sessions"] == {}
    assert "not datetime" in out["note"]


def test_an_index_series_reports_that_no_session_profiled():
    volumeless = frame([0.0] * 120)
    out = session_profiles(volumeless)

    assert out["usable_sessions"] == 0
    assert "carries no volume" in out["note"]


def test_a_naked_poc_is_one_later_sessions_never_traded_back_to():
    # Day one builds value at 23,000; the next two days trade far above it and
    # never return, so day one's POC stays naked.
    days = [concentrated(60, centre=23_000.0)]
    for i, centre in enumerate((24_000.0, 24_100.0), start=1):
        chunk = concentrated(60, centre=centre, spike_at=centre + 50)
        chunk.index = chunk.index + pd.Timedelta(days=i)
        days.append(chunk)
    out = naked_poc_levels(pd.concat(days))

    assert out["levels"], "day one's POC was never revisited"
    assert out["levels"][0]["poc"] == pytest.approx(23_000, abs=60)
    assert "unmeasured" in out["note"]


def test_a_revisited_poc_is_not_naked():
    day_one = concentrated(60, centre=23_000.0)
    day_two = concentrated(60, centre=23_000.0)       # same ground
    day_two.index = day_two.index + pd.Timedelta(days=1)
    out = naked_poc_levels(pd.concat([day_one, day_two]))

    assert out["levels"] == []


def test_a_single_session_cannot_have_naked_pocs():
    out = naked_poc_levels(concentrated(60))

    assert out["levels"] == []
    assert "two sessions" in out["note"]


def test_the_default_value_area_is_the_market_profile_convention():
    """Inherited from Market Profile, not derived from Indian data -- and
    parameterised for that reason, because an inherited constant presented as
    a law is how this project ended up with a flat 0.25% spread assumption."""
    assert DEFAULT_VALUE_AREA_PCT == 0.70
