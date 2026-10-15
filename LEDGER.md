# Signal Ledger — the feedback loop

The system could always generate signals. It could never tell you whether they
worked. This closes that gap: every signal is recorded, every outcome is graded
against the option's real premium path after real costs, and the result is
reported with confidence intervals instead of a number that flatters you.

## Daily loop

```bash
# 1. During the session — the dashboard records automatically.
streamlit run options_dashboard.py          # "Record signals" toggle, sidebar

#    Or headless, safe to cron every 5 minutes (deduped per candle):
python ledger_cli.py record --capital 20000

# 2. After 15:30 — grade the day against real option candles.
python ledger_cli.py grade

# 3. Read the result.
python ledger_cli.py report
python ledger_cli.py bands                  # does a higher score pay?
```

Grading runs **after the close**, not live, because the premium path is only
final once the session ends. Grading against unfinished bars is how look-ahead
gets in.

## What to expect, honestly

| Graded trades | What you can conclude |
|---|---|
| < 30 | Nothing. `report` will say so. |
| 30–200 | Whether expectancy is clearly negative (useful — stop early). |
| 200+, 20+ sessions | Enough to attempt weight calibration. |

A 60% win rate over 20 trades is consistent with a true rate of 36%–80%. The
report always prints the interval, because a point estimate with no interval is
what convinces people to scale a strategy that never worked.

## Supporting commands

```bash
python ledger_cli.py costs 100                      # cost drag at a premium
python ledger_cli.py plan --capital 5000 --premium 100 --index NIFTY
python ledger_cli.py fill --qty 75 --entry 100 --exit 110 --cost 190
python ledger_cli.py demo                           # end-to-end proof, fake data
```

`fill` is worth doing three times with real contract notes. It fits the
spread term to **your** actual execution, so graded P&L stops being an
assumption. Until then the model uses conservative defaults.

## Grading rules (and what each prevents)

1. **Only bars strictly after the signal bar.** The close that produced the
   signal is unobtainable by the time you know the signal exists.
2. **Entry fills at the next bar's open.** `fill_slippage` records the gap
   between the premium on screen and one actually achievable. Persistently
   positive means the screen quotes prices you cannot get.
3. **Stops tested against bar Low, targets against High — never Close.**
   `backtest.py` compares only `Close`, so an intrabar spike through the stop
   that closes back above it records *no loss at all*. Every such stop-out is
   invisible there.
4. **Stop wins when one bar touches both levels.** Bar data cannot say which
   came first; assuming the target is a free win-rate boost. Counted in
   `same_bar_conflict`.
5. **Gaps fill at the open.** A bar opening below your stop fills at the open,
   which is what your broker gives you.
6. **All P&L net of real F&O costs.** Gross P&L at retail size is meaningless.
7. **Open at 15:15 is a TIMEOUT, not a win.** Reported as its own category —
   a strategy whose wins are mostly timeouts has drift, not edge.
8. **Ungradeable is an outcome, not a deletion.** Dropping them silently is how
   a hit rate gets flattered by survivorship.

A win is **positive net P&L**, not "target touched". A +25% target on a ₹2
premium is a net loss once ₹20-per-order brokerage lands.

## Cost reality (NIFTY lot 75)

| Premium | Position | Round-trip cost | Move needed to break even |
|---|---|---|---|
| ₹10 | ₹750 | ₹56 | **+7.5%** |
| ₹20 | ₹1,500 | ₹58 | +3.8% |
| ₹100 | ₹7,500 | ₹99 | +1.3% |
| ₹400 | ₹30,000 | ₹252 | +0.8% |

Brokerage is flat per *order*, not per lot, so cheap contracts at small size
are the worst combination. Verify these rates against one contract note —
`python ledger_cli.py costs 100` prints the full breakdown to compare.

## Risk, when capital is whatever's available

With one indivisible lot, risk is what it is. The system reports it rather than
pretending otherwise:

- At 100% deployment a -20% premium stop is a **-20% account loss, regardless
  of account size** — more capital buys more lots, not less risk.
- The only lever is the "Max % of capital to deploy" slider.
- To risk 2% per trade on one NIFTY lot at a ₹100 premium you need roughly
  **₹7.5 lakh**. Below that, textbook risk is arithmetically impossible on
  index options — not a discipline problem.

Every signal card shows risk as a % of your capital, the net reward:risk after
costs, the breakeven premium, and how many consecutive stop-outs would halve
the account.

## Storage

`data/ledger.sqlite` (gitignored — it is your trading record).

- `signals` — immutable. What the engine believed, when. Never updated.
- `signal_outcomes` — written by the grader. Safe to delete and rebuild with
  `grade --regrade` after a cost-model or rule change.
- `cost_fills` — your real contract-note costs.
- `graded_signals` — the reporting view; excludes synthetic rows.

`engine_version` and `grader_version` stamp every row. When the scorer changes,
old signals are not pooled with new ones — a calibration fitted across a
scoring change is fitted to noise.

## What this does not do

It does not predict anything, and it cannot make a losing strategy profitable.
It can tell you — with an honest interval — whether what you already have makes
money after costs. If the answer is no, that is the most valuable output here,
and it arrives before the capital is gone rather than after.
