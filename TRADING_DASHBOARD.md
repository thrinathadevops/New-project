# Real-Time Trading Dashboard - Complete Guide

## 🎯 Overview

The **Real-Time Trading Dashboard** is a new, fast, zero-latency platform for live trading signal generation and execution. Unlike the main dashboard which provides comprehensive analysis, this dashboard focuses on **active trades only** with unified BUY/SELL recommendations.

### Key Features

✅ **Live Real-Time Analysis** - No latency, instant signal updates  
✅ **Combined Analysis Engine** - Merges ALL analysis techniques into unified signals  
✅ **Active Trades Only** - Shows only BUY/SELL opportunities, not HOLD  
✅ **Zero Latency Execution** - WebSocket-based streaming for instant updates  
✅ **Unified Confidence Score** - Single metric combining all methods  
✅ **Complete Trade Plans** - Entry, Stop, Target, Risk/Reward for each trade  
✅ **Smart Money Integration** - Real-time order flow, FVG, VWAP analysis  
✅ **Separate Interface** - Independent dashboard, doesn't interfere with main app  

---

## 📊 Analysis Techniques Combined

The dashboard combines these 8 proven analysis methods:

| Technique | Weight | Purpose |
|-----------|--------|---------|
| **EMA 9/21 Breakout** | 25% | Momentum entry signals |
| **Price Action** | 25% | Trend, support/resistance, volume confirmation |
| **Smart Money** | 20% | Order flow, FVG, liquidity sweeps, VWAP |
| **Box Theory** | 10% | Previous day frame references |
| **EMA 200 Filter** | 5% | Long-term trend protection |
| **Volume Confirmation** | 10% | Trade strength validation |
| **VWAP Relation** | 5% | Institutional positioning |

**Result:** Single **Unified Confidence Score (0-100)** for each trade opportunity

---

## 🚀 Quick Start

### Option 1: Use Dashboard Launcher (Recommended)

```powershell
# From project root
python launcher.py
```

Then select option **2️⃣ Trading Dashboard**

### Option 2: Direct Run

```powershell
# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Run trading dashboard directly
streamlit run trading_dashboard.py
```

Opens at: `http://localhost:8501`

### Option 3: Real-Time WebSocket Server

For zero-latency streaming to external systems:

```powershell
# Install WebSocket support (one-time)
pip install websockets

# Start server
python realtime_server.py
```

Opens WebSocket at: `ws://localhost:8765`

---

## 📋 Dashboard Layout

### 1. **Live Configuration Sidebar**

- **Monitor Symbols**: Add NSE ticker symbols (comma-separated)
- **Capital**: Your trading capital (₹)
- **Risk % per Trade**: Risk allocation percentage
- **Yahoo Finance Toggle**: Use live data vs sample data
- **Signal Thresholds**: Minimum confidence to show trades
- **Refresh Rate**: How often to update analysis (10s, 30s, 60s, manual)

### 2. **Key Metrics Row**

```
🎯 Active Trades: 3        📈 BUY: 2         📉 SELL: 1
⚡ Avg Confidence: 75%     💰 Total Risk: ₹45,000
```

### 3. **Active Trade Opportunities Table**

Shows all trades matching current filters:

| Column | Meaning |
|--------|---------|
| **Symbol** | Stock ticker |
| **Action** | 🟢 BUY or 🔴 SELL |
| **Price** | Current market price |
| **Confidence** | Combined score (0-100%) |
| **Entry** | Recommended entry price |
| **Stop** | Stop loss level |
| **Target** | Profit target |
| **Risk** | Total risk in rupees |
| **Reward** | Total reward potential |
| **R:R** | Risk-to-Reward ratio |
| **Shares** | Position size |

### 4. **Trade Details & Analysis**

Select any trade to see:

- **Current Metrics**: Price, ATR, RSI, EMA values
- **Signal Details**: Confidence, setup type, reasons
- **Trade Plan**: Entry, stop, target, position size
- **Analysis Breakdown**:
  - EMA Strategy Analysis
  - Price Action Analysis
  - Smart Money Analysis
  - Box Theory Analysis
- **Combined Decision Reasoning**: Why this signal is generated
- **Positives/Warnings**: Supporting and conflicting signals

### 5. **Quick Actions**

- 📈 Enter BUY Trade
- 📉 Enter SELL Trade
- ⏸️ Skip This Trade

---

## 🔧 Configuration Guide

### Signal Sensitivity

**Min Confidence (0-100)**
- `30-50`: Show all signals (high sensitivity, more trades)
- `50-70`: Balanced (recommended)
- `70-100`: High quality only (fewer, higher-probability trades)

### High Quality Filter

Toggle to show only top-tier setups:
- Enables: Combined score ≥ 70%
- Shows confidence levels ≥ 75%
- Filters out weak signals

### Refresh Frequency

- **Every 10s**: Aggressive monitoring (high CPU)
- **Every 30s**: Balanced (recommended)
- **Every 60s**: Conservative (low bandwidth)
- **Manual**: Click refresh when needed

### Capital & Risk Configuration

```
Capital: ₹20,000          → Your bankroll
Risk per trade: 2.0%      → ₹400 per trade
```

The dashboard auto-calculates:
- Position size (shares)
- Risk amount in rupees
- Target profit based on R:R ratio

---

## 💡 Understanding Signals

### BUY Signal (🟢)

Appears when:
- ✅ EMA9 above EMA21 + swing-high breakout
- ✅ Price above EMA200 (long-term trend)
- ✅ Strong uptrend in price action
- ✅ Volume confirmation
- ✅ Buyer-dominant order flow
- ✅ Above VWAP

### SELL Signal (🔴)

Appears when:
- ❌ EMA9 below EMA21 (exit signal)
- ❌ Strong downtrend detected
- ❌ Price rejected at resistance
- ❌ Seller-dominant order flow
- ❌ Below VWAP
- ❌ Bearish smart money setup

### HOLD (No Signal)

- No breakout active
- Mixed signals from different techniques
- Insufficient confirmation
- Price in neutral zone

---

## 📈 Trade Plan Explanation

Every active trade includes a complete **Risk Plan**:

```
Entry: ₹2,000.00          → Current price for new entry
Stop: ₹1,850.00           → Stop loss (if wrong, exit here)
Target: ₹2,300.00         → Profit target
Risk: ₹150 per share × 10 = ₹1,500
Reward: ₹300 per share × 10 = ₹3,000
Ratio: 1:2 (Risk:Reward)
Shares: 10
```

**How it's calculated:**
- Risk per share = Entry - Stop
- Target distance = Risk × Reward/Risk ratio (typically 2:1)
- Shares = Capital × Risk% ÷ Risk per share
- Total risk = Shares × Risk per share

---

## 🔌 WebSocket Server (Zero-Latency Mode)

For real-time streaming to trading platforms or external systems:

### Starting the Server

```powershell
# First install WebSocket library
pip install websockets

# Run server
python realtime_server.py
```

Output:
```
INFO - Starting Real-Time WebSocket Server on ws://0.0.0.0:8765
INFO - Monitoring symbols: RELIANCE, HDFCBANK, INFY, TATAMOTORS, JSWSTEEL
INFO - Capital: ₹20,000.00 | Risk per trade: 2%
INFO - ✅ WebSocket server running. Waiting for clients...
```

### Connecting a Client

```python
import asyncio
import json
import websockets

async def connect():
    uri = "ws://localhost:8765"
    async with websockets.connect(uri) as websocket:
        # Subscribe to trades
        await websocket.send(json.dumps({
            "command": "subscribe",
            "symbols": ["RELIANCE", "INFY"],
            "capital": 20000,
            "risk_pct": 0.02
        }))
        
        # Receive updates
        while True:
            msg = await websocket.recv()
            data = json.loads(msg)
            
            if data["type"] == "analysis_update":
                print(f"Active Trades: {data['active_trades']}")
                for trade in data['trades']:
                    print(f"  {trade['Ticker']}: {trade['signal_type']} @ {trade['confidence']}%")

asyncio.run(connect())
```

### Server Response Format

```json
{
  "type": "analysis_update",
  "timestamp": "2026-04-13T14:30:15.123456",
  "total_analyzed": 5,
  "active_trades": 2,
  "trades": [
    {
      "Ticker": "INFY",
      "Close": 2100.50,
      "Signal": "BUY",
      "confidence": 82,
      "bull_signals": ["EMA9 above EMA21", "Strong uptrend", "Above VWAP"],
      "signal_type": "BUY",
      "plan": {
        "entry": 2100.50,
        "stop": 1950.00,
        "target": 2400.00,
        "shares": 5,
        "risk": 750.00,
        "reward": 1500.00,
        "rr": 2.0
      }
    }
  ]
}
```

---

## 🎯 Common Use Cases

### 1. **Active Day Trader** 
- Set confidence to 50%
- Refresh every 10s
- Monitor 5-10 symbols
- Use Dashboard + WebSocket for alerts

### 2. **Swing Trader**
- Set confidence to 70%+
- Refresh every 60s
- Monitor 10-20 symbols
- Use Dashboard during market hours

### 3. **Risk-Averse Trader**
- High Quality Filter: ON
- Set confidence to 80%+
- Refresh every 60s
- Wait for perfect setups only

### 4. **Algorithm Integration**
- Use WebSocket Server
- Parse JSON feeds
- Auto-execute trades
- Monitor via Dashboard

---

## ⚠️ Important Warnings

🚨 **This is decision-support only, not guaranteed profit generation**

- Validate all signals with paper trading first
- Use demo/paper account before real money
- Market gaps and limit moves can exceed stops
- Past performance doesn't guarantee future results
- Always check fundamentals before entry
- Manage position size carefully
- Never trade above your risk tolerance

---

## 🔄 Comparison: Main vs Trading Dashboard

| Feature | Main Dashboard | Trading Dashboard |
|---------|---|---|
| **Purpose** | Complete analysis | Only active trades |
| **Scope** | Full watchlist | BUY/SELL only |
| **Update Rate** | Per analysis | Real-time (10-60s) |
| **Output** | All signals | High-confidence only |
| **Focus** | Research/screening | Execution |
| **Latency** | Moderate | ⚡ Zero |
| **Analysis Depth** | Deep | Unified score |
| **Use Case** | Pre-market analysis | Live trading |

---

## 📊 Example Workflow

1. **Pre-Market Prep (8:00 AM)**
   - Open Main Dashboard
   - Run screening on fundamentals
   - Identify watchlist candidates

2. **Market Open (9:15 AM)**
   - Switch to Trading Dashboard
   - Adjust confidence threshold (60-70%)
   - Monitor active trades
   - Start WebSocket server if auto-trading

3. **During Market**
   - Watch for BUY signals (🟢)
   - Watch for SELL signals (🔴)
   - Execute when confirmed
   - Monitor position with Stop/Target levels

4. **Position Management**
   - Trailing stops when winning
   - Quick exit on stop hit
   - Record in journal
   - Review performance

5. **Post-Market**
   - Analyze closed trades
   - Update strategy notes
   - Review journal entries

---

## 🐛 Troubleshooting

**Dashboard not updating?**
- Check internet connection
- Verify symbols are valid NSE tickers
- Try clicking refresh manually
- Clear browser cache

**WebSocket connection fails?**
- Ensure websockets library is installed: `pip install websockets`
- Check port 8765 is not blocked by firewall
- Try connecting from same machine first

**Symbols not found?**
- Use exact NSE symbols (e.g., RELIANCE not RELIANCE.NS)
- Check symbol spelling
- Try popular symbols first

**Confidence scores too low?**
- Lower the min confidence threshold
- Toggle off "High Quality Only"
- Add more symbols for better signal generation

**High latency or slow updates?**
- Increase refresh interval to 60s
- Reduce number of symbols
- Use WebSocket server instead of Dashboard
- Check CPU/RAM usage

---

## 📚 Related Files

- `trading_dashboard.py` - Main Trading Dashboard app
- `realtime_server.py` - WebSocket server for zero-latency
- `launcher.py` - Dashboard selector
- `app.py` - Original Main Dashboard
- `serve.py` - HTTP server
- `intraday_advisor/` - Analysis engine modules

---

## 💬 Support & Updates

For issues or feature requests, check:
- Analysis engine in `intraday_advisor/` modules
- Main README for project overview
- Test files in `tests/` for usage examples

---

**Happy Trading! 📈**  
*Remember: Risk management, not prediction, makes profit.*
