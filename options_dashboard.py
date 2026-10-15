"""
Index Options Dashboard — NIFTY & BANKNIFTY intraday CALL / PUT
================================================================
A clean, focused screen built around one question: *should I buy a CALL or a
PUT right now, and where do I get out?*

Three sections, top to bottom:
  1. 🎯 TOP TRADES NOW   — the CALL/PUT/WAIT call for each index with a full
                            premium plan (entry / +target% / -stop%).
  2. 📈 CHART            — the underlying with VWAP + EMA9/EMA21 so you see why.
  3. 💼 MY POSITIONS     — add your live trade, watch P&L%, get a loud 🚨 alert
                            when loss hits your limit or the move turns on you.

Data source is shown at the top at all times: 🟢 LIVE vs 🟡 ESTIMATED. Estimated
data (market closed / broker not connected) is clearly NOT for live trading.

Run:  streamlit run options_dashboard.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from intraday_advisor.index_options import (
    DEFAULT_STOP_PCT,
    DEFAULT_TARGET_PCT,
    INDEX_CONFIG,
    analyze_index_option,
    get_option_premium,
)
from intraday_advisor.position_monitor import evaluate_position
from intraday_advisor.data import drop_forming_candle, fetch_yahoo_ohlcv
from intraday_advisor.indicators import add_indicators
from intraday_advisor.ledger import (
    ledger_stats,
    lot_size_for,
    plan_position,
    record_index_signal,
)

# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Index Options — CALL/PUT", layout="wide", page_icon="🎯")

IST = timezone(timedelta(hours=5, minutes=30))

COLORS = {"CALL": "#16a34a", "PUT": "#dc2626", "WAIT": "#6b7280"}


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open() -> bool:
    n = now_ist()
    if n.weekday() >= 5:
        return False
    return n.replace(hour=9, minute=15, second=0, microsecond=0) <= n <= n.replace(hour=15, minute=30, second=0, microsecond=0)


st.markdown(
    """
    <style>
      .trade-card { border-radius: 14px; padding: 18px 20px; margin-bottom: 8px;
                    background: #0e1117; border: 1px solid #262730; }
      .big-badge  { font-size: 30px; font-weight: 800; padding: 6px 18px;
                    border-radius: 10px; color: white; display: inline-block; }
      .contract   { font-size: 22px; font-weight: 700; color: #e6edf3; }
      .muted      { color: #8b949e; font-size: 13px; }
      .plan-num   { font-size: 20px; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)

if "positions" not in st.session_state:
    st.session_state["positions"] = []

# --------------------------------------------------------------------------- #
# sidebar controls
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("⚙️ Settings")
    target_pct = st.slider("Profit target (%)", 5, 60, int(DEFAULT_TARGET_PCT * 100), 5) / 100
    stop_pct = st.slider("Loss warning / stop (%)", 5, 50, int(DEFAULT_STOP_PCT * 100), 5) / 100
    refresh_choice = st.selectbox("Auto-refresh", ["Every 15s", "Every 30s", "Every 60s", "Manual"], index=0)
    st.caption(
        "Options exist only on NIFTY/BANKNIFTY + ~180 F&O stocks. This screen "
        "focuses on the two most liquid index options."
    )
    st.divider()
    st.subheader("💰 Capital & risk")
    available_capital = st.number_input(
        "Capital available now (₹)", min_value=0.0, value=20_000.0, step=1_000.0,
        help="Whatever is actually in the account right now. Position size and "
             "the risk percentage below are computed from this, not from a "
             "hardcoded figure.",
    )
    max_fraction = st.slider(
        "Max % of capital to deploy", 5, 100, 100, 5,
        help="At 100%, a -20% premium stop is a -20% account loss regardless of "
             "how much capital you hold, because extra capital buys extra lots "
             "rather than safety. Deploying less is the only lever.",
    ) / 100

    st.divider()
    st.subheader("📒 Signal ledger")
    record_signals = st.toggle(
        "Record signals for grading", value=True,
        help="Writes every CALL/PUT signal to the ledger so outcomes can be "
             "graded after the close. Repeated refreshes inside one candle are "
             "deduped, so this stores one row per bar per contract.",
    )
    st.caption(
        "After 15:30 run `python ledger_cli.py grade`, then "
        "`python ledger_cli.py report` to see whether the score has any edge."
    )

    st.divider()
    st.caption(
        "⚠️ This is decision support, not a profit guarantee. WAIT is a valid, "
        "frequent call. Always cut losers at your stop."
    )

refresh_ms = {"Every 15s": 15_000, "Every 30s": 30_000, "Every 60s": 60_000}
if refresh_choice != "Manual":
    st_autorefresh(interval=refresh_ms[refresh_choice], key="opt_refresh")


# --------------------------------------------------------------------------- #
# cached data fetchers
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=12, show_spinner=False)
def get_signals(_target: float, _stop: float) -> list[dict]:
    out = []
    for index in INDEX_CONFIG:
        try:
            sig = analyze_index_option(index, target_pct=_target, stop_pct=_stop)
            out.append(sig.as_dict())
        except Exception as exc:  # noqa: BLE001
            out.append({"index": index, "direction": "WAIT", "confidence": 0, "error": str(exc),
                        "reasons": [], "warnings": [f"Analysis failed: {exc}"], "data_source": "error",
                        "spot": 0, "strike": 0, "option_type": "—", "entry_premium": 0,
                        "target_premium": 0, "stop_premium": 0, "pcr": 0, "max_pain": 0,
                        "ce_wall": 0, "pe_wall": 0, "underlying_bias": "neutral", "chain_bias": "NEUTRAL",
                        "is_live": False, "target_pct": _target, "stop_pct": _stop})
    out.sort(key=lambda s: (s["direction"] != "WAIT", s["confidence"]), reverse=True)
    return out


@st.cache_data(ttl=25, show_spinner=False)
def get_chart_df(index: str):
    df = fetch_yahoo_ohlcv(INDEX_CONFIG[index]["yahoo"], period="2d", interval="5m")
    df = add_indicators(df)
    return drop_forming_candle(df, "5m").tail(80)


@st.cache_data(ttl=8, show_spinner=False)
def live_premium(index: str, strike: float, opt_type: str, spot: float) -> tuple[float, str]:
    return get_option_premium(index, strike, opt_type, spot=spot)


@st.cache_data(ttl=3600, show_spinner=False)
def option_token(index: str, strike: float, opt_type: str) -> str | None:
    """NFO token for a contract, cached for an hour.

    Resolution loads the Angel instrument master, which is far too slow to do
    on every 15-second rerun. Without a token the grader cannot read that
    strike's premium path, so this is what makes a signal gradeable at all.
    """
    from intraday_advisor.tick_stream import resolve_option_token

    return resolve_option_token(index, strike, opt_type)


def record_to_ledger(
    signals: list[dict], capital: float, fraction: float, enabled: bool
) -> list[dict]:
    """Record directional signals and attach a sized plan to each.

    Called on every rerun rather than inside a cached function: the ledger
    dedupes by (bar, contract), so a 15-second refresh across a 5-minute candle
    stores exactly one row. Doing it inside ``get_signals`` would skip
    recording on every cache hit and lose most bars.

    Recording must never break the screen, so every failure degrades to a
    warning on the card.
    """
    for signal in signals:
        signal["_plan"] = None
        signal["_ledger_error"] = None
        if signal.get("direction") not in ("CALL", "PUT"):
            continue
        if float(signal.get("entry_premium") or 0) <= 0:
            continue

        try:
            lot = lot_size_for(signal["index"])
            signal["_plan"] = plan_position(
                available_capital=capital,
                entry_premium=float(signal["entry_premium"]),
                stop_premium=float(signal["stop_premium"]),
                target_premium=float(signal["target_premium"]),
                lot_size=lot,
                max_capital_fraction=fraction,
            )
        except (ValueError, KeyError) as exc:
            signal["_ledger_error"] = f"Sizing failed: {exc}"
            continue

        if not enabled:
            continue
        if not signal.get("bar_time"):
            signal["_ledger_error"] = "No closed candle yet — not recorded."
            continue

        try:
            record_index_signal(
                signal,
                bar_time=signal["bar_time"],
                lot_size=lot,
                option_token=option_token(
                    signal["index"], signal["strike"], signal["option_type"]
                ),
                plan=signal["_plan"],
                available_capital=capital,
                # Estimated (non-live) premiums must never enter a statistic.
                is_synthetic=not signal.get("is_live", False),
            )
        except Exception as exc:  # noqa: BLE001 - never break the screen
            signal["_ledger_error"] = f"Not recorded: {exc}"
    return signals


# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #
signals = record_to_ledger(
    get_signals(target_pct, stop_pct), available_capital, max_fraction, record_signals
)
any_live = any(s.get("is_live") for s in signals)
mkt = is_market_open()

h1, h2, h3 = st.columns([3, 1.4, 1.4])
with h1:
    st.title("🎯 Index Options — CALL / PUT")
with h2:
    st.markdown("### " + ("🟢 MARKET OPEN" if mkt else "🔴 MARKET CLOSED"))
with h3:
    if any_live:
        st.markdown("### 🟢 LIVE DATA")
    else:
        st.markdown("### 🟡 ESTIMATED")

if not any_live:
    st.warning(
        "🟡 **ESTIMATED DATA** — broker feed not live (market closed or Angel One not connected). "
        "Numbers below are modelled estimates, **not for live trading**. Connect Angel One during "
        "market hours for real premiums.",
        icon="⚠️",
    )
st.caption(f"Last updated: {now_ist().strftime('%Y-%m-%d %H:%M:%S')} IST · target +{target_pct*100:.0f}% · stop -{stop_pct*100:.0f}%")

# --- ledger status: how close the sample is to being able to prove anything ---
_stats = ledger_stats()
if _stats["real_signals"]:
    _needed = max(0, 30 - _stats["graded"])
    st.caption(
        f"📒 Ledger: {_stats['real_signals']} signals recorded over "
        f"{_stats['sessions']} session(s) · {_stats['graded']} graded, "
        f"{_stats['ungraded']} awaiting grading"
        + (f" · {_needed} more graded trades before any verdict is meaningful"
           if _needed else " · enough for a first read: `python ledger_cli.py report`")
    )
elif record_signals:
    st.caption(
        "📒 Ledger is empty. Signals recorded here get graded after the close "
        "(`python ledger_cli.py grade`) so you can find out whether the score "
        "has any edge. Expect ~30 trades before the numbers mean anything."
    )


# --------------------------------------------------------------------------- #
# Section 1 — TOP TRADES NOW
# --------------------------------------------------------------------------- #
st.subheader("🎯 Top Trades Now")
cols = st.columns(len(signals))

for col, s in zip(cols, signals):
    direction = s["direction"]
    color = COLORS.get(direction, "#6b7280")
    with col:
        with st.container():
            st.markdown(
                f"<div class='trade-card'>"
                f"<span class='big-badge' style='background:{color}'>{direction}</span>"
                f"&nbsp;&nbsp;<span class='contract'>{s['index']}</span><br>",
                unsafe_allow_html=True,
            )
            if direction == "WAIT":
                st.markdown(
                    f"<p class='muted'>No clean setup right now — wait. "
                    f"(underlying: {s['underlying_bias']}, chain: {s['chain_bias']})</p></div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<p class='contract'>{s['index']} {s['strike']} {s['option_type']}</p>"
                    f"<p class='muted'>Spot {s['spot']:.0f} · confidence {s['confidence']}%</p></div>",
                    unsafe_allow_html=True,
                )
                p1, p2, p3 = st.columns(3)
                p1.metric("Entry ₹", f"{s['entry_premium']:.2f}")
                p2.metric(f"Target +{target_pct*100:.0f}%", f"{s['target_premium']:.2f}")
                p3.metric(f"Stop -{stop_pct*100:.0f}%", f"{s['stop_premium']:.2f}")

                # --- what this actually costs and risks, at your capital ---
                plan = s.get("_plan")
                if plan is not None:
                    if not plan.affordable:
                        st.error(
                            f"❌ One lot needs ₹{plan.capital_per_lot:,.0f} — "
                            f"you have ₹{plan.available_capital:,.0f}. Cannot take this trade.",
                            icon="🚫",
                        )
                    else:
                        band_icon = {"SAFE": "🟢", "CAUTION": "🟠", "SEVERE": "🔴"}
                        r1, r2, r3 = st.columns(3)
                        r1.metric("Lots", f"{plan.lots}",
                                  help=f"₹{plan.deployed_capital:,.0f} deployed "
                                       f"({plan.capital_utilisation*100:.0f}% of capital)")
                        r2.metric(
                            f"{band_icon.get(plan.risk_band, '')} Risk",
                            f"{plan.risk_pct_of_capital*100:.1f}%",
                            help=f"₹{plan.risk_amount:,.0f} if stopped, net of costs. "
                                 f"{plan.losses_to_halve} such losses in a row halve "
                                 f"the account.",
                        )
                        r3.metric("Net R:R", f"{plan.net_rr:.2f}",
                                  help=f"After ₹{plan.round_trip_cost:,.0f} round-trip "
                                       f"costs. Breakeven ₹{plan.breakeven:.2f}.")
                        st.caption(
                            f"Breakeven ₹{plan.breakeven:.2f} · costs "
                            f"{plan.cost_drag*100:.2f}% of position"
                        )
                    for warning in plan.warnings:
                        st.warning(warning, icon="⚠️")

                if s.get("_ledger_error"):
                    st.caption(f"📒 {s['_ledger_error']}")

                if st.button(f"➕ Track this {s['index']} trade", key=f"track_{s['index']}"):
                    st.session_state["positions"].append({
                        "index": s["index"], "option_type": s["option_type"], "strike": s["strike"],
                        "entry_premium": s["entry_premium"], "qty": 75 if s["index"] == "NIFTY" else 15,
                    })
                    st.toast(f"Tracking {s['index']} {s['strike']} {s['option_type']}", icon="✅")

            st.progress(min(100, int(s["confidence"])) / 100)
            with st.expander("Why this call?"):
                st.caption(f"PCR {s['pcr']} · Max Pain {s['max_pain']:.0f} · CE wall {s['ce_wall']:.0f} · PE wall {s['pe_wall']:.0f}")
                for r in s.get("reasons", [])[:8]:
                    st.write(f"- {r}")
                for w in s.get("warnings", []):
                    st.write(f"⚠️ {w}")
                st.caption(f"Source: {s.get('data_source', 'unknown')}")


# --------------------------------------------------------------------------- #
# Section 2 — CHART
# --------------------------------------------------------------------------- #
st.subheader("📈 Underlying Chart")
chart_index = st.radio("Index", list(INDEX_CONFIG.keys()), horizontal=True, key="chart_index")
try:
    cdf = get_chart_df(chart_index)
    if cdf is None or cdf.empty:
        st.info("No chart data available right now.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Candlestick(x=cdf.index, open=cdf["Open"], high=cdf["High"],
                                     low=cdf["Low"], close=cdf["Close"], name="Price"))
        for line, dash in [("EMA9", "solid"), ("EMA21", "solid"), ("VWAP", "dot")]:
            if line in cdf.columns:
                fig.add_trace(go.Scatter(x=cdf.index, y=cdf[line], name=line, line=dict(dash=dash, width=1.5)))
        fig.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10),
                          xaxis_rangeslider_visible=False, legend=dict(orientation="h"))
        st.plotly_chart(fig, use_container_width=True)
except Exception as exc:  # noqa: BLE001
    st.info(f"Chart unavailable: {exc}")


# --------------------------------------------------------------------------- #
# Section 3 — MY POSITIONS (live monitor)
# --------------------------------------------------------------------------- #
st.subheader("💼 My Positions — Live Monitor")

with st.expander("➕ Add a position manually"):
    with st.form("add_position", clear_on_submit=True):
        c1, c2, c3, c4, c5 = st.columns(5)
        a_index = c1.selectbox("Index", list(INDEX_CONFIG.keys()))
        a_type = c2.selectbox("Type", ["CE", "PE"])
        a_strike = c3.number_input("Strike", min_value=0.0, step=50.0)
        a_entry = c4.number_input("Entry premium ₹", min_value=0.0, step=0.5)
        a_qty = c5.number_input("Quantity", min_value=1, value=75, step=1)
        if st.form_submit_button("Add position"):
            st.session_state["positions"].append({
                "index": a_index, "option_type": a_type, "strike": a_strike,
                "entry_premium": a_entry, "qty": a_qty,
            })
            st.toast("Position added", icon="✅")

positions = st.session_state["positions"]
if not positions:
    st.info("No open positions. Add one above, or click **Track this trade** on a card.")
else:
    # fresh underlying direction per index (for the thesis-broken warning)
    dir_by_index = {s["index"]: s["direction"] for s in signals}
    alarms = []

    for i, pos in enumerate(positions):
        spot = next((s["spot"] for s in signals if s["index"] == pos["index"]), 0.0)
        cur, src = live_premium(pos["index"], pos["strike"], pos["option_type"], spot)
        status = evaluate_position(
            pos["entry_premium"], cur, pos["option_type"], pos["qty"],
            dir_by_index.get(pos["index"], "WAIT"), target_pct=target_pct, stop_pct=stop_pct,
        )
        if status.is_alarm:
            alarms.append(f"{pos['index']} {pos['strike']} {pos['option_type']}")

        badge = {"TARGET_HIT": "🎯", "STOP_HIT": "🚨", "WARNING": "⚠️", "ON_TRACK": "✅", "NO_DATA": "❔"}.get(status.status, "")
        with st.container():
            m0, m1, m2, m3, m4, m5 = st.columns([2, 1, 1, 1, 1.5, 0.8])
            m0.markdown(f"**{pos['index']} {pos['strike']} {pos['option_type']}**  \n<span class='muted'>qty {pos['qty']}</span>", unsafe_allow_html=True)
            m1.metric("Entry", f"{pos['entry_premium']:.2f}")
            m2.metric("Now", f"{status.current_premium:.2f}")
            m3.metric("P&L %", f"{status.pnl_pct*100:+.1f}%")
            m4.metric("P&L ₹", f"{status.pnl_amount:,.0f}")
            if m5.button("Close", key=f"close_{i}"):
                st.session_state["positions"].pop(i)
                st.rerun()

            if status.status == "STOP_HIT":
                st.error(f"{badge} {status.message}")
            elif status.status == "WARNING":
                st.warning(f"{badge} {status.message}")
            elif status.status == "TARGET_HIT":
                st.success(f"{badge} {status.message}")
            else:
                st.info(f"{badge} {status.message}")
        st.divider()

    if alarms:
        st.toast(f"🚨 STOP HIT: {', '.join(alarms)} — exit now!", icon="🚨")
