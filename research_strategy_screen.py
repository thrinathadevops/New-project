"""Run the whole strategy catalogue and print what survived.

    python research_strategy_screen.py              # both indices
    python research_strategy_screen.py RELIANCE.NS  # a constituent

Why this script is the deliverable rather than the strategies
-------------------------------------------------------------
Asked what the app was missing, the tempting answer is more strategies. The
app had exactly one family -- *buy an option because a trend score is high* --
so that answer is not wrong, it is just not sufficient. ``scoring.py`` already
holds 37 weights over 32 price components, and on the repaired ledger
(7 sessions, 524 graded trades) ``indicator_scorecard`` finds the trusted list
**empty**: every component changes sign somewhere, and ``focused_score`` -- the
score rebuilt from the three that looked best -- was falsified out of sample.
Weighting those components differently is not the missing piece.

So the catalogue adds the missing families (volatility, premium, relative
value, market structure, flow, event) as **separately measurable objects**, and
this script measures all of them through one evaluator: same train/test split,
same HAC standard error, same non-overlapping subsamples, same block bootstrap,
same cost gate on the instrument each one actually trades.

Measured 2026-11-06 on 58 days of 5-minute bars
-----------------------------------------------
NIFTY, with BANKNIFTY supplied as the pair leg::

    strategy                      instrument       n    mean%   t_iid   t_HAC
    opening_range_breakout_15m    option_1lot    895   0.0529    4.19    1.58
    opening_range_fade            option_1lot    895  -0.0529   -4.19   -1.58
    trend_with_strength_filter    option_1lot    792   0.0333    2.37    0.88
    index_pair_ratio_reversion    futures        224   0.0109    0.35    0.11
    ema_cross_9_21                option_1lot   1292  -0.0096   -0.84   -0.33

RELIANCE.NS, where volume is real so the profile strategies run::

    poc_reversion                 equity         996   0.2251    4.91    1.32
    value_area_fade               equity         789   0.1871    3.28    0.88

**Nothing passed. Nothing came close.** Three things in that table are worth
more than a pass would have been.

**1. The harness proves itself.** ``opening_range_breakout_15m`` and
``opening_range_fade`` are exact negations, and their means are exactly equal
and opposite (+0.0529 / -0.0529, t +4.19 / -4.19). A harness that found both
profitable would be double-counting, and every other number here would be
worthless. That pair is in the catalogue permanently as a self-check, not for
its thesis.

**2. The overlap correction keeps doing the work.** Four strategies clear
t=2 on the naive statistic and none survives the HAC version -- ORB goes
4.19 -> 1.58, ``poc_reversion`` 4.91 -> 1.32. This is the same correction that
withdrew this project's headline BANKNIFTY finding (i.i.d. t +3.90 -> HAC
+1.83), and on simulated pure noise the naive statistic declares significance
**53%** of the time. Every one of these would have been written up as a
discovery by the arithmetic this project used in October.

**3. Market structure looked like the answer and was not.** ``poc_reversion``
and ``value_area_fade`` have the largest raw means in the whole catalogue
(+0.23% and +0.19%, comfortably above the 0.19% equity round trip) and the
highest naive t-statistics. They also **flip sign between the training and
holdout windows**, which means a rule fitted on the first half would have
traded the second half backwards. That is the exact signature of the 20-day
contrarian cell that nearly shipped in October: holdout t=+2.61, train
t=-2.03. Regime dependence, not an effect.

What the remaining statuses mean
--------------------------------
``missing inputs`` is honest bookkeeping, not failure: the flow, premium and
event strategies need the option chain, an IV column or an expiry flag, which
a bare candle frame does not carry. The tests supply those columns and the
signals fire correctly, so the logic is measured even where this script's data
is not enough. Running them properly needs the chain stored per bar, which is
what ``observe.py`` was built to do -- the sampling is in place and the history
is not deep enough yet.

``no positions taken`` on an index for the three volume strategies is correct
and worth keeping visible: ``^NSEI`` and ``^NSEBANK`` report no volume, so
``volume_profile.build_profile`` refuses rather than profiling a column of
zeros. That refusal is the feature.

The standing conclusion, unchanged
----------------------------------
**No rule discovered in this project clears its own transaction costs.** A
catalogue does not change that arithmetic -- a 0.1% edge against a 0.19-1.25%
round trip loses whatever it is called. What the catalogue changes is that the
failures are now *legible*: "not significant" is a dead end, while "loses to
costs" is a sizing and instrument problem, and the two were previously
indistinguishable.
"""

from __future__ import annotations

import logging
import sys

logging.disable(logging.WARNING)

from intraday_advisor.data import fetch_yahoo_ohlcv
from intraday_advisor.strategies import describe_catalogue
from intraday_advisor.strategies.harness import report, screen_all

PERIOD = "58d"
INTERVAL = "5m"


def show_catalogue() -> None:
    print("=" * 92)
    print("THE CATALOGUE")
    print("=" * 92)
    family = None
    for row in describe_catalogue():
        if row["family"] != family:
            family = row["family"]
            print(f"\n[{family}]")
        needs = f"  needs {row['requires']}" if row["requires"] else ""
        print(f"  {row['name']:32} {row['instrument']:14} "
              f"{row['horizon']:>3} bars{needs}")
    print()


def screen(symbol: str, label: str, pair_symbol: str | None = None) -> None:
    frame = fetch_yahoo_ohlcv(symbol, period=PERIOD, interval=INTERVAL)
    pair = (
        fetch_yahoo_ohlcv(pair_symbol, period=PERIOD, interval=INTERVAL)
        if pair_symbol else None
    )
    print(f"\n{label}: {len(frame):,} bars "
          f"{frame.index[0].date()} to {frame.index[-1].date()}")
    if "Volume" in frame:
        total = float(frame["Volume"].fillna(0).sum())
        print(f"  volume over the window: {total:,.0f}"
              + ("  (no volume -- profile strategies will stand aside)"
                 if total <= 0 else ""))
    print(report(screen_all(frame, pair=pair)))


if __name__ == "__main__":
    show_catalogue()

    if len(sys.argv) > 1:
        for symbol in sys.argv[1:]:
            try:
                screen(symbol, symbol)
            except Exception as exc:  # noqa: BLE001
                print(f"{symbol}: failed -- {exc}")
    else:
        try:
            screen("^NSEI", "NIFTY", pair_symbol="^NSEBANK")
        except Exception as exc:  # noqa: BLE001
            print(f"NIFTY: failed -- {exc}")
        try:
            screen("^NSEBANK", "BANKNIFTY", pair_symbol="^NSEI")
        except Exception as exc:  # noqa: BLE001
            print(f"BANKNIFTY: failed -- {exc}")
        try:
            screen("RELIANCE.NS", "RELIANCE (real volume)")
        except Exception as exc:  # noqa: BLE001
            print(f"RELIANCE: failed -- {exc}")

    print("""
HOW TO READ A FAILURE -- they are not interchangeable

  not significant          No effect to trade. Nothing fixes this.
  loses to costs           The effect is real and too small FOR THAT
                           INSTRUMENT. The same signal can fail on a one-lot
                           option (0.50-1.25% round trip) and pass on futures
                           (0.03%). A sizing problem, not a dead end.
  regime-dependent         Sign flips between train and holdout: a rule fitted
                           on the first window trades the second backwards.
  insufficient sample      The honest "do not know". An overlapping study
                           reaches this far sooner than its bar count suggests
                           -- 455 observations at a 24-bar hold carry about 19
                           independent ones.
  missing inputs           Needs the chain, an IV column or an expiry flag.
                           Bookkeeping, not a verdict.
  no positions taken       The thresholds were never reached, or the inputs are
                           absent -- as volume is on an index.

A PASS EARNS A FORWARD TEST, NOT CAPITAL. The holdout is still a backtest, and
this catalogue was searched to find it -- which is why the significance bar
rises with the number of strategies run.
""")
