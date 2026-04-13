from __future__ import annotations

import pandas as pd


def apply_screen(
    stocks: pd.DataFrame,
    min_price: float = 50.0,
    min_adtv: float = 500_000,
    min_market_cap: float = 20_000_000_000,
    min_atr_pct: float = 0.01,
    min_float_pct: float = 20.0,
    sectors: list[str] | None = None,
) -> pd.DataFrame:
    df = stocks.copy()
    mask = (
        (df["Close"] > min_price)
        & (df["ADTV20"] > min_adtv)
        & (df.get("MarketCap", min_market_cap) >= min_market_cap)
        & ((df["ATR14"] / df["Close"]) > min_atr_pct)
        & (df.get("FloatPct", min_float_pct) >= min_float_pct)
    )
    if sectors:
        mask &= df.get("Sector", "").isin(sectors)
    return df[mask].copy()


def score_stocks(stocks: pd.DataFrame) -> pd.DataFrame:
    df = stocks.copy()
    df["TrendScore"] = (
        (df["Close"] > df["SMA20"]).astype(int)
        + (df["SMA20"] > df["SMA50"]).astype(int)
        + (df["Close"] > df.get("EMA200", df["Close"])).astype(int)
    )
    df["LiquidityScore"] = df["ADTV20"].rank(pct=True)
    df["VolatilityScore"] = (df["ATR14"] / df["Close"]).rank(pct=True)
    df["MomentumScore"] = df["Momentum10"].rank(pct=True)
    df["RSIScore"] = (1 - ((df["RSI14"] - 55).abs() / 55)).clip(lower=0)
    df["Score"] = (
        2.0 * df["TrendScore"]
        + df["LiquidityScore"]
        + df["VolatilityScore"]
        + df["MomentumScore"]
        + df["RSIScore"]
    )
    return df.sort_values("Score", ascending=False)
