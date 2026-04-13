from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from intraday_advisor.situational import daily_ohlcv


@dataclass(frozen=True)
class BoxTheoryReport:
    previous_day_high: float | None
    previous_day_low: float | None
    middle_line: float | None
    zone: str
    wick_signal: str
    bias: str
    reasons: list[str]
    warnings: list[str]


def _is_bullish_john_wick(row: pd.Series, wick_to_body: float = 2.0, close_position: float = 0.6) -> bool:
    body = abs(row["Close"] - row["Open"])
    candle_range = max(row["High"] - row["Low"], 1e-9)
    lower_wick = min(row["Open"], row["Close"]) - row["Low"]
    upper_wick = row["High"] - max(row["Open"], row["Close"])
    body = max(body, candle_range * 0.05)
    close_near_high = (row["Close"] - row["Low"]) / candle_range >= close_position
    return lower_wick >= body * wick_to_body and lower_wick > upper_wick and close_near_high


def _is_bearish_john_wick(row: pd.Series, wick_to_body: float = 2.0, close_position: float = 0.4) -> bool:
    body = abs(row["Close"] - row["Open"])
    candle_range = max(row["High"] - row["Low"], 1e-9)
    upper_wick = row["High"] - max(row["Open"], row["Close"])
    lower_wick = min(row["Open"], row["Close"]) - row["Low"]
    body = max(body, candle_range * 0.05)
    close_near_low = (row["Close"] - row["Low"]) / candle_range <= close_position
    return upper_wick >= body * wick_to_body and upper_wick > lower_wick and close_near_low


def john_wick_signal(row: pd.Series) -> str:
    bullish = _is_bullish_john_wick(row)
    bearish = _is_bearish_john_wick(row)
    if bullish and not bearish:
        return "bullish john wick"
    if bearish and not bullish:
        return "bearish john wick"
    return "none"


def box_zone(price: float, previous_high: float, previous_low: float, edge_pct: float = 0.25, middle_pct: float = 0.15) -> tuple[str, float]:
    box_range = max(previous_high - previous_low, 1e-9)
    middle = previous_low + box_range / 2
    position = (price - previous_low) / box_range
    if abs(price - middle) <= box_range * middle_pct:
        return "middle no-trade zone", middle
    if position <= edge_pct:
        return "bottom buy-only zone", middle
    if position >= 1 - edge_pct:
        return "top sell-only zone", middle
    return "inside box wait zone", middle


def analyze_box_theory(df: pd.DataFrame) -> BoxTheoryReport:
    usable = df.dropna(subset=["Open", "High", "Low", "Close"])
    if usable.empty:
        return BoxTheoryReport(None, None, None, "unknown", "none", "HOLD", [], ["No OHLC data for box theory."])

    daily = daily_ohlcv(usable)
    if len(daily) < 2:
        return BoxTheoryReport(None, None, None, "unknown", "none", "HOLD", [], ["Need previous day high/low for box theory."])

    current_day = daily.index[-1]
    previous_day = daily.index[-2]
    previous = daily.loc[previous_day]
    last = usable.iloc[-1]
    zone, middle = box_zone(float(last["Close"]), float(previous["High"]), float(previous["Low"]))
    wick = john_wick_signal(last)

    reasons = [
        f"previous day box high {previous['High']:.2f}",
        f"previous day box low {previous['Low']:.2f}",
        f"box middle line {middle:.2f}",
        f"current zone: {zone}",
        f"wick signal: {wick}",
    ]
    warnings: list[str] = []
    bias = "HOLD"

    if zone == "middle no-trade zone":
        warnings.append("price is near the middle line; avoid false signals")
    elif zone == "bottom buy-only zone":
        if wick == "bullish john wick":
            bias = "BUY WATCH"
            reasons.append("bottom of box with bullish wick rejection")
        else:
            warnings.append("bottom of box, but no bullish wick confirmation")
    elif zone == "top sell-only zone":
        if wick == "bearish john wick":
            bias = "SELL WATCH"
            reasons.append("top of box with bearish wick rejection")
        else:
            warnings.append("top of box, but no bearish wick confirmation")
    else:
        warnings.append("price is inside the box but away from the best edge")

    return BoxTheoryReport(
        float(previous["High"]),
        float(previous["Low"]),
        float(middle),
        zone,
        wick,
        bias,
        reasons,
        warnings,
    )

