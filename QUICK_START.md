# 🚀 Quick Start Guide - 5 Minutes to Live Trading

Get the Real-Time Trading Dashboard running in 5 minutes!

---

## 1. Setup (2 minutes)

### Option A: Windows PowerShell (Recommended)

```powershell
# Open PowerShell in project folder

# Create virtual environment
python -m venv .venv

# Activate it
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Optional: For WebSocket real-time (zero latency)
pip install websockets
```

### Option B: Direct (Already have venv?)

```powershell
# Skip to activation
.\.venv\Scripts\Activate.ps1
```

---

## 2. Launch Dashboard (1 minute)

### Easiest Way: Use Launcher

```powershell
python launcher.py
```

Select: **2️⃣ Trading Dashboard (Real-Time)**

### Direct Way:

```powershell
streamlit run trading_dashboard.py
```

→ Opens at **http://localhost:8501**

---

## 3. Configure (1 minute)

Once dashboard loads:

**Left Sidebar - Settings:**

```
NSE Symbols: RELIANCE,INFY,HDFCBANK
Capital: 20000 (your money)
Risk/Trade: 2%
Refresh: Every 30s
Min Confidence: 60%
Yahoo Finance: OFF (sample data)
```

Then click **🔄 Refresh Now**

---

## 4. Find Trades (1 minute)

Dashboard shows:

```
🎯 Active Trades: 3        📈 BUY Signals: 2        📉 SELL Signals: 1
⚡ Avg Confidence: 75%     💰 Total Risk: ₹2,500
```

**Active Trade Table** appears with:
- Symbol, Action (BUY/SELL), Price
- Confidence %, Entry, Stop, Target
- Risk/Reward ratio, Position size

### Select a Trade

Click **highest confidence** trade from table

---

## 5. Execute (0 minutes)

Dashboard shows everything you need:

```
Entry Price: ₹2,100.50
Stop Loss: ₹1,950.00  ← Set THIS in broker FIRST
Target: ₹2,400.00     ← Set THIS SECOND
Position: 5 shares
Risk: ₹750
Reward: ₹1,500
```

**In your broker (Angel One / Zerodha):**

1. ✅ Set SELL stop-loss at ₹1,950 (first!)
2. ✅ Set SELL target at ₹2,400 (second!)
3. ✅ Set BUY at market or ₹2,100 (execute!)

---

## ✨ Key Features You Get

✅ **Real-Time** - Updates every 30 seconds  
✅ **Zero Latency** - WebSocket server available (see Step 6)  
✅ **Combined Analysis** - All 8 techniques merged into 1 score  
✅ **Complete Trade Plans** - Entry, stop, target calculated  
✅ **Unified Signals** - 🟢 BUY or 🔴 SELL only (no confusion)  
✅ **Risk Management** - Auto-calculates position size  

---

## 6️⃣ Bonus: Zero-Latency WebSocket Server

For truly live trading integrations:

```powershell
python realtime_server.py
```

→ WebSocket at **ws://localhost:8765**

Gets JSON with every trade signal:
```json
{
  "trades": [
    {
      "Ticker": "INFY",
      "signal_type": "BUY",
      "confidence": 82,
      "entry": 2100.50,
      "stop": 1950.00,
      "target": 2400.00
    }
  ]
}
```

---

## 📊 Dashboard Layout

```
HEADER
├─ Title: Real-Time Trading Dashboard
├─ Metrics: Active Trades | BUY | SELL | Confidence | Risk
│
MAIN AREA
├─ Active Trade Opportunities (table)
│  └─ Click row to see details
│
├─ Trade Details Section
│  ├─ Current price, confidence
│  ├─ Combined Analysis Breakdown
│  │  ├─ EMA Strategy
│  │  ├─ Price Action
│  │  ├─ Smart Money
│  │  └─ Box Theory
│  ├─ Entry/Stop/Target
│  └─ Reasoning
│
├─ Quick Actions
│  ├─ Enter BUY Trade
│  ├─ Enter SELL Trade
│  └─ Skip This Trade
│
└─ Status: Last update time, latency
```

---

## 🎯 First Trade Checklist

Before clicking BUY in broker:

- [ ] Confidence ≥ 60%?
- [ ] Entry, Stop, Target prices confirmed?
- [ ] Position size fits your capital?
- [ ] All 4 greens (EMA, Price, Smart Money, Box)?
- [ ] Set STOP first, then TARGET, then ENTRY

---

## ⚠️ First-Time Tips

1. **Start with small size** - 1-2 shares for first trade
2. **Monitor closely** - Watch first 5 minutes after entry
3. **Use only top signals** - Confidence 70%+ for practice
4. **Set stops immediately** - Don't trade without stop loss
5. **Log every trade** - Note what worked and what didn't

---

## 🆘 Something Not Working?

### Dashboard won't start?
```powershell
pip install streamlit plotly pandas
streamlit run trading_dashboard.py
```

### No signals appearing?
- Lower "Min Confidence" threshold
- Check symbols are valid (RELIANCE, INFY, etc.)
- Click refresh button
- Wait 30 seconds for update

### Want live data instead of sample?
- In sidebar, toggle **Yahoo Finance** ON
- Requires internet connection
- ~2-5 seconds slower

### Want zero-latency real-time?
```powershell
pip install websockets
python realtime_server.py
```

---

## 📚 Next Steps

### Learn More:
- **TRADING_DASHBOARD.md** - Full feature guide
- **EXECUTION_GUIDE.md** - Step-by-step trading instructions
- **README.md** - Full project overview

### Main Dashboard (if needed):
```powershell
streamlit run app.py
```
→ Complete analysis, screening, backtesting

### Go Live:
1. Paper trade for 1 week minimum
2. Test on small capital first ($100-500)
3. Only then scale to real amount

---

## 🎓 What the Analysis Combines

Dashboard merges 8 proven techniques:

| # | Technique | What It Does |
|---|-----------|-------------|
| 1 | **EMA 9/21** | Detects momentum shifts |
| 2 | **Price Action** | Reads buyer/seller psychology |
| 3 | **Smart Money** | Tracks institutional flow |
| 4 | **Box Theory** | Uses previous day frame |
| 5 | **EMA 200 Filter** | Protects long-term trend |
| 6 | **Volume** | Confirms trade strength |
| 7 | **VWAP** | Shows institutional pricing |
| 8 | **Situational** | Weekday pattern recognition |

→ **Result:** Single confidence score (0-100%)

High score = All techniques aligned = High probability

---

## 💡 Example Trade

**Real scenario:**

```
Dashboard shows:
├─ INFY at ₹2,100.50
├─ 🟢 BUY Signal
├─ Confidence: 82%
│
Details:
├─ EMA9 (2,105) > EMA21 (2,090) ✅
├─ TrendStructure: strong uptrend ✅
├─ OrderFlow: buyer-dominant ✅
├─ VolumeConfirmation: bullish ✅
├─ Above EMA200 ✅
│
Trade Plan:
├─ Entry: ₹2,100.50
├─ Stop: ₹1,950.00 (your risk: ₹150/share)
├─ Target: ₹2,400.00 (your reward: ₹300/share)
├─ Position: 5 shares
├─ Risk: ₹750 (3.75% of ₹20K)
└─ R:R: 2:1

✅ Action: BUY 5 shares at market
```

---

## 📊 Real-Time Monitoring

Once trade is live:

```
10:15 - Entry filled at ₹2,100.50 ✅
10:16 - Price: ₹2,105.00 (profit +₹22.50) 📈
10:18 - Price: ₹2,110.00 (profit +₹48) 📈
10:20 - Target hit at ₹2,400? → SELL ✅

Result: +₹1,500 profit on ₹750 risk
Actual R:R: 2:1 (as planned) ✅
```

---

## 🚀 Pro Tips

**Day Trading:**
- Use 5-15 min candles
- Refresh every 10s
- Target 2-5 trades/day
- Close before 3:30 PM

**Safety:**
- Always set stop loss FIRST
- Never move stops against trade
- Risk only 1-2% per trade
- 3 losses = stop trading that day

**Scaling:**
- Start with 1-2 shares
- Increase only after 5 winning days
- Never over-risk on any single trade

---

## 📞 Common Questions

**Q: How often to refresh?**  
A: Every 30s is balanced. Every 10s if aggressive. Every 60s if conservative.

**Q: What confidence to use?**  
A: Start with 70%+. Lower to 60% after practice.

**Q: How many trades per day?**  
A: 3-5 best trades. Avoid over-trading.

**Q: Can I trade pre-market/after-hours?**  
A: Market hours only: 9:15 AM - 3:30 PM IST

**Q: Should I hold overnight?**  
A: No, especially as beginner. Close before 3:30 PM.

**Q: WebSocket vs Dashboard?**  
A: Dashboard for learning, WebSocket for automation.

---

## 🎯 Success Measurement

Track daily:
```
Date: 2026-04-13
Total Trades: 3
Winners: 2  (66% win rate)
Losers: 1
Profit: +₹1,200
Daily Return: 6%
Avg Confidence: 74%

Status: ✅ ON TRACK (target is 50%+ win rate, 1-2% daily)
```

---

## 🔐 Risk Disclosure

⚠️ **Important:** This is decision-support only, not guaranteed profit.

- Markets are unpredictable
- Past performance ≠ future results
- Small gaps/limits can exceed stops
- Always use demo/paper first
- Trade responsibly with money you can afford to lose

---

## 🎉 You're Ready!

**Summary:**
1. ✅ Dashboard installed and running
2. ✅ Settings configured
3. ✅ Symbols loaded
4. ✅ Trade signals displayed
5. ✅ Ready to execute!

**Next:**
- Paper trade today
- Record all trades
- Review performance
- Repeat tomorrow

---

**Questions?** See EXECUTION_GUIDE.md or TRADING_DASHBOARD.md for full documentation.

**Happy Trading!** 📈

*Remember: Best traders risk manage first, profits come second.*

