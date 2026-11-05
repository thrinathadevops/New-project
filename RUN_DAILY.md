# Running a measurement day

One command each morning, one report whenever you want. Nothing to remember
during the day.

## Before 09:15 — one command

```bash
python supervise.py --capital 20000 --max-open 6 --interval 45 --port 8765
```

That starts the desk and the dashboard and **keeps them recording**, which is
not the same as keeping them running. It restarts a child that dies, and -- the
part that matters -- a child that is alive and has stopped writing to the
ledger during market hours.

That second case is why it exists. Five sessions (26-30 October) were lost to
processes that were alive in the task list the whole time and recorded nothing;
4 November was lost from 11:51 onward the same way. A crash is loud. A stalled
loop looks exactly like a healthy one from outside, so liveness here is
measured by **output**, not by existence: no new signal for 10 minutes while the
market is open counts as dead. Progress and restarts go to
`logs/supervisor.log`.

Starting them by hand still works:

```bash
python paper_desk.py --capital 20000 --lots 1 --max-open 6 --interval 45
python dashboard_server.py --port 8765 --capital 20000 --refresh 15
```

The desk trades the session on paper, flattens before square-off, grades its
own ledger and writes `logs/paper_desk/<date>.log` and `<date>.summary.json`.
**One file per session, so days accumulate.** The dashboard records every
signal to the real ledger with the agent's stance and auto-grades at 15:45.

### The tick websocket is off by default

Both accept `--ticks`. Without it there is no live tick stream, and position
P&L updates once per refresh from the option chain instead of per tick.

That is a measured trade, not caution: a snapshot took **~100s with the tick
stream running and 23.4s without it**, because the SDK decodes about 3,400
frames a cycle in Python and that competes with the snapshot. Against a 15s
refresh the loop can never catch up, which is exactly how 4 November lost
everything after 11:51. Live tick pricing is worth having back, but it needs
the per-frame work moved off the stream thread first -- not just the flag.

## What is on the calendar

Checked automatically every session and shown to the agent, so there is nothing
to run. To see it yourself:

```bash
python -c "from intraday_advisor.event_calendar import events_for, describe; print(describe(events_for()))"
```

It knows two things: when an index heavyweight reports, and when RBI policy
lands. NIFTY is cap-weighted, so one heavyweight's result can move it
independently of anything on the chart — on 5 November it correctly flagged
Bharti Airtel reporting that day. Both **caution**, they do not veto: a
scheduled event is elevated two-way risk, not an unsound trade.

**The RBI dates are a static list in `event_calendar.py` and need updating once
a year.** When the list runs out the calendar says so loudly instead of
reporting "nothing scheduled", and a test fails to remind you — a calendar that
quietly stops checking is worse than no calendar.

## Before you take a trade

```bash
python ledger_cli.py odds --premium 194 --index NIFTY --bars-left 40
```

This is the only command that answers "what are the odds on **this** trade",
rather than "does this rule have an edge". It replays the rest of the session
10,000 times from the index's own last 60 days, re-runs the same stop and
target on those 60 real sessions, and compares the result against the win rate
the **costs** demand.

That last comparison is the point. Measured on a real NIFTY setup:

```
                         Rs 194 premium      Rs 12 premium
replay hit rate              47.4%               47.0%
needs to win after costs     46.8%               58.3%
edge                        +0.6 points        -11.4 points
verdict                   coin flip          ODDS AGAINST
```

**Same market, same direction, same stop and target percentages.** The only
difference is which contract you buy, and it turns a coin flip into a clear
loser — because Rs 55 of round-trip cost against a Rs 234 target is 23% of the
reward. A cheap option is not a cheap trade.

Pass `--u-stop` and `--u-target` with the levels off your chart. Without them
the underlying levels are derived from the premium percentages, which is crude
and the command says so: an option moves several times its underlying.

## The daily store

Automatic — it rides the grader's schedule, so there is nothing to run. After
15:45 the dashboard archives the day's candles and writes one consolidated
record per session. A day missed to a crash or a sleeping machine is caught up
on the next pass.

```bash
python ledger_cli.py archive      # run it by hand, and show coverage
python ledger_cli.py day          # every session, side by side
```

**Why candles are archived at all:** Yahoo serves roughly 60 days of 5-minute
history, so every day that passes destroyed a day permanently. Nothing kept
them. The first backfill captured **61,600 bars across 60 sessions for 14
symbols** — everything still recoverable. From here the archive grows while the
source slides, and `ledger_cli odds` draws on it rather than the 60-day window.

What the day record makes answerable, which nothing could before:

```bash
python ledger_cli.py agent        # would the vetoed trades have made money?
python ledger_cli.py spreads      # measured half-spread by hour vs the prior
python ledger_cli.py calibration  # is conviction a probability? do odds happen?
```

**The spread shape switches to your own book automatically** once an hour has
10 samples. It is automatic but not silent: every cost figure carries whether
it came from `measured` or `prior`, and the day an hour flips is logged and
written into that day's record, so a changed verdict is always traceable to a
date.

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
