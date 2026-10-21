"""Is the market charging more for movement than the movement it delivers?

The question nothing in this project could previously answer. ``options_math``
computes implied volatility and compares it to India VIX, which tells you
whether an option is dear *relative to other options*. It cannot tell you
whether the price of movement is too high, because nothing here measured the
movement that actually happened.

Run it:

    python research_variance_premium.py

Measured 2026-10-21 over the preceding year. The headline, and the trap in it:

                        NIFTY     BANKNIFTY
    5-minute realised    7.6%        9.9%      <- excludes overnight gaps
    daily realised      12.3%       13.8%      <- Yang-Zhang, gaps included
    India VIX           ~15.9%      ~15.9%

Using the 5-minute figure gives a premium of +8.3 and +6.0 volatility points.
Using the daily figure gives **+3.6 and +2.1**. The first is wrong, and wrong
in the flattering direction: India VIX is a 30-day *calendar* measure that
includes every overnight move, while a session-aware 5-minute estimate
deliberately drops them. Comparing the two overstates the premium by more than
double.

The daily numbers also reproduce a textbook result worth seeing once:

    NIFTY daily      parkinson 10.0%   garman-klass 10.0%
                     close-to-close 13.8%   yang-zhang 14.7%

Parkinson and Garman-Klass read ~4 points lower because neither formula
contains the overnight jump -- they assume continuous trading. Yang-Zhang adds
that term back explicitly and reads highest. This is why ``volatility.py``
defaults to Garman-Klass for intraday bars and tells you to use Yang-Zhang for
daily ones.

**The conclusion, which is the same conclusion this project keeps reaching.**

The premium is real and it is on the *seller's* side -- which means every
option this ledger has bought was bought into a structural headwind, and that
is the cleanest explanation yet for "direction was RIGHT and the trade still
lost". But:

    premium over a 6-day hold   NIFTY 0.46%   BANKNIFTY 0.27%  of underlying
    measured option round trip  ~1.25%        (microstructure.py, 2 rupees
                                               on a 160 premium)

So the favoured side of the trade still does not clear retail option costs at
one lot. That is not a pessimistic reading, it is the same arithmetic that
defeated the +0.111% directional edge, and it matches the published finding
that the NIFTY variance premium "cannot be captured by a retail trader under
current market microstructure".

What that leaves, honestly: the premium is an argument about which *structure*
to prefer (sell premium inside defined risk rather than buy it), not a trade
signal. It changes the sign of the headwind you trade into. It does not create
an edge, and this script exists so that claim stays measured rather than
remembered.
"""

from __future__ import annotations

import logging

logging.disable(logging.WARNING)

from intraday_advisor.data import fetch_yahoo_ohlcv
from intraday_advisor.volatility import (
    realized_volatility,
    variance_risk_premium,
    volatility_cone,
)

#: India VIX at the time of writing. Passed in rather than fetched so the
#: script is reproducible offline; the regime card fetches the live figure.
ASSUMED_VIX = 15.9

#: Round-trip cost of a one-lot index option as a percentage of the premium,
#: measured from the book rather than assumed -- a Rs 2 spread on a Rs 160
#: premium is 1.25% before a single fee. See microstructure.py.
OPTION_ROUND_TRIP_PCT = 1.25

INDICES = (("^NSEI", "NIFTY"), ("^NSEBANK", "BANKNIFTY"))


def intraday_view(ticker: str, label: str) -> None:
    frame = fetch_yahoo_ohlcv(ticker, period="58d", interval="5m")
    print(f"\n{label} -- 5-minute bars ({len(frame):,} bars, "
          f"{frame.index[0].date()} to {frame.index[-1].date()})")
    print("  Overnight gaps EXCLUDED, so these understate total volatility.")
    for method in ("close_to_close", "parkinson", "garman_klass"):
        estimate = realized_volatility(frame, method=method, bar_size="5m")
        print(f"    {method:16} {estimate.annualized_pct:6.2f}%")

    cone = volatility_cone(frame, bar_size="5m")
    print(f"    cone: {cone.get('state')} "
          f"(short {cone.get('short_pct')}% vs long {cone.get('long_pct')}%)")


def daily_view(ticker: str, label: str) -> None:
    frame = fetch_yahoo_ohlcv(ticker, period="1y", interval="1d")
    print(f"\n{label} -- daily bars ({len(frame)} bars)")
    print("  Overnight gaps INCLUDED. This is the comparison VIX deserves.")
    for method in ("close_to_close", "parkinson", "garman_klass", "yang_zhang"):
        estimate = realized_volatility(
            frame, method=method, bar_size="1d", session_aware=False)
        gap_blind = method in ("parkinson", "garman_klass")
        marker = "   <- no overnight term" if gap_blind else ""
        print(f"    {method:16} {estimate.annualized_pct:6.2f}%{marker}")

    recent = realized_volatility(
        frame.tail(30), method="yang_zhang", bar_size="1d",
        session_aware=False)
    print(f"\n    Last 30 sessions (Yang-Zhang): {recent.annualized_pct:.2f}%")

    premium = variance_risk_premium(
        ASSUMED_VIX, recent, days_to_expiry=6,
        round_trip_cost_pct=OPTION_ROUND_TRIP_PCT,
    )
    print(f"    Against VIX {ASSUMED_VIX}: "
          f"{premium.premium_points:+.2f} volatility points, "
          f"paid side = {premium.side_being_paid.upper()}")
    horizon = premium.evidence.get("premium_over_horizon_pct")
    print(f"    Worth {horizon}% of the underlying over 6 days, against a "
          f"{OPTION_ROUND_TRIP_PCT}% round trip")
    print(f"    VERDICT: {premium.verdict}")
    for reason in premium.reasons:
        print(f"      - {reason}")


if __name__ == "__main__":
    print("=" * 78)
    print("VARIANCE RISK PREMIUM -- implied against realised")
    print("=" * 78)

    for ticker, label in INDICES:
        try:
            intraday_view(ticker, label)
            daily_view(ticker, label)
        except Exception as exc:  # noqa: BLE001
            print(f"{label}: failed -- {exc}")
        print("-" * 78)

    print("""
READ THIS BEFORE ACTING ON IT

The premium being positive means the SELLER of the option is the paid side.
It does not mean selling options is profitable:

  1. A positive variance premium is compensation for bearing crash risk. It
     pays small amounts often and loses large amounts rarely, so a win rate
     tells you almost nothing about it. Zerodha's own 45-DTE NIFTY straddle
     backtest had its worst trade lose over 1,000 points THROUGH a stop,
     because the market gapped past it.
  2. The figures above show the premium is worth less than the round trip on a
     one-lot option anyway.
  3. Never sell naked. spreads.build_structure() caps the loss; a short
     straddle does not, and `spreads.assess_edge` has already shown that
     credit structures which LOOK like sensible premium collection can carry
     negative expectancy once the required win rate is compared to the implied
     one.

Nothing here has passed validation.validate_rule. Until it does, this is a
measurement of the weather, not a strategy.
""")
