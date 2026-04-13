from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pandas as pd

from intraday_advisor.config import RiskConfig
from intraday_advisor.data import generate_sample_ohlcv, load_watchlist
from intraday_advisor.fundamentals import merge_fundamentals
from intraday_advisor.indicators import add_indicators
from intraday_advisor.risk import build_trade_plan_from_stop
from intraday_advisor.screening import apply_screen, score_stocks
from intraday_advisor.screener_provider import fetch_fundamental_candidates
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy, ema_swing_breakout_decision


DEFAULT_SYMBOLS = "RELIANCE,HDFCBANK,INFY,TATAMOTORS,JSWSTEEL"


def analyse(symbol: str, seed: int, capital: float, risk_pct: float) -> tuple[dict, object | None]:
    df = apply_ema_swing_breakout_strategy(add_indicators(generate_sample_ohlcv(seed=seed)))
    last = df.dropna(subset=["Close", "EMA9", "EMA21", "ATR14", "RecentSwingHigh", "RecentSwingLow"]).iloc[-1]
    decision = ema_swing_breakout_decision(symbol, df)
    plan = None
    if decision.signal == "BUY":
        initial_stop = max(float(last["RecentSwingLow"]), float(last["Close"] - 1.5 * last["ATR14"]))
        plan = build_trade_plan_from_stop(symbol, decision.signal, float(last["Close"]), initial_stop, RiskConfig(capital=capital, risk_per_trade_pct=risk_pct))
    row = {
        "Ticker": symbol,
        "Signal": decision.signal,
        "Confidence": decision.confidence,
        "Setup": decision.setup,
        "Reasons": "; ".join(decision.reasons),
        "Warnings": "; ".join(decision.warnings),
        "Close": float(last["Close"]),
        "ADTV20": float(last["ADTV20"]),
        "ATR14": float(last["ATR14"]),
        "ATR%": float(last["ATR14"] / last["Close"]),
        "RSI14": float(last["RSI14"]),
        "SMA20": float(last["SMA20"]),
        "SMA50": float(last["SMA50"]),
        "EMA9": float(last["EMA9"]),
        "EMA21": float(last["EMA21"]),
        "EMA200": float(last["EMA200"]),
        "AboveEMA200": bool(last["Close"] > last["EMA200"]),
        "BreakoutLevel": float(last["EMABreakoutLevel"]) if pd.notna(last["EMABreakoutLevel"]) else None,
        "SwingHigh": float(last["RecentSwingHigh"]),
        "SwingLow": float(last["RecentSwingLow"]),
        "Momentum10": float(last["Momentum10"]),
        "MarketCap": 25_000_000_000,
        "FloatPct": 35.0,
        "Sector": "Demo",
    }
    return row, plan


def table_html(df: pd.DataFrame) -> str:
    if df.empty:
        return "<p>No rows matched the current screen.</p>"
    return df.round(3).to_html(index=False, classes="data-table", border=0)


def render_page(query: dict[str, list[str]]) -> str:
    symbols_text = query.get("symbols", [DEFAULT_SYMBOLS])[0]
    capital = float(query.get("capital", ["20000"])[0])
    risk_pct = float(query.get("risk", ["2"])[0]) / 100
    fundamental_candidates = pd.DataFrame()
    screener_note = ""
    try:
        fundamental_candidates = fetch_fundamental_candidates()
        screener_note = f"{len(fundamental_candidates)} Screener stocks passed fundamentals."
    except Exception as exc:
        screener_note = f"Automatic Screener fetch skipped: {exc}"

    symbols = fundamental_candidates["Ticker"].tolist() if not fundamental_candidates.empty else load_watchlist(symbols_text)
    rows = []
    plans = []
    for index, symbol in enumerate(symbols):
        row, plan = analyse(symbol, index + 7, capital, risk_pct)
        rows.append(row)
        if plan:
            plans.append(plan.__dict__)
    summary = pd.DataFrame(rows)
    if not fundamental_candidates.empty:
        summary = merge_fundamentals(summary, fundamental_candidates)
    technical_candidates = summary[summary["AboveEMA200"]].copy()
    ranked = score_stocks(apply_screen(technical_candidates, min_market_cap=2_000_000_000, min_atr_pct=0.005))
    plan_df = pd.DataFrame(plans)
    note = f"{screener_note} Only Screener candidates above EMA200 are considered when the provider is configured."

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>Intraday Trade Advisor</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #f7f7f4; color: #1f2933; }}
    header {{ padding: 28px 36px; background: #0f766e; color: white; }}
    main {{ padding: 24px 36px 48px; }}
    form {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: end; margin-bottom: 24px; }}
    label {{ display: grid; gap: 6px; font-weight: 700; }}
    input {{ padding: 10px 12px; border: 1px solid #b8c2cc; border-radius: 6px; min-width: 180px; }}
    button {{ padding: 11px 16px; border: 0; border-radius: 6px; background: #334e68; color: white; font-weight: 700; cursor: pointer; }}
    section {{ margin-top: 28px; overflow-x: auto; }}
    .notice {{ color: #52606d; margin-top: 8px; max-width: 900px; }}
    .data-table {{ border-collapse: collapse; min-width: 900px; width: 100%; background: white; }}
    .data-table th, .data-table td {{ padding: 10px 12px; border-bottom: 1px solid #d9e2ec; text-align: right; }}
    .data-table th:first-child, .data-table td:first-child {{ text-align: left; }}
    .data-table th {{ background: #e6fffa; color: #102a43; }}
  </style>
</head>
<body>
  <header>
    <h1>Intraday Trade Advisor</h1>
    <p class="notice">Paper-trading analysis only. Signals estimate risk and reward; they do not guarantee profit or place live orders. Page refreshes every 60 seconds.</p>
  </header>
  <main>
    <form method="get">
      <label>Symbols <input name="symbols" value="{html.escape(symbols_text)}"></label>
      <label>Capital <input name="capital" type="number" value="{capital:.0f}"></label>
      <label>Risk % <input name="risk" type="number" step="0.25" value="{risk_pct * 100:.2f}"></label>
      <button type="submit">Analyze</button>
    </form>
    <p class="notice">{html.escape(note)}</p>
    <section>
      <h2>Ranked Watchlist</h2>
      {table_html(ranked[["Ticker", "Signal", "Confidence", "Setup", "Score", "Close", "EMA9", "EMA21", "EMA200", "AboveEMA200", "BreakoutLevel", "SwingHigh", "SwingLow", "ATR14", "ATR%", "RSI14", "ADTV20", "Momentum10"]])}
      <h3>Reasons</h3>
      {table_html(ranked[["Ticker", "Reasons", "Warnings"]])}
      <h3>Fundamentals</h3>
      {table_html(ranked[[column for column in ["Ticker", "MarketCapCr", "ROE", "ROCE", "DebtToEquity", "SalesGrowth", "PromoterHolding", "ProfitGrowth", "OPM", "EPS"] if column in ranked.columns]])}
    </section>
    <section>
      <h2>Trade Plans</h2>
      {table_html(plan_df) if not plan_df.empty else "<p>No fresh BUY/SELL setup on the latest candle.</p>"}
    </section>
  </main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        body = render_page(parse_qs(parsed.query)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8501), Handler)
    print("Intraday Trade Advisor running at http://127.0.0.1:8501")
    server.serve_forever()
