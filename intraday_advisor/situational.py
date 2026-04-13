from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class SituationalRule:
    name: str
    trigger_date: object
    target_date: object | None
    reference_low: float
    condition: str
    visited: bool | None
    note: str


@dataclass(frozen=True)
class SituationalReport:
    active_rules: list[SituationalRule]
    reasons: list[str]
    warnings: list[str]


def daily_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    data = df.copy()
    data.index = pd.to_datetime(data.index)
    grouped = data.groupby(data.index.date).agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    )
    grouped.index = pd.to_datetime(grouped.index)
    grouped["Weekday"] = grouped.index.day_name()
    return grouped


def _visited_low(daily: pd.DataFrame, target_date: pd.Timestamp | None, level: float) -> bool | None:
    if target_date is None or target_date not in daily.index:
        return None
    row = daily.loc[target_date]
    return bool(row["Low"] <= level <= row["High"] or row["Low"] <= level)


def _next_trading_day_after(daily: pd.DataFrame, date: pd.Timestamp) -> pd.Timestamp | None:
    later = daily[daily.index > date]
    if later.empty:
        return None
    return pd.Timestamp(later.index[0])


def analyze_situational_revisit_rules(df: pd.DataFrame) -> SituationalReport:
    daily = daily_ohlcv(df)
    if len(daily) < 2:
        return SituationalReport([], [], ["Need at least two daily candles for situational analysis."])

    rules: list[SituationalRule] = []
    reasons: list[str] = []
    warnings: list[str] = []

    for date, row in daily.iterrows():
        weekday = row["Weekday"]

        if weekday == "Friday":
            previous = daily[daily.index < date].tail(1)
            if not previous.empty and previous.iloc[0]["Weekday"] == "Thursday" and row["High"] < previous.iloc[0]["High"]:
                target_date = _next_trading_day_after(daily, date)
                visited = _visited_low(daily, target_date, float(row["Low"]))
                note = "Friday high below Thursday high; watch Friday low for Monday revisit."
                rule = SituationalRule(
                    "Friday low Monday revisit",
                    date,
                    target_date,
                    float(row["Low"]),
                    f"Friday high {row['High']:.2f} < Thursday high {previous.iloc[0]['High']:.2f}",
                    visited,
                    note,
                )
                rules.append(rule)
                reasons.append(f"{note} Level: {row['Low']:.2f}")

        if weekday == "Wednesday":
            same_week_monday = date - pd.Timedelta(days=2)
            if same_week_monday in daily.index and daily.loc[same_week_monday, "Weekday"] == "Monday":
                monday = daily.loc[same_week_monday]
                if row["High"] < monday["High"]:
                    target_date = _next_trading_day_after(daily, date)
                    visited = _visited_low(daily, target_date, float(row["Low"]))
                    note = "Wednesday high below Monday high; watch Wednesday low for Thursday revisit."
                    rule = SituationalRule(
                        "Wednesday low Thursday revisit",
                        date,
                        target_date,
                        float(row["Low"]),
                        f"Wednesday high {row['High']:.2f} < Monday high {monday['High']:.2f}",
                        visited,
                        note,
                    )
                    rules.append(rule)
                    reasons.append(f"{note} Level: {row['Low']:.2f}")

    if not rules:
        warnings.append("No weekday low-revisit rule is active in the available data.")
    return SituationalReport(rules, reasons, warnings)


def latest_situational_summary(df: pd.DataFrame) -> dict[str, object]:
    report = analyze_situational_revisit_rules(df)
    if not report.active_rules:
        return {
            "SituationalRule": "none",
            "SituationalLevel": None,
            "SituationalTargetDate": None,
            "SituationalVisited": None,
            "SituationalNote": "; ".join(report.warnings),
        }
    latest = report.active_rules[-1]
    return {
        "SituationalRule": latest.name,
        "SituationalLevel": latest.reference_low,
        "SituationalTargetDate": latest.target_date.date().isoformat() if latest.target_date is not None else None,
        "SituationalVisited": latest.visited,
        "SituationalNote": latest.note,
    }

