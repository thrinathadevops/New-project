"""
NSE Options Advisor — Professional Trading Dashboard
====================================================
Features:
• Live blinking status indicator  (auto-refreshes every 30 s during market hours)
• NIFTY & BANKNIFTY Index Option suggestions (PCR, Max Pain, OI analysis)
• Nifty-50 / BankNifty / F&O stock scan with 10-layer confluence
• Annotated candlestick chart with EMA, VWAP, FVG, BUY/SELL markers
• Market breadth pulse (advances vs declines)
• Portfolio tracker (Angel One API + manual)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from streamlit_autorefresh import st_autorefresh

from intraday_advisor.angel_one import AngelOneClient
from intraday_advisor.box_theory import analyze_box_theory
from intraday_advisor.config import RiskConfig
from intraday_advisor.data import fetch_yahoo_ohlcv, generate_sample_ohlcv
from intraday_advisor.indicators import add_indicators
from intraday_advisor.nse_option_chain import suggest_index_option
from intraday_advisor.nse_watchlist import get_scan_universe
from intraday_advisor.price_action import analyze_price_action
from intraday_advisor.risk import build_trade_plan_from_stop
from intraday_advisor.smart_money import analyze_smart_money
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy, ema_swing_breakout_decision

# ── Page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="NSE Options Advisor",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── IST helpers ──────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist() -> datetime:
    return datetime.now(IST)

def is_market_open() -> bool:
    n = now_ist()
    if n.weekday() >= 5:          # Saturday / Sunday
        return False
    market_open  = n.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = n.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= n <= market_close

# ── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

.live-dot {
    display:inline-block; width:12px; height:12px;
    border-radius:50%; background:#22c55e;
    animation: blink 1s infinite;
    margin-right:8px; vertical-align:middle;
}
.offline-dot {
    display:inline-block; width:12px; height:12px;
    border-radius:50%; background:#ef4444;
    margin-right:8px; vertical-align:middle;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.1} }

.live-bar {
    background:linear-gradient(90deg,#0f2027,#203a43,#2c5364);
    padding:0.6rem 1.2rem; border-radius:10px;
    display:flex; align-items:center; justify-content:space-between;
    margin-bottom:1rem;
}
.call-card { background:#052e16; border:1.5px solid #22c55e; border-radius:12px; padding:1.2rem; }
.put-card  { background:#3b0007; border:1.5px solid #ef4444; border-radius:12px; padding:1.2rem; }
.wait-card { background:#1c1917; border:1.5px solid #d97706; border-radius:12px; padding:1.2rem; }
.big-dir   { font-size:1.8rem; font-weight:800; }
.metric-row{ display:flex; gap:1rem; flex-wrap:wrap; }
.metric-box{ background:#0f172a; border-radius:8px; padding:0.7rem 1rem;
             border:1px solid #1e3a5f; min-width:120px; }
.metric-box .label { font-size:0.72rem; color:#94a3b8; text-transform:uppercase; }
.metric-box .value { font-size:1.3rem; font-weight:700; color:#e2e8f0; }
.oi-table  { font-size:0.82rem; }
.green { color:#22c55e; } .red { color:#ef4444; } .yellow { color:#facc15; }
div[data-testid="metric-container"] {
    background:#1e293b; border-radius:8px; padding:0.5rem 0.7rem;
    border:1px solid #334155;
}
</style>
""", unsafe_allow_html=True)

# ── Auto-refresh ─────────────────────────────────────────────────────────────
# Portfolio tracker refreshes every 5 s; scan data is cached for 5 min
# so the heavy scan only re-runs when cache expires, not on every 5 s tick.
refresh_interval_ms = 5_000 if is_market_open() else 60_000
count = st_autorefresh(interval=refresh_interval_ms, key="live_refresh")

# ── Session state ─────────────────────────────────────────────────────────────
for key, default in [("portfolio", []), ("scan_results", []), ("last_scan", 0.0)]:
    if key not in st.session_state:
        st.session_state[key] = default

# ══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.title("⚙️ Settings")

    scan_mode = st.selectbox(
        "📋 Scan Universe",
        ["nifty50", "banknifty", "fno", "all", "custom"],
        index=0,
    )
    custom_syms = ""
    if scan_mode == "custom":
        custom_syms = st.text_area("Symbols (comma-sep)", value="RELIANCE,HDFCBANK,INFY", height=70)

    st.divider()
    use_live   = st.toggle("🌐 Live Yahoo Finance Data", value=True)
    interval   = st.selectbox("Candle Interval", ["5m","15m","1h"], index=1)
    hist_period= st.selectbox("History Period",  ["5d","15d","30d"], index=0)

    st.divider()
    capital   = st.number_input("Capital (₹)", 5000.0, 1_000_000.0, 20000.0, 1000.0)
    risk_pct  = st.number_input("Risk % / Trade (e.g. 25% = 5K risk on 20K cap)", 1.0, 100.0, 25.0, 1.0) / 100
    min_rr    = st.slider("Strict Min Risk:Reward", 1.0, 5.0, 3.0, 0.5)
    min_conf  = st.slider("Min Confluence Score", 0, 100, 50, 5)

    st.divider()
    fetch_index_options = st.toggle("📊 Fetch NIFTY/BankNifty OI Chain", value=True,
                                     help="Fetches live OI data from NSE for NIFTY & BANKNIFTY index options")
    if st.button("🔍 SCAN NOW", type="primary", use_container_width=True):
        st.session_state.scan_results = []
        st.session_state.last_scan    = 0.0
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
#  TOP STATUS BAR — blinking live indicator
# ══════════════════════════════════════════════════════════════════════════════
market_open = is_market_open()
now_str     = now_ist().strftime("%d %b %Y  %H:%M:%S IST")
dot_html    = '<span class="live-dot"></span>' if market_open else '<span class="offline-dot"></span>'
mkt_status  = f"{dot_html} <b style='color:#22c55e'>MARKET OPEN</b>" if market_open else \
              f"{dot_html} <b style='color:#ef4444'>MARKET CLOSED</b>"
refresh_label = f"Auto-refreshes every {'30s' if market_open else '5m'}"

st.markdown(f"""
<div class="live-bar">
  <div style="font-size:1.4rem;font-weight:800">📊 NSE Options Advisor</div>
  <div>{mkt_status} &nbsp;|&nbsp; 🕒 {now_str} &nbsp;|&nbsp; 🔄 {refresh_label}</div>
</div>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def nse_strike(price: float, direction: str) -> tuple[int, str]:
    step = 10 if price < 500 else 20 if price < 1000 else 50 if price < 3000 else 100 if price < 10000 else 200
    atm  = round(price / step) * step
    if direction == "CALL":
        strike = atm if price <= atm else atm + step
    else:
        strike = atm if price >= atm else atm - step
    return int(strike), "CE" if direction == "CALL" else "PE"


def confluence_score(last: pd.Series, decision, price_action, smart_money, box
                     ) -> tuple[int, list[str], list[str]]:
    bull: list[str] = []
    bear: list[str] = []
    score = 0

    # 1 EMA stack
    e9, e21, e200 = last.get("EMA9",0), last.get("EMA21",0), last.get("EMA200", None)
    if e200 and pd.notna(e200):
        if e9 > e21 > e200:
            bull.append("✅ EMA9 > EMA21 > EMA200 — full bullish stack"); score += 20
        elif e9 > e21:
            bull.append("✅ EMA9 > EMA21 — short-term bullish"); score += 10
        else:
            bear.append("❌ EMA9 < EMA21 — bearish cross")
    else:
        if e9 > e21:
            bull.append("✅ EMA9 > EMA21 — bullish"); score += 12
        else:
            bear.append("❌ EMA9 < EMA21 — bearish")

    # 2 VWAP
    if pd.notna(last.get("VWAP")):
        if last["Close"] > last["VWAP"]:
            bull.append(f"✅ Price ₹{last['Close']:.1f} above VWAP ₹{last['VWAP']:.1f}"); score += 15
        else:
            bear.append(f"❌ Price ₹{last['Close']:.1f} below VWAP ₹{last['VWAP']:.1f}")

    # 3 RSI
    rsi = last.get("RSI14", 50)
    if 50 <= rsi <= 70:
        bull.append(f"✅ RSI {rsi:.0f} in bullish momentum zone (50–70)"); score += 10
    elif rsi > 70:
        bear.append(f"⚠️ RSI {rsi:.0f} overbought — avoid chasing")
    elif 30 <= rsi < 50:
        bear.append(f"⚠️ RSI {rsi:.0f} weak momentum")
    else:
        bear.append(f"❌ RSI {rsi:.0f} oversold / collapsed")

    # 4 MACD
    m, ms = last.get("MACD", None), last.get("MACDSignal", None)
    if pd.notna(m) and pd.notna(ms):
        if m > ms:
            bull.append(f"✅ MACD {m:.2f} > Signal {ms:.2f} — bullish"); score += 10
        else:
            bear.append(f"❌ MACD {m:.2f} < Signal {ms:.2f} — bearish")

    # 5 Volume
    adtv = last.get("ADTV20", 0)
    if adtv > 0:
        vr = last["Volume"] / adtv
        if vr > 1.5:
            bull.append(f"✅ Volume {vr:.1f}× ADTV — institutional activity"); score += 10
        elif vr < 0.7:
            bear.append(f"⚠️ Volume only {vr:.1f}× ADTV — low conviction")

    # 6 FVG
    if smart_money.fvg_direction == "bullish" and not smart_money.fvg_mitigated:
        bull.append(f"✅ Unmitigated Bullish FVG ₹{smart_money.fvg_lower:.0f}–₹{smart_money.fvg_upper:.0f}"); score += 10
    elif smart_money.fvg_direction == "bearish":
        bear.append(f"❌ Bearish FVG — overhead supply pressure")

    # 7 Liquidity sweep
    ls = smart_money.liquidity_sweep
    if ls == "bullish liquidity sweep":
        bull.append("✅ Bullish liquidity sweep — smart money bought dip"); score += 8
    elif "bearish" in ls or "sell-side" in ls:
        bear.append(f"❌ {ls}")

    # 8 Order flow
    of = smart_money.order_flow
    if of == "buyer-dominant order flow":
        bull.append("✅ Buyer-dominant order flow"); score += 7
    elif of == "seller-dominant order flow":
        bear.append("❌ Seller-dominant order flow")

    # 9 SuperTrend
    std = last.get("SuperTrend_Dir", 0)
    if std == 1:
        bull.append("✅ SuperTrend BULLISH"); score += 8
    elif std == -1:
        bear.append("❌ SuperTrend BEARISH")

    # 10 Box + candle
    if box.bias == "BUY WATCH":
        bull.append("✅ Box Theory: bullish wick at box low"); score += 5
    elif box.zone == "middle no-trade zone":
        bear.append("⚠️ Box Theory: price in mid-box no-trade zone")
    elif box.bias == "SELL WATCH":
        bear.append("❌ Box Theory: bearish wick at box top")
    if price_action.candle_bias == "bullish":
        bull.append(f"✅ Candle pattern: {price_action.candle_pattern}"); score += 5
    elif price_action.candle_bias == "bearish":
        bear.append(f"❌ Candle pattern: {price_action.candle_pattern}")

    return min(score, 100), bull, bear


@st.cache_data(ttl=4, show_spinner=False)   # 4-second live price cache for portfolio tracker
def _live_price(symbol: str, _live: bool) -> float | None:
    """Fetch the very latest close price for a symbol (lightweight, 4 s cache)."""
    try:
        if _live:
            import yfinance as yf
            ticker = f"{symbol}.NS"
            data = yf.download(ticker, period="1d", interval="1m",
                               auto_adjust=True, progress=False, threads=False)
            if not data.empty:
                if isinstance(data.columns, pd.MultiIndex):
                    data.columns = data.columns.get_level_values(0)
                return float(data["Close"].dropna().iloc[-1])
    except Exception:
        pass
    return None


@st.cache_data(ttl=300, show_spinner=False)   # cache 5 min — won't re-run on every 5 s refresh
def analyse_one(symbol: str, _interval: str, _period: str, _live: bool, seed: int) -> dict | None:
    try:
        df = fetch_yahoo_ohlcv(symbol, period=_period, interval=_interval) if _live \
             else generate_sample_ohlcv(seed=seed)
        if len(df) < 30:
            return None
        df  = add_indicators(df)
        df  = apply_ema_swing_breakout_strategy(df)
        usable = df.dropna(subset=["Close","EMA9","EMA21","ATR14"])
        if usable.empty:
            return None
        last         = usable.iloc[-1]
        decision     = ema_swing_breakout_decision(symbol, df)
        price_action = analyze_price_action(df)
        smart_money  = analyze_smart_money(df)
        box          = analyze_box_theory(df)
        score, bull, bear = confluence_score(last, decision, price_action, smart_money, box)
        direction = "CALL" if len(bull) > len(bear) and score >= min_conf else \
                    "PUT"  if len(bear) > len(bull) and score >= 45      else "WAIT"
        option_label = "No Trade"
        if direction in ("CALL","PUT"):
            s, sfx    = nse_strike(float(last["Close"]), direction)
            option_label = f"{symbol} {s} {sfx}"
        plan = None
        try:
            cfg = RiskConfig(capital=capital, risk_per_trade_pct=risk_pct, reward_risk_ratio=min_rr)
            if direction == "CALL" and pd.notna(last.get("RecentSwingLow")):
                stop = max(float(last["RecentSwingLow"]), float(last["Close"]) - 1.5*float(last["ATR14"]))
                plan = build_trade_plan_from_stop(symbol, "BUY",  float(last["Close"]), stop, cfg)
            elif direction == "PUT" and pd.notna(last.get("RecentSwingHigh")):
                stop = min(float(last["RecentSwingHigh"]),float(last["Close"]) + 1.5*float(last["ATR14"]))
                plan = build_trade_plan_from_stop(symbol, "SELL", float(last["Close"]), stop, cfg)
        except Exception:
            pass
        return {
            "symbol": symbol, "close": float(last["Close"]), "score": score,
            "direction": direction, "option_label": option_label,
            "bull": bull, "bear": bear, "plan": plan,
            "rsi":  float(last.get("RSI14", 50)),
            "atr":  float(last.get("ATR14", 0)),
            "vwap": float(last["VWAP"]) if pd.notna(last.get("VWAP")) else None,
            "volume": float(last["Volume"]), "adtv": float(last.get("ADTV20",0)),
            "ema9": float(last["EMA9"]), "ema21": float(last["EMA21"]),
            "macd": float(last.get("MACD",0)), "macd_signal": float(last.get("MACDSignal",0)),
            "supertrend": int(last.get("SuperTrend_Dir",0)),
            "df": df,
        }
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — INDEX OPTIONS (NIFTY & BANKNIFTY)
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("🎯 Index Options — NIFTY & BANKNIFTY")

idx_cols = st.columns(2)
for idx_col, idx_sym in zip(idx_cols, ["NIFTY", "BANKNIFTY"]):
    with idx_col:
        if fetch_index_options:
            with st.spinner(f"Fetching {idx_sym} OI chain from NSE..."):
                idx_data = suggest_index_option(idx_sym)

            if idx_data.get("error"):
                st.error(f"{idx_sym}: {idx_data['error']}")
            else:
                spot      = idx_data["spot"]
                bias      = idx_data["bias"]
                contract  = idx_data["contract"]
                atm_ltp   = idx_data["atm_ltp"]
                pcr       = idx_data["pcr"]
                max_pain  = idx_data["max_pain"]
                ce_wall   = idx_data["max_ce_wall"]
                pe_wall   = idx_data["max_pe_wall"]
                atm_ce_oi = idx_data["atm_ce_oi"]
                atm_pe_oi = idx_data["atm_pe_oi"]
                reasons   = idx_data["reasons"]

                card_cls = "call-card" if bias=="CALL" else "put-card" if bias=="PUT" else "wait-card"
                dir_text = f"📈 BUY {contract}" if bias=="CALL" else \
                           f"📉 BUY {contract}" if bias=="PUT" else "⏳ WAIT — Neutral"
                dir_color = "#22c55e" if bias=="CALL" else "#ef4444" if bias=="PUT" else "#d97706"

                st.markdown(f"""
                <div class="{card_cls}">
                  <div style="color:{dir_color}" class="big-dir">{dir_text}</div>
                  <div style="margin-top:0.4rem;font-size:0.9rem;color:#cbd5e1">
                    Spot: <b>₹{spot:,.0f}</b> &nbsp;|&nbsp;
                    ATM: <b>{idx_data['atm']:,}</b> &nbsp;|&nbsp;
                    Premium ≈ <b>₹{atm_ltp:.0f}</b>
                  </div>
                </div>
                """, unsafe_allow_html=True)

                st.markdown("**OI Intelligence:**")
                m1, m2, m3 = st.columns(3)
                m1.metric("PCR",       f"{pcr}")
                m2.metric("Max Pain",  f"₹{max_pain:,.0f}")
                m3.metric("ATM CE OI", f"{atm_ce_oi:,}")

                m4, m5, m6 = st.columns(3)
                m4.metric("CE Wall (Resistance)", f"₹{ce_wall:,}")
                m5.metric("PE Wall (Support)",    f"₹{pe_wall:,}")
                m6.metric("ATM PE OI",            f"{atm_pe_oi:,}")

                with st.expander("Why this recommendation?"):
                    for r in reasons:
                        st.markdown(f"- {r}")

                # Mini OI chart
                df_oi = idx_data.get("df", pd.DataFrame())
                if not df_oi.empty:
                    fig_oi = go.Figure()
                    fig_oi.add_bar(x=df_oi["strike"], y=df_oi["ce_oi"]/1000,
                                   name="CE OI (×1000)", marker_color="#ef4444", opacity=0.8)
                    fig_oi.add_bar(x=df_oi["strike"], y=df_oi["pe_oi"]/1000,
                                   name="PE OI (×1000)", marker_color="#22c55e", opacity=0.8)
                    fig_oi.add_vline(x=spot, line_color="#facc15", line_dash="dash",
                                     annotation_text=f"Spot ₹{spot:.0f}")
                    fig_oi.update_layout(
                        height=260, barmode="group",
                        paper_bgcolor="#0f172a", plot_bgcolor="#0f172a",
                        font=dict(color="#e2e8f0", size=10),
                        margin=dict(t=20,b=20,l=10,r=10),
                        legend=dict(orientation="h", y=1.1),
                        title=dict(text=f"{idx_sym} OI by Strike", font=dict(size=12)),
                    )
                    fig_oi.update_xaxes(gridcolor="#1e293b")
                    fig_oi.update_yaxes(gridcolor="#1e293b")
                    st.plotly_chart(fig_oi, use_container_width=True)
        else:
            st.info(f"Toggle 'Fetch NIFTY/BankNifty OI Chain' in sidebar to enable {idx_sym} index options.")

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — STOCK SCAN
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("📋 Stock Options Scan")

symbols = ([s.strip().upper() for s in custom_syms.split(",") if s.strip()]
           if scan_mode == "custom" else get_scan_universe(scan_mode))

SCAN_TTL = 90  # seconds between auto-scans
should_scan = (not st.session_state.scan_results or
               time.time() - st.session_state.last_scan > SCAN_TTL)

if should_scan:
    total   = len(symbols)
    seeds   = {s: abs(hash(s)) % 9999 for s in symbols}
    results: list[dict] = []
    prog    = st.progress(0, text=f"Scanning {total} stocks...")
    for i, sym in enumerate(symbols):
        r = analyse_one(sym, interval, hist_period, use_live, seeds[sym])
        prog.progress((i+1)/total, text=f"{'🟢' if r and r['direction']=='CALL' else '🔴' if r and r['direction']=='PUT' else '⏳'} {sym} ({i+1}/{total})")
        if r:
            results.append(r)
    prog.empty()
    st.session_state.scan_results  = results
    st.session_state.last_scan     = time.time()
else:
    results = st.session_state.scan_results

actionable = sorted([r for r in results if r["direction"] in ("CALL","PUT")],
                     key=lambda x: x["score"], reverse=True)

# ── Top metrics ──────────────────────────────────────────────────────────────
c1,c2,c3,c4,c5,c6 = st.columns(6)
c1.metric("🔍 Scanned",    len(results))
c2.metric("🎯 Actionable", len(actionable))
c3.metric("📈 CALL",       sum(1 for r in actionable if r["direction"]=="CALL"))
c4.metric("📉 PUT",        sum(1 for r in actionable if r["direction"]=="PUT"))
avg_rsi = sum(r["rsi"] for r in results)/len(results) if results else 50
c5.metric("📊 Avg RSI",    f"{avg_rsi:.0f}")
bull_pct = len(actionable) / len(results) * 100 if results else 0
c6.metric("🌡️ Market Temp", f"{bull_pct:.0f}% Bull")

# ── Signals table ─────────────────────────────────────────────────────────────
if actionable:
    st.markdown("### ⚡ Live Options Signals")
    rows = []
    for r in actionable:
        p = r.get("plan")
        rr_str = f"{p.reward_amount/p.risk_amount:.1f}:1" if p and p.risk_amount>0 else "—"
        rows.append({
            "Rank":        f"#{actionable.index(r)+1}",
            "Symbol":      r["symbol"],
            "Option":      r["option_label"],
            "Type":        "🟢 CALL" if r["direction"]=="CALL" else "🔴 PUT",
            "Score":       f"{r['score']}%",
            "Price":       f"₹{r['close']:.1f}",
            "Entry":       f"₹{p.entry:.1f}" if p else "—",
            "Stop":        f"₹{p.stop:.1f}"  if p else "—",
            "Target":      f"₹{p.target:.1f}" if p else "—",
            "R:R":         rr_str,
            "RSI":         f"{r['rsi']:.0f}",
            "Vol/ADTV":    f"{r['volume']/r['adtv']:.1f}×" if r['adtv']>0 else "—",
            "MACD Bias":   "Bullish" if r["macd"]>r["macd_signal"] else "Bearish",
            "SuperTrend":  "🟢 UP" if r["supertrend"]==1 else "🔴 DN" if r["supertrend"]==-1 else "—",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.warning("No signals above the confidence threshold. Lower 'Min Confluence Score' in the sidebar or click SCAN NOW.")

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — DEEP DIVE CHART
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("🔍 Deep Dive — Select Stock for Full Chart & Analysis")

all_syms = [r["symbol"] for r in results]
default  = actionable[0]["symbol"] if actionable else (all_syms[0] if all_syms else None)
if not all_syms:
    st.info("No data yet — click SCAN NOW.")
    st.stop()

selected_sym = st.selectbox("Select Stock", all_syms,
                              index=all_syms.index(default) if default else 0)
sel = next((r for r in results if r["symbol"] == selected_sym), None)

if sel:
    direction = sel["direction"]
    score     = sel["score"]
    plan      = sel.get("plan")

    # Signal card + reasons
    col_card, col_why = st.columns([1, 2])

    with col_card:
        if direction == "CALL":
            st.markdown(f'<div class="call-card"><div class="big-dir" style="color:#22c55e">📈 BUY CALL (CE)</div>'
                        f'<b style="font-size:1.1rem">{sel["option_label"]}</b><br>'
                        f'<span style="color:#94a3b8">Confluence Score: <b style="color:#22c55e">{score}%</b></span></div>',
                        unsafe_allow_html=True)
        elif direction == "PUT":
            st.markdown(f'<div class="put-card"><div class="big-dir" style="color:#ef4444">📉 BUY PUT (PE)</div>'
                        f'<b style="font-size:1.1rem">{sel["option_label"]}</b><br>'
                        f'<span style="color:#94a3b8">Confluence Score: <b style="color:#ef4444">{score}%</b></span></div>',
                        unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="wait-card"><div class="big-dir" style="color:#d97706">⏳ WAIT / NO TRADE</div>'
                        f'Score: <b>{score}%</b><br><span style="color:#94a3b8">Mixed signals — wait for clearer setup</span></div>',
                        unsafe_allow_html=True)

        if plan:
            st.markdown("---")
            m1, m2 = st.columns(2)
            m1.metric("Entry",   f"₹{plan.entry:.1f}")
            m2.metric("Stop",    f"₹{plan.stop:.1f}")
            m1.metric("Target",  f"₹{plan.target:.1f}")
            rr = plan.reward_amount/plan.risk_amount if plan.risk_amount>0 else 0
            m2.metric("R:R",     f"{rr:.1f}:1")
            st.metric("Risk ₹",  f"₹{plan.risk_amount:,.0f}")
            st.metric("Reward ₹",f"₹{plan.reward_amount:,.0f}")

    with col_why:
        st.markdown("### 📋 Why this signal?")
        tab1, tab2 = st.tabs(["✅ Bullish Factors", "❌ Bearish / Warnings"])
        with tab1:
            if sel["bull"]:
                for b in sel["bull"]:
                    st.markdown(f"- {b}")
            else:
                st.info("No bullish factors found.")
        with tab2:
            if sel["bear"]:
                for b in sel["bear"]:
                    st.markdown(f"- {b}")
            else:
                st.success("No bearish factors — clean signal!")

        # Summary sentence
        if direction == "CALL" and plan:
            st.success(f"**Summary:** {selected_sym} has {len(sel['bull'])} bullish signals vs {len(sel['bear'])} bearish. "
                       f"Entry ₹{plan.entry:.0f}, Stop ₹{plan.stop:.0f}, Target ₹{plan.target:.0f}. "
                       f"Risk ₹{plan.risk_amount:,.0f} to make ₹{plan.reward_amount:,.0f}.")
        elif direction == "PUT" and plan:
            st.error(f"**Summary:** {selected_sym} has {len(sel['bear'])} bearish signals. "
                     f"Entry ₹{plan.entry:.0f}, Stop ₹{plan.stop:.0f}, Target ₹{plan.target:.0f}.")
        else:
            st.warning(f"**Mixed signal** — score {score}%. Wait for one more confirmation before entering.")

    # ── Chart ──
    st.markdown("### 📊 Price Chart (Candles + Indicators + Signals)")
    df_c = sel["df"].tail(100).copy()

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                         row_heights=[0.58, 0.22, 0.20],
                         vertical_spacing=0.02,
                         subplot_titles=["Price  +  EMA  +  VWAP  +  FVG  +  Entry/SL/Target",
                                         "RSI (14)", "MACD"])

    # Candles
    fig.add_trace(go.Candlestick(x=df_c.index, open=df_c["Open"],
                                  high=df_c["High"], low=df_c["Low"], close=df_c["Close"],
                                  name="Price",
                                  increasing_line_color="#22c55e",
                                  decreasing_line_color="#ef4444",
                                  increasing_fillcolor="#052e16",
                                  decreasing_fillcolor="#3b0007"), row=1, col=1)

    # EMAs
    for col_name, clr, lbl in [("EMA9","#facc15","EMA9"),("EMA21","#60a5fa","EMA21"),("EMA200","#a78bfa","EMA200")]:
        if col_name in df_c:
            fig.add_trace(go.Scatter(x=df_c.index, y=df_c[col_name], name=lbl,
                                      line=dict(color=clr, width=1.5)), row=1, col=1)

    # VWAP
    if "VWAP" in df_c:
        fig.add_trace(go.Scatter(x=df_c.index, y=df_c["VWAP"], name="VWAP",
                                  line=dict(color="#f97316", width=2, dash="dot")), row=1, col=1)

    # FVG shading
    sm = analyze_smart_money(sel["df"])
    if sm.fvg_lower and sm.fvg_upper and not sm.fvg_mitigated:
        fvg_clr = "rgba(34,197,94,0.10)" if sm.fvg_direction=="bullish" else "rgba(239,68,68,0.10)"
        fig.add_hrect(y0=sm.fvg_lower, y1=sm.fvg_upper, fillcolor=fvg_clr, line_width=0,
                      annotation_text=f"FVG ({sm.fvg_direction})",
                      annotation_font_color="#94a3b8", row=1, col=1)

    # Box theory
    box = analyze_box_theory(sel["df"])
    for lvl, lbl, clr in [(box.previous_day_high,"PDH","#64748b"),
                            (box.previous_day_low, "PDL","#64748b"),
                            (box.middle_line,      "PDM","#475569")]:
        if lvl:
            fig.add_hline(y=lvl, line_color=clr, line_dash="dot",
                          annotation_text=lbl, annotation_font_color=clr, row=1, col=1)

    # Entry / SL / Target
    if plan:
        for lvl, lbl, clr in [(plan.entry,"Entry","#60a5fa"),
                               (plan.stop, "SL",   "#f87171"),
                               (plan.target,"Tgt", "#34d399")]:
            fig.add_hline(y=lvl, line_color=clr, line_dash="dash",
                          annotation_text=f"{lbl} ₹{lvl:.0f}",
                          annotation_font_color=clr, row=1, col=1)

    # BUY/SELL markers
    if "Signal" in df_c:
        bd = df_c[df_c["Signal"]=="BUY"]
        sd = df_c[df_c["Signal"]=="SELL"]
        if not bd.empty:
            fig.add_trace(go.Scatter(x=bd.index, y=bd["Low"]*0.998, mode="markers",
                                      marker=dict(symbol="triangle-up",size=14,color="#22c55e"),
                                      name="BUY"), row=1, col=1)
        if not sd.empty:
            fig.add_trace(go.Scatter(x=sd.index, y=sd["High"]*1.002, mode="markers",
                                      marker=dict(symbol="triangle-down",size=14,color="#ef4444"),
                                      name="SELL"), row=1, col=1)

    # RSI
    if "RSI14" in df_c:
        fig.add_trace(go.Scatter(x=df_c.index, y=df_c["RSI14"], name="RSI",
                                  line=dict(color="#c084fc", width=1.5)), row=2, col=1)
        for lvl, clr in [(70,"#ef4444"),(50,"#64748b"),(30,"#22c55e")]:
            fig.add_hline(y=lvl, line_color=clr, line_dash="dot", row=2, col=1)

    # MACD
    if "MACD" in df_c and "MACDSignal" in df_c:
        fig.add_trace(go.Scatter(x=df_c.index, y=df_c["MACD"],       name="MACD",
                                  line=dict(color="#38bdf8")), row=3, col=1)
        fig.add_trace(go.Scatter(x=df_c.index, y=df_c["MACDSignal"], name="Signal",
                                  line=dict(color="#fb923c")), row=3, col=1)
        if "MACDHist" in df_c:
            clrs_hist = ["#22c55e" if v>=0 else "#ef4444" for v in df_c["MACDHist"]]
            fig.add_trace(go.Bar(x=df_c.index, y=df_c["MACDHist"], name="Histogram",
                                  marker_color=clrs_hist, opacity=0.6), row=3, col=1)

    fig.update_layout(
        height=720, paper_bgcolor="#0f172a", plot_bgcolor="#0f172a",
        font=dict(color="#e2e8f0", size=11),
        legend=dict(orientation="h", y=1.02, bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        xaxis_rangeslider_visible=False, margin=dict(t=30, b=10),
    )
    fig.update_xaxes(gridcolor="#1e293b", zerolinecolor="#1e293b")
    fig.update_yaxes(gridcolor="#1e293b", zerolinecolor="#1e293b")
    st.plotly_chart(fig, use_container_width=True)

    # Track button
    if direction in ("CALL","PUT") and plan:
        b_col, _ = st.columns([1,3])
        with b_col:
            if st.button(f"📌 Add '{sel['option_label']}' to Tracker", type="primary", use_container_width=True):
                st.session_state.portfolio.append({
                    "symbol": selected_sym, "option": sel["option_label"],
                    "direction": direction, "entry_price": plan.entry,
                    "target": plan.target, "stop": plan.stop,
                    "qty": plan.shares,
                    "source": "Manual", "added_at": now_ist().strftime("%H:%M"),
                })
                st.success("Added to Portfolio Tracker!")

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — PORTFOLIO TRACKER  (refreshes every 5 s automatically)
# ══════════════════════════════════════════════════════════════════════════════

# Header with live blink + last-update timestamp
tracker_refresh_time = now_ist().strftime("%H:%M:%S")
st.markdown(f"""
<div style="display:flex;align-items:center;gap:12px;margin-bottom:0.5rem">
  <span class="live-dot"></span>
  <span style="font-size:1.2rem;font-weight:700">💼 Portfolio Tracker — Live Sell Signals</span>
  <span style="font-size:0.82rem;color:#64748b;margin-left:auto">
    🔄 Auto-refreshing every 5s &nbsp;|&nbsp; Last update: <b>{tracker_refresh_time} IST</b>
    &nbsp;|&nbsp; Refresh #{count}
  </span>
</div>
""", unsafe_allow_html=True)

# Pull live Angel One positions (runs fresh each 5 s refresh)
try:
    live_pos = AngelOneClient().get_open_positions()
except Exception:
    live_pos = []

# Merge broker positions into session portfolio
manual_tickers = [p["symbol"] for p in st.session_state.portfolio]
for pos in live_pos:
    if pos["Ticker"] not in manual_tickers:
        st.session_state.portfolio.append({
            "symbol": pos["Ticker"], "option": pos["Option"],
            "direction": "CALL" if "CE" in pos["Option"] else "PUT",
            "entry_price": pos["Entry Underlying Level"],
            "target": pos["Target Level"], "stop": pos["Stop Loss Level"],
            "qty": pos.get("Qty", 1),
            "source": "Angel One API", "added_at": "—",
        })

if st.session_state.portfolio:
    t_rows = []
    for pos in st.session_state.portfolio:
        sym = pos["symbol"]

        # Try fast 1-min live price first, fall back to scan result, then entry
        live_price = _live_price(sym, use_live)
        if live_price is None:
            live_r   = next((r for r in results if r["symbol"] == sym), None)
            live_price = live_r["close"] if live_r else pos["entry_price"]

        cur = live_price
        entry = pos["entry_price"]
        qty = pos.get("qty", 1)  # Default to 1 if it's an old session entry
        
        chg_pct = (cur - entry) / entry * 100 if entry > 0 else 0
        chg_str = f"+{chg_pct:.1f}%" if chg_pct >= 0 else f"{chg_pct:.1f}%"

        # Calculate exact Profit and Loss Amount based on Direction & Quantity
        if pos["direction"] == "CALL":
            pnl_amt = (cur - entry) * qty
        else:
            pnl_amt = (entry - cur) * qty
            
        pnl_str = f"+₹{pnl_amt:,.0f}" if pnl_amt >= 0 else f"-₹{abs(pnl_amt):,.0f}"

        if cur >= pos["target"]:
            status = "🟢 TARGET HIT — SELL NOW!"
        elif cur <= pos["stop"]:
            status = "🔴 STOP LOSS — EXIT NOW!"
        elif pos["direction"] == "CALL" and cur > entry:
            status = f"📈 Up {chg_str} — Hold toward Target"
        elif pos["direction"] == "PUT" and cur < entry:
            status = f"📉 Down {abs(chg_pct):.1f}% (PUT gaining) — Hold toward Target"
        else:
            status = f"⏳ {chg_str} — Hold / Monitor"

        t_rows.append({
            "Source":       pos["source"],
            "Symbol":       sym,
            "Option Held":  pos["option"],
            "Qty":          qty,
            "Bought At ₹":  f"{entry:.1f}",
            "Live Price ₹": f"{cur:.1f}",
            "Change (Unrealised)":  chg_str,
            "P&L ₹":        pnl_str,
            "Target ₹":     f"{pos['target']:.1f}",
            "Stop Loss ₹":  f"{pos['stop']:.1f}",
            "🚦 Action":    status,
            "Added":        pos.get("added_at", "—"),
        })

    # Highlight rows based on status using pandas Styler
    df_tracker = pd.DataFrame(t_rows)

    def _row_color(row):
        action = row["🚦 Action"]
        if "TARGET" in action:
            return ["background-color:#052e16; color:#22c55e; font-weight:700"] * len(row)
        elif "STOP LOSS" in action:
            return ["background-color:#3b0007; color:#ef4444; font-weight:700"] * len(row)
        elif "📈" in action or "📉 Down" in action:
            return ["background-color:#0c1a2e; color:#93c5fd"] * len(row)
        return ["background-color:#0f172a; color:#e2e8f0"] * len(row)

    st.dataframe(
        df_tracker.style.apply(_row_color, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    bc1, bc2 = st.columns([1, 5])
    with bc1:
        if st.button("🗑️ Clear Manual Trades", use_container_width=True):
            st.session_state.portfolio = [
                p for p in st.session_state.portfolio if p["source"] != "Manual"
            ]
            st.rerun()
else:
    st.info("No positions yet. Select a stock above and click '📌 Add to Tracker', or your Angel One API will auto-populate open positions.")

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — FULL SCAN TABLE
# ══════════════════════════════════════════════════════════════════════════════
with st.expander("📊 Full Scan Results (all stocks)", expanded=False):
    if results:
        st.dataframe(pd.DataFrame([{
            "Symbol": r["symbol"], "Signal": r["direction"],
            "Score": r["score"], "Option": r["option_label"],
            "Price": round(r["close"],1), "RSI": round(r["rsi"],0),
            "MACD": "Bull" if r["macd"]>r["macd_signal"] else "Bear",
            "SuperTrend": "UP" if r["supertrend"]==1 else "DN" if r["supertrend"]==-1 else "—",
            "Vol×ADTV": round(r["volume"]/r["adtv"],1) if r["adtv"]>0 else "—",
            "Bulls": len(r["bull"]), "Bears": len(r["bear"]),
        } for r in sorted(results, key=lambda x: x["score"], reverse=True)]),
        use_container_width=True, hide_index=True)

st.caption("⚠️ This tool is for decision-support and educational purposes only. "
           "Always paper-trade first before using real capital. Not SEBI-registered investment advice.")
