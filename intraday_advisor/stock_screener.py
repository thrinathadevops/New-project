"""Rank intraday equity candidates through the same scoring engine.

What this is
------------
The old dashboard had a watchlist table of NSE stocks with a confluence score.
This rebuilds it on the current engine: all 39 indicator engines, the one
scoring module, and sizing priced through the **equity** cost model.

Why equities are not options with a different ticker
----------------------------------------------------
Three differences that change the arithmetic, all handled here:

* **Shares are divisible.** An option position is a whole lot or nothing --
  which is why a ₹20,000 account cannot take a BANKNIFTY contract. Shares can
  be bought one at a time, so a small account can actually size to its risk
  budget rather than being forced into 17% per trade.
* **No time decay.** An option bleeds premium while you wait; a share does not.
  A thesis that takes all afternoon is survivable here.
* **Costs are a smaller share of a larger position.** Intraday equity
  brokerage is 0.03%-or-₹20, and STT is 0.025% on the sell side only, against
  options' flat ₹20 per order plus 0.1% STT on premium. The cost floor that
  makes cheap options unviable mostly disappears.

What it is not
--------------
Not a list of stocks that will go up. It is the watchlist ranked by how much
the indicators currently agree, with the real cost of taking each one attached.
Nothing here is graded yet, so the ranking is a reading aid, not a measured
edge.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

import pandas as pd

from intraday_advisor.config import CostModel, RiskConfig
from intraday_advisor.data import clean_ohlcv, drop_forming_candle
from intraday_advisor.indicators import add_indicators
from intraday_advisor.nse_watchlist import get_scan_universe
from intraday_advisor.risk import calculate_position_size
from intraday_advisor.scoring import ScoreWeights, score_signal
from intraday_advisor.smart_money import analyze_smart_money
from intraday_advisor.strategy import (
    apply_ema_swing_breakout_strategy,
    ema_swing_breakout_decision,
)

logger = logging.getLogger("stock_screener")

# Indicators need warm-up (EMA200, SMA200). Below this the row is mostly NaN
# and the score is built on nothing.
MIN_BARS = 60

# Bars kept per symbol: 200 for the slowest indicator to warm up, plus a
# margin for the strategy's swing lookback.
WARMUP_BARS = 260


@dataclass
class StockRow:
    """One screened stock: the read, the plan, and what it costs."""

    symbol: str
    price: float
    change_pct: float
    score: int
    signal: str                   # BUY / SELL / HOLD
    # --- the read ---
    trend: str = ""
    above_vwap: bool = False
    rsi: float | None = None
    adx: float | None = None
    atr_pct: float | None = None
    volume_ratio: float | None = None
    # --- the plan, in rupees ---
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    shares: int = 0
    deployed: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    reward_amount: float = 0.0
    net_rr: float = 0.0
    cost_round_trip: float = 0.0
    cost_pct: float = 0.0
    breakeven: float = 0.0
    affordable: bool = False
    # --- traceability ---
    components: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bar_time: str = ""
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _equity_round_trip(entry: float, exit_price: float, shares: int,
                       costs: CostModel) -> float:
    """Total cost of an intraday equity round trip.

    Brokerage is the lower of a flat fee and a percentage (the real intraday
    structure), STT falls on the sell side only, and slippage is charged on
    both legs.
    """
    if shares <= 0:
        return 0.0
    buy_value = entry * shares
    sell_value = exit_price * shares

    brokerage = (min(costs.brokerage_flat, buy_value * costs.brokerage_pct)
                 + min(costs.brokerage_flat, sell_value * costs.brokerage_pct))
    stt = sell_value * costs.stt_sell_pct
    gst = brokerage * costs.gst_pct
    slippage = (buy_value + sell_value) * costs.slippage_pct
    return brokerage + stt + gst + slippage


def screen_one(
    symbol: str,
    frame: pd.DataFrame,
    *,
    capital: float,
    risk_pct: float,
    interval: str = "5m",
    weights: ScoreWeights | None = None,
    risk: RiskConfig | None = None,
    costs: CostModel | None = None,
) -> StockRow:
    """Score and size one stock from its candles."""
    risk = risk or RiskConfig()
    costs = costs or CostModel()

    try:
        clean = clean_ohlcv(frame)
    except Exception as exc:  # noqa: BLE001
        return StockRow(symbol=symbol, price=0, change_pct=0, score=0,
                        signal="HOLD", error=f"bad data: {exc}")

    # Signals come from closed candles only -- the forming bar repaints.
    closed = drop_forming_candle(clean, interval)
    if closed is None or len(closed) < MIN_BARS:
        return StockRow(symbol=symbol, price=0, change_pct=0, score=0,
                        signal="HOLD",
                        error=f"only {0 if closed is None else len(closed)} bars "
                              f"(need {MIN_BARS})")

    enriched = apply_ema_swing_breakout_strategy(add_indicators(closed))
    usable = enriched.dropna(subset=["Close", "EMA9", "EMA21", "ATR14"])
    if usable.empty:
        return StockRow(symbol=symbol, price=0, change_pct=0, score=0,
                        signal="HOLD", error="indicators did not warm up")

    last = usable.iloc[-1]
    row = {c: v for c, v in last.items()}
    decision = ema_swing_breakout_decision(symbol, enriched)
    smart = analyze_smart_money(enriched)

    scored = score_signal(
        row, decision_signal=decision.signal,
        vwap_relation=smart.vwap_relation,
        weights=weights or ScoreWeights.load(),
    )

    price = float(last["Close"])
    first = float(usable["Close"].iloc[0])
    change_pct = ((price / first) - 1) * 100 if first else 0.0
    atr = float(last.get("ATR14") or 0)

    # --- plan: ATR stop, fixed reward:risk, sized to the risk budget -------
    entry = price
    stop = max(0.01, entry - risk.atr_stop_multiplier * atr) if atr > 0 else 0.0
    target = entry + risk.reward_risk_ratio * (entry - stop) if stop else 0.0
    shares = (calculate_position_size(capital, entry, stop, risk_pct)
              if stop and stop < entry else 0)

    cost_rt = _equity_round_trip(entry, target or entry, shares, costs)
    gross_risk = shares * (entry - stop) if shares else 0.0
    gross_reward = shares * (target - entry) if shares else 0.0
    # Costs land on both outcomes, so they widen the loss and shrink the win.
    net_risk = gross_risk + cost_rt
    net_reward = max(0.0, gross_reward - cost_rt)
    deployed = shares * entry

    breakeven = entry + (cost_rt / shares) if shares else entry

    warnings: list[str] = []
    if shares <= 0:
        warnings.append("Risk budget does not cover one share at this stop distance.")
    if deployed > capital:
        warnings.append("Position exceeds available capital (intraday margin assumed).")
    if atr and price and (atr / price) < 0.0015:
        warnings.append("Very quiet - the move may not cover costs.")
    if net_reward and net_risk and (net_reward / net_risk) < 1.0:
        warnings.append("Net reward:risk below 1 after costs.")

    volume_ratio = None
    adtv = float(last.get("ADTV20") or 0)
    if adtv > 0:
        volume_ratio = round(float(last.get("Volume") or 0) / adtv, 2)

    return StockRow(
        symbol=symbol,
        price=round(price, 2),
        change_pct=round(change_pct, 2),
        score=scored.score,
        signal=scored.signal_type,
        trend=getattr(smart, "order_flow", "") or "",
        above_vwap=smart.vwap_relation == "above VWAP",
        rsi=round(float(last.get("RSI14")), 1) if pd.notna(last.get("RSI14")) else None,
        adx=round(float(last.get("ADX")), 1) if pd.notna(last.get("ADX")) else None,
        atr_pct=round(atr / price * 100, 2) if price else None,
        volume_ratio=volume_ratio,
        entry=round(entry, 2), stop=round(stop, 2), target=round(target, 2),
        shares=shares, deployed=round(deployed, 2),
        risk_amount=round(net_risk, 2),
        risk_pct=round(net_risk / capital, 4) if capital else 0.0,
        reward_amount=round(net_reward, 2),
        net_rr=round(net_reward / net_risk, 2) if net_risk > 0 else 0.0,
        cost_round_trip=round(cost_rt, 2),
        cost_pct=round(cost_rt / deployed, 5) if deployed else 0.0,
        breakeven=round(breakeven, 2),
        affordable=shares > 0,
        components=dict(scored.components),
        reasons=list(scored.bull) + list(scored.bear),
        warnings=warnings,
        bar_time=str(closed.index[-1]),
    )


def fetch_universe(symbols: list[str], *, interval: str = "5m",
                   period: str = "5d") -> dict[str, pd.DataFrame]:
    """Batch-download candles for a whole watchlist in one request.

    One batched call rather than thirty: far faster and much less likely to be
    rate-limited.
    """
    import yfinance as yf

    tickers = [f"{s}.NS" for s in symbols]
    try:
        raw = yf.download(
            tickers, period=period, interval=interval, progress=False,
            auto_adjust=True, threads=False, group_by="ticker",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Universe download failed: %s", exc)
        return {}

    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    # The slowest 200-period indicator (EMA200/SMA200) needs 200 bars to warm
    # up; everything beyond that is paid for on every symbol and used by
    # nothing. Trimming cut the per-symbol cost materially across 50 names.
    available = (set(raw.columns.get_level_values(0))
                 if isinstance(raw.columns, pd.MultiIndex) else set())
    for symbol, ticker in zip(symbols, tickers):
        try:
            frame = raw[ticker] if ticker in available else None
            if frame is None or frame.empty:
                continue
            out[symbol] = frame.dropna(how="all").tail(WARMUP_BARS)
        except Exception:  # noqa: BLE001
            continue
    return out


# Screening the whole universe is expensive; the result changes only on a new
# candle, so it is cached for part of a bar.
_CACHE: dict = {"key": None, "at": 0.0, "rows": [], "meta": {}}


def screen_universe(
    mode: str = "nifty50",
    *,
    capital: float = 20_000.0,
    risk_pct: float = 0.02,
    interval: str = "5m",
    limit: int = 30,
    max_workers: int = 6,
    ttl: float = 120.0,
    force: bool = False,
) -> dict:
    """Score a watchlist and return the top candidates, best first.

    ``mode`` selects the universe: nifty50 / banknifty / fno / all.
    """
    symbols = get_scan_universe(mode)
    key = (mode, interval, round(capital), round(risk_pct, 4))
    now = time.time()
    if not force and _CACHE["key"] == key and (now - _CACHE["at"]) < ttl:
        return {**_CACHE["meta"], "rows": _CACHE["rows"], "cached": True}

    started = time.perf_counter()
    frames = fetch_universe(symbols, interval=interval)
    fetch_ms = (time.perf_counter() - started) * 1000

    weights = ScoreWeights.load()
    risk = RiskConfig(capital=capital, risk_per_trade_pct=risk_pct)
    costs = CostModel()

    def work(item):
        symbol, frame = item
        try:
            return screen_one(symbol, frame, capital=capital, risk_pct=risk_pct,
                              interval=interval, weights=weights, risk=risk,
                              costs=costs)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the scan
            return StockRow(symbol=symbol, price=0, change_pct=0, score=0,
                            signal="HOLD", error=str(exc)[:120])

    # The indicator set costs ~0.1-0.6s per symbol and is CPU-bound in pandas,
    # which releases the GIL often enough for threads to help materially here.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        rows = list(pool.map(work, frames.items()))

    scored = [r for r in rows if r.error is None]
    failed = [r for r in rows if r.error is not None]

    # Directional conviction first: the score is a distance from the neutral
    # 50, so a strong SELL is as interesting as a strong BUY.
    scored.sort(key=lambda r: abs(r.score - 50), reverse=True)
    top = scored[:limit]

    meta = {
        "mode": mode,
        "universe_size": len(symbols),
        "fetched": len(frames),
        "scored": len(scored),
        "failed": len(failed),
        "interval": interval,
        "capital": capital,
        "risk_pct": risk_pct,
        "fetch_ms": round(fetch_ms),
        "total_ms": round((time.perf_counter() - started) * 1000),
        "failures": [{"symbol": r.symbol, "error": r.error} for r in failed[:8]],
        "note": (
            "Ranked by how far the indicators lean from neutral, with the real "
            "round-trip cost of each attached. Shares are divisible, so a small "
            "account can size to its risk budget here -- unlike an option, which "
            "is one whole lot or nothing. None of this is graded yet, so the "
            "ranking is a reading aid, not a measured edge."
        ),
    }
    rows_out = [r.as_dict() for r in top]
    _CACHE.update({"key": key, "at": now, "rows": rows_out, "meta": meta})
    return {**meta, "rows": rows_out, "cached": False}

# --------------------------------------------------------------------------- #
# Background scanning
# --------------------------------------------------------------------------- #
# A full scan costs ~0.9s per symbol and pandas holds the GIL through most of
# it, so threads barely help and 50 names take the better part of a minute.
# Rather than make a dashboard refresh wait for that, the scan runs on a
# worker thread and the last completed result is served immediately.

import threading

_SCAN_LOCK = threading.Lock()
_SCAN_STATE: dict = {"running": False, "started_at": 0.0, "result": None}


def scan_async(mode: str = "nifty50", **kwargs) -> dict:
    """Serve the last completed scan and refresh it in the background.

    Returns immediately, always. The payload carries ``scanning`` and
    ``age_seconds`` so the UI can say "working" rather than showing an empty
    table that looks broken.
    """
    with _SCAN_LOCK:
        cached = _SCAN_STATE["result"]
        running = _SCAN_STATE["running"]
        age = time.time() - _SCAN_STATE["started_at"] if _SCAN_STATE["started_at"] else None

    stale = cached is None or (age is not None and age > kwargs.get("ttl", 120.0))
    if stale and not running:
        with _SCAN_LOCK:
            _SCAN_STATE["running"] = True

        def worker():
            try:
                result = screen_universe(mode, force=True, **kwargs)
            except Exception as exc:  # noqa: BLE001
                result = {"rows": [], "error": str(exc)[:200], "mode": mode}
            with _SCAN_LOCK:
                _SCAN_STATE.update({
                    "result": result, "running": False, "started_at": time.time(),
                })

        threading.Thread(target=worker, daemon=True, name="stock-scan").start()

    if cached is None:
        return {
            "rows": [], "mode": mode, "scanning": True, "age_seconds": None,
            "note": "First scan running - about a minute for 50 symbols. "
                    "Results appear here when it finishes and then refresh in "
                    "the background.",
        }
    return {**cached, "scanning": running, "age_seconds": round(age or 0)}
