# Trading Dashboard Execution Guide

## 🎯 How to Execute Trades Using the Real-Time Trading Dashboard

This guide walks you through using the Trading Dashboard to identify and execute trades with **zero latency** and **unified analysis**.

---

## 📖 Table of Contents

1. [Pre-Market Setup](#pre-market-setup)
2. [Live Trading Session](#live-trading-session)
3. [Signal Identification](#signal-identification)
4. [Trade Execution](#trade-execution)
5. [Position Management](#position-management)
6. [Post-Market Review](#post-market-review)

---

## 🌅 Pre-Market Setup

### 1. Start the Dashboard

**First time setup:**
```powershell
# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Run launcher
python launcher.py

# Select option 2️⃣
```

Or directly:
```powershell
streamlit run trading_dashboard.py
```

Opens at: **http://localhost:8501**

### 2. Configure Your Settings

**⚙️ Sidebar Configuration:**

| Setting | Recommended | Purpose |
|---------|------|---------|
| **NSE Symbols** | 5-10 stocks | Start with liquid, known stocks |
| **Capital** | Your actual amount | e.g., ₹20,000 |
| **Risk %/trade** | 1-2% | e.g., 2% = ₹400/trade |
| **Yahoo Finance** | OFF for testing | ON for live data |
| **Period** | 30d | Enough history for indicators |
| **Candle** | 15m or 5m | Intraday timeframe |
| **Min Confidence** | 50-70% | 50=aggressive, 70=conservative |
| **High Quality Only** | Toggle based on risk | ON for safer trades |
| **Refresh** | Every 30s | Balance speed vs CPU |

### 3. Understand the Dashboard Metrics

After refresh, you see 5 key metrics:

```
🎯 Active Trades: 2        📈 BUY: 1         📉 SELL: 1
⚡ Avg Confidence: 72%     💰 Total Risk: ₹2,500
```

**What it means:**
- **Active Trades**: Number of BUY/SELL signals generated
- **BUY Signals**: Count of bullish opportunities
- **SELL Signals**: Count of bearish/exit opportunities
- **Avg Confidence**: Average combined score (higher = better)
- **Total Risk**: Sum of all planned position risks

---

## 🕐 Live Trading Session

### Market Hours (9:15 AM - 3:30 PM)

**9:15 AM - Market Open**
```
✅ Set refresh to "Every 10s"
✅ Monitor symbols for first signals
✅ Avoid trading first 5 minutes (avoid gap noise)
```

**9:30 AM - 2:00 PM (Peak Trading)**
```
✅ Main trading window
✅ Most liquid and established trends
✅ Highest confidence signals
✅ Best time for new entries
```

**2:00 PM - 3:30 PM (Late Session)**
```
✅ Closing positions or taking profits
✅ Reduced trading due to lower volume
✅ Avoid new entries close to market close
```

---

## 🔍 Signal Identification

### Understanding the Active Trades Table

```
| Symbol | Action  | Price   | Confidence | Entry   | Stop    | Target  | Risk    | Reward  | R:R  | Shares |
|--------|---------|---------|------------|---------|---------|---------|---------|---------|------|--------|
| INFY   | 🟢 BUY  | ₹2100.5 | 82%        | ₹2100.5 | ₹1950   | ₹2400   | ₹750    | ₹1500   | 2.0  | 5      |
| RELI   | 🔴 SELL | ₹2850.2 | 65%        | ₹2850.2 | ₹2950   | ₹2650   | ₹500    | ₹1000   | 2.0  | 2      |
```

### Signal Quality Indicators

| Confidence | Recommendation | Example |
|------------|---|---|
| **90-100%** | STRONG - Execute immediately | Perfect alignment of all signals |
| **75-89%** | GOOD - Safe to execute | 6-7 signals aligned |
| **60-74%** | MODERATE - Good setup | 5 signals aligned |
| **50-59%** | WEAK - Wait for confirmation | Mixed signals |
| **<50%** | POOR - Skip this trade | Better opportunities elsewhere |

### BUY Signal Checklist ✅

Before executing a BUY trade, verify:

- [ ] Confidence ≥ 60% (or your threshold)
- [ ] EMA9 > EMA21 (momentum positive)
- [ ] Price > EMA200 (long-term trend intact)
- [ ] TrendStructure = "uptrend" or "strong uptrend"
- [ ] OrderFlow = "buyer-dominant"
- [ ] VolumeConfirmation = "bullish confirmation"
- [ ] VWAPRelation = "above VWAP"
- [ ] Entry price near recent swing high
- [ ] Risk:Reward ≥ 1.5:1
- [ ] Position size fits your capital

### SELL Signal Checklist ✅

Before executing a SELL trade, verify:

- [ ] Confidence ≥ 60%
- [ ] EMA9 < EMA21 (momentum negative)
- [ ] TrendStructure = "downtrend" or "strong downtrend"
- [ ] Price shows rejection at resistance
- [ ] OrderFlow = "seller-dominant"
- [ ] VolumeConfirmation shows seller pressure
- [ ] VWAPRelation = "below VWAP"
- [ ] Stop above recent swing high
- [ ] Risk:Reward ≥ 1.5:1

---

## ⚡ Trade Execution

### Step 1: Select a Trade

From the **Active Trade Opportunities** table, click a trade that meets your checklist.

The detailed view loads showing:

```
Symbol: INFY
Current Price: ₹2,100.50
Confidence: 82%

Entry: ₹2,100.50
Stop: ₹1,950.00
Target: ₹2,400.00
Shares: 5
Risk: ₹750.00
Reward: ₹1,500.00
```

### Step 2: Review Combined Analysis

Scroll down to see **Combined Analysis Breakdown**:

**EMA Strategy Analysis:**
- Signal: BUY ✅
- EMA9: 2,105.20 (above EMA21)
- EMA21: 2,090.30
- Recent swing high: 2,098.50 (price breaking above)

**Price Action Analysis:**
- Trend: strong uptrend ✅
- Support: 2,050.00
- Resistance: 2,150.00
- Candle: bullish engulfing (buyer control) ✅
- Volume: bullish confirmation ✅

**Smart Money Analysis:**
- Order Flow: buyer-dominant ✅
- VWAP: 2,087.50 (price above) ✅
- FVG: bullish ✅

**Box Theory:**
- Price in lower half of box
- Wick signal: None
- Setup: Normal entry

**Green flags:** ✅ ✅ ✅ ✅ (4/4 major techniques aligned)

### Step 3: Verify Risk Management

Check the trade plan:
```
Capital: ₹20,000
Risk per trade: 2% = ₹400 planned

Actual Plan:
Entry: ₹2,100.50
Stop: ₹1,950.00 (₹150.50 risk per share)
Position size: 5 shares
Total risk: ₹750 (actually 3.75% of capital!)

⚠️ WARNING: Exceeds planned 2% risk!
✅ ACTION: Reduce to 2-3 shares for ₹300-450 risk
```

### Step 4: Record Entry Details

**In your trading journal:**
```
Date: 2026-04-13
Time: 10:15 AM
Symbol: INFY
Type: BUY
Entry: ₹2,100.50
Stop: ₹1,950.00
Target: ₹2,400.00
Quantity: 3 shares
Risk: ₹450.00
Reward: ₹900.00
Setup: EMA9/EMA21 swing breakout + price action + smart money alignment
Confidence: 82%
Notes: Strong bullish sequence, buyer-dominant order flow, above all EMAs
```

### Step 5: Execute Trade

**Before clicking BUY in broker platform:**

1. ✅ Verify entry price is still valid (check current price in dashboard)
2. ✅ Set stop loss order FIRST (prevents losses if internet drops)
3. ✅ Set target order SECOND (locks in profits)
4. ✅ Only then set BUY order

**Broker platform example (Angel One/Zerodha):**

```
ORDER 1 (STOP LOSS) - Set first
Type: SELL
Quantity: 3 shares
Stop Price: ₹1,950.00
Trigger: ₹1,950.00
Status: PENDING

ORDER 2 (TARGET) - Set second
Type: SELL
Quantity: 3 shares  
Limit Price: ₹2,400.00
Status: PENDING

ORDER 3 (ENTRY) - Set last
Type: BUY
Quantity: 3 shares
Price: MARKET (for quick entry) or ₹2,100.50 (limit)
Status: SUBMIT
```

---

## 📊 Position Management

### After Trade Entry

**First 30 minutes:**
- Monitor price close to entry
- If trades 2% against you → consider exiting early
- If trades 2% in your favor → move stop to breakeven

**Profit management:**
```
Reached 25% of target (₹2,175)?
  → Move stop to entry (lock breakeven)

Reached 50% of target (₹2,250)?
  → Take partial profit (sell 1-2 shares)
  → Move stop to first profit level

Reached 75% of target (₹2,325)?
  → Take another partial profit
  → Hold remaining to target

Reached target (₹2,400)?
  → Close entire position
  → Record final P&L
```

### Stop Loss Management

**NEVER move stops against your trade plan:**

❌ **WRONG:**
- Entry: ₹2,100 at BUY
- Stop: ₹1,950
- Price drops to ₹1,960 and you set new stop at ₹1,920
- LOSS: Full stop hit, larger loss than planned

✅ **CORRECT:**
- Entry: ₹2,100 at BUY
- Stop: ₹1,950 (stick to plan)
- Price drops to ₹1,960, stop hits
- LOSS: ₹150/share = planned risk

### Trailing Stops (Advanced)

Once trade hits 50% profit:

```
Entry: ₹2,100
Target: ₹2,400 (50% = ₹2,250)

Price reaches ₹2,250 - Set trailing stop ₹50 below high
- If price goes to ₹2,270, stop at ₹2,220
- If price goes to ₹2,290, stop at ₹2,240 (protect profits)
- If price comes back to ₹2,240, close at trailing stop
```

---

## 📈 Post-Market Review

### 3:30 PM - After Market Close

**Close all holdings:**
```
16:00 - No open positions should carry overnight
       (unless explicit swing setup)
```

**Record results:**
```
TRADE LOG ENTRY:

Date: 2026-04-13
Symbol: INFY
Entry: 10:15 AM at ₹2,100.50
Exit: 2:45 PM at ₹2,380.00
Profit: (₹2,380 - ₹2,100.50) × 3 = ₹840

Risk taken: ₹450 (2.25% of capital)
Profit realized: ₹840 (4.2% of capital)
R:R achieved: 1.87:1
Status: WIN ✅

Analysis Quality: 82% confidence
Setup Quality: Perfect alignment (4/4 techniques)
Volume at entry: Good confirmation
Breakout strength: Strong

Lessons:
- Setup was perfect
- Best to exit after first target
- Could have held for full target but exits early
- Add to winners list
```

### Daily Statistics

Calculate post-market:
```
Today's Summary:
- Trades taken: 3
- Winners: 2
- Losers: 1
- Win rate: 66.7%
- Total profit: +₹1,200
- Daily return: 6% (on ₹20K capital)
- Capital: ₹21,200 (up from ₹20,000)

Best trade: INFY +840 (82% confidence, perfect setup)
Worst trade: TATAMOTORS -200 (45% confidence, weak signal)

Learnings:
✅ Took high-confidence trades → All winners
❌ Traded low-confidence trade → Loss
Action: Raise confidence threshold to 60% minimum
```

### Weekly/Monthly Review

```
Every Friday:
1. Review all trades from the week
2. Identify patterns (best times, best setups)
3. Update strategy notes
4. Calculate cumulative P&L
5. Adjust thresholds/settings for next week

Every month:
1. Calculate monthly return %
2. Review all losses
3. Update trading journal with learnings
4. Update risk management rules
5. Report back-test results vs live results
```

---

## 🚀 Pro-Level Techniques

### Multiple Time-Frame Confirmation

Before entering:
1. Check 15-min chart (your trading timeframe) - see BUY signal
2. Check 60-min chart - verify trend is still up
3. Check 4-hour chart - confirm bigger picture is bullish
4. If all aligned → HIGH CONFIDENCE trade
5. If 15-min bullish but 4-hour bearish → SKIP or take smaller size

### Pattern Recognition

Track recurring signals:
```
9:30-9:45 AM: Morning breakout trades
- Best confidence: 70%+
- Avoid gaps first 5 minutes
- Quality: Excellent

11:00 AM-12:30 PM: Mid-day consolidation
- Best confidence: 60%+
- Quality: Moderate, mix of setups

1:30-2:15 PM: Afternoon breakout
- Best confidence: 65%+
- Lower volume than morning
- Quality: Good

2:15 PM-3:30 PM: End of day
- Best confidence: 50%+ only
- Low volume, wide spreads
- Avoid new entries
```

### Volume-Based Filters

Enhance signals with volume analysis:
```
Breakout with volume spike (2x average)?
  → HIGH QUALITY signal (confidence +10%)

Breakout on weak volume (0.5x average)?
  → WARNING (confidence -10%)

Use this to adjust position size:
- High-volume breakout: Full position
- Normal volume: 75% position
- Low volume: Skip or 50% position
```

### Time-of-Day Optimization

```
Best Trading Hours (ranked):
1. 9:15-10:30 AM (Morning session - highest volume)
   Target: BUY breakouts
   Expected win rate: 60%+

2. 10:30-12:00 PM (Mid-morning - still active)
   Target: Mixed signals
   Expected win rate: 50-55%

3. 12:00-1:30 PM (Lunch - reduced volume)
   Target: Conservative entries only
   Expected win rate: 45%+

4. 1:30-3:00 PM (Afternoon - rebuilding volume)
   Target: BUY breakouts again
   Expected win rate: 55%

5. 3:00-3:30 PM (Closing - close positions)
   Target: Exit only, no new entry
   Expected win rate: NA
```

---

## ⚠️ Common Mistakes to Avoid

### ❌ Mistake #1: Ignoring Risk Management
**Problem:** "Just this once" trades with oversized positions
**Solution:** Always calculate position size = Capital × Risk% ÷ Risk per share

### ❌ Mistake #2: Moving Stops
**Problem:** Stop gets hit at -2%, but you move it to -5%
**Solution:** Never modify stops after entry. If setup is wrong, just exit.

### ❌ Mistake #3: FOMO Entry
**Problem:** Trade missed, so you enter late at bad price
**Solution:** Wait for next signal. Perfect trades come daily.

### ❌ Mistake #4: Ignoring Confidence Score
**Problem:** Trading 40% confidence signal
**Solution:** Wait for 60%+ confidence signals only.

### ❌ Mistake #5: Trading Against Trend
**Problem:** Taking SELL signal when price is above EMA200
**Solution:** Conservative rule: Only BUY when above EMA200, only SELL when below.

### ❌ Mistake #6: Revenge Trading
**Problem:** Lost trade, immediately takes risky trade to win back
**Solution:** After loss, wait 1 hour before next trade.

### ❌ Mistake #7: Over-Trading
**Problem:** 20 trades per day, low win rate
**Solution:** Take 3-5 best setups per day only.

---

## 📞 Quick Reference Card

### Trading Checklist

Before EVERY trade:
```
□ Confidence ≥ 60%?
□ All 4 checks aligned (EMA, Price, Smart Money, Box)?
□ Position size calculated correctly?
□ Stop loss set first in broker?
□ Risk is ≤ 2% of capital?
□ Risk:Reward ≥ 1.5:1?
□ Recorded in journal?
```

### Exit Rules

```
AUTOMATIC EXITS:
1. Stop loss hit → AUTO EXIT (no thinking)
2. Target reached → Take profit (or partial)
3. Market close (3:30 PM) → Close all

MANUAL EXIT SIGNALS:
- EMA9 crossed below EMA21 on hourly → EXIT (for BUY)
- Price breaks below support → EXIT
- Daily loss limit hit → STOP TRADING TODAY
```

### Daily Checklist

```
✅ Pre-market (8:30-9:15 AM)
   □ Dashboard open and running
   □ Symbols configured
   □ Risk settings verified
   □ Stop/Target plan ready

✅ During Market (9:15 AM - 3:30 PM)
   □ Monitor signals every 30sec
   □ Execute high-confidence trades only
   □ Manage positions actively
   □ Record in journal

✅ Post-market (3:30 PM+)
   □ Close all open positions
   □ Record daily results
   □ Update statistics
   □ Review lessons learned
```

---

## 🎓 Learning Path

### Week 1: Mastery
- Use only high-quality filters (confidence 70%+)
- Take only 1-2 trades per day
- Focus on understanding each signal
- Build intuition for setup

### Week 2: Scaling
- Lower to 60% confidence
- Increase to 3-5 trades
- Multiple symbols simultaneously
- Faster decision-making

### Week 3: Optimization
- Find best trade times
- Optimize position sizing
- Track win rates by time/symbol
- Refine your rules

### Week 4: Automation
- Consider WebSocket + bots
- Auto-update stop/target
- Risk management triggers
- Performance monitoring

---

## 📊 Success Metrics

To know if you're on track:

```
✅ GOOD PROGRESS:
- Win rate: 50%+
- Avg winning trade: 2x average loser
- Daily return: +1-2% of capital
- Trades per day: 3-5 (not too many)
- Confidence scores: Mostly 65%+

⚠️ NEEDS WORK:
- Win rate: <50%
- Avg loss: Same size as avg win
- Daily return: Highly variable
- Trades per day: 10+ (too many)
- Taking 40% confidence trades

❌ RED FLAGS:
- Negative daily return
- Win rate <35%
- Over-trading (15+ trades/day)
- Ignoring stop losses
- Revenge trading
```

---

**Remember:** *The best trade is the one you don't take. Only execute high-confidence, well-planned setups.*

Happy Trading! 📈

