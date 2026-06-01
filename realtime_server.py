"""
Real-Time WebSocket Server for Live Trading Analysis.

Phase-1 correctness rewrite. Key guarantees vs. the previous version:

  * REAL DATA: candles come from a configurable feed (Angel One by default),
    not a random-number generator. Synthetic data is still available for demos
    but is structurally prevented from ever being broadcast as actionable.
  * NO REPAINTING: the still-forming bar is dropped before analysis, so signals
    are computed only on closed candles.
  * ONE SCORE: confidence comes from the unified ``scoring`` engine.
  * RISK ENFORCED: a ``LiveRiskState`` gate caps concurrent open-risk and halts
    on daily-loss / drawdown limits before any signal is marked actionable.
  * STALENESS GUARD: data older than a few bars (e.g. market closed) is flagged
    and never marked actionable.

Configure via environment variables:
    REALTIME_FEED          angelone | synthetic   (default: synthetic)
    REALTIME_SYMBOLS       comma-separated tickers
    REALTIME_INTERVAL      5m | 15m | ...         (default: 5m)
    REALTIME_CAPITAL       account capital (INR)  (default: 20000)
    REALTIME_RISK_PCT      risk per trade         (default: 0.02)
    ANALYSIS_REFRESH_SECONDS   re-analysis cadence (default: 15)
    BUY_THRESHOLD / SELL_THRESHOLD   score cutoffs (default: 65 / 35)
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Set

try:
    import websockets
    from websockets.server import serve
except ImportError:
    print("❌ websockets not installed. Install with: pip install websockets")
    sys.exit(1)

# Add project to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from intraday_advisor.config import RiskConfig
from intraday_advisor.data import candle_age, drop_forming_candle, parse_interval
from intraday_advisor.feeds import DataFeedError, resolve_feed
from intraday_advisor.indicators import add_indicators
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy, ema_swing_breakout_decision
from intraday_advisor.price_action import analyze_price_action
from intraday_advisor.smart_money import analyze_smart_money
from intraday_advisor.box_theory import analyze_box_theory
from intraday_advisor.risk import LiveRiskState, build_trade_plan_from_stop
from intraday_advisor.scoring import ScoreWeights, score_signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("realtime_server")

# ---- Global state ----------------------------------------------------------
clients: Set = set()
analysis_cache: Dict = {}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class ServerConfig:
    def __init__(self) -> None:
        self.feed_name = os.getenv("REALTIME_FEED", "synthetic")
        symbols = os.getenv("REALTIME_SYMBOLS", "RELIANCE,HDFCBANK,INFY,TATAMOTORS,JSWSTEEL")
        self.symbols = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        self.interval = os.getenv("REALTIME_INTERVAL", "5m")
        self.capital = _env_float("REALTIME_CAPITAL", 20_000.0)
        self.risk_pct = _env_float("REALTIME_RISK_PCT", 0.02)
        self.refresh_seconds = int(_env_float("ANALYSIS_REFRESH_SECONDS", 15))
        self.buy_threshold = _env_float("BUY_THRESHOLD", 65.0)
        self.sell_threshold = _env_float("SELL_THRESHOLD", 35.0)
        # Bar age beyond this multiple of the interval => stale (market closed, feed gap).
        self.staleness_multiple = _env_float("STALENESS_MULTIPLE", 3.0)


def analyze_symbol(feed, symbol: str, cfg: ServerConfig, weights: ScoreWeights) -> dict | None:
    """Blocking analysis for one symbol. Run inside an executor by the loop.

    Returns a result row, or None if there is not enough usable data.
    Never raises feed errors — they are converted into an error row so one bad
    symbol cannot take down the whole broadcast.
    """
    try:
        raw = feed.get_candles(symbol, cfg.interval)
    except DataFeedError as exc:
        logger.warning("Feed error for %s: %s", symbol, exc)
        return {"Ticker": symbol, "error": str(exc), "actionable": False,
                "signal_type": "HOLD", "confidence": 0, "data_source": feed.name}

    # --- anti-repaint: analyse only the last CLOSED candle ---
    closed = drop_forming_candle(raw, cfg.interval)
    if closed is None or len(closed) < 30:
        return None

    df = apply_ema_swing_breakout_strategy(add_indicators(closed))
    df_clean = df.dropna(subset=["Close", "EMA9", "EMA21", "ATR14", "RecentSwingHigh", "RecentSwingLow"])
    if df_clean.empty:
        return None
    last = df_clean.iloc[-1]

    decision = ema_swing_breakout_decision(symbol, df)
    price_action = analyze_price_action(df)
    smart_money = analyze_smart_money(df)
    box = analyze_box_theory(df)

    # --- staleness: is the latest closed bar recent enough to act on? ---
    age = candle_age(closed)
    interval_delta = parse_interval(cfg.interval)
    stale = False
    if age is not None and interval_delta is not None:
        stale = age > interval_delta * cfg.staleness_multiple
    age_seconds = float(age.total_seconds()) if age is not None else None

    row = {
        "Ticker": symbol,
        "Close": float(last["Close"]),
        "Signal": decision.signal,
        "EMA9": float(last["EMA9"]),
        "EMA21": float(last["EMA21"]),
        "EMA200": float(last["EMA200"]),
        "AboveEMA200": bool(last["Close"] > last["EMA200"]),
        "ATR14": float(last["ATR14"]),
        "RSI14": float(last["RSI14"]),
        "SwingHigh": float(last["RecentSwingHigh"]),
        "SwingLow": float(last["RecentSwingLow"]),
        "TrendStructure": price_action.trend,
        "OrderFlow": smart_money.order_flow,
        "VWAPRelation": smart_money.vwap_relation,
        "BoxBias": box.bias,
        "Bullish_OB": bool(last.get("Bullish_OB", False)),
        "Bearish_OB": bool(last.get("Bearish_OB", False)),
        "Bullish_FVG": bool(last.get("Bullish_FVG", False)),
        "Bearish_FVG": bool(last.get("Bearish_FVG", False)),
        "Liquidity_Sweep_Bullish": bool(last.get("Liquidity_Sweep_Bullish", False)),
        "Liquidity_Sweep_Bearish": bool(last.get("Liquidity_Sweep_Bearish", False)),
        "Adaptive_MACD_Hist": float(last.get("Adaptive_MACD_Hist", 0)),
        "SuperTrend_Dir": int(last.get("SuperTrend_Dir", 0)),
        "data_source": feed.name,
        "is_synthetic": bool(feed.is_synthetic),
        "candle_time": str(closed.index[-1]),
        "candle_age_seconds": age_seconds,
        "stale": bool(stale),
    }

    scored = score_signal(
        row,
        decision_signal=decision.signal,
        vwap_relation=smart_money.vwap_relation,
        weights=weights,
        buy_threshold=cfg.buy_threshold,
        sell_threshold=cfg.sell_threshold,
    )
    row.update({
        "confidence": scored.score,
        "signal_type": scored.signal_type,
        "bull_signals": scored.bull,
        "bear_signals": scored.bear,
        "score_components": scored.components,
    })

    # Build a trade plan + per-trade rupee risk for the risk gate.
    row["plan"] = None
    row["trade_risk"] = 0.0
    if scored.signal_type in ("BUY", "SELL"):
        if scored.signal_type == "BUY":
            initial_stop = max(float(last["RecentSwingLow"]), float(last["Close"] - 1.5 * last["ATR14"]))
        else:
            initial_stop = min(float(last["RecentSwingHigh"]), float(last["Close"] + 1.5 * last["ATR14"]))
        try:
            plan = build_trade_plan_from_stop(
                symbol, scored.signal_type, float(last["Close"]), initial_stop,
                RiskConfig(capital=cfg.capital, risk_per_trade_pct=cfg.risk_pct),
            )
            row["plan"] = {
                "entry": plan.entry, "stop": plan.stop, "target": plan.target,
                "shares": plan.shares, "risk": plan.risk_amount, "reward": plan.reward_amount,
                "rr": round(plan.reward_amount / plan.risk_amount, 2) if plan.risk_amount > 0 else 0,
            }
            row["trade_risk"] = float(plan.risk_amount)
        except (ValueError, ZeroDivisionError) as exc:
            logger.debug("Plan build skipped for %s: %s", symbol, exc)

    # HARD GUARD: actionable requires real, fresh data and a directional signal.
    # The risk-gate decision is applied later in the loop (needs cross-symbol state).
    row["actionable"] = (
        not feed.is_synthetic
        and not stale
        and scored.signal_type in ("BUY", "SELL")
    )
    row["timestamp"] = datetime.now().isoformat()
    return row


async def broadcast_update(message: dict) -> None:
    if not clients:
        return
    payload = json.dumps(message, default=str)
    dead = set()
    for client in clients:
        try:
            await client.send(payload)
        except Exception as exc:  # noqa: BLE001 - log + drop the client
            logger.warning("Client send failed: %s", exc)
            dead.add(client)
    for client in dead:
        clients.discard(client)


async def analysis_loop(cfg: ServerConfig) -> None:
    feed = resolve_feed(cfg.feed_name)
    weights = ScoreWeights.load()
    risk_state = LiveRiskState(config=RiskConfig(capital=cfg.capital, risk_per_trade_pct=cfg.risk_pct))
    loop = asyncio.get_event_loop()

    if feed.is_synthetic:
        logger.warning("=" * 70)
        logger.warning("DEMO MODE: feed is SYNTHETIC random-walk data.")
        logger.warning("Signals will be marked actionable=False and MUST NOT be traded.")
        logger.warning("Set REALTIME_FEED=angelone for real signals.")
        logger.warning("=" * 70)

    while True:
        try:
            # Network/CPU work off the event loop so sends stay responsive.
            tasks = [loop.run_in_executor(None, analyze_symbol, feed, sym, cfg, weights)
                     for sym in cfg.symbols]
            results = [r for r in await asyncio.gather(*tasks) if r]

            # Apply the risk gate across this cycle's candidates, best score first.
            risk_state.begin_cycle()
            for r in sorted(results, key=lambda x: x.get("confidence", 0), reverse=True):
                if not r.get("actionable"):
                    continue
                trade_risk = float(r.get("trade_risk", 0.0))
                if risk_state.can_admit(trade_risk):
                    risk_state.admit(trade_risk)
                else:
                    r["actionable"] = False
                    r["throttled_by_risk"] = True

            active = [r for r in results if r.get("actionable")]
            mode = "DEMO_SYNTHETIC_DATA" if feed.is_synthetic else "LIVE"

            analysis_cache.update({"results": results, "active": active, "mode": mode})
            await broadcast_update({
                "type": "analysis_update",
                "mode": mode,
                "timestamp": datetime.now().isoformat(),
                "feed": feed.name,
                "interval": cfg.interval,
                "total_analyzed": len(results),
                "active_trades": len(active),
                "risk": risk_state.status(),
                "trades": active,
                "all_results": results,
            })
            await asyncio.sleep(cfg.refresh_seconds)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            logger.error("Analysis loop error: %s", exc)
            await asyncio.sleep(5)


async def handler(websocket, path=None) -> None:
    clients.add(websocket)
    logger.info("Client connected. Total clients: %d", len(clients))
    try:
        await websocket.send(json.dumps({
            "type": "welcome",
            "message": "Connected to Real-Time Trading Analysis Server",
            "mode": analysis_cache.get("mode", "STARTING"),
            "timestamp": datetime.now().isoformat(),
        }))
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"type": "error", "message": "Invalid JSON format"}))
                continue

            command = data.get("command", "ping")
            if command == "ping":
                await websocket.send(json.dumps({"type": "pong", "timestamp": datetime.now().isoformat()}))
            elif command == "subscribe":
                # Server runs one shared watchlist (env-configured) in Phase 1;
                # acknowledge so clients know they are attached to the stream.
                await websocket.send(json.dumps({
                    "type": "subscribed",
                    "note": "Server streams a shared env-configured watchlist in this phase.",
                    "timestamp": datetime.now().isoformat(),
                }))
            elif command == "status":
                await websocket.send(json.dumps({
                    "type": "status",
                    "mode": analysis_cache.get("mode", "STARTING"),
                    "active_trades": len(analysis_cache.get("active", [])),
                    "timestamp": datetime.now().isoformat(),
                }, default=str))
            else:
                await websocket.send(json.dumps({"type": "error", "message": f"Unknown command: {command}"}))
    except websockets.exceptions.ConnectionClosed:
        logger.info("Client disconnected")
    finally:
        clients.discard(websocket)
        logger.info("Client removed. Total clients: %d", len(clients))


async def main() -> None:
    host, port = "0.0.0.0", 8765
    cfg = ServerConfig()
    logger.info("Starting Real-Time WebSocket Server on ws://%s:%d", host, port)
    logger.info("Feed: %s | Symbols: %s | Interval: %s", cfg.feed_name, ", ".join(cfg.symbols), cfg.interval)
    logger.info("Capital: ₹%s | Risk/trade: %.1f%% | Refresh: %ds",
                f"{cfg.capital:,.0f}", cfg.risk_pct * 100, cfg.refresh_seconds)
    async with serve(handler, host, port):
        logger.info("✅ WebSocket server running. Waiting for clients...")
        await analysis_loop(cfg)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as exc:  # noqa: BLE001
        logger.error("Fatal error: %s", exc)
        sys.exit(1)
