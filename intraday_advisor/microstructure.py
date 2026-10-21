"""The order book, read as a cost rather than as a signal.

Why this module exists
----------------------
Every measurement this project has taken points at the same conclusion:
``validation.py`` found a real, out-of-sample edge of **+0.111%** on BANKNIFTY
and then showed it losing to a **0.19%-0.50%** round trip. Costs are two to
five times the edge. The honest summary in that module is that only three
things change the arithmetic -- a bigger edge, a smaller cost, or more capital.

This module is about the second one, and it starts from an embarrassing fact:

**the most important number in the system is a guess.**

``ledger/costs.py`` models the bid-ask spread as ``half_spread_pct = 0.0025``
-- a flat 0.25% per leg, 0.5% round trip, the same for a liquid ATM weekly and
a dead far-month wing. That constant decides whether any trade clears its
costs, which means it decides every verdict the app gives. It was never
measured. The module even provides ``with_measured_spread()`` for exactly this
purpose and **nothing in the codebase ever called it.**

Meanwhile ``nse_option_chain.py`` already asks Angel One for ``FULL`` mode
market data on every single chain refresh, and FULL mode returns **five levels
of bid and ask depth per contract**. The parser kept ``ltp`` and ``oi`` and
threw the book away.

So the cost that beats every edge was being assumed, while the data needed to
measure it was being fetched and discarded. That is the gap this closes.

What measuring it actually changes
----------------------------------
A quoted spread is not a detail at these premium levels. One tick is Rs 0.05;
on a Rs 160 premium that is 0.03%, but option books are rarely one tick wide.
A two-rupee spread on a Rs 160 premium is **1.25% per leg, 2.5% round trip** --
five times the modelled constant, and on its own larger than any edge this
project has ever measured. The same two-rupee spread on a Rs 12 wing is 16.7%.

That is why a measured spread is not a refinement of the cost model. It is the
difference between "this contract is tradeable" and "this contract can never
repay the cost of entering it", and the app currently cannot tell those apart.

Walking the book, not assuming a percentage
-------------------------------------------
``half_spread_pct`` is a fraction, which quietly assumes your order is small
enough to fill at the touch. One NIFTY lot is 75 units; if the best offer shows
40, a market order fills 40 there and the rest higher up. :meth:`BookRead.walk`
computes the actual average fill for the size you are actually sending, which
is the real slippage rather than a proxy for it.

Time of day
-----------
NSE's own microstructure research (Competitive Paper 128) reports spreads
**wider at the open and the close and narrowest between roughly 11:00 and
14:00**, and an academic study of NIFTY constituents finds the familiar
intra-day U-shape in volume and spread measures. This matters here because the
scorer rewards opening-range breakouts -- it recommends trading in the single
most expensive window of the day and charges a flat cost for it.

:data:`SPREAD_SHAPE_BY_HOUR` encodes the *shape* that research describes. It is
labelled a prior, not a measurement, and :func:`spread_multiplier_at` says so
in its own output, because this project has already learned what happens when a
plausible constant is mistaken for a fact. Once the ledger holds enough fills,
:func:`calibrate_shape_from_fills` replaces the prior with your own book.

What this module deliberately does not do
-----------------------------------------
Depth imbalance is computed because it is cheap and it is genuinely orthogonal
to the 37 price-derived components in the scorer. It is **not** scored, and
:func:`depth_imbalance` says plainly that it is unvalidated. Adding an
unmeasured component to a scorer that has already been measured to have no
edge would repeat the exact mistake this project spent six sessions
diagnosing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .ledger.costs import TICK_SIZE, OptionsCostModel

#: A quoted spread wider than this fraction of the mid is not a price, it is an
#: absence of one. Nothing should be traded on it and no cost model should
#: pretend to. Chosen at 25% because a round trip there costs half the premium.
UNTRADEABLE_SPREAD_PCT = 0.25

#: Relative spread above which a contract is flagged as expensive to cross.
#: 1% per leg is 2% round trip, already four times the modelled constant and
#: roughly twenty times any edge measured in this project.
WIDE_SPREAD_PCT = 0.01

#: The intraday shape of the quoted spread, as a multiplier on the day's
#: typical level, keyed by the hour of the IST trading session.
#:
#: **This is a documented prior, not a measurement.** The direction and shape
#: come from NSE Competitive Paper 128 (spreads wider at the open and close,
#: narrowest between roughly 11:00 and 14:00) and from the intraday U-shape
#: found in NIFTY constituents. The magnitudes are a deliberately conservative
#: reading of that shape -- they are the right sign and the right ordering, and
#: they should be replaced by :func:`calibrate_shape_from_fills` as soon as
#: your own fills can support it.
#:
#: The practical consequence is already significant: an opening-range entry is
#: modelled as costing roughly **1.6 times** a midday entry, which is the
#: difference between a marginal trade and a losing one.
SPREAD_SHAPE_BY_HOUR = {
    9: 1.60,    # 09:15-09:59 -- price discovery, widest book of the day
    10: 1.20,
    11: 1.00,   # 11:00-13:59 -- the documented trough
    12: 1.00,
    13: 1.05,
    14: 1.20,
    15: 1.45,   # 15:00-15:30 -- closing auction pressure, square-off flow
}


@dataclass(frozen=True)
class DepthLevel:
    """One price level of the book."""

    price: float
    quantity: float
    orders: int = 0


@dataclass
class BookRead:
    """A contract's book, and what crossing it actually costs.

    ``bids`` are ordered best (highest) first and ``asks`` best (lowest) first,
    whatever order the feed delivered them in.
    """

    symbol: str = ""
    bids: list[DepthLevel] = field(default_factory=list)
    asks: list[DepthLevel] = field(default_factory=list)
    #: Anything the parser had to correct or could not establish. Read it.
    notes: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- prices
    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        """The midpoint, which is the only defensible 'fair' price here.

        LTP is a *historical* print and may sit on either side of a book that
        has since moved; sizing a trade off it understates the cost of getting
        in by up to the full spread.
        """
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float | None:
        """Quoted spread in rupees."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def spread_ticks(self) -> float | None:
        """The spread in ticks -- the unit a trader actually sees."""
        if self.spread is None:
            return None
        return self.spread / TICK_SIZE

    @property
    def relative_spread(self) -> float | None:
        """Quoted spread as a fraction of the mid.

        This, not the rupee spread, is the number that matters: the same
        Rs 2 spread is 1.25% of a Rs 160 premium and 16.7% of a Rs 12 wing.
        """
        mid = self.mid
        if mid is None or mid <= 0 or self.spread is None:
            return None
        return self.spread / mid

    @property
    def half_spread_pct(self) -> float | None:
        """Per-leg spread cost, in the units ``OptionsCostModel`` expects.

        Crossing the book once costs half the quoted spread against the mid,
        so this is exactly the quantity that module's ``half_spread_pct``
        field was always meant to hold.
        """
        relative = self.relative_spread
        return None if relative is None else relative / 2.0

    # ------------------------------------------------------------- liquidity
    @property
    def touch_quantity(self) -> tuple[float, float]:
        """Size resting at the best bid and the best offer."""
        return (
            self.bids[0].quantity if self.bids else 0.0,
            self.asks[0].quantity if self.asks else 0.0,
        )

    def walk(self, side: str, quantity: float) -> dict:
        """Average fill price for a market order of ``quantity``.

        ``side`` is the side you are *taking*: ``"buy"`` consumes asks,
        ``"sell"`` consumes bids.

        This is the honest answer to "what will this cost me", and it differs
        from the percentage model exactly when it matters -- when your size is
        larger than the touch. A fraction cannot express "the best offer only
        shows 40 of the 75 you need".

        Returns ``filled`` (what the visible book can actually absorb),
        ``average_price``, ``slippage_vs_mid_pct`` and ``complete``. When the
        visible book cannot fill the order, ``complete`` is ``False`` and the
        average price describes only the part that fills -- the remainder has
        no price, and inventing one would be the confident-wrong-number
        failure this project refuses elsewhere.
        """
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        levels = self.asks if side == "buy" else self.bids
        remaining, cost, filled = float(quantity), 0.0, 0.0
        for level in levels:
            if remaining <= 0:
                break
            take = min(remaining, level.quantity)
            cost += take * level.price
            filled += take
            remaining -= take

        result = {
            "side": side,
            "requested": float(quantity),
            "filled": filled,
            "complete": remaining <= 0,
            "average_price": (cost / filled) if filled > 0 else None,
            "slippage_vs_mid_pct": None,
            "levels_consumed": sum(
                1 for i, lv in enumerate(levels)
                if i == 0 or sum(l.quantity for l in levels[:i]) < quantity
            ),
        }
        mid = self.mid
        if result["average_price"] is not None and mid and mid > 0:
            signed = (result["average_price"] - mid) if side == "buy" else (
                mid - result["average_price"])
            result["slippage_vs_mid_pct"] = round(signed / mid * 100, 4)
        if not result["complete"]:
            result["note"] = (
                f"The visible book holds only {filled:,.0f} of "
                f"{quantity:,.0f}. The rest has no quoted price, so the true "
                f"average fill is worse than the figure shown -- by an amount "
                f"nothing on screen can tell you."
            )
        return result

    def depth_imbalance(self, levels: int = 5) -> dict:
        """Resting size on the bid against the ask.

        Genuinely orthogonal to every component in ``scoring.py``, all of
        which are functions of the same OHLC series. That makes it interesting
        and does **not** make it predictive.

        **This is unvalidated and is not scored.** Resting size is the easiest
        quantity in the book to fake -- quotes can be pulled faster than they
        can be read -- and this project's standing conclusion is that no rule
        it has discovered clears its own costs. Run it through
        ``validation.validate_rule`` before letting it influence anything.
        """
        bid_size = sum(level.quantity for level in self.bids[:levels])
        ask_size = sum(level.quantity for level in self.asks[:levels])
        total = bid_size + ask_size
        ratio = (bid_size - ask_size) / total if total > 0 else None
        return {
            "bid_quantity": bid_size,
            "ask_quantity": ask_size,
            "imbalance": round(ratio, 4) if ratio is not None else None,
            "levels": levels,
            "status": "unvalidated",
            "caveat": (
                "Resting size is not committed size; displayed quotes can be "
                "withdrawn faster than they can be acted on. Measured, not "
                "scored."
            ),
        }

    # ---------------------------------------------------------------- health
    @property
    def is_usable(self) -> bool:
        """Is there a two-sided price here at all?"""
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > 0
            and self.best_ask > 0
            and self.spread is not None
            and self.spread >= 0
        )

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid": self.mid,
            "spread": self.spread,
            "spread_ticks": round(self.spread_ticks, 1) if self.spread_ticks is not None else None,
            "relative_spread_pct": (
                round(self.relative_spread * 100, 3)
                if self.relative_spread is not None else None
            ),
            "half_spread_pct": self.half_spread_pct,
            "touch_bid_qty": self.touch_quantity[0],
            "touch_ask_qty": self.touch_quantity[1],
            "usable": self.is_usable,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Parsing -- defensively, because this feed's field names are not a contract
# --------------------------------------------------------------------------- #

#: Where the best-five arrays have been found, across the shapes Angel One
#: actually returns. The REST quote and the binary WebSocket disagree, and the
#: project has already been bitten once by trusting a provider's naming
#: (see ``AGENTS.md`` on retired model ids).
_BID_KEYS = ("best_5_buy_data", "buy", "bids", "bestBids", "bidDepth")
_ASK_KEYS = ("best_5_sell_data", "sell", "asks", "bestAsks", "askDepth")
_PRICE_KEYS = ("price", "Price", "bidprice", "askPrice", "p")
_QTY_KEYS = ("quantity", "qty", "Quantity", "bidQty", "askQty", "q")
_ORDER_KEYS = ("no of orders", "orders", "numberOfOrders", "no_of_orders")


def _first(mapping: dict, keys: tuple[str, ...], default=None):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _levels(raw, scale: float) -> list[DepthLevel]:
    out: list[DepthLevel] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            price = float(_first(entry, _PRICE_KEYS, 0) or 0) / scale
            quantity = float(_first(entry, _QTY_KEYS, 0) or 0)
        except (TypeError, ValueError):
            continue
        # A zero-price level is a padded empty slot, not an offer to trade.
        if price <= 0 or quantity <= 0:
            continue
        try:
            orders = int(_first(entry, _ORDER_KEYS, 0) or 0)
        except (TypeError, ValueError):
            orders = 0
        out.append(DepthLevel(price=price, quantity=quantity, orders=orders))
    return out


def parse_book(
    payload: dict, *, symbol: str = "", reference_price: float | None = None,
) -> BookRead:
    """Read a :class:`BookRead` out of whatever shape the feed delivered.

    Handles the REST quote's ``depth: {buy: [...], sell: [...]}`` and the
    WebSocket's ``best_5_buy_data`` / ``best_5_sell_data``, both of which this
    account's own SDK produces, with different key names and different units.

    Three corrections are applied, each because the feed genuinely needs it:

    **Paise versus rupees.** The binary WebSocket feed reports prices as
    integers in paise; the REST quote reports rupees. Guessing wrong scales
    every cost by 100. When ``reference_price`` (the contract's LTP) is given,
    the scale is *derived* by comparing magnitudes rather than assumed. Without
    it, prices are taken at face value and a note records that they were.

    **Buy and sell arriving swapped.** The installed ``SmartApi`` SDK assigns
    ``best_5_buy_data`` from its own parser's *sell* list and vice versa
    (``smartWebSocketV2.py``, the two lines after the best-five parse). Rather
    than hard-code a correction to someone else's bug -- which silently
    inverts the day they fix it -- this checks the only invariant that cannot
    be violated by a real market: **a book cannot be crossed.** If the best
    "bid" sits above the best "ask", the sides are swapped and a note says so.
    That is self-correcting in both directions.

    **Padding.** Empty depth slots arrive as zero-price, zero-quantity rows and
    are dropped, so a half-populated book is not read as a free one.
    """
    book = BookRead(symbol=symbol)
    if not isinstance(payload, dict):
        book.notes.append("No depth payload; the book is unknown, not tight.")
        return book

    container = payload
    nested = payload.get("depth")
    if isinstance(nested, dict):
        container = nested

    raw_bids = _first(container, _BID_KEYS)
    raw_asks = _first(container, _ASK_KEYS)
    if raw_bids is None and raw_asks is None:
        book.notes.append(
            "Feed carried no best-five depth, so the spread cannot be "
            "measured and the modelled constant still applies."
        )
        return book

    # --- units ---------------------------------------------------------- #
    scale = 1.0
    sample = [
        float(_first(e, _PRICE_KEYS, 0) or 0)
        for e in (list(raw_bids or []) + list(raw_asks or []))
        if isinstance(e, dict)
    ]
    sample = [p for p in sample if p > 0]
    if reference_price and reference_price > 0 and sample:
        typical = sorted(sample)[len(sample) // 2]
        if typical > reference_price * 20:
            scale = 100.0
            book.notes.append(
                "Depth prices were in paise (median level was "
                f"{typical:,.0f} against an LTP of {reference_price:,.2f}); "
                "divided by 100."
            )
    elif sample:
        book.notes.append(
            "No reference price supplied, so depth prices were taken at face "
            "value. If this feed reports paise, every cost here is 100x too "
            "large -- pass the contract's LTP as reference_price."
        )

    bids = sorted(_levels(raw_bids, scale), key=lambda l: -l.price)
    asks = sorted(_levels(raw_asks, scale), key=lambda l: l.price)

    # --- the crossed-book invariant ------------------------------------- #
    if bids and asks and bids[0].price > asks[0].price:
        bids, asks = (
            sorted(asks, key=lambda l: -l.price),
            sorted(bids, key=lambda l: l.price),
        )
        book.notes.append(
            "Bid and ask arrived swapped (the quoted book was crossed, which "
            "a real market cannot be) and have been exchanged. The installed "
            "SmartApi SDK is known to assign these two fields from each "
            "other's parser output."
        )

    book.bids, book.asks = bids, asks
    if not book.is_usable:
        book.notes.append(
            "No two-sided price: one side of the book is empty or zero. This "
            "contract has no measurable cost of entry."
        )
    return book


# --------------------------------------------------------------------------- #
# Time of day
# --------------------------------------------------------------------------- #

def spread_multiplier_at(moment, shape: dict | None = None) -> dict:
    """How expensive is the book at this hour, relative to a typical one?

    ``moment`` is anything with ``.hour`` (a ``datetime``, ``time``, or
    ``pandas.Timestamp``) in IST.

    The returned ``basis`` is always ``"prior"`` unless you pass a ``shape``
    you calibrated yourself, because the shape is read off published research
    rather than off your fills. A number whose provenance is invisible is how
    the 0.25% constant survived six sessions unexamined.
    """
    table = shape or SPREAD_SHAPE_BY_HOUR
    hour = getattr(moment, "hour", None)
    if hour is None:
        raise TypeError("moment must expose an .hour attribute")

    multiplier = table.get(hour)
    if multiplier is None:
        # Outside the session there is no book to be wide or tight.
        nearest = min(table, key=lambda h: abs(h - hour))
        return {
            "hour": hour,
            "multiplier": table[nearest],
            "basis": "prior",
            "in_session": False,
            "note": (
                f"{hour:02d}:00 is outside the 09:15-15:30 session; used the "
                f"{nearest:02d}:00 figure. No live book exists to measure."
            ),
        }

    if multiplier >= 1.4:
        note = (
            f"{hour:02d}:00 is one of the two widest windows of the day. The "
            f"scorer's opening-range components fire here, so a breakout entry "
            f"pays roughly {multiplier:.2f}x the midday spread -- on a cost "
            f"that already exceeds every edge measured in this project."
        )
    elif multiplier <= 1.05:
        note = (
            f"{hour:02d}:00 sits in the documented cheap window (roughly "
            f"11:00-14:00), where NSE's own microstructure work puts the "
            f"tightest spreads of the session."
        )
    else:
        note = f"{hour:02d}:00 is between the open and the midday trough."

    return {
        "hour": hour,
        "multiplier": multiplier,
        "basis": "prior" if shape is None else "calibrated",
        "in_session": True,
        "note": note,
    }


def calibrate_shape_from_fills(fills: "list[dict] | object") -> dict:
    """Replace the prior with the spread shape your own fills actually show.

    ``fills`` is a sequence of mappings carrying ``hour`` and
    ``half_spread_pct`` (as :func:`cost_model_from_book` records them).

    Returns a shape table normalised so the median hour is 1.0, which is what
    :func:`spread_multiplier_at` expects, plus the sample behind every hour so
    a thin cell is visible rather than averaged into confidence. Hours with
    fewer than ``MIN_FILLS_PER_HOUR`` observations keep the prior and say so.
    """
    MIN_FILLS_PER_HOUR = 10

    buckets: dict[int, list[float]] = {}
    for fill in fills or []:
        try:
            hour = int(fill["hour"])
            value = float(fill["half_spread_pct"])
        except (KeyError, TypeError, ValueError):
            continue
        if value > 0:
            buckets.setdefault(hour, []).append(value)

    if not buckets:
        return {
            "shape": dict(SPREAD_SHAPE_BY_HOUR),
            "basis": "prior",
            "sample": {},
            "note": "No usable fills; the published prior is unchanged.",
        }

    means = {h: sum(v) / len(v) for h, v in buckets.items()
             if len(v) >= MIN_FILLS_PER_HOUR}
    if not means:
        deepest = max(buckets, key=lambda h: len(buckets[h]))
        return {
            "shape": dict(SPREAD_SHAPE_BY_HOUR),
            "basis": "prior",
            "sample": {h: len(v) for h, v in buckets.items()},
            "note": (
                f"No hour reached {MIN_FILLS_PER_HOUR} fills (the deepest was "
                f"{deepest:02d}:00 with {len(buckets[deepest])}). Calibrating "
                f"on that would fit noise, so the prior stands."
            ),
        }

    ordered = sorted(means.values())
    reference = ordered[len(ordered) // 2]
    shape = dict(SPREAD_SHAPE_BY_HOUR)
    calibrated = {}
    for hour, mean in means.items():
        shape[hour] = round(mean / reference, 3) if reference > 0 else 1.0
        calibrated[hour] = len(buckets[hour])

    return {
        "shape": shape,
        "basis": "calibrated",
        "sample": {h: len(v) for h, v in buckets.items()},
        "calibrated_hours": calibrated,
        "note": (
            f"Calibrated {len(calibrated)} of {len(SPREAD_SHAPE_BY_HOUR)} "
            f"session hours from your own fills; the rest keep the prior. "
            f"Normalised so the median calibrated hour is 1.0."
        ),
    }


# --------------------------------------------------------------------------- #
# Wiring the measurement into the cost model that decides every verdict
# --------------------------------------------------------------------------- #

def cost_model_from_book(
    book: BookRead, *, base: OptionsCostModel | None = None,
    moment=None, shape: dict | None = None,
) -> dict:
    """Turn a measured book into the cost model the rest of the app uses.

    This is the call ``OptionsCostModel.with_measured_spread`` was written for
    and never received. It returns the model plus the evidence behind it, so a
    verdict can cite a measurement instead of inheriting a constant.

    When the book is unusable the **base model is returned unchanged**, flagged
    ``measured=False``. A missing book must never read as a cheap one: that is
    the same failure mode as an agent's broken LLM call returning ``PROCEED``.
    """
    model = base or OptionsCostModel()
    half = book.half_spread_pct
    adjustment = spread_multiplier_at(moment, shape=shape) if moment is not None else None

    if not book.is_usable or half is None:
        # No book to measure, so the assumed constant is all there is -- but it
        # can at least be put in the right part of the day. This is the ONLY
        # branch where the time-of-day multiplier is applied: see below for why
        # applying it to a measured spread would be double-counting.
        assumed = model.half_spread_pct
        note = (
            f"No two-sided book, so the modelled {assumed:.4%} per leg still "
            f"applies. This is an assumption, not a measurement -- treat any "
            f"verdict that depends on it as provisional."
        )
        if adjustment and adjustment["in_session"]:
            adjusted = assumed * adjustment["multiplier"]
            model = model.with_measured_spread(adjusted)
            note += (
                f" Scaled to {adjusted:.4%} for the hour "
                f"({adjustment['multiplier']:.2f}x): {adjustment['note']}"
            )
        return {
            "cost_model": model,
            "measured": False,
            "half_spread_pct": model.half_spread_pct,
            "assumed_half_spread_pct": assumed,
            "time_of_day": adjustment,
            "book": book.as_dict(),
            "note": note,
        }

    # The time-of-day multiplier is deliberately NOT applied here. The shape it
    # encodes -- spreads wider at the open and close -- is a statement about
    # what the book typically looks like at this hour. A measured book already
    # *is* what the book looks like at this hour, so multiplying it by the
    # prior would charge the same effect twice. The adjustment is still
    # returned, because the comparison is informative: a measured spread well
    # above the hour's expectation is a contract that is wide for its own
    # reasons rather than for the time of day.
    measured = half
    model = model.with_measured_spread(measured)
    ratio = measured / base.half_spread_pct if (
        base and base.half_spread_pct > 0) else (
        measured / OptionsCostModel().half_spread_pct)

    note = (
        f"Measured {measured:.4%} per leg ({book.spread:.2f} wide on a "
        f"{book.mid:.2f} mid, {book.spread_ticks:.0f} ticks) against the "
        f"{OptionsCostModel().half_spread_pct:.4%} the model assumed -- "
        f"{ratio:.1f}x."
    )
    if ratio >= 2:
        note += (
            " The assumed constant was understating the cost of this contract "
            "by more than half."
        )

    return {
        "cost_model": model,
        "measured": True,
        "half_spread_pct": measured,
        "assumed_half_spread_pct": OptionsCostModel().half_spread_pct,
        "ratio_to_assumed": round(ratio, 2),
        "time_of_day": adjustment,
        "book": book.as_dict(),
        "note": note,
        # What to hand calibrate_shape_from_fills later.
        "fill_record": (
            {"hour": getattr(moment, "hour", None), "half_spread_pct": measured}
            if moment is not None else None
        ),
    }


@dataclass
class Tradeability:
    """Whether a contract can repay the cost of entering it."""

    symbol: str = ""
    verdict: str = "unknown"
    round_trip_spread_pct: float | None = None
    required_move_pct: float | None = None
    expected_move_pct: float | None = None
    fills_at_touch: bool | None = None
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return self.verdict == "tradeable"

    def as_dict(self) -> dict:
        from dataclasses import asdict
        out = asdict(self)
        out["tradeable"] = self.tradeable
        return out


def assess_tradeability(
    book: BookRead, *, quantity: float, expected_move_pct: float | None = None,
    costs: OptionsCostModel | None = None, moment=None,
) -> Tradeability:
    """Can this contract pay for itself? Asked before direction, not after.

    ``expected_move_pct`` is the move you expect **in the premium**, as a
    percentage -- the target, in the units the dashboard already uses (a +25%
    default target is ``25.0``).

    The app has always asked "which way will it go" and then "what does it
    cost". For a Rs 20,000 account buying one indivisible lot, the second
    question settles most trades on its own, so it is asked first here. A
    contract whose round trip costs more than the move you are playing for is
    not a marginal trade; it is a trade with a negative expectancy that no
    directional read can rescue. That is the arithmetic that lost Rs 395 on a
    *correct* direction on 2026-10-21.
    """
    result = Tradeability(symbol=book.symbol)
    model = costs or OptionsCostModel()

    if not book.is_usable:
        result.verdict = "no price"
        result.reasons.append(
            "No two-sided quote. There is no measurable cost of entry, so "
            "there is nothing to assess -- and an unquoted contract is the "
            "one most likely to fill badly."
        )
        return result

    relative = book.relative_spread or 0.0
    round_trip = relative * 100  # in and out, as a percentage of the mid
    result.round_trip_spread_pct = round(round_trip, 3)
    result.expected_move_pct = expected_move_pct

    walk = book.walk("buy", quantity)
    result.fills_at_touch = bool(walk["complete"]) and (
        book.touch_quantity[1] >= quantity)
    result.evidence = {
        "book": book.as_dict(),
        "entry_walk": walk,
        "time_of_day": spread_multiplier_at(moment) if moment is not None else None,
    }

    # Statutory + brokerage, on the mid, so the whole round trip is in view.
    mid = book.mid or 0.0
    statutory_pct = 0.0
    if mid > 0 and quantity > 0:
        bare = replace(model, half_spread_pct=0.0, min_half_spread_ticks=0.0)
        from .ledger.costs import round_trip_cost
        try:
            fees = round_trip_cost(mid, mid, quantity, costs=bare)
            total = fees["total"] if isinstance(fees, dict) else float(fees)
            statutory_pct = total / (mid * quantity) * 100
        except Exception:  # noqa: BLE001 -- cost model shape is not a contract
            statutory_pct = 0.0

    required = round_trip + statutory_pct
    result.required_move_pct = round(required, 3)
    result.evidence["statutory_pct"] = round(statutory_pct, 3)

    if relative >= UNTRADEABLE_SPREAD_PCT:
        result.verdict = "untradeable"
        result.reasons.append(
            f"The quoted spread is {relative:.1%} of the mid "
            f"({book.spread:.2f} on {book.mid:.2f}). A round trip costs "
            f"{round_trip:.1f}% of the premium before a single fee. This is "
            f"not a price, it is the absence of one."
        )
        return result

    if not result.fills_at_touch:
        result.reasons.append(
            f"One lot ({quantity:,.0f}) does not fill at the touch -- the best "
            f"offer shows {book.touch_quantity[1]:,.0f}. The spread above "
            f"understates your real cost, because you will walk the book."
        )

    if relative >= WIDE_SPREAD_PCT:
        result.reasons.append(
            f"Wide book: {relative:.2%} of the mid, "
            f"{book.spread_ticks:.0f} ticks. The cost model's default assumes "
            f"{OptionsCostModel().half_spread_pct * 2:.2%} round trip; this is "
            f"{round_trip / (OptionsCostModel().half_spread_pct * 200):.1f}x that."
        )

    if expected_move_pct is None:
        result.verdict = "cost measured"
        result.reasons.append(
            f"A round trip here needs a {required:.2f}% move in the premium "
            f"just to break even ({round_trip:.2f}% spread + "
            f"{statutory_pct:.2f}% fees). Supply the target to get a verdict."
        )
        return result

    if expected_move_pct <= required:
        result.verdict = "cost exceeds target"
        result.reasons.append(
            f"The target is {expected_move_pct:.1f}% and breakeven is "
            f"{required:.2f}%. You would be risking the stop to collect less "
            f"than the round trip costs. No directional read fixes this."
        )
        return result

    ratio = expected_move_pct / required if required > 0 else float("inf")
    result.verdict = "tradeable"
    result.reasons.append(
        f"Target {expected_move_pct:.1f}% against a {required:.2f}% breakeven "
        f"-- the move is {ratio:.1f}x the round trip."
    )
    if ratio < 3:
        result.reasons.append(
            f"Thin margin: at {ratio:.1f}x, roughly "
            f"{100 / ratio:.0f}% of the target is paid away in costs. The "
            f"measured edge in this project is 0.1%; be certain the target is "
            f"real and not a default."
        )
    return result


def chain_spread_report(rows: "list[dict]") -> dict:
    """Spread across a whole chain, so the liquid strikes are visible at once.

    ``rows`` are mappings with ``symbol`` and either a parsed
    :class:`BookRead` under ``book`` or a raw depth ``payload``.

    The purpose is narrow and useful: the chain builder currently ranks
    candidates by score and open interest, and OI says a strike is *defended*,
    not that it is *cheap to cross*. Those are different questions and the app
    has only ever asked the first.
    """
    assessed, unusable = [], []
    for row in rows or []:
        book = row.get("book")
        if book is None:
            book = parse_book(
                row.get("payload") or {},
                symbol=str(row.get("symbol", "")),
                reference_price=row.get("ltp"),
            )
        if book.is_usable:
            assessed.append(book)
        else:
            unusable.append(book.symbol or "?")

    if not assessed:
        return {
            "contracts": 0,
            "unusable": unusable,
            "note": (
                "No contract in this chain carried a two-sided book, so no "
                "spread could be measured anywhere on it."
            ),
        }

    relatives = sorted(
        (b.relative_spread, b) for b in assessed if b.relative_spread is not None
    )
    median = relatives[len(relatives) // 2][0] if relatives else None
    tightest = relatives[0][1] if relatives else None
    widest = relatives[-1][1] if relatives else None

    return {
        "contracts": len(assessed),
        "unusable": unusable,
        "median_relative_spread_pct": round(median * 100, 3) if median else None,
        "tightest": tightest.as_dict() if tightest else None,
        "widest": widest.as_dict() if widest else None,
        "wide_count": sum(
            1 for r, _ in relatives if r >= WIDE_SPREAD_PCT),
        "untradeable_count": sum(
            1 for r, _ in relatives if r >= UNTRADEABLE_SPREAD_PCT),
        "note": (
            f"{len(assessed)} contracts quoted; median spread "
            f"{median * 100:.2f}% of mid"
            + (f", tightest {tightest.relative_spread * 100:.2f}% "
               f"({tightest.symbol})" if tightest else "")
            + (f", widest {widest.relative_spread * 100:.2f}% "
               f"({widest.symbol})" if widest else "")
            + ". Open interest ranks which strike is defended; this ranks "
              "which one you can afford to enter."
        ),
    }
