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


# ---------------------------------------------------------------------------
# Closed-candle boundary (anti-repaint)
#
# On a live feed the most recent bar is still FORMING: its high/low/close keep
# changing until the bar's time window ends. Every analysis module reads the
# last row (`iloc[-1]`), so acting on a forming bar makes signals flicker and
# repaint — a BUY appears mid-bar, the bar reverses, the signal vanishes, and
# you have already chased a price that never closed there.
#
# Fixing this once at the data boundary (drop the forming bar before analysis)
# is far safer than auditing every downstream `iloc[-1]`.
# ---------------------------------------------------------------------------

# Accepts both Yahoo-style ("5m", "1h", "1d") and Angel-style ("FIVE_MINUTE")
# interval labels and maps them to a pandas-parseable frequency string.
_INTERVAL_TO_FREQ: dict[str, str] = {
    "1m": "1min", "2m": "2min", "3m": "3min", "5m": "5min", "10m": "10min",
    "15m": "15min", "30m": "30min", "60m": "60min", "90m": "90min",
    "1h": "60min", "1d": "1D", "1day": "1D", "day": "1D",
    "ONE_MINUTE": "1min", "THREE_MINUTE": "3min", "FIVE_MINUTE": "5min",
    "TEN_MINUTE": "10min", "FIFTEEN_MINUTE": "15min", "THIRTY_MINUTE": "30min",
    "ONE_HOUR": "60min", "ONE_DAY": "1D",
}


def parse_interval(interval: str) -> pd.Timedelta | None:
    """Return the bar duration as a Timedelta, or None if it cannot be parsed.

    Returning None (rather than guessing) lets callers fail safe: an unknown
    interval means "don't drop anything" instead of silently trimming good data.
    """
    if not interval:
        return None
    key = str(interval).strip()
    freq = _INTERVAL_TO_FREQ.get(key) or _INTERVAL_TO_FREQ.get(key.lower())
    if freq is None:
        return None
    try:
        return pd.Timedelta(freq)
    except ValueError:
        return None


def drop_forming_candle(
    df: pd.DataFrame,
    interval: str,
    now: "datetime | pd.Timestamp | None" = None,
) -> pd.DataFrame:
    """Drop the last bar if its time window has not finished closing.

    Uses bar-open timestamp convention (Yahoo/Angel): a bar stamped at time T
    covers [T, T + interval). It is closed only once `now >= T + interval`.
    Timezone-aware and naive indices are both handled.
    """
    if df is None or df.empty:
        return df
    delta = parse_interval(interval)
    if delta is None:
        return df  # fail safe: unknown interval, keep all data

    index = df.index
    last_open = index[-1]
    tz = getattr(index, "tz", None)

    if now is None:
        current = pd.Timestamp.now(tz=tz) if tz is not None else pd.Timestamp.now()
    else:
        current = pd.Timestamp(now)
        if tz is not None and current.tzinfo is None:
            current = current.tz_localize(tz)
        elif tz is None and current.tzinfo is not None:
            current = current.tz_localize(None)

    if current < last_open + delta:
        return df.iloc[:-1]
    return df


def last_closed_row(
    df: pd.DataFrame,
    interval: str,
    now: "datetime | pd.Timestamp | None" = None,
) -> pd.Series | None:
    """Convenience: the most recent fully-closed bar, or None if none exists."""
    closed = drop_forming_candle(df, interval, now=now)
    if closed is None or closed.empty:
        return None
    return closed.iloc[-1]


def candle_age(
    df: pd.DataFrame,
    now: "datetime | pd.Timestamp | None" = None,
) -> pd.Timedelta | None:
    """Age of the last bar's open timestamp vs now — used for staleness checks."""
    if df is None or df.empty:
        return None
    index = df.index
    tz = getattr(index, "tz", None)
    if now is None:
        current = pd.Timestamp.now(tz=tz) if tz is not None else pd.Timestamp.now()
    else:
        current = pd.Timestamp(now)
        if tz is not None and current.tzinfo is None:
            current = current.tz_localize(tz)
        elif tz is None and current.tzinfo is not None:
            current = current.tz_localize(None)
    return current - index[-1]


def load_watchlist(symbols: str | Iterable[str]) -> list[str]:
    if isinstance(symbols, str):
        items = symbols.replace("\n", ",").split(",")
    else:
        items = list(symbols)
    return [item.strip().upper() for item in items if item.strip()]

