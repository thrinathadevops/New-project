from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def normalize_ticker_for_yahoo(ticker: str, suffix: str = ".NS") -> str:
    clean = ticker.strip().upper()
    if clean.startswith("^"):
        return clean
    if "." in clean:
        return clean
    return f"{clean}{suffix}"


def fetch_yahoo_ohlcv(ticker: str, period: str = "60d", interval: str = "5m", suffix: str = ".NS") -> pd.DataFrame:
    import yfinance as yf

    data = yf.download(
        normalize_ticker_for_yahoo(ticker, suffix),
        period=period,
        interval=interval,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if data.empty:
        raise ValueError(f"No OHLCV data returned for {ticker}")
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    return clean_ohlcv(data)


def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    missing = set(OHLCV_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Missing OHLCV columns: {sorted(missing)}")

    clean = df.copy()
    clean = clean[OHLCV_COLUMNS]
    clean.index = pd.to_datetime(clean.index)
    clean = clean.sort_index()
    clean = clean.replace([np.inf, -np.inf], np.nan)
    for col in OHLCV_COLUMNS:
        clean[col] = pd.to_numeric(clean[col], errors="coerce")
    clean = clean.dropna(subset=["Open", "High", "Low", "Close"])
    clean = clean[clean["Volume"].fillna(0) >= 0]
    clean["Volume"] = clean["Volume"].fillna(0)
    clean = clean[~clean.index.duplicated(keep="last")]
    return clean


def store_ohlcv_sqlite(df: pd.DataFrame, db_path: str | Path, table_name: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        df.to_sql(table_name, conn, if_exists="replace", index_label="Date")


def load_ohlcv_sqlite(db_path: str | Path, table_name: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql(f'SELECT * FROM "{table_name}"', conn, parse_dates=["Date"])
    return clean_ohlcv(df.set_index("Date"))


from datetime import datetime, timedelta

def generate_sample_ohlcv(rows: int = 260, seed: int = 7, open_hour: int = 9, open_minute: int = 15) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start_time = datetime(2025, 1, 1, open_hour, open_minute) - timedelta(minutes=15)
    time_str = start_time.strftime("%Y-%m-%d %H:%M")
    index = pd.date_range(time_str, periods=rows, freq="15min")
    
    # Introduce explicit simulated trend to guarantee BUY/SELL setups for demo UI
    trend_bias = 0.0015 if seed % 3 != 0 else -0.0015
    returns = rng.normal(trend_bias, 0.008, size=rows)
    close = 250 * np.exp(np.cumsum(returns))
    spread = rng.uniform(0.004, 0.025, size=rows) * close
    open_ = close * (1 + rng.normal(0, 0.003, size=rows))
    high = np.maximum(open_, close) + spread / 2
    low = np.minimum(open_, close) - spread / 2
    volume = rng.integers(250_000, 2_000_000, size=rows)
    return clean_ohlcv(
        pd.DataFrame(
            {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
            index=index,
        )
    )


def load_watchlist(symbols: str | Iterable[str]) -> list[str]:
    if isinstance(symbols, str):
        items = symbols.replace("\n", ",").split(",")
    else:
        items = list(symbols)
    return [item.strip().upper() for item in items if item.strip()]

