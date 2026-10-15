"""
Options Scanner — Top Intraday CALL/PUT across ALL NIFTY & BANKNIFTY strikes
============================================================================
Separate page from options_dashboard.py (which only names the ATM contract).

This one continuously scans EVERY strike's CALL and PUT in both index chains,
ranks them, and shows the top intraday buy candidates. You set your BUDGET; it
tells you how many lots fit and your exact rupee profit/loss at target/stop, so
you can pick a trade you can actually afford and then track it live.

Run:  streamlit run options_scanner_dashboard.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from intraday_advisor.options_scanner import DEFAULT_LOT_SIZES, scan_option_contracts
from intraday_advisor.index_options import get_option_premium
from intraday_advisor.position_monitor import evaluate_position

st.set_page_config(page_title="Options Scanner — Top CALL/PUT", layout="wide", page_icon="🔎")
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open() -> bool:
    n = now_ist()
    if n.weekday() >= 5:
        return False
    return n.replace(hour=9, minute=15, second=0, microsecond=0) <= n <= n.replace(hour=15, minute=30, second=0, microsecond=0)


if "scanner_positions" not in st.session_state:
    st.session_state["scanner_positions"] = []

# --------------------------------------------------------------------------- #
# sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("⚙️ Scan settings")
    budget = st.number_input("Your budget (₹)", min_value=500, value=10_000, step=500)
    target_pct = st.slider("Profit target (%)", 5, 60, 25, 5) / 100
    stop_pct = st.slider("Loss / stop (%)", 5, 50, 20, 5) / 100
    top_n = st.slider("Show top N trades", 10, 40, 20, 5)
    st.divider()
    st.caption("Lot sizes change at the exchange — set yours:")
    nifty_lot = st.number_input("NIFTY lot size", min_value=1, value=DEFAULT_LOT_SIZES["NIFTY"], step=1)
    bn_lot = st.number_input("BANKNIFTY lot size", min_value=1, value=DEFAULT_LOT_SIZES["BANKNIFTY"], step=1)
    st.divider()
    aligned_only = st.checkbox("Only trades aligned with trend", value=True)
    affordable_only = st.checkbox("Only what fits my budget", value=True)
    min_conf = st.slider("Min confidence", 0, 100, 55, 5)
    refresh_choice = st.selectbox("Auto-refresh", ["Every 15s", "Every 30s", "Every 60s", "Manual"], index=0)
    st.caption("⚠️ Decision support, not a profit guarantee. Always exit at your stop.")

if refresh_choice != "Manual":
    st_autorefresh(interval={"Every 15s": 15_000, "Every 30s": 30_000, "Every 60s": 60_000}[refresh_choice], key="scan_refresh")


# --------------------------------------------------------------------------- #
# scan (cached)
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=12, show_spinner="Scanning all strikes…")
def run_scan(_target: float, _stop: float, _nifty_lot: int, _bn_lot: int) -> list[dict]:
    cands = scan_option_contracts(
        target_pct=_target, stop_pct=_stop,
        lot_sizes={"NIFTY": _nifty_lot, "BANKNIFTY": _bn_lot},
    )
    return [c.as_dict() for c in cands]


@st.cache_data(ttl=8, show_spinner=False)
def live_premium(index: str, strike: float, opt_type: str, spot: float) -> tuple[float, str]:
    return get_option_premium(index, strike, opt_type, spot=spot)


candidates = run_scan(target_pct, stop_pct, nifty_lot, bn_lot)
any_live = any(c.get("is_live") for c in candidates)

# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #
h1, h2, h3 = st.columns([3, 1.3, 1.3])
h1.title("🔎 Options Scanner — Top CALL/PUT")
h2.markdown("### " + ("🟢 MARKET OPEN" if is_market_open() else "🔴 MARKET CLOSED"))
h3.markdown("### " + ("🟢 LIVE DATA" if any_live else "🟡 ESTIMATED"))
if not any_live:
    st.warning(
        "🟡 **ESTIMATED DATA** — broker feed not live (market closed or Angel One not connected). "
        "Premiums are modelled estimates, **not for live trading**.", icon="⚠️",
    )
st.caption(f"Scanned {len(candidates)} contracts · {now_ist():%H:%M:%S} IST · target +{target_pct*100:.0f}% / stop -{stop_pct*100:.0f}% · budget ₹{budget:,.0f}")

# --------------------------------------------------------------------------- #
# filter + rank
# --------------------------------------------------------------------------- #
def lots_for(c: dict) -> int:
    cap = c["capital_per_lot"]
    return int(budget // cap) if cap > 0 else 0


filtered = []
for c in candidates:
    if aligned_only and not c["aligned"]:
        continue
    if c["score"] < min_conf:
        continue
    lots = lots_for(c)
    if affordable_only and lots < 1:
        continue
    filtered.append((c, lots))

top = filtered[:top_n]

st.subheader(f"🏆 Top {len(top)} Intraday Trades")
if not top:
    st.info("No trades match your filters right now. Loosen 'aligned only', lower min confidence, or raise budget.")
else:
    table = []
    for rank, (c, lots) in enumerate(top, 1):
        table.append({
            "#": rank,
            "Contract": f"{c['index']} {int(c['strike'])} {c['option_type']}",
            "Side": "🟢 CALL" if c["contract_direction"] == "CALL" else "🔴 PUT",
            "Conf": c["score"],
            "Premium ₹": c["premium"],
            "₹/Lot": c["capital_per_lot"],
            "Lots": lots,
            "Target ₹": c["target_premium"],
            "Stop ₹": c["stop_premium"],
            "Max Profit ₹": round(c["profit_per_lot"] * lots),
            "Max Loss ₹": round(c["loss_per_lot"] * lots),
            "Money": c["moneyness"],
            "OI": int(c["open_interest"]),
        })
    df = pd.DataFrame(table)
    event = st.dataframe(
        df, use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Conf": st.column_config.ProgressColumn("Conf", min_value=0, max_value=100, format="%d"),
            "Premium ₹": st.column_config.NumberColumn(format="%.2f"),
            "₹/Lot": st.column_config.NumberColumn(format="₹%.0f"),
            "Target ₹": st.column_config.NumberColumn(format="%.2f"),
            "Stop ₹": st.column_config.NumberColumn(format="%.2f"),
            "Max Profit ₹": st.column_config.NumberColumn(format="₹%d"),
            "Max Loss ₹": st.column_config.NumberColumn(format="₹%d"),
        },
    )

    sel = event.selection.rows if event and event.selection else []
    if sel:
        c, lots = top[sel[0]]
        st.markdown(f"### Selected: **{c['index']} {int(c['strike'])} {c['option_type']}**  ({c['contract_direction']})")
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Entry premium ₹", f"{c['premium']:.2f}")
        d2.metric(f"Target +{target_pct*100:.0f}% ₹", f"{c['target_premium']:.2f}")
        d3.metric(f"Stop -{stop_pct*100:.0f}% ₹", f"{c['stop_premium']:.2f}")
        d4.metric("Capital / lot ₹", f"{c['capital_per_lot']:,.0f}")
        st.caption("Why: " + " · ".join(c.get("reasons", [])))
        trade_lots = st.number_input("Lots to buy", min_value=1, value=max(1, lots), step=1, key="sel_lots")
        if st.button("➕ Track this trade", type="primary"):
            st.session_state["scanner_positions"].append({
                "index": c["index"], "option_type": c["option_type"], "strike": c["strike"],
                "entry_premium": c["premium"], "qty": int(trade_lots * c["lot_size"]),
                "spot": 0,  # 0 => premium fetcher resolves spot live
            })
            st.toast(f"Tracking {c['index']} {int(c['strike'])} {c['option_type']}", icon="✅")

st.caption("Tip: click a row to select it, set your lots, and track it. WAIT/low-confidence trades are filtered out.")

# --------------------------------------------------------------------------- #
# My Picks — live monitor
# --------------------------------------------------------------------------- #
st.subheader("💼 My Picks — Live Monitor")
positions = st.session_state["scanner_positions"]
if not positions:
    st.info("No tracked trades yet. Select a row above and click **Track this trade**.")
else:
    # fresh per-index direction for the thesis-broken warning
    dir_by_index = {}
    for c in candidates:
        dir_by_index.setdefault(c["index"], c["index_direction"])

    alarms = []
    for i, pos in enumerate(positions):
        cur, _src = live_premium(pos["index"], pos["strike"], pos["option_type"], pos.get("spot", 0) or 0)
        status = evaluate_position(
            pos["entry_premium"], cur, pos["option_type"], pos["qty"],
            dir_by_index.get(pos["index"], "WAIT"), target_pct=target_pct, stop_pct=stop_pct,
        )
        if status.is_alarm:
            alarms.append(f"{pos['index']} {int(pos['strike'])} {pos['option_type']}")

        m0, m1, m2, m3, m4, m5 = st.columns([2, 1, 1, 1, 1.5, 0.8])
        m0.markdown(f"**{pos['index']} {int(pos['strike'])} {pos['option_type']}**  \nqty {pos['qty']}")
        m1.metric("Entry", f"{pos['entry_premium']:.2f}")
        m2.metric("Now", f"{status.current_premium:.2f}")
        m3.metric("P&L %", f"{status.pnl_pct*100:+.1f}%")
        m4.metric("P&L ₹", f"{status.pnl_amount:,.0f}")
        if m5.button("Close", key=f"sclose_{i}"):
            st.session_state["scanner_positions"].pop(i)
            st.rerun()

        if status.status == "STOP_HIT":
            st.error(status.message)
        elif status.status == "WARNING":
            st.warning(status.message)
        elif status.status == "TARGET_HIT":
            st.success(status.message)
        else:
            st.info(status.message)
        st.divider()

    if alarms:
        st.toast(f"🚨 STOP HIT: {', '.join(alarms)} — exit now!", icon="🚨")
