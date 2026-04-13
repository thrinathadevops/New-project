import pandas as pd

from intraday_advisor.situational import analyze_situational_revisit_rules, daily_ohlcv, latest_situational_summary


def _daily_frame(rows):
    return pd.DataFrame(
        rows,
        columns=["Date", "Open", "High", "Low", "Close", "Volume"],
    ).assign(Date=lambda df: pd.to_datetime(df["Date"])).set_index("Date")


def test_friday_high_below_thursday_high_flags_monday_revisit():
    df = _daily_frame(
        [
            ("2026-04-09", 100, 110, 95, 106, 1000),  # Thursday
            ("2026-04-10", 105, 108, 98, 102, 1200),  # Friday
            ("2026-04-13", 101, 103, 97, 100, 1400),  # Monday revisits 98
        ]
    )
    report = analyze_situational_revisit_rules(df)
    rule = report.active_rules[-1]
    assert rule.name == "Friday low Monday revisit"
    assert rule.reference_low == 98
    assert rule.visited is True


def test_wednesday_high_below_monday_high_flags_thursday_revisit():
    df = _daily_frame(
        [
            ("2026-04-06", 100, 120, 99, 115, 1000),  # Monday
            ("2026-04-08", 111, 118, 106, 110, 1200),  # Wednesday
            ("2026-04-09", 109, 112, 105, 108, 1400),  # Thursday revisits 106
        ]
    )
    report = analyze_situational_revisit_rules(df)
    rule = report.active_rules[-1]
    assert rule.name == "Wednesday low Thursday revisit"
    assert rule.reference_low == 106
    assert rule.visited is True


def test_daily_ohlcv_aggregates_intraday_rows():
    df = _daily_frame(
        [
            ("2026-04-09 09:15", 100, 105, 99, 103, 1000),
            ("2026-04-09 09:30", 103, 110, 102, 106, 1200),
        ]
    )
    daily = daily_ohlcv(df)
    assert daily.iloc[0]["Open"] == 100
    assert daily.iloc[0]["High"] == 110
    assert daily.iloc[0]["Volume"] == 2200


def test_latest_summary_returns_none_when_no_rule():
    df = _daily_frame(
        [
            ("2026-04-09", 100, 110, 95, 106, 1000),
            ("2026-04-10", 105, 112, 98, 102, 1200),
        ]
    )
    summary = latest_situational_summary(df)
    assert summary["SituationalRule"] == "none"
