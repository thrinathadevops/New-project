"""Market-data feeds for the real-time engine.

A *feed* is anything that can return recent OHLCV candles for a symbol. The key
property each feed exposes is :attr:`is_synthetic`: the real-time server uses it
to guarantee that random/sample data can never be broadcast as an actionable
live trade signal.

Phase 1 wires the Angel One REST candle endpoint as the real feed. Phase 2 will
add a tick-streaming feed (Angel One ``SmartWebSocketV2``) that aggregates live
ticks into rolling candles for true sub-minute analysis.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

from intraday_advisor.data import generate_sample_ohlcv


class DataFeedError(RuntimeError):
    """Raised when a real feed cannot return usable candles."""


class Feed(Protocol):
    name: str
    is_synthetic: bool

    def get_candles(self, symbol: str, interval: str) -> pd.DataFrame: ...


@dataclass
class SyntheticFeed:
    """Deterministic random-walk data. NEVER actionable — demos and tests only."""

    name: str = "synthetic"
    is_synthetic: bool = True

    def get_candles(self, symbol: str, interval: str = "5m") -> pd.DataFrame:
        # Stable per-symbol seed so a symbol looks consistent across refreshes.
        seed = abs(hash(symbol)) % 10_000
        return generate_sample_ohlcv(seed=seed)


@dataclass
class AngelOneFeed:
    """Real NSE candles via the Angel One SmartAPI REST endpoint.

    Symbol→token lookups are cached because they are slow and rate-limited.
    A single client is reused so we authenticate once per process.
    """

    name: str = "angelone"
    is_synthetic: bool = False
    _client: object | None = field(default=None, repr=False)
    _token_cache: dict[str, str] = field(default_factory=dict, repr=False)

    _ANGEL_INTERVALS = {
        "1m": "ONE_MINUTE", "3m": "THREE_MINUTE", "5m": "FIVE_MINUTE",
        "10m": "TEN_MINUTE", "15m": "FIFTEEN_MINUTE", "30m": "THIRTY_MINUTE",
        "1h": "ONE_HOUR", "60m": "ONE_HOUR", "1d": "ONE_DAY",
    }

    def _ensure_client(self):
        if self._client is None:
            try:
                from intraday_advisor.angel_one import AngelOneClient
            except Exception as exc:  # pragma: no cover - import guard
                raise DataFeedError(f"Angel One adapter unavailable: {exc}") from exc
            try:
                self._client = AngelOneClient()
                self._client.login()
            except Exception as exc:
                raise DataFeedError(f"Angel One login failed: {exc}") from exc
        return self._client

    def _token(self, symbol: str) -> str:
        if symbol not in self._token_cache:
            client = self._ensure_client()
            try:
                self._token_cache[symbol] = client.search_symbol_token(symbol)
            except Exception as exc:
                raise DataFeedError(f"No Angel One token for {symbol}: {exc}") from exc
        return self._token_cache[symbol]

    def get_candles(self, symbol: str, interval: str = "5m") -> pd.DataFrame:
        client = self._ensure_client()
        token = self._token(symbol)
        ao_interval = self._ANGEL_INTERVALS.get(interval, interval if interval in self._ANGEL_INTERVALS.values() else "FIVE_MINUTE")
        try:
            df = client.get_candles(token, interval=ao_interval)
        except Exception as exc:
            raise DataFeedError(f"Angel One candle fetch failed for {symbol}: {exc}") from exc
        if df is None or df.empty:
            raise DataFeedError(f"Angel One returned no candles for {symbol}")
        return df


def resolve_feed(name: str | None = None) -> Feed:
    """Build a feed from an explicit name or the ``REALTIME_FEED`` env var.

    Defaults to the synthetic feed so that running the server without
    configuration produces clearly-labelled demo data, never silent fakes.
    """
    choice = (name or os.getenv("REALTIME_FEED", "synthetic")).strip().lower()
    if choice in ("angel", "angelone", "angel_one", "smartapi"):
        return AngelOneFeed()
    if choice in ("synthetic", "demo", "sample"):
        return SyntheticFeed()
    raise ValueError(f"Unknown feed '{choice}'. Use 'angelone' or 'synthetic'.")
