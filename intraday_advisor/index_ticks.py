"""Live index prices for the chart, so it stops lagging the market.

The problem this solves
-----------------------
The chart is drawn from Yahoo candles, which are delayed: measured during a
live session, Yahoo's most recent BANKNIFTY bar was stamped 14:35 while the
clock read 14:40, and the option chain (on the broker feed) implied a spot 170
points away from Yahoo's. So the chart was both **stale** and refreshed only
when a snapshot landed, every 10-20 seconds. That reads as "the graph is slow",
and no amount of render optimisation fixes it -- the canvas redraw measured
0.5 ms, about 3% of one core at 60fps. The data was the problem, not the
drawing.

The fix is not more polling. Broker REST endpoints rate-limit hard (a repeated
candle fetch earned "Access denied because of exceeding access rate" within
minutes). Instead the index itself is subscribed on the tick WebSocket already
used for positions, and the latest price is pushed to the browser on a light,
frequent channel separate from the heavy snapshot.

Index tokens are resolved from the broker's instrument master rather than
hardcoded, for the same reason lot sizes are: the published value is right and
a literal in this file is a guess.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("index_ticks")

# The NSE index names as they appear in the instrument master.
INDEX_NAMES = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
}

_TOKENS: dict[str, str | None] = {}


def index_token(index: str) -> str | None:
    """Spot-index token from the instrument master. Cached; never raises."""
    key = (index or "").strip().upper()
    if key in _TOKENS:
        return _TOKENS[key]

    token = None
    try:
        from intraday_advisor.nse_option_chain import _load_instruments

        instruments = _load_instruments().copy()
        instruments.columns = [c.strip().lower() for c in instruments.columns]
        name = INDEX_NAMES.get(key, key)

        # The spot index sits on the NSE segment with no option/future type.
        rows = instruments[
            (instruments["name"].astype(str).str.upper() == name)
            & (instruments["exch_seg"].astype(str).str.upper() == "NSE")
        ]
        if not rows.empty:
            # Prefer an exact symbol match; indices are listed with a suffix.
            exact = rows[rows["symbol"].astype(str).str.upper().isin(
                {name, f"{name}-INDEX", f"{name} INDEX"}
            )]
            pick = exact if not exact.empty else rows
            token = str(pick["token"].iloc[0])
    except Exception as exc:  # noqa: BLE001
        logger.debug("Index token lookup failed for %s: %s", key, exc)

    _TOKENS[key] = token
    return token


# NSE cash/index segment code for the tick WebSocket (options use 2).
NSE_EXCHANGE_TYPE = 1


def watch_indices(indices) -> dict[str, str]:
    """Subscribe to the spot indices. Returns {index: token} for those found."""
    from intraday_advisor import live_ticks

    found: dict[str, str] = {}
    for index in indices:
        token = index_token(index)
        if token:
            found[index.upper()] = token
    if not found:
        return {}

    streamer = live_ticks.ensure_stream()
    if streamer is None:
        return found

    # Indices live on a different exchange segment from options, so they
    # cannot ride the same subscribe call.
    try:
        streamer.subscribe_segment(
            list(found.values()), exchange_type=NSE_EXCHANGE_TYPE
        )
    except AttributeError:
        # Older streamer without segment support: fall back to the default.
        streamer.subscribe(list(found.values()))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Index subscribe failed: %s", exc)
    return found


def latest(indices) -> dict[str, dict]:
    """Latest streamed price per index, for the fast channel to the browser."""
    from intraday_advisor import live_ticks

    out: dict[str, dict] = {}
    for index in indices:
        token = index_token(index)
        if not token:
            continue
        price, age = live_ticks.live_price(token)
        if price:
            out[index.upper()] = {"price": round(price, 2), "age": age}
    return out
