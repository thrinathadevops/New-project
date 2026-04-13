from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class FairValueGap:
    direction: str
    lower: float
    upper: float
    index: object
    mitigated: bool


@dataclass(frozen=True)
class VolumeProfile:
    point_of_control: float | None
    value_area_low: float | None
    value_area_high: float | None
    bins: pd.DataFrame


@dataclass(frozen=True)
class SmartMoneyReport:
    fvg_direction: str
    fvg_lower: float | None
    fvg_upper: float | None
    fvg_mitigated: bool | None
    liquidity_sweep: str
    order_flow: str
    cumulative_delta: float
    volume_poc: float | None
    value_area_low: float | None
    value_area_high: float | None
    vwap_relation: str
    reasons: list[str]
    warnings: list[str]


def detect_fair_value_gaps(df: pd.DataFrame) -> list[FairValueGap]:
    gaps: list[FairValueGap] = []
    for position in range(2, len(df)):
        current = df.iloc[position]
        two_back = df.iloc[position - 2]
        if current["Low"] > two_back["High"]:
            lower = float(two_back["High"])
            upper = float(current["Low"])
            mitigated = bool(df.iloc[position + 1 :]["Low"].le(upper).any())
            gaps.append(FairValueGap("bullish", lower, upper, df.index[position], mitigated))
        elif current["High"] < two_back["Low"]:
            lower = float(current["High"])
            upper = float(two_back["Low"])
            mitigated = bool(df.iloc[position + 1 :]["High"].ge(lower).any())
            gaps.append(FairValueGap("bearish", lower, upper, df.index[position], mitigated))
    return gaps


def latest_unmitigated_fvg(df: pd.DataFrame) -> FairValueGap | None:
    gaps = detect_fair_value_gaps(df)
    for gap in reversed(gaps):
        if not gap.mitigated:
            return gap
    return gaps[-1] if gaps else None


def liquidity_sweep(df: pd.DataFrame, lookback: int = 20) -> str:
    if len(df) <= lookback:
        return "insufficient history"
    prior = df.iloc[-lookback - 1 : -1]
    last = df.iloc[-1]
    prior_high = prior["High"].max()
    prior_low = prior["Low"].min()
    if last["Low"] < prior_low and last["Close"] > prior_low:
        return "bullish liquidity sweep"
    if last["High"] > prior_high and last["Close"] < prior_high:
        return "bearish liquidity sweep"
    if last["Close"] > prior_high:
        return "buy-side liquidity breakout"
    if last["Close"] < prior_low:
        return "sell-side liquidity breakdown"
    return "no sweep"


def order_flow_proxy(df: pd.DataFrame, window: int = 20) -> tuple[str, float]:
    usable = df.tail(window).copy()
    if usable.empty:
        return "insufficient history", 0.0
    candle_range = (usable["High"] - usable["Low"]).replace(0, pd.NA)
    delta = ((usable["Close"] - usable["Open"]) / candle_range).fillna(0) * usable["Volume"]
    cumulative_delta = float(delta.sum())
    avg_abs = float(delta.abs().mean()) if not delta.empty else 0.0
    if avg_abs == 0:
        return "neutral order flow", cumulative_delta
    if cumulative_delta > avg_abs * 3:
        return "buyer-dominant order flow", cumulative_delta
    if cumulative_delta < -avg_abs * 3:
        return "seller-dominant order flow", cumulative_delta
    return "mixed order flow", cumulative_delta


def build_volume_profile(df: pd.DataFrame, bins: int = 24, value_area_pct: float = 0.70) -> VolumeProfile:
    usable = df.dropna(subset=["Close", "Volume"])
    if usable.empty:
        return VolumeProfile(None, None, None, pd.DataFrame(columns=["Price", "Volume"]))
    min_price = float(usable["Low"].min())
    max_price = float(usable["High"].max())
    if min_price == max_price:
        profile = pd.DataFrame({"Price": [min_price], "Volume": [float(usable["Volume"].sum())]})
        return VolumeProfile(min_price, min_price, max_price, profile)

    step = (max_price - min_price) / bins
    rows = []
    for bucket in range(bins):
        lower = min_price + bucket * step
        upper = lower + step
        if bucket == bins - 1:
            mask = usable["Close"].between(lower, upper, inclusive="both")
        else:
            mask = usable["Close"].between(lower, upper, inclusive="left")
        rows.append({"Price": (lower + upper) / 2, "Volume": float(usable.loc[mask, "Volume"].sum())})
    profile = pd.DataFrame(rows)
    if profile["Volume"].sum() == 0:
        return VolumeProfile(None, None, None, profile)

    point_of_control = float(profile.loc[profile["Volume"].idxmax(), "Price"])
    ranked = profile.sort_values("Volume", ascending=False)
    cumulative = ranked["Volume"].cumsum()
    selected = ranked[cumulative <= profile["Volume"].sum() * value_area_pct]
    if selected.empty:
        selected = ranked.head(1)
    value_area_low = float(selected["Price"].min())
    value_area_high = float(selected["Price"].max())
    return VolumeProfile(point_of_control, value_area_low, value_area_high, profile)


def analyze_smart_money(df: pd.DataFrame) -> SmartMoneyReport:
    usable = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if usable.empty:
        return SmartMoneyReport("none", None, None, None, "unknown", "unknown", 0.0, None, None, None, "unknown", [], ["No OHLCV data."])

    fvg = latest_unmitigated_fvg(usable)
    sweep = liquidity_sweep(usable)
    flow, cumulative_delta = order_flow_proxy(usable)
    profile = build_volume_profile(usable.tail(120))
    last = usable.iloc[-1]
    vwap_relation = "above VWAP" if "VWAP" in usable and last["Close"] > last["VWAP"] else "below VWAP"
    reasons: list[str] = [f"liquidity: {sweep}", f"order flow: {flow}", f"volume POC: {profile.point_of_control}"]
    warnings: list[str] = []

    fvg_direction = "none"
    fvg_lower = None
    fvg_upper = None
    fvg_mitigated = None
    if fvg:
        fvg_direction = fvg.direction
        fvg_lower = fvg.lower
        fvg_upper = fvg.upper
        fvg_mitigated = fvg.mitigated
        reasons.append(f"{fvg.direction} FVG {fvg.lower:.2f}-{fvg.upper:.2f}")
    else:
        warnings.append("no fair value gap context")

    if fvg_direction == "bullish" and not (last["Close"] > last.get("EMA9", last["Close"]) and last["Close"] > last.get("EMA21", last["Close"])):
        warnings.append("bullish FVG is not paired with price above EMA9/EMA21")
    if vwap_relation == "below VWAP":
        warnings.append("price is below VWAP")
    if flow == "seller-dominant order flow":
        warnings.append("order flow proxy favors sellers")
    if sweep == "bearish liquidity sweep":
        warnings.append("recent sweep rejected higher prices")

    return SmartMoneyReport(
        fvg_direction,
        fvg_lower,
        fvg_upper,
        fvg_mitigated,
        sweep,
        flow,
        cumulative_delta,
        profile.point_of_control,
        profile.value_area_low,
        profile.value_area_high,
        vwap_relation,
        reasons,
        warnings,
    )

