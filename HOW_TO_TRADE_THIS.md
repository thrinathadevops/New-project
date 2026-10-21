# How to run a session on this desk

A checklist you can follow yourself, in the order the screen should be read.
Written after trading it live on 21 October 2026 — including the trade that
lost, because that is where most of the lessons are.

---

## Before anything: the number that decides everything

**At ₹20,000, one NIFTY lot costs about ₹11,000 — more than half your account.
A 20% stop on it risks about 11% of everything you have.**

No setting fixes this. A lot cannot be divided. The deploy slider cannot help:
if one lot costs more than the cap allows, you get "UNAFFORDABLE", not a
smaller position.

So before you look at a single indicator, know which game you are in:

| Capital | NIFTY options | Equity screener |
|---|---|---|
| ₹20,000 | ~11% risk per trade — too much | **0.5–0.6% risk** — shares divide |
| ₹115,000+ | ~2% risk — professional | also fine |

**At your current capital the equity table is the tradeable instrument and the
option chain is education.** That is not a limitation of the app; it is
arithmetic. Six losing option trades in a row halve the account. Six losing
equity trades cost about 3%.

---

## 1. Which way is the market leaning? (regime card)

Read these five, in this order. You are looking for **agreement**, not opinion.

- **Timeframes (5m / 15m / 1h / daily).** All four pointing the same way is the
  only strong signal here. The daily is the context the 5-minute moves inside,
  never the other way round.
- **Conviction %.** This is how much the inputs *agree*, not a probability of
  being right. 80%+ with all four timeframes aligned is a real trend. 50% means
  the inputs are arguing — stand aside.
- **India VIX, and its direction.** The level matters less than the change. A
  *rising* VIX accompanies falling markets far more often than rising ones.
- **Breadth (heavyweights above their EMA20).** 50% while the index falls means
  a narrow decline — a few big names dragging it. Narrow moves reverse.
- **Range position.** This is the one that cost me money. **It is where price
  sits in today's range, not how far through the session you are.** At 5% you
  are at the session low; at 95% the high.

### The trap I walked into

On 21 October the screen said: TRENDING_DOWN, **conviction 82%**, all four
timeframes down, VIX rising. Strong. It also said **range position 5% — "at the
bottom of the day's range: breakdown or exhaustion"**, after a 1.8% fall.

I bought the put. The index had already made its move; the remaining session
was the bounce. **A strong trend read tells you the direction. It tells you
nothing about whether the move is still available.** When those two disagree —
strong trend, exhausted range position — the trend is the past and the range
position is the present.

**Rule: do not buy a put at the bottom of the day's range or a call at the
top.** Wait for the pullback. If there is no pullback left in the session,
there is no trade.

---

## 2. Am I overpaying? (the IV chip — new)

On each signal card: `IV 14.8c / 15.2p · skew +0.4`

- First number is call implied volatility, second is put.
- **Skew = put IV − call IV.** Positive means puts are the dearer side.
- Compare the relevant one to **India VIX** on the regime card.

Measured live, same bearish view, same expiry:

| | Put IV | Call IV | Verdict |
|---|---|---|---|
| NIFTY | 15.2% | 14.8% | level, and below VIX 15.9 → **fair** |
| BANKNIFTY | **17.1%** | 15.0% | **2 points dearer** → you pay up for downside |

That single comparison moved me out of BANKNIFTY and into NIFTY. Two
volatility points is real money you never see on the premium ticket.

**Rule: when two indices give the same signal, buy the one with the cheaper
volatility.**

---

## 3. Is the chain defending a level? (the PIN chip — new)

`PIN 55000` appears when the call wall, the put wall **and** max pain all sit on
one strike. Both sides of the chain are holding that level, so price tends to
be pulled back to it rather than trend away.

Live example: BANKNIFTY had its largest call OI (1.32M) *and* largest put OI
(1.30M) at 55,000, with max pain also at 55,000 and spot 146 points below. A put
there needs price to break through the one level the whole chain is defending.

**Rule: do not take a directional trade that has to travel through a pin.**

---

## 4. Where do I take profit? (the walls on the chart)

The chart marks `PUT WALL` and `CALL WALL`. These are the strikes with the
heaviest open interest — where option writers defend.

- Falling toward the put wall → **that is your target, not a level to break.**
- The chart now says so directly: *"Room down to the 22500 PUT wall — that
  level is a sensible place to book."*

### The biggest single improvement available to you

The default target is **+25%** on the premium. It is a constant; it knows
nothing about where price can actually go.

On the live trade, spot was 22,705 and the put wall was 205 points below at
22,500. Repricing the option at the wall said roughly **+55%** was available
over the same distance.

Setting target to **55** instead of 25, with the stop unchanged:

```
net reward:risk   1.13  ->  2.55     at identical risk
```

Nothing was added to the trade. The default was simply asking for less than the
chart was offering.

**Rule: set the target from the wall, not from the default.** Read the wall off
the chart, work out roughly how far it is, and raise the target to match.

---

## 5. Sizing — read the slider label, it is now honest

`MAX DEPLOY — 100% · one stop costs 14.8% of capital — measured, not estimated`

This used to be arithmetic (`stop% × deploy%`) and was wrong both ways: it
promised 20% when the real figure was 11.5%, and promised 1% when the position
was actually unaffordable. It now reports what the **sized position** risks.

If it says "no contract fits ₹1,000 — cheapest lot is ₹11,144", that is the
indivisible-lot problem, not a bug.

---

## 6. Placing it

1. Pick the contract. The **alternatives table** under each card now respects
   your deploy cap, so what it shows is what you can actually buy.
2. Check the ticket on the chart: **BUY NIFTY 22750 PE · entry · stop · target
   · one stop costs X% · flat at ₹Y after costs**.
3. **The breakeven line is the one to internalise.** You are not flat at your
   entry. On the live trade entry was ₹162.75 and breakeven ₹164.60 — the
   premium had to rise ₹1.85 before I owned a single rupee. On one lot that gap
   is ₹122.
4. `+ Add position` → lots → premium.

---

## 7. Managing it — the part that actually decides your P&L

### Protect the trade once it pays for itself (new button)

When a position is up meaningfully, click **Protect**. It pulls the stop up to
**net breakeven** — entry *plus* the round-trip cost, not the entry itself.

A stop can only ever be **tightened**. Trying to widen one is refused outright,
not warned about. Widening a stop to avoid being taken out is how a planned
small loss becomes an unplanned large one, and it always feels justified at the
time.

### What happened to me, exactly

```
entry   ₹162.75
peak    ₹179.80   (+₹984, +10.5%)   <- Protect pulled the stop to ₹164.60
bounce  ₹154.95                      <- stop breached while I was not watching
exit    ₹158.50   net -₹395
```

The stop was correct. **I was not at the screen when it fired** — I had the
server restarting. Exiting at ₹164.60 would have been roughly flat; exiting at
₹158.50 cost ₹395.

**This is the most important sentence in this document: a stop that exists only
in a dashboard you are not watching is not a stop.** This app alarms you; it
does not place a stop-loss order with your broker. If you cannot watch the
screen, place a real SL order in the Angel app, or do not take the trade.

### Time

Intraday options are squared off at 15:15. A trade that has not worked by about
**14:45** usually will not — you are then paying time decay to be hopeful.

---

## 8. After the close

```bash
python ledger_cli.py grade      # score the day's signals
python ledger_cli.py report     # win rate with confidence intervals
```

Until roughly **20 graded trades** exist, the win rate means nothing. The
dashboard says so itself: *"1 closed trade — too few to call."* Do not raise
size because three went well.

---

## The session in one table

| | |
|---|---|
| Trade | NIFTY 22700 PE, 1 lot, entry ₹162.75 |
| Best point | +₹984 (+10.5%) |
| Exit | ₹158.50 |
| **Net** | **−₹395 (−2.0% of capital)** |
| Second trade | **declined** — identical setup to the one that stopped me out |

### Why I declined the second trade

After the stop-out the screen showed: PUT, score 47, TRENDING_DOWN 83%, range
position 10%, agent CAUTION. **Identical to the setup that had just failed**,
with no new evidence — only a smaller account.

Taking the same trade again because the last one lost is the fastest way to
turn −2% into −6%. **No new evidence, no new trade.**

---

## The five rules, if you remember nothing else

1. **At ₹20,000, trade the equity table.** Options risk 11% a go; shares risk 0.5%.
2. **Never buy a put at the day's low or a call at the day's high**, however
   strong the trend reads.
3. **Set the target from the wall on the chart, not the default 25%** — that
   alone took reward:risk from 1.13 to 2.55.
4. **Click Protect once the trade pays for itself.** Breakeven is entry *plus*
   costs.
5. **If you cannot watch the screen, place a real stop with your broker.** The
   dashboard alarms; it does not execute.
