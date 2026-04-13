from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pandas as pd


@dataclass(frozen=True)
class FundamentalCriteria:
    max_market_cap_cr: float = 10_000
    min_roe_pct: float = 20
    min_roce_pct: float = 20
    max_debt_to_equity: float = 0.25
    min_sales_growth_pct: float = 20
    min_promoter_holding_pct: float = 50
    min_profit_growth_pct: float = 20
    min_opm_pct: float = 15
    min_eps: float = 20


SCREENER_COLUMN_ALIASES = {
    "ticker": "Ticker",
    "symbol": "Ticker",
    "nse code": "Ticker",
    "name": "Name",
    "company": "Name",
    "market capitalization": "MarketCapCr",
    "market cap": "MarketCapCr",
    "mar cap rs.cr.": "MarketCapCr",
    "mar cap rs cr": "MarketCapCr",
    "return on equity": "ROE",
    "roe": "ROE",
    "roe %": "ROE",
    "return on capital employed": "ROCE",
    "roce": "ROCE",
    "roce %": "ROCE",
    "debt to equity": "DebtToEquity",
    "debt / eq.": "DebtToEquity",
    "debt / eq": "DebtToEquity",
    "sales growth": "SalesGrowth",
    "sales growth %": "SalesGrowth",
    "promoter holding": "PromoterHolding",
    "prom. hold.": "PromoterHolding",
    "prom. hold. %": "PromoterHolding",
    "prom hold %": "PromoterHolding",
    "promoter holding %": "PromoterHolding",
    "profit growth": "ProfitGrowth",
    "profit growth %": "ProfitGrowth",
    "opm": "OPM",
    "opm %": "OPM",
    "eps": "EPS",
    "eps rs.": "EPS",
    "eps rs": "EPS",
}


def _clean_column_name(column: str) -> str:
    return " ".join(str(column).strip().lower().replace("\n", " ").split())


def _to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False).str.strip(),
        errors="coerce",
    )


def normalize_screener_export(df: pd.DataFrame) -> pd.DataFrame:
    renamed = {}
    for column in df.columns:
        cleaned = _clean_column_name(column)
        renamed[column] = SCREENER_COLUMN_ALIASES.get(cleaned, column)

    normalized = df.rename(columns=renamed).copy()
    if "Ticker" not in normalized and "Name" in normalized:
        normalized["Ticker"] = normalized["Name"].astype(str).str.upper().str.replace(" ", "", regex=False)

    for column in ["MarketCapCr", "ROE", "ROCE", "DebtToEquity", "SalesGrowth", "PromoterHolding", "ProfitGrowth", "OPM", "EPS"]:
        if column in normalized:
            normalized[column] = _to_number(normalized[column])

    if "Ticker" in normalized:
        normalized["Ticker"] = normalized["Ticker"].astype(str).str.strip().str.upper().str.replace(".NS", "", regex=False)
    return normalized


def load_screener_csv(path_or_buffer: str | Path | IO[bytes] | IO[str]) -> pd.DataFrame:
    return normalize_screener_export(pd.read_csv(path_or_buffer))


def apply_fundamental_quality_screen(
    fundamentals: pd.DataFrame,
    criteria: FundamentalCriteria = FundamentalCriteria(),
) -> pd.DataFrame:
    df = normalize_screener_export(fundamentals)
    required = ["MarketCapCr", "ROE", "ROCE", "DebtToEquity", "SalesGrowth", "PromoterHolding", "ProfitGrowth", "OPM", "EPS"]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"Missing fundamental columns: {', '.join(missing)}")

    mask = (
        (df["MarketCapCr"] < criteria.max_market_cap_cr)
        & (df["ROE"] > criteria.min_roe_pct)
        & (df["ROCE"] > criteria.min_roce_pct)
        & (df["DebtToEquity"] < criteria.max_debt_to_equity)
        & (df["SalesGrowth"] > criteria.min_sales_growth_pct)
        & (df["PromoterHolding"] > criteria.min_promoter_holding_pct)
        & (df["ProfitGrowth"] > criteria.min_profit_growth_pct)
        & (df["OPM"] > criteria.min_opm_pct)
        & (df["EPS"] > criteria.min_eps)
    )
    return df[mask].copy()


def merge_fundamentals(technical: pd.DataFrame, fundamentals: pd.DataFrame) -> pd.DataFrame:
    if fundamentals.empty:
        return technical.copy()
    normalized = normalize_screener_export(fundamentals)
    if "Ticker" not in normalized:
        raise ValueError("Fundamental data must include Ticker, Symbol, NSE Code, Name, or Company.")
    return technical.merge(normalized, on="Ticker", how="inner", suffixes=("", "_Fundamental"))
