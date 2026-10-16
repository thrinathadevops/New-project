"""Angel One live tick streaming for open-position monitoring.

Scope (per the chosen design): stream live premiums only for the option
contracts you are *holding*, so the position monitor shows per-second P&L and
fires the 🚨 stop alarm the instant price crosses it. The scanner stays on its
periodic refresh.

Design notes:
  * The correctness-critical, testable core is :func:`parse_tick` (Angel sends
    LTP in paise, ×100) and :class:`TickCache` (thread-safe latest-tick store).
    These are unit-tested offline.
  * :class:`AngelTickStreamer` wraps ``SmartWebSocketV2`` and runs its blocking
    ``connect()`` on a daemon thread. It is BEST-EFFORT: any failure (no creds,
    no network, library missing, market closed) leaves it in a disabled state
    and callers fall back to REST. It can only be fully validated live, during
    market hours, with a real broker session.
  * Nothing here ever fabricates a price. If there is no live tick, ``get_ltp``
    returns ``None`` and the caller uses the REST premium instead.
"""

from __future__ import annotations

import os
import threading
import time

# Angel WebSocket exchange-type code for NFO (derivatives).
NFO_EXCHANGE_TYPE = 2
# SmartWebSocketV2 mode 3 = SnapQuote (includes open interest).
SNAP_QUOTE_MODE = 3


def parse_tick(tick: dict) -> dict | None:
    """Normalise one Angel tick dict to ``{token, ltp, oi}`` (rupees).

    Angel streams ``last_traded_price`` in **paise** (price × 100). Returns
    None if the message has no token or no usable price.
    """
    if not isinstance(tick, dict):
        return None
    token = tick.get("token") or tick.get("symbolToken")
    if token is None:
        return None
    # Tick feed always carries price in paise under 'last_traded_price'.
    ltp_paise = tick.get("last_traded_price")
    if ltp_paise is None:
        return None
    try:
        ltp = float(ltp_paise) / 100.0
    except (TypeError, ValueError):
        return None
    oi_raw = tick.get("open_interest", tick.get("opnInterest"))
    try:
        oi = float(oi_raw) if oi_raw is not None else None
    except (TypeError, ValueError):
        oi = None
    return {"token": str(token), "ltp": ltp, "oi": oi}


class TickCache:
    """Thread-safe store of the latest tick per token."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}

    def update_from_tick(self, tick: dict) -> bool:
        parsed = parse_tick(tick)
        if parsed is None:
            return False
        with self._lock:
            prev = self._data.get(parsed["token"], {})
            self._data[parsed["token"]] = {
                "ltp": parsed["ltp"],
                # keep last known OI if this tick didn't carry one
                "oi": parsed["oi"] if parsed["oi"] is not None else prev.get("oi"),
                "ts": time.time(),
            }
        return True

    def get(self, token: str) -> dict | None:
        with self._lock:
            v = self._data.get(str(token))
            return dict(v) if v else None

    def get_ltp(self, token: str) -> float | None:
        v = self.get(token)
        return v["ltp"] if v else None

    def age(self, token: str) -> float | None:
        v = self.get(token)
        return (time.time() - v["ts"]) if v else None

    def size(self) -> int:
        with self._lock:
            return len(self._data)


class AngelTickStreamer:
    """Best-effort live tick stream. Degrades to disabled on any failure."""

    def __init__(self) -> None:
        self.cache = TickCache()
        self._ws = None
        self._thread: threading.Thread | None = None
        self._subscribed: set[str] = set()
        self._connected = False
        # connect() returns before the socket handshake completes, so a
        # subscribe sent immediately after start() hits a socket that is not
        # open yet ("Error occurred during subscribe: socket is already
        # closed"). Tokens are queued until _on_open flushes them.
        self._open = False
        self._pending: set[str] = set()
        # Queued per exchange segment, because indices and options differ.
        self._pending_segments: dict[int, set] = {}
        self._error: str | None = None

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> bool:
        if self._connected:
            return True
        try:
            import pyotp
            from SmartApi import SmartConnect
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        except Exception as exc:  # noqa: BLE001
            self._error = f"SmartAPI not installed: {exc}"
            return False

        api_key = os.getenv("ANGEL_ONE_API_KEY", "")
        client_code = os.getenv("ANGEL_ONE_CLIENT_CODE", "")
        pin = os.getenv("ANGEL_ONE_PIN", "")
        totp_secret = os.getenv("ANGEL_ONE_TOTP_SECRET", "")
        if not all([api_key, client_code, pin, totp_secret]):
            self._error = "Angel One credentials not configured."
            return False

        try:
            smart = SmartConnect(api_key)
            session = smart.generateSession(client_code, pin, pyotp.TOTP(totp_secret).now())
            jwt = session["data"]["jwtToken"]
            feed_token = smart.getfeedToken()
        except Exception as exc:  # noqa: BLE001
            self._error = f"Angel One login failed: {exc}"
            return False

        try:
            self._ws = SmartWebSocketV2(jwt, api_key, client_code, feed_token)
            self._ws.on_open = self._on_open
            self._ws.on_data = self._on_data
            self._ws.on_error = self._on_error
            self._ws.on_close = self._on_close
            self._thread = threading.Thread(target=self._ws.connect, daemon=True)
            self._thread.start()
            self._connected = True
            return True
        except Exception as exc:  # noqa: BLE001
            self._error = f"WebSocket start failed: {exc}"
            self._connected = False
            return False

    # -- callbacks -------------------------------------------------------- #
    def _on_open(self, wsapp):
        self._open = True
        queued = sorted(self._pending | self._subscribed)
        self._pending.clear()
        if queued:
            self._send_subscribe(queued)
        # Flush anything waiting on a non-default segment.
        for exchange_type, tokens in list(self._pending_segments.items()):
            if tokens:
                self._send_subscribe(sorted(tokens), exchange_type=exchange_type)
        self._pending_segments.clear()

    def _on_data(self, wsapp, message):
        if isinstance(message, dict):
            self.cache.update_from_tick(message)

    def _on_error(self, wsapp, error):
        self._error = str(error)

    def _on_close(self, wsapp):
        self._connected = False
        self._open = False
        # Re-subscribe on the next open rather than losing the watch list.
        self._pending.update(self._subscribed)

    # -- subscription ----------------------------------------------------- #
    def subscribe(self, tokens: list[str]) -> None:
        tokens = [str(t) for t in tokens if t]
        new = [t for t in tokens if t not in self._subscribed]
        self._subscribed.update(tokens)
        if not new:
            return
        if self._ws and self._open:
            self._send_subscribe(new)
        else:
            # Not open yet: queue and let _on_open send it.
            self._pending.update(new)

    def _send_subscribe(self, tokens: list[str],
                        exchange_type: int = NFO_EXCHANGE_TYPE) -> None:
        try:
            self._ws.subscribe("posmon", SNAP_QUOTE_MODE,
                               [{"exchangeType": exchange_type,
                                 "tokens": [str(t) for t in tokens]}])
        except Exception as exc:  # noqa: BLE001
            self._error = f"subscribe failed: {exc}"

    def subscribe_segment(self, tokens: list[str], *, exchange_type: int) -> None:
        """Subscribe on a specific exchange segment.

        Spot indices sit on the NSE segment (1) while options are on NFO (2),
        so they cannot share a subscribe call -- the wrong segment silently
        returns no ticks.
        """
        tokens = [str(t) for t in tokens if t]
        new = [t for t in tokens if t not in self._subscribed]
        self._subscribed.update(tokens)
        if not new:
            return
        if self._ws and self._open:
            self._send_subscribe(new, exchange_type=exchange_type)
        else:
            self._pending_segments.setdefault(exchange_type, set()).update(new)

    # -- reads ------------------------------------------------------------ #
    def get_ltp(self, token: str) -> float | None:
        return self.cache.get_ltp(str(token))

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "socket_open": self._open,
            "pending": len(self._pending),
            "error": self._error,
            "subscribed": len(self._subscribed),
            "cached": self.cache.size(),
        }


def resolve_option_token(index: str, strike: float, opt_type: str) -> str | None:
    """Best-effort NFO token for (index, strike, CE/PE) at the nearest expiry.

    Uses the Angel instrument master. Returns None if it can't be resolved
    (caller then keeps using REST). Never raises.
    """
    try:
        import pandas as pd
        from intraday_advisor.nse_option_chain import _load_instruments
        inst = _load_instruments().copy()
        inst.columns = [c.strip().lower() for c in inst.columns]
        name = index.upper()
        opt = opt_type.upper()
        sym = inst.get("symbol", "").astype(str).str.upper()
        mask = (
            (inst.get("name", "").astype(str).str.upper() == name)
            & (inst.get("exch_seg", "").astype(str).str.upper() == "NFO")
            & (inst.get("instrumenttype", "").astype(str).str.upper() == "OPTIDX")
            & (sym.str.endswith(opt))
        )
        cand = inst[mask].copy()
        if cand.empty:
            return None
        strike_num = pd.to_numeric(cand["strike"], errors="coerce")
        # master strike is usually in paise (×100); accept either scale
        cand = cand[(strike_num == float(strike) * 100) | (strike_num == float(strike))]
        if cand.empty:
            return None
        cand["_exp"] = pd.to_datetime(cand.get("expiry"), errors="coerce", format="%d%b%Y")
        today = pd.Timestamp.now().normalize()
        future = cand[cand["_exp"] >= today].sort_values("_exp")
        pick = future.iloc[0] if not future.empty else cand.sort_values("_exp").iloc[-1]
        token = pick.get("token") or pick.get("symboltoken")
        return str(token) if token is not None else None
    except Exception:
        return None
