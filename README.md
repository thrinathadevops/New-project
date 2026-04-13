# Intraday Trade Advisor

Decision-support prototype for intraday market pre-analysis, best-stock shortlisting, signal monitoring, risk sizing, and backtesting.

This app does not guarantee profit and does not place live broker orders. Treat the output as research support. Validate every strategy with out-of-sample data, realistic costs, and paper trading before risking money.

## What it includes

- Data ingestion and cleaning for OHLCV, with optional Yahoo Finance data and deterministic offline sample data.
- Technical indicators: SMA20/50/200, EMA12/26, ATR14, RSI14, MACD, OBV, VWAP, 10-bar momentum, ADTV20.
- Fundamental screening from a Screener-style CSV export, plus technical screening by price, liquidity, ATR percentage, and EMA200 trend.
- Scoring and ranking for watchlists.
- BUY/HOLD/SELL signal generation using EMA9/EMA21 crossover, recent swing-high breakout confirmation, VWAP, RSI, volume, and ATR context.
- Price-action confirmation for trend structure, support/resistance, candle psychology, volume confirmation, breakout/retest state, and stop zone.
- Smart-money context for fair value gaps, liquidity sweeps, OHLCV-based order-flow proxy, VWAP relation, and volume profile POC/value area.
- Weekday situational analysis for Friday-low Monday revisits and Wednesday-low Thursday revisits.
- Box theory context using the previous day high/low box, middle no-trade line, and wick rejection at box edges.
- Swing/ATR-based position sizing and stop/target planning. The strategy exit is EMA9 crossing back below EMA21.
- Backtesting with brokerage, GST, STT, and slippage assumptions.
- Paper broker and trade journal building blocks.
- Optional Angel One SmartAPI adapter for historical candles and symbol-token lookup. The dashboard does not place live orders.
- Streamlit dashboard and unit tests.
- SQLite cache for OHLCV, fundamentals, analysis results, and journal-ready records.

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

## Database

The app uses SQLite by default:

```text
data/intraday_advisor.sqlite
```

SQLite is a good fit for this local analyzer because it needs no separate server and can store:

```text
ohlcv              price/volume candles by ticker and source
fundamentals       Screener quality-filter data
analysis_results   latest ranked watchlist output and reasons
trade_journal      journal-ready paper/order records
```

If you later run live tick data continuously or use multiple machines, this layer can be upgraded to Postgres without changing the strategy modules.

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

After that, it waits for the EMA9/EMA21 crossover and recent swing-high breakout setup. The app shows the best watchlist candidates with risk/reward guidance; it does not automatically buy or sell.

## Price-action confirmation

The analyzer also reads market psychology before accepting a setup:

```text
Trend structure: higher highs + higher lows = uptrend; lower highs + lower lows = downtrend.
Support/resistance: recent reaction zones define breakout levels, entries, exits, and stop zones.
Candlestick psychology: bullish/bearish engulfing, pin bars, doji, rejection wicks, buyer/seller control.
Volume confirmation: breakout with rising volume is stronger; breakout with weak volume is warned as risky.
Breakout strategy: watch break and retest behavior around resistance/support.
Risk management: position size comes from capital risk, stop distance, and planned reward/risk.
```

## Smart-money context

The analyzer adds these confirmations to the EMA setup:

```text
Fair value gap: bullish FVG means the current low is above the high from two candles back; bearish FVG is the inverse.
FVG + EMA pairing: bullish setups are stronger when price is above EMA9/EMA21 and aligned with a bullish FVG.
Liquidity sweep: checks whether price swept recent highs/lows and rejected or broke through.
Order flow proxy: estimates buyer/seller pressure from candle body location and volume. True order flow needs broker tick/depth data.
VWAP: above VWAP supports long bias; below VWAP adds a warning.
Volume profile: estimates point of control and value area from recent close/volume distribution.
```

## Weekday Situational Analysis

The analyzer flags two low-revisit conditions:

```text
Friday rule:
If Friday high is lower than Thursday high, watch Friday low as a possible Monday revisit level.

Wednesday rule:
If Wednesday high is lower than Monday high, watch Wednesday low as a possible Thursday revisit level.
```

These are shown as situational levels, not automatic trades. Use them as context with EMA/FVG/VWAP/price-action confirmation.

## Box Theory

The analyzer builds a box from the previous trading day:

```text
Box high = previous day's high
Box low = previous day's low
Middle line = 50% of that range
```

Rules:

```text
Price near bottom of box: only look for BUY WATCH setups.
Price near top of box: only look for SELL WATCH setups.
Price near middle line: no trade; ignore signals because false signals are common there.
Entry confirmation: look for a long-wick rejection candle at the box edge.
```

In code this appears as `BoxHigh`, `BoxLow`, `BoxMiddle`, `BoxZone`, `BoxWickSignal`, and `BoxBias`.

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

The dashboard does not place live orders. Keep `ENABLE_LIVE_TRADING=NO` unless you are separately testing broker integration code outside the analyzer UI.

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
