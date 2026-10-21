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



# --------------------------------------------------------------------------- #
# Broker session reuse
# --------------------------------------------------------------------------- #
# A fresh SmartConnect login per chain fetch means 12 logins a minute at a
# 10-second refresh across two indices, which earns
# "Access denied because of exceeding access rate" and takes the live feed down
# mid-session. One session is created and reused until it actually fails.

_SESSION: dict = {"smart": None, "at": 0.0}
# Angel sessions last hours; an hour is a safe re-login interval.
_SESSION_TTL = 3600.0


def _angel_session(force: bool = False):
    """A logged-in SmartConnect, reused across calls. None if unavailable."""
    import time

    api_key = os.getenv("ANGEL_ONE_API_KEY", "")
    client_code = os.getenv("ANGEL_ONE_CLIENT_CODE", "")
    pin = os.getenv("ANGEL_ONE_PIN", "")
    totp_secret = os.getenv("ANGEL_ONE_TOTP_SECRET", "")
    if not (api_key and client_code and pin and totp_secret):
        return None

    fresh = (time.time() - _SESSION["at"]) < _SESSION_TTL
    if _SESSION["smart"] is not None and fresh and not force:
        return _SESSION["smart"]

    import pyotp
    from SmartApi import SmartConnect

    smart = SmartConnect(api_key)
    session = smart.generateSession(client_code, pin, pyotp.TOTP(totp_secret).now())
    if not session.get("status"):
        raise RuntimeError(f"Angel One login rejected: {session}")

    _SESSION["smart"] = smart
    _SESSION["at"] = time.time()
    return smart


def implied_spot_from_chain(df, atm: float) -> float | None:
    """Spot implied by put-call parity at the ATM strike.

    ``C - P = S - K`` (ignoring carry over an intraday horizon), so the option
    prices themselves reveal where the underlying is **right now**.

    This matters because the premiums come from the live broker feed while the
    spot came from Yahoo, which is delayed. Measured live, the gap was ~170
    BANKNIFTY points -- nearly two strikes, which is enough to pick the wrong
    ATM contract and quote a plan against the wrong premium.
    """
    try:
        row = df[df["strike"] == atm]
        if row.empty:
            return None
        call = float(row["ce_ltp"].iloc[0])
        put = float(row["pe_ltp"].iloc[0])
        if call <= 0 or put <= 0:
            return None
        return (call - put) + float(atm)
    except Exception:  # noqa: BLE001
        return None



def list_expiries(index: str, limit: int = 8) -> list[dict]:
    """Upcoming expiries for an index, nearest first.

    Which one you pick changes the trade completely. NIFTY carries weeklies
    (3 days out) alongside monthlies (11 days); BANKNIFTY is monthly only since
    the weeklies were withdrawn. A near expiry is cheap and moves fast but
    bleeds time value by the hour; a far one costs more and decays slower.
    """
    try:
        instruments = _load_instruments().copy()
        instruments.columns = [c.strip().lower() for c in instruments.columns]
        rows = instruments[
            (instruments["name"].astype(str).str.upper() == index.upper())
            & (instruments["instrumenttype"].astype(str).str.upper() == "OPTIDX")
        ].copy()
        if rows.empty:
            return []
        rows["_exp"] = pd.to_datetime(rows["expiry"], format="%d%b%Y", errors="coerce")
        today = pd.Timestamp.now().normalize()
        rows = rows[rows["_exp"] >= today]
        if rows.empty:
            return []

        # The monthly contract is the LAST expiry in its calendar month.
        # Checking "is there another expiry 7 days later" gets it wrong
        # whenever the next weekly falls in the following month.
        all_dates = sorted(rows["_exp"].dropna().unique())
        last_of_month = {
            max(d for d in all_dates
                if (pd.Timestamp(d).year, pd.Timestamp(d).month) == key)
            for key in {(pd.Timestamp(d).year, pd.Timestamp(d).month)
                        for d in all_dates}
        }

        out = []
        for label, group in rows.groupby("expiry", sort=False):
            when = group["_exp"].iloc[0]
            days = int((when - today).days)
            out.append({
                "label": str(label),
                "date": when.strftime("%Y-%m-%d"),
                "pretty": when.strftime("%a %d %b"),
                "days": days,
                "contracts": int(len(group)),
                "kind": "monthly" if when.to_datetime64() in
                        {pd.Timestamp(d).to_datetime64() for d in last_of_month}
                        else "weekly",
                # Phrased by days-to-expiry, because that is what drives
                # decay -- a weekly 24 days out behaves like a monthly.
                "decay_note": (
                    "expires today - extreme decay, do not buy" if days == 0 else
                    "1-2 days - cheapest, but bleeds by the hour" if days <= 2 else
                    "3-9 days - the usual intraday choice" if days <= 9 else
                    "10-20 days - costlier, decays slower" if days <= 20 else
                    "far out - expensive, slow decay, poor for intraday"
                ),
            })
        out.sort(key=lambda e: e["days"])
        return out[:limit]
    except Exception:  # noqa: BLE001
        return []


def _angel_option_filter(instruments: pd.DataFrame, index: str, strikes: list[int],
                         expiry: str | None = None) -> pd.DataFrame:
    """Filter instrument master for current-week NIFTY/BANKNIFTY options at given strikes."""
    name_col  = "name"
    inst_col  = "instrumenttype"
    exch_col  = "exch_seg"
    strike_col = "strike"

    # Column names vary slightly — normalize
    instruments.columns = [c.strip().lower() for c in instruments.columns]

    mask = (
        (instruments.get(name_col, "").astype(str).str.upper() == index.upper())
        & (instruments.get(inst_col, "").astype(str).str.upper().isin(["OPTIDX", "OPTSTK"]))
        & (instruments.get(exch_col, "").astype(str).str.upper() == "NFO")
    )
    filtered = instruments[mask].copy()

    if filtered.empty:
        return filtered

    # --- strike scale ----------------------------------------------------- #
    # Angel's instrument master stores strikes in PAISE: a 48000 strike is the
    # string "4800000.000000". Comparing those against [54700, 54800, ...]
    # matched nothing, every time, so the live chain silently fell back to
    # synthetic estimates and every premium on screen was fiction.
    # (tick_stream.resolve_option_token already knew this; this module did not.)
    if strike_col in filtered.columns:
        raw = pd.to_numeric(filtered[strike_col], errors="coerce")
        # Decide the scale from the data rather than assuming: if the median
        # strike is ~100x the spot-scale strikes we are looking for, it is paise.
        target = max(strikes) if strikes else 0
        median = float(raw.dropna().median()) if raw.notna().any() else 0.0
        if target and median > target * 10:
            raw = raw / 100.0
        filtered[strike_col] = raw.round().astype("Int64")
        filtered = filtered[filtered[strike_col].isin(strikes)]

    if filtered.empty:
        return filtered

    # --- expiry ----------------------------------------------------------- #
    # Without this every expiry is returned at once, so one strike appears
    # several times with different premiums and whichever row lands first wins.
    # Intraday trading means the NEAREST unexpired contract.
    if "expiry" in filtered.columns:
        parsed = pd.to_datetime(filtered["expiry"], format="%d%b%Y", errors="coerce")
        today = pd.Timestamp.now().normalize()
        if expiry:
            # An explicitly chosen expiry wins, provided it has not passed.
            chosen = filtered[filtered["expiry"].astype(str).str.upper() == expiry.upper()]
            if not chosen.empty:
                return chosen
        future = parsed[parsed >= today]
        if not future.empty:
            nearest = future.min()
            filtered = filtered[parsed == nearest]

    return filtered


def _fetch_market_data_angel(tokens: list[str], smart_api) -> dict[str, dict]:
    """Fetch LTP **and open interest** via Angel One getMarketData (FULL mode).

    FULL mode already returns ``opnInterest`` per token — the previous version
    only kept ``ltp`` and hard-coded OI to 0, which broke PCR/max-pain/walls and
    the scanner's liquidity ranking on live data. Returns
    ``{token: {"ltp": float, "oi": float}}``. Batches in 50s (the endpoint cap).
    """
    if not smart_api or not tokens:
        return {}
    out: dict[str, dict] = {}
    try:
        for start in range(0, len(tokens), 50):
            batch = tokens[start:start + 50]
            resp = smart_api.getMarketData("FULL", {"NFO": batch})
            if not resp.get("status"):
                continue
            for item in resp.get("data", {}).get("fetched", []):
                token = str(item.get("symbolToken", ""))
                out[token] = {
                    "ltp": float(item.get("ltp", 0) or 0),
                    "oi": float(item.get("opnInterest", item.get("openInterest", 0)) or 0),
                }
        return out
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


# PCR thresholds. Above/below these the chain is taken to have a directional
# opinion; between them it does not, and saying so is the honest answer.
PCR_CALL_THRESHOLD = 1.3
PCR_PUT_THRESHOLD = 0.75


def decide_chain_bias(
    pcr: float, spot: float, max_pain: float, step: float
) -> tuple[str, list[str]]:
    """The option chain's directional read: ``CALL`` / ``PUT`` / ``NEUTRAL``.

    Pulled out of :func:`suggest_option_trade` so it can be tested on its own.
    It could not be before, which is how the following survived unnoticed.

    **Only PCR sets the direction.** Max pain reports its distance and nothing
    more. The previous rule was ``if bias == "NEUTRAL": bias = "CALL"`` whenever
    spot sat below max pain, which was wrong three ways:

    1. It invented a directional opinion the chain did not have. PCR is the
       metric designed to give one; when PCR is mid-range, NEUTRAL is the
       answer.
    2. Max pain is an **expiry**-settlement concept -- the strike where writers
       keep the most premium. Treating it as an intraday magnet days before
       expiry is not a supported inference.
    3. It was **asymmetric**. Below max pain *created* a CALL bias; above max
       pain merely demoted CALL to NEUTRAL and never created a PUT. That gave
       the chain a standing bullish tilt, so in a falling market it
       manufactured a CALL that vetoed every well-evidenced PUT.

    Seen live on 2026-10-21: NIFTY PCR 0.95 and BANKNIFTY PCR 0.90 -- both
    neutral -- were promoted to CALL purely on max-pain distance. That
    collided with a price read of 0/100 built from fourteen independent
    bearish indicators, and the system stood aside from a trade it had strong
    evidence for.

    Max-pain distance still reaches the score as the weighted, symmetric
    ``max_pain_pull`` component in :mod:`intraday_advisor.scoring`, where it
    can be measured against outcomes and re-fitted -- or zeroed -- instead of
    silently overriding the direction.
    """
    reasons: list[str] = []
    bias = "NEUTRAL"

    if pcr > PCR_CALL_THRESHOLD:
        bias = "CALL"
        reasons.append(
            f"📊 PCR = {pcr} > {PCR_CALL_THRESHOLD} → More PUT writers than "
            f"CALL writers → Bullish bias (contrarian interpretation)"
        )
    elif pcr < PCR_PUT_THRESHOLD:
        bias = "PUT"
        reasons.append(
            f"📊 PCR = {pcr} < {PCR_PUT_THRESHOLD} → More CALL writers → "
            f"Bearish bias (contrarian interpretation)"
        )
    else:
        reasons.append(
            f"📊 PCR = {pcr} between {PCR_PUT_THRESHOLD}–{PCR_CALL_THRESHOLD} "
            f"→ Neutral zone — the chain has no directional opinion"
        )

    # Reported, never acted on. See the docstring.
    if spot > max_pain + step:
        reasons.append(
            f"⚠️ Spot ₹{spot:,.0f} is {spot - max_pain:,.0f} pts above Max Pain "
            f"₹{max_pain:,.0f} — writers would gain from a pullback "
            f"(scored, not a direction call)"
        )
    elif spot < max_pain - step:
        reasons.append(
            f"ℹ️ Spot ₹{spot:,.0f} is {max_pain - spot:,.0f} pts below Max Pain "
            f"₹{max_pain:,.0f} — writers would gain from a drift up "
            f"(scored, not a direction call)"
        )
    else:
        reasons.append(
            f"📍 Spot ₹{spot:,.0f} is near Max Pain ₹{max_pain:,.0f} "
            f"— normal range"
        )
    return bias, reasons


def days_to_expiry(expiry) -> float:
    """Calendar days to an Angel-format expiry such as ``27OCT2026``.

    Calendar, not trading, days: time value decays over the weekend too, which
    is exactly the cost a Friday buyer underestimates.
    """
    if not expiry:
        return 0.0
    import datetime as _dt
    text = str(expiry).strip().upper()
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            when = _dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return float(max(0, (when - _dt.date.today()).days))
    return 0.0


def _volatility_read(frame, spot: float, expiry) -> dict:
    """What the chain is charging for movement.

    Never raises: a desk screen must not go blank because one option quote was
    crossed or stale. VIX is deliberately NOT fetched here -- see
    :func:`intraday_advisor.options_math.vix_comparison_note`.
    """
    try:
        from intraday_advisor.options_math import analyse_chain_volatility
        return analyse_chain_volatility(
            frame, spot, days_to_expiry(expiry)
        ).as_dict()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Volatility read failed: %s", exc)
        return {}


def _pin_read(ce_wall, pe_wall, max_pain, spot, step) -> dict:
    """Is the chain defending one strike from both sides?"""
    try:
        from intraday_advisor.options_math import detect_pin
        return detect_pin(
            float(ce_wall or 0), float(pe_wall or 0), float(max_pain or 0),
            float(spot or 0), float(step or 50),
        ).as_dict()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Pin read failed: %s", exc)
        return {}


def suggest_option_trade(symbol: str, spot: float = 0.0,
                         expiry: str | None = None) -> dict:
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
    live_error: str | None = None

    try:
        smart = _angel_session()

        if smart is not None:
            if True:
                instruments = _load_instruments()
                filtered    = _angel_option_filter(instruments, index, strikes, expiry)

                if not filtered.empty:
                    tokens = filtered["token"].astype(str).tolist()
                    md = _fetch_market_data_angel(tokens, smart)

                    rows = []
                    for s in strikes:
                        ce_row = filtered[(filtered["strike"] == s) &
                                          (filtered.get("symbol", "").str.endswith("CE"))]
                        pe_row = filtered[(filtered["strike"] == s) &
                                          (filtered.get("symbol", "").str.endswith("PE"))]
                        ce_tok = str(ce_row["token"].iloc[0]) if not ce_row.empty else ""
                        pe_tok = str(pe_row["token"].iloc[0]) if not pe_row.empty else ""
                        ce_md = md.get(ce_tok, {})
                        pe_md = md.get(pe_tok, {})
                        rows.append({
                            "strike":    s,
                            "ce_ltp":   ce_md.get("ltp", 0),
                            "pe_ltp":   pe_md.get("ltp", 0),
                            "ce_oi":    ce_md.get("oi", 0),   # real OI from FULL feed
                            "pe_oi":    pe_md.get("oi", 0),
                            "ce_chg_oi": 0,
                            "pe_chg_oi": 0,
                        })
                    df_oi = pd.DataFrame(rows)
                    has_oi = (sum(r["ce_oi"] + r["pe_oi"] for r in rows) > 0)
                    data_source = "Angel One live (LTP + OI)" if has_oi else "Angel One LTP (live)"
    except Exception as exc:  # noqa: BLE001
        # Recorded, not swallowed. A bare `pass` here made a login failure look
        # exactly like a closed market: both produced "Estimated" with no clue
        # which. That is the difference between "wait for 09:15" and "your
        # credentials are not loading".
        live_error = f"{type(exc).__name__}: {exc}"

    # ── 3. Fallback synthetic OI ───────────────────────────────────────────
    if df_oi.empty or df_oi["ce_ltp"].sum() == 0:
        df_oi       = _build_synthetic_oi(strikes, spot)
        data_source = "Estimated (market closed or API unavailable)"
        if live_error:
            data_source = "Estimated (broker call failed)"

    # ── 3b. Correct the spot from the live chain ──────────────────────────
    # The premiums above are live; the spot came from a delayed source. Put-call
    # parity at the ATM recovers the real level, and the ATM strike is then
    # re-chosen from it. Without this the plan can be quoted against a contract
    # one or two strikes away from the money.
    spot_source = "delayed feed"
    if "live" in data_source.lower():
        implied = implied_spot_from_chain(df_oi, atm)
        if implied and abs(implied - spot) < spot * 0.02:   # sanity bound
            if abs(implied - spot) > step / 2:
                spot = implied
                atm = round(spot / step) * step
                spot_source = "implied from live option prices (parity)"
            else:
                spot_source = "live feed agrees with the delayed spot"

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

    # ── 5. Direction logic ─────────────────────────────────────────
    bias, bias_reasons = decide_chain_bias(pcr, spot, max_pain, step)
    reasons = [f"Data source: **{data_source}**"] + bias_reasons

    # Walls
    reasons.append(f"🔴 Strongest CE (Call) wall = Resistance at ₹{max_ce_wall:,} — market struggles above this")
    reasons.append(f"🟢 Strongest PE (Put) wall = Support at ₹{max_pe_wall:,} — market defended here")

    if spot > max_pe_wall and spot < max_ce_wall:
        reasons.append(f"✅ Spot is between support ₹{max_pe_wall:,} and resistance ₹{max_ce_wall:,} — tradable zone")

    _expiry_label = expiry or (
        filtered["expiry"].iloc[0]
        if "filtered" in dir() and not filtered.empty else None
    )

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
        "live_error":   live_error,
        "spot_source":  spot_source,
        "expiry":       _expiry_label,
        "volatility":   _volatility_read(df_oi, spot, _expiry_label),
        "pin":          _pin_read(max_ce_wall, max_pe_wall, max_pain, spot, step),
        "expiries":     list_expiries(index),
        "reasons":      reasons,
        "df":           df_oi,
        "error":        None,
    }


suggest_index_option = suggest_option_trade
