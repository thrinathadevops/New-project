import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from intraday_advisor.box_theory import analyze_box_theory
from intraday_advisor.config import RiskConfig
from intraday_advisor.data import fetch_yahoo_ohlcv, generate_sample_ohlcv, load_watchlist
from intraday_advisor.database import DEFAULT_DB_PATH, load_fundamentals, store_analysis_results, store_fundamentals, store_ohlcv
from intraday_advisor.explainability import decision_guide, theory_info_rows
from intraday_advisor.fundamentals import merge_fundamentals
from intraday_advisor.indicators import add_indicators
from intraday_advisor.price_action import analyze_price_action
from intraday_advisor.risk import build_trade_plan_from_stop
from intraday_advisor.screening import apply_screen, score_stocks
from intraday_advisor.screener_provider import fetch_fundamental_candidates
from intraday_advisor.smart_money import analyze_smart_money
from intraday_advisor.situational import latest_situational_summary
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy, ema_swing_breakout_decision


st.set_page_config(page_title="Intraday Trade Advisor", layout="wide")
st.title("Intraday Trade Advisor")
st.caption("Decision-support analysis only. It ranks watchlist candidates and estimates risk/reward; it does not place live orders.")

with st.sidebar:
    st.header("Settings")
    symbols_text = st.text_area("NSE symbols", value="RELIANCE,HDFCBANK,INFY,TATAMOTORS,JSWSTEEL")
    capital = st.number_input("Capital", min_value=1000.0, value=20000.0, step=1000.0)
    risk_pct = st.slider("Risk per trade", min_value=0.5, max_value=3.0, value=2.0, step=0.25) / 100
    use_live_data = st.toggle("Use Yahoo Finance data", value=False)
    period = st.selectbox("Data period", ["5d", "30d", "60d"], index=1)
    interval = st.selectbox("Candle interval", ["5m", "15m", "30m", "1h", "1d"], index=1)
    use_screener_auto = st.toggle("Use automatic Screener screen", value=True)
    use_db_cache = st.toggle("Use SQLite cache", value=True)


def analyse_symbol(symbol: str, seed: int) -> tuple[pd.DataFrame, dict]:
    if use_live_data:
        df = fetch_yahoo_ohlcv(symbol, period=period, interval=interval)
    else:
        df = generate_sample_ohlcv(seed=seed)
    if use_db_cache:
        store_ohlcv(symbol, df, DEFAULT_DB_PATH, source="yahoo" if use_live_data else "sample")
    df = apply_ema_swing_breakout_strategy(add_indicators(df))
    last = df.dropna(subset=["Close", "EMA9", "EMA21", "ATR14", "RecentSwingHigh", "RecentSwingLow"]).iloc[-1]
    decision = ema_swing_breakout_decision(symbol, df)
    price_action = analyze_price_action(df)
    smart_money = analyze_smart_money(df)
    situational = latest_situational_summary(df)
    box = analyze_box_theory(df)
    plan = None
    if decision.signal == "BUY":
        initial_stop = max(float(last["RecentSwingLow"]), float(last["Close"] - 1.5 * last["ATR14"]))
        plan = build_trade_plan_from_stop(
            symbol,
            decision.signal,
            float(last["Close"]),
            initial_stop,
            RiskConfig(capital=capital, risk_per_trade_pct=risk_pct),
        )
    summary = {
        "Ticker": symbol,
        "Close": float(last["Close"]),
        "Volume": float(last["Volume"]),
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
        "Signal": decision.signal,
        "Confidence": decision.confidence,
        "Setup": decision.setup,
        "TrendStructure": price_action.trend,
        "Support": price_action.support,
        "Resistance": price_action.resistance,
        "CandlePattern": price_action.candle_pattern,
        "CandleBias": price_action.candle_bias,
        "VolumeConfirmation": price_action.volume_confirmation,
        "BreakoutState": price_action.breakout_state,
        "FVG": smart_money.fvg_direction,
        "FVGLower": smart_money.fvg_lower,
        "FVGUpper": smart_money.fvg_upper,
        "FVGMitigated": smart_money.fvg_mitigated,
        "LiquiditySweep": smart_money.liquidity_sweep,
        "OrderFlow": smart_money.order_flow,
        "CumulativeDelta": smart_money.cumulative_delta,
        "VolumePOC": smart_money.volume_poc,
        "ValueAreaLow": smart_money.value_area_low,
        "ValueAreaHigh": smart_money.value_area_high,
        "VWAPRelation": smart_money.vwap_relation,
        "BoxHigh": box.previous_day_high,
        "BoxLow": box.previous_day_low,
        "BoxMiddle": box.middle_line,
        "BoxZone": box.zone,
        "BoxWickSignal": box.wick_signal,
        "BoxBias": box.bias,
        **situational,
        "Reasons": "; ".join(decision.reasons),
        "Warnings": "; ".join(decision.warnings),
        "Plan": plan,
    }
    suggested_action, action_reason = decision_guide(summary)
    summary["SuggestedAction"] = suggested_action
    summary["ActionReason"] = action_reason
    return df, summary


fundamental_candidates = pd.DataFrame()
if use_screener_auto:
    try:
        fundamental_candidates = fetch_fundamental_candidates()
        if use_db_cache:
            store_fundamentals(fundamental_candidates, DEFAULT_DB_PATH)
        st.sidebar.success(f"{len(fundamental_candidates)} Screener stocks passed fundamentals.")
    except Exception as exc:
        st.sidebar.warning(f"Automatic Screener fetch skipped: {exc}")
        if use_db_cache:
            cached = load_fundamentals(DEFAULT_DB_PATH)
            if not cached.empty:
                fundamental_candidates = cached
                st.sidebar.info(f"Using {len(fundamental_candidates)} cached fundamental rows from SQLite.")

symbols = fundamental_candidates["Ticker"].tolist() if not fundamental_candidates.empty else load_watchlist(symbols_text)
results = []
frames = {}
for index, symbol in enumerate(symbols):
    try:
        frame, summary = analyse_symbol(symbol, seed=7 + index)
        frames[symbol] = frame
        results.append(summary)
    except Exception as exc:
        st.warning(f"{symbol}: {exc}")

if not results:
    st.stop()

summary_df = pd.DataFrame([{key: value for key, value in row.items() if key != "Plan"} for row in results])
if not fundamental_candidates.empty:
    summary_df = merge_fundamentals(summary_df, fundamental_candidates)

technical_candidates = summary_df[summary_df["AboveEMA200"]].copy()
ranked = score_stocks(apply_screen(technical_candidates, min_market_cap=2_000_000_000, min_atr_pct=0.005))
if use_db_cache and not ranked.empty:
    store_analysis_results(ranked, DEFAULT_DB_PATH)
    st.caption(f"Cached latest analysis in {DEFAULT_DB_PATH}.")

st.subheader("Ranked Watchlist")
with st.expander("Info: how to understand each theory"):
    st.dataframe(pd.DataFrame(theory_info_rows()), use_container_width=True)
if not fundamental_candidates.empty:
    st.caption("Only stocks passing your Screener fundamentals and trading above EMA200 are considered.")
st.dataframe(
    ranked[["Ticker", "SuggestedAction", "ActionReason", "Signal", "Score", "Close", "EMA200", "AboveEMA200", "ATR14", "ATR%", "RSI14", "ADTV20", "Momentum10"]].round(3),
    use_container_width=True,
)
st.dataframe(
    ranked[["Ticker", "Setup", "Confidence", "TrendStructure", "CandlePattern", "VolumeConfirmation", "BreakoutState", "Support", "Resistance", "EMA9", "EMA21", "BreakoutLevel", "SwingHigh", "SwingLow", "Reasons", "Warnings"]],
    use_container_width=True,
)
st.dataframe(
    ranked[["Ticker", "FVG", "FVGLower", "FVGUpper", "FVGMitigated", "LiquiditySweep", "OrderFlow", "CumulativeDelta", "VolumePOC", "ValueAreaLow", "ValueAreaHigh", "VWAPRelation"]],
    use_container_width=True,
)
st.dataframe(
    ranked[["Ticker", "BoxHigh", "BoxLow", "BoxMiddle", "BoxZone", "BoxWickSignal", "BoxBias"]],
    use_container_width=True,
)
st.dataframe(
    ranked[["Ticker", "SituationalRule", "SituationalLevel", "SituationalTargetDate", "SituationalVisited", "SituationalNote"]],
    use_container_width=True,
)
fundamental_columns = ["Ticker", "MarketCapCr", "ROE", "ROCE", "DebtToEquity", "SalesGrowth", "PromoterHolding", "ProfitGrowth", "OPM", "EPS"]
if not fundamental_candidates.empty and all(column in ranked.columns for column in fundamental_columns):
    st.subheader("Fundamental Quality")
    st.dataframe(ranked[fundamental_columns].round(2), use_container_width=True)

plans = [row["Plan"] for row in results if row["Plan"] is not None]
st.subheader("Watchlist Trade Plans")
if plans:
    plans_df = pd.DataFrame([plan.__dict__ for plan in plans])
    st.dataframe(plans_df, use_container_width=True)
    st.info("Use these as watchlist plans only. Confirm manually in your broker app before taking any trade.")
else:
    st.info("No fresh BUY/SELL setup on the latest candle. Keep monitoring or loosen filters after testing.")

selected = st.selectbox("Chart", list(frames.keys()))
chart_df = frames[selected].tail(120)
fig = go.Figure()
fig.add_trace(go.Candlestick(x=chart_df.index, open=chart_df["Open"], high=chart_df["High"], low=chart_df["Low"], close=chart_df["Close"], name="Price"))
fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df["SMA20"], name="SMA20"))
fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df["SMA50"], name="SMA50"))
fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df["EMA9"], name="EMA9"))
fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df["EMA21"], name="EMA21"))
fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df["EMA200"], name="EMA200"))
fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df["RecentSwingHigh"], name="Recent Swing High"))
box = analyze_box_theory(frames[selected])
if box.previous_day_high is not None and box.previous_day_low is not None and box.middle_line is not None:
    fig.add_hline(y=box.previous_day_high, line_dash="dash", annotation_text="Box High")
    fig.add_hline(y=box.middle_line, line_dash="dot", annotation_text="Box Middle")
    fig.add_hline(y=box.previous_day_low, line_dash="dash", annotation_text="Box Low")
fig.update_layout(height=560, xaxis_rangeslider_visible=False)
st.plotly_chart(fig, use_container_width=True)
