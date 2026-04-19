with open("trading_dashboard.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

start_idx = -1
for i, l in enumerate(lines):
    if "col1, col2, col3, col4 = st.columns(4)" in l:
        start_idx = i
        break

end_idx = -1
if start_idx != -1:
    for i in range(start_idx, len(lines)):
        if "st.write(\"**EMA Strategy Analysis**\")" in lines[i]:
            end_idx = i - 3
            break

if start_idx != -1 and end_idx != -1:
    patch = """    col1, col2, col3, col4 = st.columns(4)
    
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

    # ============ DEEP DERIVATIVES OPTIONS ANALYSIS ============
    opt_data = selected_trade.get("OptionData")
    if opt_data and not opt_data.get("error"):
        st.divider()
        st.subheader("⚙️ Derivatives Strategy (Option Chain)")
        st.markdown(f"<p style='color:#8892b0; font-size: 14px;'><i>{opt_data.get('data_source')}</i></p>", unsafe_allow_html=True)
        
        o_col1, o_col2, o_col3, o_col4 = st.columns(4)
        with o_col1:
            st.metric("Contract", opt_data.get("contract"))
            st.metric("PCR", f"{opt_data.get('pcr')}")
        with o_col2:
            st.metric("Call (CE) Wall", f"₹{opt_data.get('max_ce_wall'):,}")
            st.metric("Put (PE) Wall", f"₹{opt_data.get('max_pe_wall'):,}")
        with o_col3:
            st.metric("Option Bias", opt_data.get("bias"))
            st.metric("Max Pain", f"₹{opt_data.get('max_pain'):,}")
        with o_col4:
            st.metric("CE LTP", f"₹{opt_data.get('atm_ce_ltp'):.2f}")
            st.metric("PE LTP", f"₹{opt_data.get('atm_pe_ltp'):.2f}")
        
        with st.expander("View Option Chain Reasoning"):
            for reason in opt_data.get("reasons", []):
                st.write(f"- {reason}")

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
    col1, col2 = st.columns(2)\\n"""
    
    lines[start_idx:end_idx] = [patch]
    with open("trading_dashboard.py", "w", encoding="utf-8") as f:
        f.writelines(lines)
    print("PATCH APPLIED.")
else:
    print(f"FAILED TO FIND BOUNDARIES: start {start_idx}, end {end_idx}")
