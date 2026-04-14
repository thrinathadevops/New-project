"""
Real-Time WebSocket Server for Live Trading Analysis
Zero-latency streaming of trade signals using WebSockets
Replaces HTTP polling with push-based updates
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Set, Dict
import sys
from pathlib import Path

try:
    import websockets
    from websockets.server import serve
except ImportError:
    print("❌ websockets not installed. Install with: pip install websockets")
    sys.exit(1)

import pandas as pd
import numpy as np

# Add project to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from intraday_advisor.data import generate_sample_ohlcv
from intraday_advisor.indicators import add_indicators
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy, ema_swing_breakout_decision
from intraday_advisor.price_action import analyze_price_action
from intraday_advisor.smart_money import analyze_smart_money
from intraday_advisor.box_theory import analyze_box_theory
from intraday_advisor.situational import latest_situational_summary
from intraday_advisor.risk import build_trade_plan_from_stop
from intraday_advisor.config import RiskConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global state
clients: Set = set()
analysis_cache: Dict = {}
analysis_update_interval = 30  # seconds


def calculate_signal_strength(decision, price_action, smart_money, box, row) -> dict:
    """
    Calculate unified signal strength from all analysis techniques
    Returns confidence score, bull/bear reasons, and risk/reward metrics
    """
    bull_signals = []
    bear_signals = []
    score = 50  # neutral baseline
    
    # EMA signals (most important)
    if decision.signal == "BUY":
        bull_signals.append("EMA9 above EMA21 with swing-high breakout")
        score += 20
    elif decision.signal == "SELL":
        bear_signals.append("EMA9 below EMA21")
        score -= 20
    
    # Trend check
    if row["AboveEMA200"]:
        bull_signals.append("Price above EMA200 long-term trend")
        score += 10
    else:
        bear_signals.append("Price below EMA200 - weak uptrend")
        score -= 10
    
    # Price action
    if price_action.trend == "strong uptrend":
        bull_signals.append("Strong uptrend in price action")
        score += 15
    elif price_action.trend == "downtrend":
        bear_signals.append("Downtrend in price action")
        score -= 15
    
    # Volume confirmation
    if price_action.volume_confirmation == "bullish confirmation":
        bull_signals.append("Volume confirms move")
        score += 10
    
    # Smart money indicators
    if smart_money.order_flow == "buyer-dominant order flow":
        bull_signals.append("Buyer-dominant order flow")
        score += 10
    elif smart_money.order_flow == "seller-dominant order flow":
        bear_signals.append("Seller-dominant order flow")
        score -= 10
    
    # VWAP
    if smart_money.vwap_relation == "above VWAP":
        bull_signals.append("Above VWAP")
        score += 5
    elif smart_money.vwap_relation == "below VWAP":
        bear_signals.append("Below VWAP")
        score -= 5
    
    # Box theory
    if box.bias == "BUY WATCH":
        bull_signals.append("Box bottom bullish setup")
        score += 5
    
    # Clamp score to 0-100
    confidence = max(0, min(100, score))
    
    return {
        "confidence": confidence,
        "bull_signals": bull_signals,
        "bear_signals": bear_signals,
        "signal_type": "BUY" if confidence > 60 else "SELL" if confidence < 40 else "HOLD"
    }


async def analyze_symbol_realtime(symbol: str, seed: int, capital: float, risk_pct: float) -> dict:
    """
    Real-time analysis for a single symbol
    Minimal latency, maximum accuracy
    """
    try:
        # Generate/fetch current OHLCV
        df = generate_sample_ohlcv(seed=seed)
        df = apply_ema_swing_breakout_strategy(add_indicators(df))
        
        df_clean = df.dropna(subset=["Close", "EMA9", "EMA21", "ATR14", "RecentSwingHigh", "RecentSwingLow"])
        if df_clean.empty:
            return None
        
        last = df_clean.iloc[-1]
        
        # Run all analysis in parallel-like fashion
        decision = ema_swing_breakout_decision(symbol, df)
        price_action = analyze_price_action(df)
        smart_money = analyze_smart_money(df)
        box = analyze_box_theory(df)
        situational = latest_situational_summary(df)
        
        # Build analysis row
        row = {
            "Ticker": symbol,
            "Close": float(last["Close"]),
            "Signal": decision.signal,
            "Confidence": decision.confidence,
            "EMA9": float(last["EMA9"]),
            "EMA21": float(last["EMA21"]),
            "EMA200": float(last["EMA200"]),
            "AboveEMA200": bool(last["Close"] > last["EMA200"]),
            "ATR14": float(last["ATR14"]),
            "RSI14": float(last["RSI14"]),
            "SwingHigh": float(last["RecentSwingHigh"]),
            "SwingLow": float(last["RecentSwingLow"]),
            "TrendStructure": price_action.trend,
            "Support": float(price_action.support) if isinstance(price_action.support, (int, float)) else 0.0,
            "Resistance": float(price_action.resistance) if isinstance(price_action.resistance, (int, float)) else 0.0,
            "VolumeConfirmation": price_action.volume_confirmation,
            "OrderFlow": smart_money.order_flow,
            "VWAPRelation": smart_money.vwap_relation,
            "BoxBias": box.bias,
        }
        
        # Calculate signal strength
        signal_data = calculate_signal_strength(decision, price_action, smart_money, box, row)
        row.update(signal_data)
        
        # Build trade plan if signal is active
        if decision.signal == "BUY":
            initial_stop = max(float(last["RecentSwingLow"]), float(last["Close"] - 1.5 * last["ATR14"]))
            plan = build_trade_plan_from_stop(
                symbol, decision.signal, float(last["Close"]), initial_stop,
                RiskConfig(capital=capital, risk_per_trade_pct=risk_pct)
            )
            row["plan"] = {
                "entry": plan.entry,
                "stop": plan.stop,
                "target": plan.target,
                "shares": plan.shares,
                "risk": plan.risk_amount,
                "reward": plan.reward_amount,
                "rr": round(plan.reward_amount / plan.risk_amount, 2) if plan.risk_amount > 0 else 0
            }
        elif decision.signal == "SELL":
            initial_stop = min(float(last["RecentSwingHigh"]), float(last["Close"] + 1.5 * last["ATR14"]))
            plan = build_trade_plan_from_stop(
                symbol, decision.signal, float(last["Close"]), initial_stop,
                RiskConfig(capital=capital, risk_per_trade_pct=risk_pct)
            )
            row["plan"] = {
                "entry": plan.entry,
                "stop": plan.stop,
                "target": plan.target,
                "shares": plan.shares,
                "risk": plan.risk_amount,
                "reward": plan.reward_amount,
                "rr": round(plan.reward_amount / plan.risk_amount, 2) if plan.risk_amount > 0 else 0
            }
        
        row["timestamp"] = datetime.now().isoformat()
        return row
    
    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {str(e)}")
        return None


async def broadcast_update(message: dict):
    """Broadcast update to all connected clients"""
    if not clients:
        return
    
    message_json = json.dumps(message, default=str)
    dead_clients = set()
    
    for client in clients:
        try:
            await client.send(message_json)
        except Exception as e:
            logger.warning(f"Client disconnection: {e}")
            dead_clients.add(client)
    
    # Remove dead clients
    for client in dead_clients:
        clients.discard(client)


async def analysis_loop(symbols: list, capital: float, risk_pct: float):
    """Continuously analyze symbols and broadcast updates"""
    seeds = {symbol: hash(symbol) % 10000 for symbol in symbols}
    
    while True:
        try:
            logger.info(f"Analyzing {len(symbols)} symbols...")
            
            results = []
            for symbol in symbols:
                result = await analyze_symbol_realtime(symbol, seeds[symbol], capital, risk_pct)
                if result:
                    results.append(result)
            
            # Filter active trades only
            active = [r for r in results if r.get("signal_type") in ["BUY", "SELL"] and r.get("confidence", 0) >= 60]
            
            # Broadcast update
            await broadcast_update({
                "type": "analysis_update",
                "timestamp": datetime.now().isoformat(),
                "total_analyzed": len(results),
                "active_trades": len(active),
                "trades": active,
                "all_results": results  # Include all for reference
            })
            
            # Wait before next update
            await asyncio.sleep(analysis_update_interval)
        
        except Exception as e:
            logger.error(f"Analysis loop error: {e}")
            await asyncio.sleep(5)


async def handler(websocket, path):
    """Handle WebSocket client connection"""
    clients.add(websocket)
    logger.info(f"Client connected. Total clients: {len(clients)}")
    
    try:
        # Welcome message
        await websocket.send(json.dumps({
            "type": "welcome",
            "message": "Connected to Real-Time Trading Analysis Server",
            "timestamp": datetime.now().isoformat()
        }))
        
        # Keep connection alive and receive commands
        async for message in websocket:
            try:
                data = json.loads(message)
                command = data.get("command", "ping")
                
                if command == "ping":
                    await websocket.send(json.dumps({
                        "type": "pong",
                        "timestamp": datetime.now().isoformat()
                    }))
                
                elif command == "subscribe":
                    symbols = data.get("symbols", [])
                    capital = data.get("capital", 20000)
                    risk_pct = data.get("risk_pct", 0.02)
                    
                    await websocket.send(json.dumps({
                        "type": "subscribed",
                        "symbols": symbols,
                        "timestamp": datetime.now().isoformat()
                    }))
                
                else:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": f"Unknown command: {command}"
                    }))
            
            except json.JSONDecodeError:
                await websocket.send(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON format"
                }))
    
    except websockets.exceptions.ConnectionClosed:
        logger.info("Client disconnected")
    
    finally:
        clients.discard(websocket)
        logger.info(f"Client removed. Total clients: {len(clients)}")


async def main():
    """Start WebSocket server with analysis loop"""
    host = "0.0.0.0"
    port = 8765
    
    # Default symbols
    symbols = ["RELIANCE", "HDFCBANK", "INFY", "TATAMOTORS", "JSWSTEEL"]
    capital = 20000.0
    risk_pct = 0.02
    
    logger.info(f"Starting Real-Time WebSocket Server on ws://{host}:{port}")
    logger.info(f"Monitoring symbols: {', '.join(symbols)}")
    logger.info(f"Capital: ₹{capital:,.0f} | Risk per trade: {risk_pct*100}%")
    
    # Start WebSocket server
    async with serve(handler, host, port):
        logger.info("✅ WebSocket server running. Waiting for clients...")
        
        # Start analysis loop
        try:
            await analysis_loop(symbols, capital, risk_pct)
        except KeyboardInterrupt:
            logger.info("Shutting down...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
