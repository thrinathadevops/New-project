import pandas as pd
import pytest

from intraday_advisor.database import connect, init_db, load_fundamentals, load_ohlcv, store_analysis_results, store_fundamentals, store_ohlcv


@pytest.fixture
def workspace_db_path():
    path = pd.Timestamp.utcnow().strftime("data/test-advisor-%Y%m%d%H%M%S%f.sqlite")
    db = pd.io.common.stringify_path(path)
    yield db


def test_store_and_load_ohlcv(workspace_db_path):
    db = workspace_db_path
    df = pd.DataFrame(
        {"Open": [10], "High": [12], "Low": [9], "Close": [11], "Volume": [1000]},
        index=pd.to_datetime(["2026-04-13 09:15"]),
    )
    store_ohlcv("TEST", df, db, source="unit")
    loaded = load_ohlcv("TEST", db, source="unit")
    assert loaded.iloc[0]["Close"] == 11


def test_store_and_load_fundamentals(workspace_db_path):
    db = workspace_db_path
    df = pd.DataFrame({"Ticker": ["GOOD"], "MarketCapCr": [9000], "ROE": [25], "ROCE": [24]})
    store_fundamentals(df, db)
    loaded = load_fundamentals(db)
    assert loaded.iloc[0]["Ticker"] == "GOOD"
    assert loaded.iloc[0]["MarketCapCr"] == 9000


def test_store_analysis_results(workspace_db_path):
    db = workspace_db_path
    init_db(db)
    results = pd.DataFrame({"Ticker": ["GOOD"], "Signal": ["BUY"], "Confidence": [80], "Setup": ["test"], "Score": [5.5], "Close": [100]})
    store_analysis_results(results, db)
    with connect(db) as conn:
        loaded = pd.read_sql_query("SELECT * FROM analysis_results", conn)
    assert loaded.iloc[0]["ticker"] == "GOOD"
