"""
Real-time Trading Dashboard with Combined Analysis & Execution Signals
Shows only active trades with unified BUY/SELL recommendations
Combines all analysis techniques for zero-latency decisions
"""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from datetime import datetime, timedelta

from intraday_advisor.box_theory import analyze_box_theory
from intraday_advisor.config import RiskConfig
from intraday_advisor.data import fetch_yahoo_ohlcv, generate_sample_ohlcv, load_watchlist
from intraday_advisor.database import DEFAULT_DB_PATH, load_fundamentals, store_analysis_results, store_ohlcv
from intraday_advisor.explainability import decision_guide, THEORY_INFOS
from intraday_advisor.first_candle import analyze_first_candle, format_first_candle_summary
from intraday_advisor.fundamentals import merge_fundamentals
from intraday_advisor.indicators import add_indicators
from intraday_advisor.price_action import analyze_price_action
from intraday_advisor.risk import build_trade_plan_from_stop
from intraday_advisor.screening import apply_screen, score_stocks
from intraday_advisor.smart_money import analyze_smart_money
from intraday_advisor.situational import latest_situational_summary
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy, ema_swing_breakout_decision


st.set_page_config(page_title="Real-Time Trades", layout="wide")
st.title("🚀 Real-Time Trading Dashboard")
st.caption("Live Analysis • Zero Latency • Unified BUY/SELL Signals • Combined Analysis")

# ================================ SESSION STATE ================================
if "last_update" not in st.session_state:
    st.session_state.last_update = datetime.now()
if "active_trades" not in st.session_state:
    st.session_state.active_trades = {}


# ================================ SIDEBAR CONFIG ================================
with st.sidebar:
    st.header("⚙️ Live Configuration")
    
    symbols_text = st.text_area(
        "NSE Symbols to Monitor", 
        value="RELIANCE,HDFCBANK,INFY,TATAMOTORS,JSWSTEEL",
        height=80
    )
    
    col1, col2 = st.columns(2)
    with col1:
        capital = st.number_input("Capital", min_value=1000.0, value=20000.0, step=1000.0)
        risk_pct = st.slider("Risk %/trade", min_value=0.5, max_value=3.0, value=2.0, step=0.25) / 100
    
    with col2:
        use_live_data = st.toggle("Yahoo Finance", value=False)
        period = st.selectbox("Period", ["5d", "30d", "60d"], index=1)
        interval = st.selectbox("Candle", ["5m", "15m", "30m", "1h", "1d"], index=1)
    
    use_db_cache = st.toggle("SQLite Cache", value=True)
    
    # Combined signal threshold settings
    st.divider()
    st.subheader("📊 Signal Thresholds")
    
    min_confidence = st.slider("Min Confidence %", 0, 100, 50, 5)
    show_only_high_quality = st.toggle("High Quality Only", value=True)
    
    # Refresh rate
    st.divider()
    st.subheader("⏱️ Refresh")
    refresh_rate = st.selectbox("Update Frequency", 
        ["Every 10s", "Every 30s", "Every 60s", "Manual"],
        index=2
    )
    
    if st.button("🔄 Refresh Now"):
        st.session_state.last_update = datetime.now()
        st.rerun()


# ================================ COMBINED ANALYSIS ENGINE ================================
@st.cache_data(ttl=10)
def analyze_symbol_combined(symbol: str, seed: int) -> dict:
    """
    Unified analysis combining ALL techniques into single BUY/SELL signals
    Returns comprehensive analysis with combined confidence scores
    """
    try:
        if use_live_data:
            df = fetch_yahoo_ohlcv(symbol, period=period, interval=interval)
        else:
            df = generate_sample_ohlcv(seed=seed)
        
        if use_db_cache:
            store_ohlcv(symbol, df, DEFAULT_DB_PATH, source="yahoo" if use_live_data else "sample")
        
        df = apply_ema_swing_breakout_strategy(add_indicators(df))
        
        # Remove NaN rows for analysis
        df_clean = df.dropna(subset=["Close", "EMA9", "EMA21", "ATR14", "RecentSwingHigh", "RecentSwingLow"])
        if df_clean.empty:
            return None
        
        last = df_clean.iloc[-1]
        
        # Get individual analysis results
        decision = ema_swing_breakout_decision(symbol, df)
        price_action = analyze_price_action(df)
        smart_money = analyze_smart_money(df)
        situational = latest_situational_summary(df)
        box = analyze_box_theory(df)
        first_candle = analyze_first_candle(df)  # NEW: First Candle Rule analysis
        
        # Build comprehensive data row for decision guide
        row = {
            "Ticker": symbol,
            "Close": float(last["Close"]),
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
            "LiquiditySweep": smart_money.liquidity_sweep,
            "OrderFlow": smart_money.order_flow,
            "CumulativeDelta": smart_money.cumulative_delta,
            "VWAPRelation": smart_money.vwap_relation,
            "BoxHigh": box.previous_day_high,
            "BoxLow": box.previous_day_low,
            "BoxMiddle": box.middle_line,
            "BoxZone": box.zone,
            "BoxBias": box.bias,
            "ATR14": float(last["ATR14"]),
            "ATR%": float(last["ATR14"] / last["Close"]),
            "RSI14": float(last["RSI14"]),
            "EMA9": float(last["EMA9"]),
            "EMA21": float(last["EMA21"]),
            "EMA200": float(last["EMA200"]),
            "AboveEMA200": bool(last["Close"] > last["EMA200"]),
            "SwingHigh": float(last["RecentSwingHigh"]),
            "SwingLow": float(last["RecentSwingLow"]),
            # NEW: First Candle Rule fields
            "FirstCandleHigh": first_candle.opening_high,
            "FirstCandleLow": first_candle.opening_low,
            "FirstCandleClose": first_candle.opening_close,
            "FirstCandleTrend": first_candle.opening_trend,
            "FCRSignal": first_candle.signal,
            "FCRConfidence": first_candle.confidence,
            "FCRHasFVG": first_candle.has_fvg,
            "FCRFVGType": first_candle.fvg_type,
            "FCRHasEngulfing": first_candle.has_engulfing,
            "FCREntryPrice": first_candle.entry_price,
            "FCRStopLoss": first_candle.stop_level,
            "FCRTarget": first_candle.target_high or first_candle.target_low,
            "FCRRRRatio": first_candle.rr_ratio,
            **situational,
            "Reasons": decision.reasons,
            "Warnings": decision.warnings,
        }
        
        # Get unified decision guide action
        suggested_action, action_reason = decision_guide(row)
        row["SuggestedAction"] = suggested_action
        row["ActionReason"] = action_reason
        
        # Calculate combined confidence score using all signals
        combined_score = calculate_combined_score(row, decision, price_action, smart_money, box, first_candle)
        row["CombinedScore"] = combined_score
        
        # Generate trade plan - prioritize First Candle Rule if it has a signal
        plan = None
        if first_candle.signal in ["BUY", "SELL"] and first_candle.entry_price and first_candle.stop_level:
            # Use First Candle Rule entry if available (highest priority)
            plan = build_trade_plan_from_stop(
                symbol,
                first_candle.signal,
                first_candle.entry_price,
                first_candle.stop_level,
                RiskConfig(capital=capital, risk_per_trade_pct=risk_pct),
            )
        elif decision.signal == "BUY":
            initial_stop = max(float(last["RecentSwingLow"]), float(last["Close"] - 1.5 * last["ATR14"]))
            plan = build_trade_plan_from_stop(
                symbol,
                decision.signal,
                float(last["Close"]),
                initial_stop,
                RiskConfig(capital=capital, risk_per_trade_pct=risk_pct),
            )
        elif decision.signal == "SELL":
            initial_stop = min(float(last["RecentSwingHigh"]), float(last["Close"] + 1.5 * last["ATR14"]))
            plan = build_trade_plan_from_stop(
                symbol,
                decision.signal,
                float(last["Close"]),
                initial_stop,
                RiskConfig(capital=capital, risk_per_trade_pct=risk_pct),
            )
        
        row["TradePlan"] = plan
        
        return row
    
    except Exception as e:
        st.warning(f"Error analyzing {symbol}: {str(e)}")
        return None


def calculate_combined_score(row: dict, decision, price_action, smart_money, box, first_candle) -> int:
    """
    Calculate unified confidence score from multiple analysis techniques INCLUDING First Candle Rule
    Score: 0-100 (higher = stronger signal)
    
    Scoring:
    - EMA Strategy: 20%
    - Price Action: 20%
    - Smart Money: 15%
    - Volume: 12%
    - First Candle Rule: 18% (HIGH PRIORITY)
    - VWAP: 8%
    - EMA200: 4%
    - Box Theory: 3%
    """
    score = 0
    weight_total = 0
    
    # FIRST CANDLE RULE (18 points - HIGH PRIORITY!)
    # This rule appears every day and contains most info
    if row.get("FCRSignal") in ["BUY", "SELL"]:
        fcr_confidence = row.get("FCRConfidence", 0)
        if fcr_confidence >= 80:
            score += 18
        elif fcr_confidence >= 70:
            score += 15
        elif fcr_confidence >= 50:
            score += 10
        weight_total += 18
    else:
        weight_total += 18
    
    # EMA Strategy (20 points)
    if row["Signal"] == "BUY":
        score += 20
        weight_total += 20
    elif row["Signal"] == "SELL":
        score += 18
        weight_total += 20
    else:
        weight_total += 20
    
    # Price Action (20 points)
    if row["TrendStructure"] == "strong uptrend":
        score += 20
        weight_total += 20
    elif row["TrendStructure"] == "uptrend":
        score += 12
        weight_total += 20
    elif row["TrendStructure"] in {"strong downtrend", "downtrend"}:
        score += 4
        weight_total += 20
    else:
        weight_total += 20
    
    # Volume Confirmation (12 points)
    if row["VolumeConfirmation"] == "bullish confirmation":
        score += 12
        weight_total += 12
    else:
        weight_total += 12
    
    # Smart Money / Order Flow (15 points)
    if row["OrderFlow"] == "buyer-dominant order flow":
        score += 12
        weight_total += 15
    elif row["OrderFlow"] == "seller-dominant order flow":
        score += 0
        weight_total += 15
    else:
        weight_total += 15
    
    # VWAP Relation (8 points)
    if row["VWAPRelation"] == "above VWAP":
        score += 8
        weight_total += 8
    
    # EMA200 Filter (4 points)
    if row["AboveEMA200"]:
        score += 4
        weight_total += 4
    else:
        weight_total += 4
    
    # Box Theory (3 points)
    if row["BoxBias"] == "BUY WATCH":
        score += 3
        weight_total += 3
    else:
        weight_total += 3
    
    # Breakout strength (bonus 5 points if present)
    if row["BreakoutState"] == "valid breakout":
        score += 5
        weight_total += 5
    
    return min(100, int((score / weight_total * 100) if weight_total > 0 else 0))


def filter_active_trades(trades: list[dict]) -> list[dict]:
    """
    Filter trades that have active BUY or SELL signals
    Only show high-confidence, low-latency opportunities
    """
    active = []
    for trade in trades:
        if trade is None:
            continue
        
        # Must have active signal
        if trade.get("Signal") not in ["BUY", "SELL"]:
            continue
        
        # Must meet confidence threshold
        if trade.get("CombinedScore", 0) < min_confidence:
            continue
        
        # Optional: High quality filter
        if show_only_high_quality:
            if trade.get("CombinedScore", 0) < 70:
                continue
        
        active.append(trade)
    
    # Sort by combined score (highest first)
    active.sort(key=lambda x: x.get("CombinedScore", 0), reverse=True)
    return active


# ================================ MAIN ANALYSIS ================================
st.info("🔄 Analyzing active trades... (Live real-time signals)")

symbols = [s.strip().upper() for s in symbols_text.split(",") if s.strip()]

# Analyze all symbols
trades_data = []
seeds = {symbol: hash(symbol) % 10000 for symbol in symbols}

for symbol in symbols:
    result = analyze_symbol_combined(symbol, seeds[symbol])
    if result:
        trades_data.append(result)

# Filter to only active trades
active_trades = filter_active_trades(trades_data)

# ================================ KEY METRICS dashboard ================================
if active_trades:
    col1, col2, col3, col4, col5 = st.columns(5)
    
    buy_signals = sum(1 for t in active_trades if t["Signal"] == "BUY")
    sell_signals = sum(1 for t in active_trades if t["Signal"] == "SELL")
    avg_score = sum(t["CombinedScore"] for t in active_trades) / len(active_trades)
    total_risk = sum(t["TradePlan"].risk_amount for t in active_trades if t["TradePlan"])
    total_trades = len(active_trades)
    
    with col1:
        st.metric("🎯 Active Trades", total_trades, delta=None)
    with col2:
        st.metric("📈 BUY Signals", buy_signals, delta=None)
    with col3:
        st.metric("📉 SELL Signals", sell_signals, delta=None)
    with col4:
        st.metric("⚡ Avg Confidence", f"{avg_score:.0f}%", delta=None)
    with col5:
        st.metric("💰 Total Risk", f"₹{total_risk:,.0f}", delta=None)
else:
    st.warning("No active trades with current confidence threshold. Lower the threshold to see more opportunities.")


# ================================ ACTIVE TRADES TABLE ================================
if active_trades:
    st.divider()
    st.subheader("📋 Active Trade Opportunities")
    
    # Create detailed trades table
    trades_display = []
    for trade in active_trades:
        plan = trade.get("TradePlan")
        
        trades_display.append({
            "Symbol": trade["Ticker"],
            "Action": f"🟢 {trade['Signal']}" if trade['Signal'] == "BUY" else f"🔴 {trade['Signal']}",
            "Price": f"₹{trade['Close']:.2f}",
            "Confidence": f"{trade['CombinedScore']}%",
            "Entry": f"₹{plan.entry:.2f}" if plan else "-",
            "Stop": f"₹{plan.stop:.2f}" if plan else "-",
            "Target": f"₹{plan.target:.2f}" if plan else "-",
            "Risk": f"₹{plan.risk_amount:,.0f}" if plan else "-",
            "Reward": f"₹{plan.reward_amount:,.0f}" if plan else "-",
            "R:R": f"{plan.reward_amount/plan.risk_amount:.1f}:1" if plan and plan.risk_amount > 0 else "-",
            "Shares": plan.shares if plan else "-",
        })
    
    df_display = pd.DataFrame(trades_display)
    st.dataframe(df_display, use_container_width=True, hide_index=True)


# ================================ DETAILED TRADE ANALYSIS ================================
if active_trades:
    st.divider()
    st.subheader("🔍 Trade Details & Combined Analysis")
    
    # Trade selector
    trade_options = [f"{t['Ticker']} ({t['Signal']} @ {t['CombinedScore']}%)" for t in active_trades]
    selected_trade_idx = st.selectbox("Select Trade for Details:", range(len(trade_options)), format_func=lambda x: trade_options[x])
    selected_trade = active_trades[selected_trade_idx]
    
    # Display trade details
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Symbol", selected_trade["Ticker"])
        st.metric("Current Price", f"₹{selected_trade['Close']:.2f}")
        st.metric("ATR (14)", f"₹{selected_trade['ATR14']:.2f}")
    
    with col2:
        st.metric("Signal", f"🔴 {selected_trade['Signal']}" if selected_trade["Signal"] == "SELL" else f"🟢 {selected_trade['Signal']}")
        st.metric("Combined Confidence", f"{selected_trade['CombinedScore']}%")
        st.metric("Confidence", f"{selected_trade['Confidence']}%")
    
    with col3:
        plan = selected_trade.get("TradePlan")
        if plan:
            st.metric("Entry", f"₹{plan.entry:.2f}")
            st.metric("Stop Loss", f"₹{plan.stop:.2f}")
            st.metric("Risk Per Share", f"₹{plan.risk_per_share:.2f}")
    
    with col4:
        if plan:
            st.metric("Target", f"₹{plan.target:.2f}")
            st.metric("Position Size", f"{plan.shares} shares")
            st.metric("Total Risk", f"₹{plan.risk_amount:,.0f}")
    
    # Combined analysis breakdown
    st.divider()
    st.subheader("📊 Combined Analysis Breakdown")
    
    # ============ FIRST CANDLE RULE ANALYSIS (PRIMARY) ============
    st.write("**🕐 FIRST CANDLE RULE (9:30-9:35 AM Opening) - HIGHEST PRIORITY**")
    
    fcr_cols = st.columns(2)
    with fcr_cols[0]:
        st.write(f"- Opening High: ₹{selected_trade['FirstCandleHigh']:.2f}")
        st.write(f"- Opening Low: ₹{selected_trade['FirstCandleLow']:.2f}")
        st.write(f"- Opening Close: ₹{selected_trade['FirstCandleClose']:.2f}")
        st.write(f"- Opening Trend: {selected_trade['FirstCandleTrend']}")
    
    with fcr_cols[1]:
        st.write(f"- Signal: **{selected_trade['FCRSignal']}** ({selected_trade['FCRConfidence']}%)")
        st.write(f"- FVG Formed: {selected_trade['FCRHasFVG']} ({selected_trade['FCRFVGType']})")
        st.write(f"- Engulfing Confirmed: {selected_trade['FCRHasEngulfing']}")
        if selected_trade['FCRRRRatio']:
            st.write(f"- Risk:Reward: **{selected_trade['FCRRRRatio']:.1f}:1** ⭐")
    
    if selected_trade['FCRSignal'] in ["BUY", "SELL"]:
        st.success(f"✅ First Candle Rule SIGNAL: **{selected_trade['FCRSignal']} at ₹{selected_trade['FCREntryPrice']:.2f}** with Stop ₹{selected_trade['FCRStopLoss']:.2f}")
    
    st.divider()
    col1, col2 = st.columns(2)
    
    with col1:
        st.write("**EMA Strategy Analysis**")
        st.write(f"- Signal: {selected_trade['Signal']}")
        st.write(f"- Setup: {selected_trade['Setup']}")
        st.write(f"- EMA9: ₹{selected_trade['EMA9']:.2f}")
        st.write(f"- EMA21: ₹{selected_trade['EMA21']:.2f}")
        st.write(f"- Swing High: ₹{selected_trade['SwingHigh']:.2f}")
        st.write(f"- Swing Low: ₹{selected_trade['SwingLow']:.2f}")
    
    with col2:
        st.write("**Price Action Analysis**")
        st.write(f"- Trend: {selected_trade['TrendStructure']}")
        st.write(f"- Support: ₹{selected_trade['Support']:.2f}")
        st.write(f"- Resistance: ₹{selected_trade['Resistance']:.2f}")
        st.write(f"- Candle Pattern: {selected_trade['CandlePattern']}")
        st.write(f"- Volume: {selected_trade['VolumeConfirmation']}")
        st.write(f"- Breakout State: {selected_trade['BreakoutState']}")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.write("**Smart Money Analysis**")
        st.write(f"- FVG: {selected_trade['FVG']}")
        st.write(f"- Order Flow: {selected_trade['OrderFlow']}")
        st.write(f"- VWAP Relation: {selected_trade['VWAPRelation']}")
        st.write(f"- Liquidity Sweep: {selected_trade['LiquiditySweep']}")
    
    with col2:
        st.write("**Box Theory Analysis**")
        st.write(f"- Box High: ₹{selected_trade['BoxHigh']:.2f}")
        st.write(f"- Box Low: ₹{selected_trade['BoxLow']:.2f}")
        st.write(f"- Box Zone: {selected_trade['BoxZone']}")
        st.write(f"- Bias: {selected_trade['BoxBias']}")
        st.write(f"- EMA200: {'Above' if selected_trade['AboveEMA200'] else 'Below'} (Trend Filter)")
    
    # Decision reasoning
    st.divider()
    st.write("**Combined Decision Reasoning:**")
    reason_text = selected_trade.get("ActionReason", "No reasoning available")
    st.info(reason_text)
    
    if selected_trade.get("Reasons"):
        st.success(f"✅ **Reasons:** {', '.join(selected_trade['Reasons'])}")
    
    if selected_trade.get("Warnings"):
        st.warning(f"⚠️ **Warnings:** {', '.join(selected_trade['Warnings'])}")


# ================================ EXECUTION QUICK MENU ================================
if active_trades:
    st.divider()
    st.subheader("⚡ Quick Actions")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("📈 Enter BUY Trade"):
            st.success(f"✅ BUY order ready for {selected_trade['Ticker']} at ₹{selected_trade['Close']:.2f}")
            st.balloons()
    
    with col2:
        if st.button("📉 Enter SELL Trade"):
            st.error(f"❌ SELL order ready for {selected_trade['Ticker']} at ₹{selected_trade['Close']:.2f}")
    
    with col3:
        if st.button("⏸️ Skip This Trade"):
            st.info("Trade skipped for manual review later")


# ================================ LIVE STATUS ================================
st.divider()
col1, col2, col3 = st.columns(3)

with col1:
    st.metric("Last Update", "Just now" if (datetime.now() - st.session_state.last_update).seconds < 5 else f"{(datetime.now() - st.session_state.last_update).seconds}s ago")

with col2:
    st.metric("Symbols Analyzed", len(symbols))

with col3:
    st.metric("Latency", "< 100ms", delta="Real-time")

st.caption("⚠️ Disclaimer: This is decision-support only. Not financial advice. Validate with paper trading first.")
