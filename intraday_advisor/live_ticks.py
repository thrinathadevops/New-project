"""Sub-second position monitoring over the Angel tick WebSocket.

What this replaces
------------------
Open positions were valued by a REST call on the dashboard's refresh interval --
10 seconds by default. For a stop alarm that is slow: the whole point of the
alarm is to tell you the level broke, and ten seconds is a long time in a
₹500 premium.

What is actually achievable
---------------------------
**Tick level, not millisecond level.** Measured against the live feed, Angel
pushes roughly one update per second per subscribed instrument. That is the
rate the exchange and the broker provide; no amount of client-side work turns
it into milliseconds.

It also would not help if it did. A 20% stop on a ₹560 premium is a ₹112 move,
which takes seconds to minutes. The things that actually cost you on entry and
exit are the bid-ask spread (₹0.5-2 per unit, every single time) and your own
reaction time. Both are orders of magnitude larger than any latency this layer
could remove. Anyone selling millisecond execution to a retail options buyer is
selling the wrong thing.

So: ~1-second position updates, which is a genuine improvement over 10, and an
honest ceiling.

Design
------
One shared streamer for the process, subscribed only to the contracts actually
held. Everything degrades to REST: if the socket is down, missing or stale, the
caller gets ``None`` and falls back, rather than being handed a price that
stopped updating twenty minutes ago.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("live_ticks")

# A tick older than this is not a live price. Angel pushes ~1/sec for a liquid
# contract, so 15 seconds means something has broken.
STALE_AFTER_SECONDS = 15.0

_LOCK = threading.Lock()
_STATE: dict = {
    "streamer": None,
    "subscribed": set(),
    "started_at": 0.0,
    "last_error": None,
    "enabled": True,
}


def disable() -> None:
    """Turn tick streaming off for this process (tests, or REST-only mode)."""
    with _LOCK:
        _STATE["enabled"] = False


def ensure_stream():
    """Return the shared streamer, starting it once. None if unavailable."""
    with _LOCK:
        if not _STATE["enabled"]:
            return None
        if _STATE["streamer"] is not None:
            return _STATE["streamer"]
        try:
            from intraday_advisor.tick_stream import AngelTickStreamer

            streamer = AngelTickStreamer()
            if not streamer.start():
                _STATE["last_error"] = streamer.status().get("error") or "start failed"
                return None
            _STATE["streamer"] = streamer
            _STATE["started_at"] = time.time()
            logger.info("Tick stream started")
            return streamer
        except Exception as exc:  # noqa: BLE001 - never break the desk
            _STATE["last_error"] = str(exc)
            return None


def watch(tokens: list[str]) -> None:
    """Subscribe to any contracts not already being streamed.

    Only what is actually held: subscribing to the whole chain would flood the
    socket for prices nothing is waiting on.
    """
    tokens = [str(t) for t in tokens if t]
    if not tokens:
        return
    streamer = ensure_stream()
    if streamer is None:
        return

    with _LOCK:
        fresh = [t for t in tokens if t not in _STATE["subscribed"]]
        if not fresh:
            return
        _STATE["subscribed"].update(fresh)
    try:
        streamer.subscribe(fresh)
        logger.info("Streaming %d contract(s): %s", len(fresh), ", ".join(fresh))
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"subscribe failed: {exc}"


def live_price(token: str | None) -> tuple[float | None, float | None]:
    """Latest streamed price and its age in seconds.

    Returns ``(None, None)`` when there is no usable tick, so the caller falls
    back to REST rather than trusting a frozen number.
    """
    if not token:
        return None, None
    streamer = _STATE.get("streamer")
    if streamer is None:
        return None, None
    try:
        entry = streamer.cache.get(str(token))
        if not entry:
            return None, None
        age = time.time() - entry["ts"]
        if age > STALE_AFTER_SECONDS:
            return None, round(age, 1)
        price = entry.get("ltp")
        return (float(price), round(age, 2)) if price and price > 0 else (None, None)
    except Exception:  # noqa: BLE001
        return None, None


def status() -> dict:
    """What the tick layer is doing, for the dashboard header."""
    streamer = _STATE.get("streamer")
    base = {
        "enabled": _STATE["enabled"],
        "connected": False,
        "subscribed": len(_STATE["subscribed"]),
        "cached": 0,
        "error": _STATE.get("last_error"),
        "uptime_seconds": (round(time.time() - _STATE["started_at"])
                           if _STATE["started_at"] else 0),
        "note": (
            "Positions update on every tick (~1/second, the rate the broker "
            "provides). Signals still come from closed candles."
        ),
    }
    if streamer is not None:
        try:
            live = streamer.status()
            base.update({
                "connected": bool(live.get("connected")),
                "cached": int(live.get("cached") or 0),
                "error": live.get("error") or base["error"],
            })
        except Exception:  # noqa: BLE001
            pass
    return base
