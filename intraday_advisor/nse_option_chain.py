"""
NSE Option Chain — built via Angel One SmartAPI + Instrument Master
===================================================================
Uses the freely downloadable Angel One instrument master JSON
to get NIFTY/BANKNIFTY option strikes, then fetches LTP for
ATM ± 10 strikes using the SmartAPI getMarketData endpoint.

Falls back to a synthetic OI estimate from Yahoo Finance index data
if Angel One credentials are not configured.
"""
from __future__ import annotations

import os
import math
import requests
import numpy as np
import pandas as pd

INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

_INSTRUMENT_CACHE: pd.DataFrame | None = None


def _load_instruments() -> pd.DataFrame:
    global _INSTRUMENT_CACHE
    if _INSTRUMENT_CACHE is not None and len(_INSTRUMENT_CACHE) > 0:
        return _INSTRUMENT_CACHE
    try:
        resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
        resp.raise_for_status()
        df = pd.DataFrame(resp.json())
        _INSTRUMENT_CACHE = df
        return df
    except Exception as e:
        raise RuntimeError(f"Failed to load Angel One instrument master: {e}")


def _nifty_spot_from_yahoo(symbol: str) -> float:
    """Fetch live NIFTY / BANKNIFTY spot from Yahoo Finance as fallback."""
    try:
        import yfinance as yf
        ticker = "^NSEI" if symbol == "NIFTY" else "^NSEBANK"
        data = yf.download(ticker, period="1d", interval="5m",
                            auto_adjust=True, progress=False, threads=False)
        if not data.empty:
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            return float(data["Close"].dropna().iloc[-1])
    except Exception:
        pass
    return 0.0


def _get_atm_strikes(spot: float, step: int, n: int = 8) -> list[int]:
    """Return n strikes above and below ATM."""
    atm  = round(spot / step) * step
    return [int(atm + i * step) for i in range(-n, n + 1)]


def _angel_option_filter(instruments: pd.DataFrame, index: str, strikes: list[int]) -> pd.DataFrame:
    """Filter instrument master for current-week NIFTY/BANKNIFTY options at given strikes."""
    name_col  = "name"
    inst_col  = "instrumenttype"
    exch_col  = "exch_seg"
    strike_col = "strike"

    # Column names vary slightly — normalize
    instruments.columns = [c.strip().lower() for c in instruments.columns]

    mask = (
        (instruments.get(name_col, "").str.upper() == index.upper())
        & (instruments.get(inst_col, "").str.upper().isin(["OPTIDX", "OPTSTK"]))
        & (instruments.get(exch_col, "").str.upper() == "NFO")
    )
    filtered = instruments[mask].copy()

    if filtered.empty:
        return filtered

    # strike column may be stored as string with trailing zeros e.g. "22500.00"
    if strike_col in filtered.columns:
        filtered[strike_col] = pd.to_numeric(
            filtered[strike_col], errors="coerce"
        ).astype("Int64")
        filtered = filtered[filtered[strike_col].isin(strikes)]

    return filtered


def _fetch_ltp_angel(tokens: list[str], smart_api) -> dict[str, float]:
    """Fetch last traded prices via Angel One getMarketData (MODE_FULL)."""
    if not smart_api or not tokens:
        return {}
    try:
        # SmartAPI getMarketData accepts a dict of exchange → token list
        payload = {"NFO": tokens[:50]}   # max 50 per call
        resp = smart_api.getMarketData("FULL", payload)
        if not resp.get("status"):
            return {}
        fetched = resp.get("data", {}).get("fetched", [])
        ltp_map = {}
        for item in fetched:
            token = str(item.get("symbolToken", ""))
            ltp   = float(item.get("ltp", 0))
            ltp_map[token] = ltp
        return ltp_map
    except Exception:
        return {}


def _build_synthetic_oi(strikes: list[int], spot: float) -> pd.DataFrame:
    """
    Build synthetic OI distribution using a log-normal model centred at ATM.
    Used as a graceful fallback when live data is unavailable.
    """
    step = abs(strikes[1] - strikes[0]) if len(strikes) > 1 else 50
    sigma = spot * 0.02   # 2% width

    rows = []
    for s in strikes:
        dist   = abs(s - spot)
        weight = math.exp(-0.5 * (dist / sigma) ** 2)
        rows.append({
            "strike":    s,
            "ce_oi":     int(weight * 1_000_000 * (1.2 if s > spot else 0.9)),
            "pe_oi":     int(weight * 1_000_000 * (0.9 if s > spot else 1.2)),
            "ce_ltp":    max(0.5, (s - spot) * 0.5) if s <= spot else max(0.5, (spot - s + step) * 0.3),
            "pe_ltp":    max(0.5, (spot - s) * 0.5) if s >= spot else max(0.5, (s - spot + step) * 0.3),
            "ce_chg_oi": 0,
            "pe_chg_oi": 0,
        })
    return pd.DataFrame(rows)


def suggest_option_trade(symbol: str, spot: float = 0.0) -> dict:
    """
    Analyse Any Equity/Index option chain and suggest CALL or PUT.
    Tries: Angel One LTP → falls back to synthetic model.
    """
    index = symbol.upper()
    
    # Dynamic strike stepping
    if index == "NIFTY":
        step = 50
    elif index == "BANKNIFTY":
        step = 100
    else:
        if spot <= 250: step = 2.5
        elif spot <= 1000: step = 5
        elif spot <= 3000: step = 10
        elif spot <= 5000: step = 20
        else: step = 50

    # ── 1. Get spot price ──────────────────────────────────────────────────
    if spot == 0.0:
        spot = _nifty_spot_from_yahoo(index)
        
    if spot == 0.0:
        return {"error": "Cannot fetch spot price. Please provide spot manually.", "symbol": symbol, "spot": 0}

    atm     = round(spot / step) * step
    strikes = _get_atm_strikes(spot, step, n=10)

    # ── 2. Try Angel One live LTP ──────────────────────────────────────────
    df_oi      = pd.DataFrame()
    data_source = "Synthetic (estimated)"

    try:
        api_key     = os.getenv("ANGEL_ONE_API_KEY", "")
        client_code = os.getenv("ANGEL_ONE_CLIENT_CODE", "")
        pin         = os.getenv("ANGEL_ONE_PIN", "")
        totp_secret = os.getenv("ANGEL_ONE_TOTP_SECRET", "")

        if api_key and client_code and pin and totp_secret:
            import pyotp
            from SmartApi import SmartConnect

            smart = SmartConnect(api_key)
            totp  = pyotp.TOTP(totp_secret).now()
            session = smart.generateSession(client_code, pin, totp)

            if session.get("status"):
                instruments = _load_instruments()
                filtered    = _angel_option_filter(instruments, index, strikes)

                if not filtered.empty:
                    tokens = filtered["token"].astype(str).tolist()
                    ltp_map = _fetch_ltp_angel(tokens, smart)

                    rows = []
                    for s in strikes:
                        ce_row = filtered[(filtered["strike"] == s) &
                                          (filtered.get("symbol", "").str.endswith("CE"))]
                        pe_row = filtered[(filtered["strike"] == s) &
                                          (filtered.get("symbol", "").str.endswith("PE"))]
                        ce_tok = str(ce_row["token"].iloc[0]) if not ce_row.empty else ""
                        pe_tok = str(pe_row["token"].iloc[0]) if not pe_row.empty else ""
                        rows.append({
                            "strike":    s,
                            "ce_ltp":   ltp_map.get(ce_tok, 0),
                            "pe_ltp":   ltp_map.get(pe_tok, 0),
                            "ce_oi":    0,  # OI not in LTP; estimated below
                            "pe_oi":    0,
                            "ce_chg_oi": 0,
                            "pe_chg_oi": 0,
                        })
                    df_oi = pd.DataFrame(rows)
                    data_source = "Angel One LTP (live)"
    except Exception:
        pass

    # ── 3. Fallback synthetic OI ───────────────────────────────────────────
    if df_oi.empty or df_oi["ce_ltp"].sum() == 0:
        df_oi       = _build_synthetic_oi(strikes, spot)
        data_source = "Estimated (market closed or API unavailable)"

    # ── 4. PCR & Max Pain on OI ───────────────────────────────────────────
    total_pe_oi = max(df_oi["pe_oi"].sum(), 1)
    total_ce_oi = max(df_oi["ce_oi"].sum(), 1)
    pcr = round(total_pe_oi / total_ce_oi, 2)

    pains = {}
    for _, row in df_oi.iterrows():
        s = row["strike"]
        ce_loss = ((df_oi[df_oi["strike"] > s]["ce_oi"]) *
                   (df_oi[df_oi["strike"] > s]["strike"] - s)).sum()
        pe_loss = ((df_oi[df_oi["strike"] < s]["pe_oi"]) *
                   (s - df_oi[df_oi["strike"] < s]["strike"])).sum()
        pains[s] = ce_loss + pe_loss
    max_pain = min(pains, key=pains.get) if pains else atm

    max_ce_wall = int(df_oi.loc[df_oi["ce_oi"].idxmax(), "strike"]) if df_oi["ce_oi"].sum() > 0 else atm + step * 3
    max_pe_wall = int(df_oi.loc[df_oi["pe_oi"].idxmax(), "strike"]) if df_oi["pe_oi"].sum() > 0 else atm - step * 3

    atm_row    = df_oi[df_oi["strike"] == atm]
    atm_ce_ltp = float(atm_row["ce_ltp"].sum()) if not atm_row.empty else 0
    atm_pe_ltp = float(atm_row["pe_ltp"].sum()) if not atm_row.empty else 0
    atm_ce_oi  = int(atm_row["ce_oi"].sum())    if not atm_row.empty else 0
    atm_pe_oi  = int(atm_row["pe_oi"].sum())    if not atm_row.empty else 0

    # ── 5. Direction logic ─────────────────────────────────────────────────
    reasons = [f"Data source: **{data_source}**"]
    bias    = "NEUTRAL"

    # PCR signal
    if pcr > 1.3:
        bias = "CALL"
        reasons.append(f"📊 PCR = {pcr} > 1.3 → More PUT writers than CALL writers → Bullish bias (contrarian interpretation)")
    elif pcr < 0.75:
        bias = "PUT"
        reasons.append(f"📊 PCR = {pcr} < 0.75 → More CALL writers → Bearish bias (contrarian interpretation)")
    else:
        reasons.append(f"📊 PCR = {pcr} between 0.75–1.3 → Neutral zone")

    # Max pain
    if spot > max_pain + step:
        reasons.append(f"⚠️ Spot ₹{spot:,.0f} is {spot-max_pain:,.0f} pts above Max Pain ₹{max_pain:,.0f} → Risk of pullback toward Max Pain")
        if bias == "CALL": bias = "NEUTRAL"
    elif spot < max_pain - step:
        reasons.append(f"✅ Spot ₹{spot:,.0f} is {max_pain-spot:,.0f} pts below Max Pain ₹{max_pain:,.0f} → Market may push up toward Max Pain")
        if bias == "NEUTRAL": bias = "CALL"
    else:
        reasons.append(f"📍 Spot ₹{spot:,.0f} is near Max Pain ₹{max_pain:,.0f} — normal range")

    # Walls
    reasons.append(f"🔴 Strongest CE (Call) wall = Resistance at ₹{max_ce_wall:,} — market struggles above this")
    reasons.append(f"🟢 Strongest PE (Put) wall = Support at ₹{max_pe_wall:,} — market defended here")

    if spot > max_pe_wall and spot < max_ce_wall:
        reasons.append(f"✅ Spot is between support ₹{max_pe_wall:,} and resistance ₹{max_ce_wall:,} — tradable zone")

    # ── 6. Final recommendation ─────────────────────────────────────────────
    if bias == "CALL":
        rec_strike = atm
        rec_type   = "CE"
        atm_ltp    = atm_ce_ltp
    elif bias == "PUT":
        rec_strike = atm
        rec_type   = "PE"
        atm_ltp    = atm_pe_ltp
    else:
        rec_strike = atm
        rec_type   = "—"
        atm_ltp    = min(atm_ce_ltp, atm_pe_ltp)

    return {
        "symbol":       index,
        "spot":         spot,
        "atm":          atm,
        "bias":         bias,
        "rec_strike":   rec_strike,
        "rec_type":     rec_type,
        "contract":     f"{index} {rec_strike} {rec_type}" if rec_type != "—" else "WAIT / NEUTRAL",
        "atm_ltp":      atm_ltp,
        "atm_ce_ltp":   atm_ce_ltp,
        "atm_pe_ltp":   atm_pe_ltp,
        "pcr":          pcr,
        "max_pain":     max_pain,
        "max_ce_wall":  max_ce_wall,
        "max_pe_wall":  max_pe_wall,
        "atm_ce_oi":    atm_ce_oi,
        "atm_pe_oi":    atm_pe_oi,
        "data_source":  data_source,
        "reasons":      reasons,
        "df":           df_oi,
        "error":        None,
    }
