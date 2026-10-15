# The Options Desk

One screen. One scoring engine. Everything the system knows, visible.

```bash
python dashboard_server.py                      # http://localhost:8700
python dashboard_server.py --capital 5000 --max-fraction 0.3
```

Settings change live from the control row — no restart.

## What replaced what

Six surfaces became one. Four competing scorers became one.

| Was | Now |
|---|---|
| `app.py` + its `confluence_score` | retired → [legacy/](legacy/) |
| `trading_dashboard.py` + its `calculate_combined_score` | retired |
| `options_dashboard.py`, `options_scanner_dashboard.py` | retired |
| `serve.py`, `launcher.py` | retired |
| ad-hoc `confidence` maths inside `index_options.py` | folded into `scoring.py` as weighted components |

`intraday_advisor/engine.py` produces one snapshot; the browser only renders it.
A second view cannot reintroduce a second opinion, because **no view performs
analysis**.

## Reading the screen

**Signal cards** — direction badge, confluence score ring, premium plan, and the
risk strip. The risk strip is the part worth staring at:

```
‼ SEVERE   21 LOTS   20.6% RISK   1.15 NET R:R   4 LOSSES TO HALVE
```

That reads: 21 lots, one stop-out costs 20.6% of your capital, after all costs
you make ₹1.15 for every ₹1 risked, and four such losses in a row halve the
account. `SAFE` / `CAUTION` / `SEVERE` is set from the risk percentage, and the
band always carries an icon and a word — never colour alone.

**"Why this score — full breakdown"** opens the component bars: every signed
contribution that produced the number, the raw reasons, and the data source. A
score with no breakdown is an unexplained number, which is exactly what the four
old scorers each produced.

**Chart** — candles, VWAP, EMA 9, EMA 21, with a crosshair tooltip and a
`Table view` toggle so no value is reachable only by hovering. A **forming
candle is drawn outlined and pulsing**: shown, so the chart feels live, but
never scored, because signals come from closed candles only. A legend entry
dims when that series has no data (index candles from Yahoo often carry no
volume, so VWAP is absent).

**Alerts** appear bottom-right — new signal, direction flip, score move, target
hit, stop hit, square-off countdown, estimated-data warning. Stop hits and flips
stay until dismissed; the rest clear themselves. The 🔔 button adds a sound.
Alerts sit bottom-right so they never cover the session P&L.

**Positions** persist in the database, so they survive a refresh, a restart and
a second browser. A position at its stop pulses red and says `EXIT NOW`. P&L is
**net of costs** — the old screen showed gross premium movement, which
overstates every small winner.

## Colour

The previous dashboard had white text on `#3987e5`, which measures **3.64:1** —
below the 4.5:1 body text needs. That is why words were hard to read. White on
the warning yellow was **1.83:1**, effectively invisible.

The rule now: dark ink on mid-tone fills, white only on deep ones. Every pairing
was computed. The three chart series passed a colour-vision-deficiency check in
both themes (worst adjacent pair ΔE 9.4 deutan, ≥8 target). Light and dark are
separately chosen step sets, not an inversion. `prefers-reduced-motion` disables
every animation — each one only reinforces something also written as text.

## Backtesting

```python
from intraday_advisor.backtest import compare_fill_assumptions, run_backtest
```

`run_backtest` now resolves exits through `intraday_advisor/fills.py` — the
**same function the live grader calls**, not a parallel copy. Stops test against
the bar's Low, targets against its High, same-bar conflicts resolve as the stop,
and gaps fill at the open.

`compare_fill_assumptions` reproduces the old behaviour alongside the honest one.
On synthetic data the old convention overstated returns by up to **23 percentage
points** (42.6% vs 19.5% on one seed). Real option premiums spike harder than a
random walk, so the live gap is wider.

**There is no full historical options backtest, and there cannot be one.** The
strategy needs the option chain — PCR, max pain, OI walls — as it stood at each
decision point, and historical chain snapshots are not retrievable after the
fact. Open interest at 10:35 last Tuesday is simply gone. Reconstructing it
would mean inventing the main input and then calibrating on the invention. So:

- `run_backtest` — historical, underlying price strategy, real data.
- `backtest_from_ledger` — replays signals the system actually emitted, through
  sequential capital, modelling indivisible lots and compounding. A **forward
  test**: it grows one session at a time.
- `ledger_walk_forward` — out-of-sample folds, so weights are never fitted and
  judged on the same rows.

## Bugs found while building this

Worth knowing, because each was invisible until something checked:

- **`add_indicators` was not idempotent** — `.join()` raised on an
  already-enriched frame. Any double-enrich crashed the analysis.
- **Python emits bare `NaN`**, which `JSON.parse` rejects. Every snapshot
  containing one early candle was silently dropped and the dashboard sat on
  "CONNECTING" with no error anywhere. `float(x) or 0` does **not** guard
  against it — NaN is truthy in Python.
- **Duplicate `Content-Length`** from mutating the websockets response object;
  curl tolerated it, Chrome refused the page outright.
- **BANKNIFTY lot size 15** in `options_scanner.py` — stale since Nov 2024; it
  is 35. Correct values are in `ledger/sizing.py:LOT_SIZES`.

## Honest limits

The dashboard shows what the system believes and what it costs. It does not
know whether the system is right — that is what the ledger measures, and it
needs roughly 30 graded trades before any number means anything. See
[LEDGER.md](LEDGER.md).

Estimated data (market closed, or broker not connected) is never actionable, is
flagged on every card, and is recorded as synthetic so it can never enter a
performance statistic.
