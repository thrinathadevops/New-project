"""The experiment that decides whether profit is possible.

Takes every closed 5-minute bar of history we can get, scores it with the live
scorer, and measures what the index ACTUALLY did next. If high scores are
followed by rises and low scores by falls, there is an edge to trade. If the
forward return is flat across score bands, no dashboard, indicator or structure
can help -- the signal has nothing in it.

No lookahead: indicators are backward-looking, the score for bar i uses only
bar i, and the outcome is measured from the close of bar i forward.
"""
from __future__ import annotations

import logging
import sys

import numpy as np
import pandas as pd

logging.disable(logging.WARNING)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[0]))

from intraday_advisor.data import fetch_yahoo_ohlcv
from intraday_advisor.indicators import add_indicators
from intraday_advisor.scoring import ScoreWeights, score_signal
from intraday_advisor.strategy import apply_ema_swing_breakout_strategy

HORIZONS = (6, 12, 24)          # bars ahead: 30 min, 1 hour, 2 hours


def study(ticker: str, label: str, period: str = "58d", interval: str = "5m"):
    frame = fetch_yahoo_ohlcv(ticker, period=period, interval=interval)
    enriched = apply_ema_swing_breakout_strategy(add_indicators(frame))
    enriched = enriched.dropna(subset=["Close", "EMA9", "EMA21", "ATR14"])
    if len(enriched) < 500:
        print(f"{label}: only {len(enriched)} usable bars — not enough.")
        return None

    weights = ScoreWeights.load()
    rows = []
    # Score every bar. The scorer takes a mapping, so feed it the row dict.
    records = enriched.to_dict("records")
    index = enriched.index
    for i, record in enumerate(records):
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
        scored = score_signal(
            clean,
            decision_signal=clean.get("Signal") or None,
            vwap_relation=None,          # index candles carry no volume
            weights=weights,
        )
        rows.append({
            "ts": index[i],
            "close": float(record["Close"]),
            "score": scored.score,
            "raw": scored.raw_score,
            "signal": scored.signal_type,
        })

    study_frame = pd.DataFrame(rows).set_index("ts")
    for horizon in HORIZONS:
        study_frame[f"fwd{horizon}"] = (
            study_frame["close"].shift(-horizon) / study_frame["close"] - 1.0
        ) * 100.0

    print("=" * 78)
    print(f"{label}   {len(study_frame):,} scored bars   "
          f"{study_frame.index[0].date()} to {study_frame.index[-1].date()}")
    print("=" * 78)

    # --- does the score separate outcomes at all? --- #
    bands = [(0, 10), (10, 25), (25, 40), (40, 60), (60, 75), (75, 90), (90, 101)]
    print(f"\n{'score band':>12} {'bars':>7} "
          + "".join(f"{'fwd' + str(h) + ' mean%':>14}" for h in HORIZONS)
          + f"{'  up-rate(1h)':>14}")
    for low, high in bands:
        chunk = study_frame[(study_frame.score >= low) & (study_frame.score < high)]
        if chunk.empty:
            continue
        cells = "".join(f"{chunk[f'fwd{h}'].mean():14.4f}" for h in HORIZONS)
        up = (chunk["fwd12"] > 0).mean() * 100
        print(f"{low:4}-{high - 1:<7} {len(chunk):7,}{cells}{up:13.1f}%")

    # --- the headline test: does a BUY beat a SELL? --- #
    print()
    for horizon in HORIZONS:
        buys = study_frame[study_frame.signal == "BUY"][f"fwd{horizon}"].dropna()
        sells = study_frame[study_frame.signal == "SELL"][f"fwd{horizon}"].dropna()
        holds = study_frame[study_frame.signal == "HOLD"][f"fwd{horizon}"].dropna()
        if len(buys) < 30 or len(sells) < 30:
            print(f"  fwd{horizon}: too few BUY/SELL bars "
                  f"({len(buys)}/{len(sells)}) to test.")
            continue
        # Directional edge: a BUY should rise, a SELL should fall.
        spread = buys.mean() - sells.mean()
        pooled = np.sqrt(buys.var(ddof=1) / len(buys) + sells.var(ddof=1) / len(sells))
        t = spread / pooled if pooled else 0.0
        print(f"  fwd{horizon:>2} bars: BUY {buys.mean():+.4f}%  "
              f"SELL {sells.mean():+.4f}%  HOLD {holds.mean():+.4f}%   "
              f"spread {spread:+.4f}pp  t={t:+.2f}  "
              f"n={len(buys)}/{len(sells)}")
    return study_frame


if __name__ == "__main__":
    for ticker, label in (("^NSEI", "NIFTY"), ("^NSEBANK", "BANKNIFTY")):
        try:
            study(ticker, label)
            print()
        except Exception as exc:  # noqa: BLE001
            print(f"{label}: failed — {exc}")
