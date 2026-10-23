# Running a measurement day

One command each morning, one report whenever you want. Nothing to remember
during the day.

## Before 09:15

```bash
python paper_desk.py --capital 20000 --lots 1 --max-open 6 --interval 45
python dashboard_server.py --port 8765 --capital 20000 --refresh 15
```

The desk trades the session on paper, flattens before square-off, grades its
own ledger and writes `logs/paper_desk/<date>.log` and `<date>.summary.json`.
**One file per session, so days accumulate.** The dashboard records every
signal to the real ledger with the agent's stance and auto-grades at 15:45.

## Any time after the close

```bash
python ledger_cli.py sessions     # <- the one that matters
python ledger_cli.py coverage     # <- read BEFORE believing any hour number
python ledger_cli.py shape        # what kind of day was it
python ledger_cli.py report       # pooled all-time
python ledger_cli.py bands        # by score band, index, hour
```

## Why `sessions` is the one that matters

Measured over the first three sessions:

```
2026-10-16    34 signals   -Rs 1,222/signal    0% hit target
2026-10-21   111 signals   -Rs 1,262/signal    9% hit target
2026-10-22    92 signals     +Rs 478/signal   60% hit target
```

The pooled per-trade figure says "-Rs 580/lot, entire interval below zero",
which reads as settled and is not. Trades inside one day share that day's
regime, so they are not independent draws: pooling 237 of them and dividing by
the square root of 237 overstates the precision badly. Treating each **session**
as one observation, the interval is **-Rs 1,792 to +Rs 456** and straddles zero.

Same error as the overlapping-window t-statistic that was corrected earlier.
`sessions` is the unit that does not lie.

**You need about 34 sessions before the average is twice its own margin of
error.** You have 3. That is roughly seven trading weeks, and until then no
verdict from this data is worth acting on.

## What `--max-open 6` is for

On 2026-10-22 the strategy produced 92 signals. A desk holding 2 at a time took
18 of them, and *which* 18 was decided by when a slot happened to be free --
which has nothing to do with quality. The full signal set returned +Rs 478 per
signal that day; the 20% this desk harvested lost Rs 7,738.

So the desk now:

* **ranks signals by distance from neutral before taking one**, so capacity
  binds on the best available rather than the earliest;
* **counts the signals it had to pass over** while full, so the cost of the
  constraint is visible rather than invisible;
* **reports per-signal and per-account separately**, because they are different
  questions. Per-account is what a Rs 20,000 book makes. Per-signal is whether
  the strategy works. On 22 October those two numbers had opposite signs.

Raising `--max-open` samples more of the signal set and measures the strategy.
Keeping it at 2 measures your account size. Both are worth knowing; they are
not the same thing.

## What is still unanswered

The agent vetoed **100% of signals** on both recorded days, so there is no
PROCEED bucket to compare against and "do the vetoed trades lose?" remains
open. `LivePosition.notes` now carries the stance correctly (it silently
returned empty before, which is why the first run reported every trade as
UNKNOWN), so the breakdown will work as soon as the agent approves anything.

## Why `coverage` comes before any hour claim

With the hour buckets finally parsed correctly in IST, the pooled table showed
hour 9 at an **88.9% win rate and +Rs 1,630 per lot** against losses everywhere
else -- an apparent opening edge, and a tempting one.

All 18 of those trades were from 2026-10-22, the single positive session,
because the recorder was not running before 09:15 on the other two days:

```
            16 Oct   21 Oct   22 Oct
hour  9        -        -       18
hour 10        9        -       27
hour 12        -       36        4
hour 13        -       37        -
```

Each session sampled **different hours**, for the mundane reason that the server
was started at a different time each day. The consequence is not mundane: every
hour-versus-hour comparison is confounded with which day it was, and a pooled
table shows none of that. `coverage` reports how many distinct sessions
contributed to each hour, and an hour present in one session is an anecdote
about that session.

Right now only hours **11, 14 and 15** appear in all three sessions, and all
three are negative.

**This is the reason to start before 09:15 and run to the close every day.** Not
thoroughness for its own sake -- without consistent coverage the time-of-day
question cannot be asked at all.

## Why the market is now sampled every cycle

`observations` stores a row per index per cycle: spot, regime, conviction, VIX,
breadth, IV skew, PCR, max pain, the agent's stance, and open P&L. Roughly five
hundred rows a session.

It exists because after three sessions the one profitable day was
**unexplainable**. 2026-10-22 returned +Rs 478 per signal with a 60% target rate
while the others lost about Rs 1,200, and nothing stored said what was different
about it -- not the regime, not where volatility sat, not whether breadth
confirmed. A trade is a biased slice of a session: it says nothing about the
hours when nothing was taken, and those are most of the day.

`shape` reads it back per day and side by side. It is deliberately descriptive
and makes no judgement -- with three sessions and a between-session spread 1.5x
the mean, nothing can be fitted to it yet. Collect first.
