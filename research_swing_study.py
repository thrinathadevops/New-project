"""Does a contrarian reading of the score work on daily equity bars?

The intraday study said the 5-minute index mean-reverts and the trend-following
score is therefore backwards, but the edge (0.1%) was below the cost of taking
it (0.19-0.50%). Two things change on daily equity bars:

  * the horizon is longer, and the edge grew with horizon;
  * equities are divisible and cost 0.19% round trip, not 0.50%.

Method, deliberately strict:
  * 5 years of daily bars for the Nifty 50, so ~1,200 observations per name.
  * Score each bar with the live scorer -- real volume here, so VWAP/MFI/OBV
    actually contribute, unlike on the index.
  * CROSS-SECTIONAL: each day, rank the universe by score and take the extreme
    quintiles. This removes market beta: if everything rose, a long book rising
    is not an edge.
  * Measure EXCESS return against the equal-weight universe, which is what
    "alpha" means and what a long-only retail book is actually competing with.
  * Chronological 70/30 split; the test window is untouched until the rule is
    fixed.
"""
from __future__ import annotations

import logging
import sys

import numpy as np
import pandas as pd

logging.disable(logging.WARNING)

from intraday_advisor.data import fetch_yahoo_ohlcv
from intraday_advisor.indicators import add_indicators
from intraday_advisor.nse_watchlist import get_scan_universe
from intraday_advisor.scoring import ScoreWeights, score_signal
from intraday_advisor.smart_money import analyze_smart_money
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy
from intraday_advisor.validation import autocorrelation_note

HORIZONS = (1, 3, 5, 10, 20)
EQUITY_COST_PCT = 0.19


def score_one(symbol: str, period: str = "5y") -> pd.DataFrame | None:
    try:
        frame = fetch_yahoo_ohlcv(symbol, period=period, interval="1d")
    except Exception:
        return None
    if len(frame) < 400:
        return None
    enriched = apply_ema_swing_breakout_strategy(add_indicators(frame))
    enriched = enriched.dropna(subset=["Close", "EMA9", "EMA21", "ATR14"])
    if len(enriched) < 300:
        return None

    weights = ScoreWeights.load()
    vwap_relation = None
    try:
        vwap_relation = analyze_smart_money(enriched).vwap_relation
    except Exception:
        pass

    scores = []
    for record in enriched.to_dict("records"):
        clean = {}
        for key, value in record.items():
            if isinstance(value, (bool, np.bool_)):
                clean[key] = bool(value)
            elif isinstance(value, (int, np.integer)):
                clean[key] = int(value)
            elif isinstance(value, (float, np.floating)):
                clean[key] = None if pd.isna(value) else float(value)
            else:
                clean[key] = value
        scores.append(score_signal(
            clean, decision_signal=clean.get("Signal") or None,
            vwap_relation=vwap_relation, weights=weights,
        ).score)

    out = pd.DataFrame({"close": enriched["Close"].astype(float).values,
                        "score": scores}, index=enriched.index)
    out["symbol"] = symbol
    for horizon in HORIZONS:
        out[f"fwd{horizon}"] = (
            out["close"].shift(-horizon) / out["close"] - 1.0
        ) * 100.0
    return out


def main() -> None:
    universe = get_scan_universe("nifty50")
    print(f"Scoring {len(universe)} names, 5 years of daily bars each...")
    panels, failed = [], []
    for symbol in universe:
        panel = score_one(symbol)
        if panel is None:
            failed.append(symbol)
        else:
            panels.append(panel)
    print(f"  scored {len(panels)}, failed {len(failed)}"
          + (f" ({', '.join(failed)})" if failed else ""))
    if len(panels) < 20:
        print("Not enough names to form a cross-section.")
        return

    panel = pd.concat(panels)
    panel.index.name = "date"
    panel = panel.reset_index()

    # --- character of daily equity returns --------------------------------- #
    print("\nDaily autocorrelation, per name (is the reversion still there?):")
    reverting = trending = neither = 0
    for symbol in [p["symbol"].iloc[0] for p in panels[:50]]:
        rows = panel[panel.symbol == symbol].set_index("date")
        note = autocorrelation_note(rows.rename(columns={"close": "Close"}))
        if note["character"] == "mean-reverting":
            reverting += 1
        elif note["character"] == "trending":
            trending += 1
        else:
            neither += 1
    print(f"  mean-reverting {reverting} | trending {trending} | neither {neither}"
          f"  (of {len(panels)})")

    # --- cross-sectional quintiles, excess of the equal-weight universe ----- #
    print("\nCross-sectional test. Each day: rank by score, take the extremes.")
    print("Returns are EXCESS of the equal-weight universe that day (= alpha).\n")

    dates = sorted(panel["date"].unique())
    split_date = dates[int(len(dates) * 0.70)]
    print(f"  train {dates[0].date()}..{split_date.date()}   "
          f"test {split_date.date()}..{dates[-1].date()}\n")

    results = {}
    for horizon in HORIZONS:
        column = f"fwd{horizon}"
        usable = panel.dropna(subset=[column, "score"])
        if usable.empty:
            continue
        # Daily universe mean = the benchmark a long book must beat.
        bench = usable.groupby("date")[column].transform("mean")
        usable = usable.assign(excess=usable[column] - bench)
        # Rank within each day; 0 = lowest score.
        usable = usable.assign(
            rank=usable.groupby("date")["score"].rank(pct=True, method="first")
        )
        low = usable[usable["rank"] <= 0.20]       # most "bearish" score
        high = usable[usable["rank"] >= 0.80]      # most "bullish" score
        results[horizon] = (low, high, split_date)

        def stats(chunk, name):
            values = chunk["excess"].dropna()
            if len(values) < 50:
                return f"{name}: n={len(values)} too few"
            se = values.std(ddof=1) / np.sqrt(len(values))
            t = values.mean() / se if se else 0.0
            return (f"{name}: n={len(values):6,}  excess {values.mean():+.4f}%  "
                    f"win {(values > 0).mean() * 100:5.1f}%  t={t:+6.2f}")

        print(f"  --- {horizon}-day hold ---")
        print("    " + stats(low[low.date <= split_date], "TRAIN low-score  (contrarian long) "))
        print("    " + stats(high[high.date <= split_date], "TRAIN high-score (momentum  long) "))
        print("    " + stats(low[low.date > split_date], "TEST  low-score  (contrarian long) "))
        print("    " + stats(high[high.date > split_date], "TEST  high-score (momentum  long) "))

    # --- the cost gate on the best test result ----------------------------- #
    print("\nCost gate (equity round trip 0.19%):")
    for horizon, (low, high, split) in results.items():
        for label, chunk in (("contrarian", low), ("momentum", high)):
            values = chunk[chunk.date > split]["excess"].dropna()
            if len(values) < 50:
                continue
            net = values.mean() - EQUITY_COST_PCT
            if net > 0:
                print(f"  {horizon:2}d {label:11} excess {values.mean():+.4f}% "
                      f"- 0.19% = {net:+.4f}%  CLEARS COSTS")


if __name__ == "__main__":
    main()
