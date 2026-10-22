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
