# Intraday Trade Advisor

Paper-trading prototype for intraday market pre-analysis, signal monitoring, risk sizing, and backtesting.

This app does not guarantee profit and does not place live broker orders. Treat the output as research support. Validate every strategy with out-of-sample data, realistic costs, and paper trading before risking money.

## What it includes

- Data ingestion and cleaning for OHLCV, with optional Yahoo Finance data and deterministic offline sample data.
- Technical indicators: SMA20/50/200, EMA12/26, ATR14, RSI14, MACD, OBV, VWAP, 10-bar momentum, ADTV20.
- Fundamental screening from a Screener-style CSV export, plus technical screening by price, liquidity, ATR percentage, and EMA200 trend.
- Scoring and ranking for watchlists.
- BUY/HOLD/SELL signal generation using EMA9/EMA21 crossover, recent swing-high breakout confirmation, VWAP, RSI, volume, and ATR context.
- Swing/ATR-based position sizing and stop/target planning. The strategy exit is EMA9 crossing back below EMA21.
- Backtesting with brokerage, GST, STT, and slippage assumptions.
- Paper broker and trade journal building blocks.
- Optional Angel One SmartAPI adapter for historical candles and guarded live intraday order placement.
- Streamlit dashboard and unit tests.

## Run locally without extra UI packages

```powershell
python serve.py
```

Open `http://127.0.0.1:8501`. This standard-library dashboard uses offline sample data and refreshes every 60 seconds.

## Run the Streamlit dashboard

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

The dashboard starts in offline sample-data mode. Toggle `Use Yahoo Finance data` in the sidebar when you have network access.

## Automatic Screener quality filter

Create a screen in Screener.in with this logic:

```text
Market Capitalization < 10000
AND Return on equity > 20
AND Return on capital employed > 20
AND Debt to equity < 0.25
AND Sales growth > 20
AND Promoter holding > 50
AND Profit growth > 20
AND OPM > 15
AND EPS > 20
```

The analyzer then keeps only those stocks and applies the technical condition:

```text
Close > EMA200
```

After that, it waits for the EMA9/EMA21 crossover and recent swing-high breakout entry.

Screener does not provide a public API. The app supports automatic fetching through a saved screen export URL. Set one of these:

```powershell
$env:SCREENER_EXPORT_URL="https://www.screener.in/api/export/screen/?screen_id=YOUR_SCREEN_ID&slug_name=YOUR_SLUG&url_name=YOUR_URL_NAME"
```

Or:

```powershell
$env:SCREENER_SCREEN_ID="YOUR_SCREEN_ID"
$env:SCREENER_SLUG_NAME="your-slug"
$env:SCREENER_URL_NAME="your-url-name"
```

If your export requires login:

```powershell
$env:SCREENER_EMAIL="your_email"
$env:SCREENER_PASSWORD="your_password"
```

Keep these values in environment variables, not in source code.

## Angel One SmartAPI setup

Create a `.env` file or set these environment variables:

```powershell
$env:ANGEL_ONE_API_KEY="your_api_key"
$env:ANGEL_ONE_CLIENT_CODE="your_client_code"
$env:ANGEL_ONE_PIN="your_pin"
$env:ANGEL_ONE_TOTP_SECRET="your_totp_secret"
$env:ENABLE_LIVE_TRADING="NO"
```

Install broker dependencies:

```powershell
python -m pip install smartapi-python pyotp logzero websocket-client
```

Live buying is blocked unless `ENABLE_LIVE_TRADING=YES` and the code passes `live_confirmed=True` to the Angel One adapter. In the Streamlit UI you must also type `BUY LIVE`. Keep it on `NO` while testing and paper-trading.

Angel One order and candle APIs require a `symboltoken`. The adapter includes `search_symbol_token("RELIANCE")`, which uses SmartAPI symbol search and prefers exact `-EQ` NSE matches.

## Run tests

```powershell
pytest
```

## Project layout

```text
intraday_advisor/
  backtest.py     Backtest loop and metrics
  config.py       Risk and cost settings
  data.py         OHLCV fetching, cleaning, cache helpers, sample data
  execution.py    Paper broker simulator
  fundamentals.py Screener CSV normalization and quality filter
  indicators.py   Technical indicators
  journal.py      CSV trade journal
  risk.py         Position sizing and trade plans
  screening.py    Filters and ranking
  signals.py      Signal generation
tests/
  test_*.py
app.py            Streamlit dashboard
```

## Suggested next steps

1. Add official NSE/BSE or broker minute-feed adapters.
2. Add a real database schema for OHLCV, fundamentals, FII/DII flows, and corporate actions.
3. Add walk-forward strategy optimization with strict train/test separation.
4. Add broker-specific paper/live adapters only after compliance checks, API-key storage, and rate limits are in place.
