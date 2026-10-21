"""Implied volatility, skew and pin detection for the live option chain.

Why this exists
---------------
Trading the desk on 2026-10-21 I could not answer the first question any option
buyer has to answer -- *am I overpaying?* -- from anything on the screen. The
dashboard showed premiums, open interest, PCR and max pain, but no volatility,
so there was no way to tell a cheap option from a dear one.

Computing it by hand changed the trade. With India VIX at 15.9 and rising 7.7%:

    NIFTY      PE IV 15.0-15.4%   CE IV 14.9-15.4%   flat skew, BELOW VIX
    BANKNIFTY  PE IV 16.9-17.2%   CE IV 14.9-15.1%   ~2 points of put skew

Same bearish view, same expiry, and the BANKNIFTY put cost two volatility
points more than the NIFTY put. That is a real, avoidable cost, and nothing on
the screen hinted at it.

What the numbers mean
---------------------
**Implied volatility** is the market's own estimate of how much the index will
move, backed out of the price you are being asked to pay. Compared against
India VIX it answers "dear or cheap": an option priced at 17% IV while VIX says
15.9% is asking you to pay for movement the broader market does not expect.

**Skew** is the gap between put IV and call IV at the same distance from spot.
A positive put skew means downside protection is in demand -- normal in a
falling market, but it is *you* who pays for it when you buy the put.

**A pin** is a strike where the call wall, the put wall and max pain coincide.
Writers on both sides are defending the same level, so price tends to be drawn
back to it rather than trend away from it. Observed live: BANKNIFTY had CE OI
1,324,650 and PE OI 1,296,780 both peaking at 55,000 with max pain also at
55,000, and spot sitting 146 points below. A directional trade *through* a pin
is fighting both sides of the chain at once.

Honest limits
-------------
This is Black-Scholes on a European-style index option, which NSE index options
are, so the model is appropriate. But it ignores dividends, uses a flat risk-
free rate, and takes the last traded price as fair -- a stale or wide-spread
quote produces a nonsense IV. Values outside a sane band are returned as
``None`` rather than as a confident wrong number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Indian risk-free proxy. The IV of a short-dated option barely moves with
#: this, so a reasonable constant beats a network call for the repo rate.
RISK_FREE_RATE = 0.065

#: Outside this band an "implied volatility" is a quote problem, not a market
#: view: a stale LTP or a one-rupee-wide spread on an illiquid strike.
MIN_SENSIBLE_IV = 0.02
MAX_SENSIBLE_IV = 2.00

#: A put/call IV gap of at least this much is worth telling the operator about.
NOTABLE_SKEW = 0.01          # one volatility point


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(
    spot: float, strike: float, years: float, vol: float, option_type: str,
    rate: float = RISK_FREE_RATE,
) -> float:
    """Theoretical European option price. ``option_type`` is ``CE`` or ``PE``."""
    kind = (option_type or "").upper()
    if spot <= 0 or strike <= 0:
        return 0.0
    if years <= 0 or vol <= 0:
        intrinsic = (spot - strike) if kind == "CE" else (strike - spot)
        return max(0.0, intrinsic)

    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2.0) * years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    discount = strike * math.exp(-rate * years)
    if kind == "CE":
        return spot * _norm_cdf(d1) - discount * _norm_cdf(d2)
    return discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(
    price: float, spot: float, strike: float, years: float, option_type: str,
    rate: float = RISK_FREE_RATE,
) -> float | None:
    """Back the volatility out of a traded price. ``None`` if it is not sane.

    Bisection rather than Newton-Raphson: vega collapses on deep in- and
    out-of-the-money strikes, where Newton diverges and returns a confident
    absurdity. Bisection is slower and cannot do that.
    """
    if price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None

    # Below intrinsic there is no volatility that explains the price; the quote
    # is stale or crossed.
    kind = (option_type or "").upper()
    intrinsic = max(0.0, (spot - strike) if kind == "CE" else (strike - spot))
    if price < intrinsic * 0.999:
        return None

    low, high = MIN_SENSIBLE_IV, MAX_SENSIBLE_IV
    if black_scholes(spot, strike, years, high, kind, rate) < price:
        return None          # even 200% vol cannot reach this price
    for _ in range(100):
        mid = (low + high) / 2.0
        if black_scholes(spot, strike, years, mid, kind, rate) > price:
            high = mid
        else:
            low = mid
    vol = (low + high) / 2.0
    if vol <= MIN_SENSIBLE_IV * 1.01 or vol >= MAX_SENSIBLE_IV * 0.99:
        return None
    return round(vol, 4)


@dataclass
class ChainVolatility:
    """What the chain is charging for movement, and how that compares to VIX."""

    atm_strike: float = 0.0
    atm_ce_iv: float | None = None
    atm_pe_iv: float | None = None
    #: Mean IV across the strikes either side of ATM, per side.
    mean_ce_iv: float | None = None
    mean_pe_iv: float | None = None
    #: pe_iv - ce_iv. Positive means puts are the dearer side.
    skew: float | None = None
    #: ATM IV against India VIX, as a ratio. >1 means dearer than VIX implies.
    vs_vix: float | None = None
    days_to_expiry: float = 0.0
    notes: list[str] = field(default_factory=list)
    per_strike: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def analyse_chain_volatility(
    frame, spot: float, days_to_expiry: float, *,
    vix: float | None = None, window: int = 4,
) -> ChainVolatility:
    """IV per strike, the ATM pair, the skew, and a read against VIX.

    ``frame`` is the chain table with ``strike``/``ce_ltp``/``pe_ltp``.
    ``window`` is how many strikes either side of ATM to average -- far wings
    are illiquid and their IV says more about the spread than about the market.
    """
    result = ChainVolatility(days_to_expiry=round(days_to_expiry, 2))
    if frame is None or getattr(frame, "empty", True) or spot <= 0:
        result.notes.append("No chain data for a volatility read.")
        return result
    if days_to_expiry <= 0:
        result.notes.append("Expiry today or past — IV is not meaningful.")
        return result

    years = days_to_expiry / 365.0
    strikes = sorted(float(s) for s in frame["strike"].tolist())
    if not strikes:
        return result
    atm = min(strikes, key=lambda k: abs(k - spot))
    result.atm_strike = atm

    ce_ivs: list[float] = []
    pe_ivs: list[float] = []
    for _, row in frame.iterrows():
        strike = float(row["strike"])
        steps = abs(strikes.index(strike) - strikes.index(atm))
        entry = {}
        for side, column in (("CE", "ce_ltp"), ("PE", "pe_ltp")):
            price = float(row.get(column, 0) or 0)
            vol = implied_vol(price, spot, strike, years, side)
            if vol is None:
                continue
            entry[side] = round(vol * 100, 2)
            if steps <= window:
                (ce_ivs if side == "CE" else pe_ivs).append(vol)
        if entry:
            result.per_strike[int(strike)] = entry
            if strike == atm:
                result.atm_ce_iv = entry.get("CE")
                result.atm_pe_iv = entry.get("PE")

    if ce_ivs:
        result.mean_ce_iv = round(sum(ce_ivs) / len(ce_ivs) * 100, 2)
    if pe_ivs:
        result.mean_pe_iv = round(sum(pe_ivs) / len(pe_ivs) * 100, 2)

    if result.mean_ce_iv is not None and result.mean_pe_iv is not None:
        result.skew = round(result.mean_pe_iv - result.mean_ce_iv, 2)
        if result.skew >= NOTABLE_SKEW * 100:
            result.notes.append(
                f"Puts carry {result.skew:.1f} more volatility points than calls — "
                f"downside protection is in demand and you pay for it when you "
                f"buy a put here."
            )
        elif result.skew <= -NOTABLE_SKEW * 100:
            result.notes.append(
                f"Calls carry {abs(result.skew):.1f} more volatility points than "
                f"puts — upside is the crowded side."
            )
        else:
            result.notes.append(
                "Put and call volatility are level — neither side is priced "
                "richer than the other."
            )

    atm_iv = result.atm_pe_iv or result.atm_ce_iv
    if atm_iv and vix and vix > 0:
        result.vs_vix = round(atm_iv / vix, 3)
        if result.vs_vix >= 1.08:
            result.notes.append(
                f"At-the-money IV {atm_iv:.1f}% is above India VIX {vix:.1f} — "
                f"you are paying for more movement than the index gauge expects."
            )
        elif result.vs_vix <= 0.92:
            result.notes.append(
                f"At-the-money IV {atm_iv:.1f}% is below India VIX {vix:.1f} — "
                f"options are cheap against the index gauge."
            )
        else:
            result.notes.append(
                f"At-the-money IV {atm_iv:.1f}% is in line with India VIX "
                f"{vix:.1f}."
            )
    return result


# --------------------------------------------------------------------------- #
# Pin detection
# --------------------------------------------------------------------------- #

@dataclass
class PinRead:
    """Whether the chain is defending one strike from both sides."""

    is_pin: bool = False
    strike: float = 0.0
    distance_pct: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def detect_pin(
    ce_wall: float, pe_wall: float, max_pain: float, spot: float,
    step: float,
) -> PinRead:
    """A strike where the call wall, put wall and max pain coincide.

    Writers on both sides are defending the same level, so price is drawn back
    to it rather than trending away. A directional trade that has to travel
    *through* a pin is fighting both sides of the chain at once.

    Seen live on 2026-10-21: BANKNIFTY carried its largest call OI (1,324,650)
    and its largest put OI (1,296,780) both at 55,000, with max pain also at
    55,000 and spot 146 points below. The dashboard printed the three identical
    numbers and said nothing about what they meant together.
    """
    levels = [ce_wall, pe_wall, max_pain]
    if not all(level and level > 0 for level in levels) or spot <= 0:
        return PinRead()
    if max(levels) - min(levels) > step * 0.5:
        return PinRead()

    strike = float(pe_wall)
    distance = (spot - strike) / spot
    where = (
        "above" if distance > 0.0005 else
        "below" if distance < -0.0005 else "right at"
    )
    pull = (
        "the pull is downward, against a CALL"
        if distance > 0.0005 else
        "the pull is upward, against a PUT"
        if distance < -0.0005 else
        "price is already there"
    )
    return PinRead(
        is_pin=True,
        strike=strike,
        distance_pct=round(distance * 100, 2),
        note=(
            f"PIN at {strike:,.0f}: the call wall, the put wall and max pain "
            f"all sit on this strike, so both sides of the chain are defending "
            f"it. Spot is {where} it and {pull}. A directional trade has to "
            f"travel through the level the whole chain is holding."
        ),
    )


# --------------------------------------------------------------------------- #
# Level-based exits
# --------------------------------------------------------------------------- #

def target_from_level(
    *, spot: float, strike: float, entry_premium: float, option_type: str,
    level: float, days_to_expiry: float, vol: float,
    rate: float = RISK_FREE_RATE,
) -> dict:
    """What the premium is worth if the index reaches ``level``.

    The point of this is that a fixed percentage target ignores where price can
    actually go. Measured live on 2026-10-21: NIFTY spot 22,705 with the put
    wall 205 points below at 22,500. The default +25% target asked for about 80
    index points; repricing the option at the wall showed roughly +55% was
    available over the same distance, and net reward:risk went from **1.13 to
    2.55 at identical risk**. Nothing was added to the trade except using a
    level the chain was already publishing.

    Re-prices the option at ``level`` with the same volatility and one less
    session of time value, which is the honest way to ask the question: it
    accounts for the decay you pay while waiting for the move.
    """
    if entry_premium <= 0 or spot <= 0 or level <= 0:
        return {}
    remaining = max(0.0, days_to_expiry - 1.0) / 365.0
    projected = black_scholes(level, strike, remaining, vol, option_type, rate)
    change = (projected / entry_premium) - 1.0
    return {
        "level": round(level, 2),
        "index_move_pct": round((level - spot) / spot * 100, 3),
        "projected_premium": round(projected, 2),
        "premium_change_pct": round(change * 100, 1),
        "suggested_target_pct": round(max(0.0, change), 4),
        "basis": (
            f"Option repriced at index {level:,.0f} with one session of time "
            f"value given up."
        ),
    }


def vix_comparison_note(atm_iv_pct: float | None, vix: float | None) -> str:
    """One line on whether the chain is dearer or cheaper than India VIX.

    Kept separate from :func:`analyse_chain_volatility` because the regime
    already fetches VIX once every five minutes. Fetching it again inside the
    chain builder would be a second network call for a number the process is
    already holding, on the hottest path in the app.
    """
    if not atm_iv_pct or not vix or vix <= 0:
        return ""
    ratio = atm_iv_pct / vix
    if ratio >= 1.08:
        return (
            f"At-the-money IV {atm_iv_pct:.1f}% is above India VIX {vix:.1f} "
            f"— you are paying for more movement than the index gauge expects."
        )
    if ratio <= 0.92:
        return (
            f"At-the-money IV {atm_iv_pct:.1f}% is below India VIX {vix:.1f} "
            f"— options are cheap against the index gauge."
        )
    return f"At-the-money IV {atm_iv_pct:.1f}% is in line with India VIX {vix:.1f}."
