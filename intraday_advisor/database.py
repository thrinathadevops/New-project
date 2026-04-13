from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_DB_PATH = Path("data/intraday_advisor.sqlite")


SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    ticker TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (ticker, ts, source)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    market_cap_cr REAL,
    roe REAL,
    roce REAL,
    debt_to_equity REAL,
    sales_growth REAL,
    promoter_holding REAL,
    profit_growth REAL,
    opm REAL,
    eps REAL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS analysis_results (
    ticker TEXT NOT NULL,
    ts TEXT NOT NULL,
    signal TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    setup TEXT NOT NULL,
    score REAL,
    close REAL,
    risk_amount REAL,
    reward_amount REAL,
    reasons TEXT,
    warnings TEXT,
    PRIMARY KEY (ticker, ts)
);

CREATE TABLE IF NOT EXISTS trade_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    shares INTEGER,
    entry REAL,
    stop REAL,
    target REAL,
    status TEXT,
    rationale TEXT
);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def store_ohlcv(ticker: str, df: pd.DataFrame, db_path: str | Path = DEFAULT_DB_PATH, source: str = "unknown") -> None:
    init_db(db_path)
    rows = []
    for ts, row in df.iterrows():
        rows.append(
            {
                "ticker": ticker.upper(),
                "ts": pd.Timestamp(ts).isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
                "source": source,
            }
        )
    if not rows:
        return
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO ohlcv (ticker, ts, open, high, low, close, volume, source)
            VALUES (:ticker, :ts, :open, :high, :low, :close, :volume, :source)
            """,
            rows,
        )


def load_ohlcv(ticker: str, db_path: str | Path = DEFAULT_DB_PATH, source: str | None = None) -> pd.DataFrame:
    init_db(db_path)
    query = "SELECT ts, open, high, low, close, volume FROM ohlcv WHERE ticker = ?"
    params: list[Any] = [ticker.upper()]
    if source:
        query += " AND source = ?"
        params.append(source)
    query += " ORDER BY ts"
    with connect(db_path) as conn:
        frame = pd.read_sql_query(query, conn, params=params, parse_dates=["ts"])
    if frame.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    frame = frame.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}).set_index("ts")
    return frame[["Open", "High", "Low", "Close", "Volume"]]


def store_fundamentals(df: pd.DataFrame, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    if df.empty:
        return
    rows = []
    for _, row in df.iterrows():
        rows.append(
            {
                "ticker": str(row["Ticker"]).upper(),
                "name": row.get("Name"),
                "market_cap_cr": row.get("MarketCapCr"),
                "roe": row.get("ROE"),
                "roce": row.get("ROCE"),
                "debt_to_equity": row.get("DebtToEquity"),
                "sales_growth": row.get("SalesGrowth"),
                "promoter_holding": row.get("PromoterHolding"),
                "profit_growth": row.get("ProfitGrowth"),
                "opm": row.get("OPM"),
                "eps": row.get("EPS"),
            }
        )
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO fundamentals (
                ticker, name, market_cap_cr, roe, roce, debt_to_equity, sales_growth,
                promoter_holding, profit_growth, opm, eps, updated_at
            )
            VALUES (
                :ticker, :name, :market_cap_cr, :roe, :roce, :debt_to_equity, :sales_growth,
                :promoter_holding, :profit_growth, :opm, :eps, CURRENT_TIMESTAMP
            )
            """,
            rows,
        )


def load_fundamentals(db_path: str | Path = DEFAULT_DB_PATH) -> pd.DataFrame:
    init_db(db_path)
    with connect(db_path) as conn:
        frame = pd.read_sql_query("SELECT * FROM fundamentals ORDER BY ticker", conn)
    return frame.rename(
        columns={
            "ticker": "Ticker",
            "name": "Name",
            "market_cap_cr": "MarketCapCr",
            "roe": "ROE",
            "roce": "ROCE",
            "debt_to_equity": "DebtToEquity",
            "sales_growth": "SalesGrowth",
            "promoter_holding": "PromoterHolding",
            "profit_growth": "ProfitGrowth",
            "opm": "OPM",
            "eps": "EPS",
        }
    )


def store_analysis_results(results: pd.DataFrame, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    if results.empty:
        return
    rows = []
    now = pd.Timestamp.utcnow().isoformat()
    for _, row in results.iterrows():
        rows.append(
            {
                "ticker": row["Ticker"],
                "ts": now,
                "signal": row.get("Signal", "HOLD"),
                "confidence": int(row.get("Confidence", 0)),
                "setup": row.get("Setup", ""),
                "score": row.get("Score"),
                "close": row.get("Close"),
                "risk_amount": row.get("risk_amount"),
                "reward_amount": row.get("reward_amount"),
                "reasons": row.get("Reasons", ""),
                "warnings": row.get("Warnings", ""),
            }
        )
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO analysis_results (
                ticker, ts, signal, confidence, setup, score, close, risk_amount, reward_amount, reasons, warnings
            )
            VALUES (:ticker, :ts, :signal, :confidence, :setup, :score, :close, :risk_amount, :reward_amount, :reasons, :warnings)
            """,
            rows,
        )

