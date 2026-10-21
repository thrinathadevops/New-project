"""The single analysis engine. One snapshot, one score, one source of truth.

Why this exists
---------------
The project had grown six surfaces -- ``app.py``, ``trading_dashboard.py``,
``options_dashboard.py``, ``options_scanner_dashboard.py``,
``realtime_server.py``, ``serve.py`` -- and four different ways of scoring the
same setup: ``app.confluence_score``, ``trading_dashboard.calculate_combined_score``,
the ad-hoc confidence arithmetic formerly inside ``index_options``, and the
``scoring`` module that was supposed to have replaced all of them. Four answers
to one question, and no way to know which to believe.

Everything now funnels through :meth:`Engine.snapshot`, which returns one
complete picture of the market: per-index signals scored by
:func:`intraday_advisor.scoring.score_option_signal`, sized against the capital
actually available, plus positions, risk state, ledger coverage and candles for
the chart. Any UI is a renderer of that snapshot and holds no analysis logic.

Events
------
The engine is stateful on purpose. It keeps the previous snapshot and diffs
against it, emitting :class:`Event` records when something changes that you
would want to be told about: a new signal, a direction flip, a target or stop
being touched, the risk governor halting. That diff is what makes the dashboard
able to notify rather than merely display -- a screen you have to stare at to
notice a stop being hit is not a monitoring tool.

Repaint discipline
------------------
Signals are computed from the last **closed** candle only. The chart is sent
the forming candle *separately*, flagged as forming, so the display can look
live without the decision being made on a bar that is still moving. Those two
requirements conflict in every naive implementation; keeping them in separate
fields is the resolution.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from intraday_advisor.data import drop_forming_candle
from intraday_advisor.index_options import (
    DEFAULT_STOP_PCT,
    DEFAULT_TARGET_PCT,
    INDEX_CONFIG,
    analyze_index_option,
    get_option_premium,
)
from intraday_advisor.indicators import add_indicators
from intraday_advisor.ledger import (
    ENGINE_VERSION,
    fitted_cost_model,
    ledger_stats,
    lot_size_for,
    plan_position,
    record_index_signal,
)
from intraday_advisor.ledger.report import expectancy_report, score_band_report
from intraday_advisor.ledger.schema import DEFAULT_LEDGER_PATH
from intraday_advisor.positions import (
    closed_trades,
    daily_pnl_series,
    evaluate_live,
    load_open_positions,
    performance_summary,
    session_summary,
)
from intraday_advisor.readings import GLOSSARY, build_playbook, build_readings
from intraday_advisor.agents import agent_status, build_agents, review_signal
from intraday_advisor.agents.rules import RuleAgent
from intraday_advisor.market_calendar import (
    DEFAULT_MARKET,
    get_market,
    list_markets,
    session_state,
)
from intraday_advisor.premarket import brief_notification, build_brief
from intraday_advisor import index_ticks, live_ticks
from intraday_advisor.candidates import build_candidates
from intraday_advisor.stock_screener import scan_async

logger = logging.getLogger("engine")

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
SQUARE_OFF = dtime(15, 15)

# How many candles of history the chart receives.
CHART_BARS = 120
# A score moving by at least this much between snapshots is worth announcing.
SCORE_MOVE_THRESHOLD = 12


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open(when: datetime | None = None) -> bool:
    moment = when or now_ist()
    if moment.weekday() >= 5:
        return False
    return MARKET_OPEN <= moment.time() <= MARKET_CLOSE


def minutes_to_square_off(when: datetime | None = None) -> int | None:
    """Minutes until intraday square-off, or None outside market hours."""
    moment = when or now_ist()
    if not is_market_open(moment):
        return None
    deadline = moment.replace(hour=SQUARE_OFF.hour, minute=SQUARE_OFF.minute,
                              second=0, microsecond=0)
    return max(0, int((deadline - moment).total_seconds() // 60))


@dataclass
class Event:
    """Something the operator should be told about, now."""

    kind: str          # SIGNAL_NEW / SIGNAL_FLIP / SIGNAL_CLEARED / SCORE_MOVE /
                       # POSITION_TARGET / POSITION_STOP / POSITION_WARNING /
                       # RISK_HALT / SQUARE_OFF / DATA_ESTIMATED
    severity: str      # info / good / warn / danger
    title: str
    message: str
    subject: str = ""  # index or position id this concerns
    ts: str = field(default_factory=lambda: now_ist().isoformat(timespec="seconds"))
    # Events the operator must acknowledge stay on screen until dismissed.
    sticky: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Engine:
    """Stateful analysis engine. Build once, call :meth:`snapshot` repeatedly."""

    available_capital: float = 20_000.0
    max_capital_fraction: float = 1.0
    target_pct: float = DEFAULT_TARGET_PCT
    stop_pct: float = DEFAULT_STOP_PCT
    interval: str = "5m"
    record: bool = True
    #: Which exchange's clock drives the session state and the LLM gate.
    market: str = DEFAULT_MARKET
    #: Expiry to quote against, per index. None = nearest unexpired.
    expiries: dict = field(default_factory=dict)
    #: How many alternative contracts to offer per index.
    candidate_count: int = 8
    #: Equity watchlist: nifty50 / fno / banknifty / all / off
    stock_universe: str = "nifty50"
    stock_limit: int = 30
    db_path: str | Path = DEFAULT_LEDGER_PATH
    indices: tuple[str, ...] = tuple(INDEX_CONFIG)

    # --- internal state for diffing ---
    _previous: dict = field(default_factory=dict, repr=False)
    _announced: set = field(default_factory=set, repr=False)
    _chart_cache: dict = field(default_factory=dict, repr=False)
    _chart_fetched_at: dict = field(default_factory=dict, repr=False)
    _regime_cache: dict = field(default_factory=dict, repr=False)
    _agent_cache: dict = field(default_factory=dict, repr=False)
    _regime_refreshing: bool = field(default=False, repr=False)
    _announced_briefs: set = field(default_factory=set, repr=False)
    # Carried between snapshots so the fast tick clock can re-price positions
    # without repeating a snapshot's network work. See tick_positions().
    _last_premiums: dict = field(default_factory=dict, repr=False)
    _last_directions: dict = field(default_factory=dict, repr=False)
    _regime_fetched_at: float = field(default=0.0, repr=False)
    _agents: Any = field(default=None, repr=False)
    _costs: Any = None

    # The regime pulls four timeframes, India VIX, four global indices and a
    # breadth basket. That is far too slow to repeat every refresh, and none of
    # it changes meaningfully inside a few minutes.
    regime_ttl: float = 300.0
    with_breadth: bool = True

    def __post_init__(self) -> None:
        if self._costs is None:
            self._costs = fitted_cost_model(self.db_path)
        if self._agents is None:
            self._agents = build_agents()

    # ------------------------------------------------------------------ config
    def configure(self, **changes) -> None:
        """Apply settings from the UI. Unknown keys are ignored deliberately,
        so a stale client cannot crash the server with an old field name."""
        if changes.get("expiries"):
            # Per-index, so NIFTY can sit on a weekly while BANKNIFTY is
            # necessarily on the monthly.
            self.expiries.update({
                str(k).upper(): v for k, v in changes["expiries"].items() if v
            })
        for key in ("available_capital", "max_capital_fraction", "target_pct",
                    "stop_pct", "record", "market", "candidate_count",
                    "stock_universe", "stock_limit"):
            if key in changes and changes[key] is not None:
                setattr(self, key, changes[key])

    # ------------------------------------------------------------------ charts
    def _candles(self, index: str, *, max_age: float = 20.0) -> pd.DataFrame | None:
        """Underlying candles, cached briefly so a fast refresh loop does not
        hammer the data source."""
        fetched = self._chart_fetched_at.get(index, 0.0)
        if index in self._chart_cache and (time.time() - fetched) < max_age:
            return self._chart_cache[index]
        try:
            from intraday_advisor.data import fetch_yahoo_ohlcv
            frame = fetch_yahoo_ohlcv(
                INDEX_CONFIG[index]["yahoo"], period="5d", interval=self.interval
            )
            frame = add_indicators(frame)
            self._chart_cache[index] = frame
            self._chart_fetched_at[index] = time.time()
            return frame
        except Exception as exc:  # noqa: BLE001 - a chart must never break the engine
            logger.warning("Candle fetch failed for %s: %s", index, exc)
            return self._chart_cache.get(index)

    @staticmethod
    def _chart_payload(frame: pd.DataFrame | None, interval: str) -> dict:
        """Closed candles plus the forming one, kept separate.

        The forming candle is what makes the chart feel live; keeping it out of
        ``closed`` is what stops a decision being made on a moving bar.
        """
        if frame is None or frame.empty:
            return {"closed": [], "forming": None}

        closed = drop_forming_candle(frame, interval)
        forming = None
        if closed is not None and len(closed) < len(frame):
            row = frame.iloc[-1]
            forming = {
                "time": str(frame.index[-1]),
                "open": float(row["Open"]), "high": float(row["High"]),
                "low": float(row["Low"]), "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0) or 0),
            }

        tail = (closed if closed is not None else frame).tail(CHART_BARS)
        bars = [
            {
                "time": str(stamp),
                "open": float(row["Open"]), "high": float(row["High"]),
                "low": float(row["Low"]), "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0) or 0),
                "vwap": _safe(row.get("VWAP")),
                "ema9": _safe(row.get("EMA9")),
                "ema21": _safe(row.get("EMA21")),
            }
            for stamp, row in tail.iterrows()
        ]
        return {"closed": bars, "forming": forming}

    # ----------------------------------------------------------------- signals
    def _analyse_index(self, index: str) -> dict:
        """One index: signal, score breakdown, sized plan, chart."""
        candles = self._candles(index)
        try:
            signal = analyze_index_option(
                index, candles=candles, interval=self.interval,
                target_pct=self.target_pct, stop_pct=self.stop_pct,
                expiry=self.expiries.get(index.upper()),
            )
        except Exception as exc:  # noqa: BLE001 - one index must not kill the snapshot
            logger.warning("Analysis failed for %s: %s", index, exc)
            return {
                "index": index, "direction": "WAIT", "score": 0, "error": str(exc),
                "warnings": [f"Analysis failed: {exc}"], "reasons": [],
                "chart": self._chart_payload(candles, self.interval),
                "is_live": False, "plan": None, "score_components": {},
            }

        view = signal.as_dict()
        view["score"] = view.pop("confidence", 0)
        view["chart"] = self._chart_payload(candles, self.interval)

        # --- size it against capital actually on hand ---
        view["plan"] = None
        view["lot_size"] = None
        if signal.direction in ("CALL", "PUT") and signal.entry_premium > 0:
            try:
                lot_size = lot_size_for(index)
                plan = plan_position(
                    available_capital=self.available_capital,
                    entry_premium=signal.entry_premium,
                    stop_premium=signal.stop_premium,
                    target_premium=signal.target_premium,
                    lot_size=lot_size,
                    max_capital_fraction=self.max_capital_fraction,
                    costs=self._costs,
                )
                view["plan"] = plan.as_dict()
                view["lot_size"] = lot_size
            except ValueError as exc:
                view["warnings"] = list(view.get("warnings", [])) + [f"Sizing: {exc}"]

        # --- alternative contracts, so one ATM strike is not the only
        # option offered. Each is priced and sized at the real capital. ---
        view["candidates"] = []
        try:
            from intraday_advisor.index_options import INDEX_CONFIG
            from intraday_advisor.nse_option_chain import suggest_option_trade

            chain = suggest_option_trade(
                index, spot=signal.spot or 0,
                expiry=self.expiries.get(index.upper()),
            )
            view["candidates"] = build_candidates(
                index, chain, direction=signal.direction,
                available_capital=self.available_capital,
                target_pct=self.target_pct, stop_pct=self.stop_pct,
                lot_size=view.get("lot_size"),
                step=float(INDEX_CONFIG[index]["step"]),
                costs=self._costs, limit=self.candidate_count,
                max_capital_fraction=self.max_capital_fraction,
            )
        except Exception as exc:  # noqa: BLE001 - alternatives are a bonus
            logger.warning("Candidates failed for %s: %s", index, exc)

        # --- plain-language interpretation of the same features that were
        # scored, so a number on screen is never unexplained ---
        view["readings"] = build_readings(view)
        view["playbook"] = build_playbook(view)

        # --- record for grading ---
        view["recorded"] = False
        if self.record and signal.direction in ("CALL", "PUT") and signal.bar_time:
            try:
                token = _option_token(index, signal.strike, signal.option_type)
                signal_id = record_index_signal(
                    signal,
                    bar_time=signal.bar_time,
                    interval=self.interval,
                    lot_size=view.get("lot_size"),
                    option_token=token,
                    features=signal.features,
                    score_components=signal.score_components,
                    available_capital=self.available_capital,
                    is_synthetic=not signal.is_live,
                    engine_version=ENGINE_VERSION,
                    db_path=self.db_path,
                )
                view["recorded"] = signal_id is not None
                view["signal_id"] = signal_id
                view["option_token"] = token
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ledger write failed for %s: %s", index, exc)
                view["warnings"] = list(view.get("warnings", [])) + [
                    f"Not recorded: {exc}"
                ]
        return view

    # ---------------------------------------------------------------- stocks
    def _stocks(self) -> dict:
        """The intraday equity watchlist, scanned off the request path.

        A full scan is ~0.9s per symbol and pandas holds the GIL through most
        of it, so 50 names take the better part of a minute. It therefore runs
        on a worker thread and the last completed result is served at once --
        a snapshot never waits for it.
        """
        if (self.stock_universe or "off").lower() in ("off", "none", ""):
            return {"rows": [], "disabled": True,
                    "note": "Equity watchlist is off."}
        try:
            return scan_async(
                self.stock_universe,
                capital=self.available_capital,
                risk_pct=0.02,
                interval=self.interval,
                limit=self.stock_limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stock scan failed: %s", exc)
            return {"rows": [], "error": str(exc)[:200]}

    # -------------------------------------------------------------- premarket
    def _premarket(self, market_session) -> dict:
        """Today's news brief, built once per trading day.

        Cached by (market, date) inside :mod:`premarket`, so a ten-second
        refresh loop does not re-ask the same question all session. Never
        raises: a missing brief must not take the desk down.
        """
        try:
            brief = build_brief(self.market)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pre-market brief failed: %s", exc)
            return {"lean": "UNCLEAR", "degraded": True,
                    "note": f"Brief unavailable: {exc}", "drivers": [],
                    "watch": [], "caveats": [], "top_headlines": []}

        payload = brief.as_dict()
        # Announce it once per day, when it first carries a view.
        key = f"premarket:{brief.market}:{brief.session_date}"
        if key not in self._announced_briefs and not brief.degraded:
            note = brief_notification(brief)
            if note:
                self._announced_briefs.add(key)
                payload["_notify"] = note
        return payload

    # ------------------------------------------------------------------ regime
    def _regime(self, index: str) -> dict:
        """Market-wide context: cached, and refreshed WITHOUT blocking.

        Building it costs ~20 seconds of network (four timeframes, VIX, four
        global indices, a breadth basket). Rebuilding it inline would stall one
        snapshot in every five minutes for 20 seconds -- during market hours
        that is a visible freeze exactly when the screen matters. So a stale
        regime is served immediately while a daemon thread refreshes it.

        Stale market context is a far smaller problem than a frozen dashboard:
        it moves slowly by construction, which is why it is cached at all.
        """
        now = time.time()
        cached = self._regime_cache.get(index)
        fresh = (now - self._regime_fetched_at) < self.regime_ttl

        if cached and fresh:
            return cached
        if cached and not fresh:
            self._refresh_regime_async()
            stale = dict(cached)
            stale["stale"] = True
            return stale
        # Nothing cached at all: this first build has to block.
        return self._build_regime(index)

    def _refresh_regime_async(self) -> None:
        """Rebuild every index's regime on a daemon thread, at most one at a time."""
        if self._regime_refreshing:
            return
        self._regime_refreshing = True

        def worker() -> None:
            try:
                for index in self.indices:
                    self._regime_cache[index] = self._build_regime(index, store=False)
                self._regime_fetched_at = time.time()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Background regime refresh failed: %s", exc)
            finally:
                self._regime_refreshing = False

        threading.Thread(target=worker, daemon=True,
                         name="regime-refresh").start()

    def _build_regime(self, index: str, *, store: bool = True) -> dict:
        """The blocking build. Never raises."""
        try:
            from intraday_advisor.regime import analyse_market
            result = analyse_market(index, with_breadth=self.with_breadth).as_dict()
        except Exception as exc:  # noqa: BLE001 - context must never break the desk
            logger.warning("Regime analysis failed for %s: %s", index, exc)
            result = {
                "index": index, "regime": "UNKNOWN", "bias": "NEUTRAL",
                "conviction": 0, "alignment": 0.0, "headline": f"{index}: no read",
                "timeframes": [], "volatility": {}, "breadth": {}, "levels": {},
                "global_cues": {}, "reasons": [], "invalidation": [],
                "warnings": [f"Market context unavailable: {exc}"],
            }
        if store:
            self._regime_cache[index] = result
            self._regime_fetched_at = time.time()
        return result

    # --------------------------------------------------------------- positions
    def _positions(self, signals: list[dict]) -> list[dict]:
        direction_by_index = {s["index"]: s.get("direction", "WAIT") for s in signals}
        spot_by_index = {s["index"]: s.get("spot", 0) for s in signals}

        positions = load_open_positions(db_path=self.db_path)

        # Stream only the contracts actually held: ~1-second updates instead of
        # waiting for the next 10-second refresh, which is the difference that
        # matters for a stop alarm.
        live_ticks.watch([p.option_token for p in positions if p.option_token])

        self._last_directions = direction_by_index

        out = []
        for position in positions:
            premium, source = None, "unavailable"

            tick_price, tick_age = live_ticks.live_price(position.option_token)
            if tick_price:
                premium = tick_price
                source = f"live tick ({tick_age:.0f}s ago)"
            else:
                try:
                    premium, source = get_option_premium(
                        position.index_name, position.strike, position.option_type,
                        spot=spot_by_index.get(position.index_name, 0) or 0,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Premium fetch failed for %s: %s",
                                 position.position_id, exc)

            # Kept so the tick clock can fall back to the last known price
            # instead of blanking a row it has no fresh tick for.
            if premium and premium > 0:
                self._last_premiums[position.position_id] = (premium, source)

            out.append(
                evaluate_live(
                    position,
                    current_premium=premium if premium and premium > 0 else None,
                    premium_source=source,
                    underlying_direction=direction_by_index.get(
                        position.index_name, "WAIT"
                    ),
                    costs=self._costs,
                ).as_dict()
            )
        return out

    # ---------------------------------------------------- positions, tick clock
    def tick_positions(self) -> dict:
        """Re-price open positions from ticks alone, for the fast channel.

        Why this exists
        ---------------
        A snapshot already values positions from the tick cache, but a snapshot
        only lands every 10-20 seconds, so the P&L you read was a live price
        that had gone cold on screen. The chart was given a 1.5s tick channel;
        the positions table is the panel where a stale number actually costs
        money, so it rides the same clock.

        What it deliberately does **not** do
        ------------------------------------
        * **No REST.** A premium fetch every 1.5s would hit the broker's rate
          limiter within minutes (measured: repeated candle fetches earned
          "Access denied because of exceeding access rate"). Ticks only.
        * **No status logic of its own.** It calls the same
          :func:`evaluate_live` the snapshot calls, so the stop alarm cannot
          mean one thing on the fast path and another on the slow one. A second
          copy of that arithmetic in JavaScript was the alternative, and two
          copies drift.
        * **No writes.** Reading the ledger is cheap and local; recording stays
          with the snapshot and with explicit operator actions.

        A row with no fresh tick keeps the last price the snapshot obtained,
        still labelled with where that price came from, rather than collapsing
        to NO_DATA and flickering.
        """
        positions = load_open_positions(db_path=self.db_path)
        if not positions:
            # Reported explicitly: an empty list is a real state (all closed),
            # and the browser has to clear the table rather than hold old rows.
            return {"positions": [], "events": [], "priced": 0, "total": 0}

        live_ticks.watch([p.option_token for p in positions if p.option_token])

        rows, priced = [], 0
        for position in positions:
            tick_price, tick_age = live_ticks.live_price(position.option_token)
            if tick_price:
                premium = tick_price
                source = f"live tick ({tick_age:.0f}s ago)"
                priced += 1
            else:
                premium, source = self._last_premiums.get(
                    position.position_id, (None, "unavailable")
                )
            rows.append(
                evaluate_live(
                    position,
                    current_premium=premium if premium and premium > 0 else None,
                    premium_source=source,
                    underlying_direction=self._last_directions.get(
                        position.index_name, "WAIT"
                    ),
                    costs=self._costs,
                ).as_dict()
            )

        return {
            "positions": rows,
            # Alarms go through the same dedupe set as the snapshot's, so a
            # stop found on the tick clock is announced once, by whichever
            # path saw it first -- which is the point of running this often.
            "events": [e.as_dict() for e in self._position_events(rows)],
            "priced": priced,
            "total": len(rows),
        }

    def _position_events(self, positions: list[dict]) -> list[Event]:
        """Target/stop/warning alarms for already-evaluated position rows."""
        events: list[Event] = []
        for position in positions:
            if position["status"] not in ("TARGET_HIT", "STOP_HIT", "WARNING"):
                continue
            key = f"{position['position_id']}:{position['status']}"
            if key in self._announced:
                continue
            self._announced.add(key)
            events.append(Event(
                f"POSITION_{position['status'].replace('_HIT', '')}",
                {"TARGET_HIT": "good", "STOP_HIT": "danger",
                 "WARNING": "warn"}[position["status"]],
                f"{position['index_name']} {position['strike']:g} "
                f"{position['option_type']} — {position['action']}",
                position["message"],
                subject=position["position_id"],
                sticky=position["status"] in ("STOP_HIT", "TARGET_HIT"),
            ))
        return events

    # ------------------------------------------------------------------ events
    def _detect_events(
        self, signals: list[dict], positions: list[dict], mode: str
    ) -> list[Event]:
        events: list[Event] = []

        for signal in signals:
            index = signal["index"]
            direction = signal.get("direction", "WAIT")
            score = int(signal.get("score") or 0)
            previous = self._previous.get(index)

            if previous is None:
                continue  # first snapshot: nothing to compare, announce nothing
            old_direction = previous.get("direction", "WAIT")
            old_score = int(previous.get("score") or 0)

            if direction != old_direction:
                if old_direction == "WAIT" and direction in ("CALL", "PUT"):
                    plan = signal.get("plan") or {}
                    risk = plan.get("risk_pct_of_capital")
                    detail = (
                        f" Risk {risk * 100:.1f}% of capital."
                        if isinstance(risk, (int, float)) else ""
                    )
                    events.append(Event(
                        "SIGNAL_NEW", "good" if direction == "CALL" else "warn",
                        f"{index} → {direction}",
                        f"{signal.get('strike')} {signal.get('option_type')} "
                        f"at ₹{signal.get('entry_premium', 0):.2f}, score {score}."
                        + detail,
                        subject=index,
                    ))
                elif direction in ("CALL", "PUT") and old_direction in ("CALL", "PUT"):
                    events.append(Event(
                        "SIGNAL_FLIP", "danger",
                        f"{index} flipped {old_direction} → {direction}",
                        "The read reversed. Any open position on the old side is "
                        "now against the signal.",
                        subject=index, sticky=True,
                    ))
                elif direction == "WAIT":
                    events.append(Event(
                        "SIGNAL_CLEARED", "info", f"{index} → WAIT",
                        f"The {old_direction} setup is gone. No clean edge now.",
                        subject=index,
                    ))
            elif abs(score - old_score) >= SCORE_MOVE_THRESHOLD:
                rising = score > old_score
                events.append(Event(
                    "SCORE_MOVE", "good" if rising else "warn",
                    f"{index} score {old_score} → {score}",
                    "Confluence strengthening." if rising else "Confluence weakening.",
                    subject=index,
                ))

        # Same helper the tick clock uses, sharing the same dedupe set: an
        # alarm is announced once by whichever path reached it first.
        events.extend(self._position_events(positions))

        remaining = minutes_to_square_off()
        if remaining is not None and remaining <= 15 and positions:
            if "square_off" not in self._announced:
                self._announced.add("square_off")
                events.append(Event(
                    "SQUARE_OFF", "warn", f"{remaining} min to square-off",
                    f"{len(positions)} position(s) open. Intraday positions are "
                    f"closed at 15:15 whether you act or not.",
                    sticky=True,
                ))

        if mode != "LIVE" and "estimated" not in self._announced:
            self._announced.add("estimated")
            events.append(Event(
                "DATA_ESTIMATED", "warn", "Estimated data",
                "The broker feed is not live, so premiums are modelled "
                "estimates. Nothing here is actionable and nothing enters the "
                "performance statistics.",
                sticky=True,
            ))

        self._previous = {
            s["index"]: {"direction": s.get("direction"), "score": s.get("score")}
            for s in signals
        }
        return events

    # ---------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        """One complete picture of the market. The only thing any UI renders."""
        started = time.perf_counter()
        signals = [self._analyse_index(index) for index in self.indices]

        # Market-wide context, then the agent review that uses it. The agent
        # can only hold a trade back, never create or improve one.
        # Subscribe the spot indices so the chart can track the market
        # between snapshots. Yahoo candles run minutes behind; measured live,
        # these ticks were 0.02-0.15s old.
        try:
            index_ticks.watch_indices(self.indices)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Index tick subscribe failed: %s", exc)

        regimes = {index: self._regime(index) for index in self.indices}
        for signal in signals:
            signal["regime"] = regimes.get(signal["index"], {})
        signals.sort(
            key=lambda s: (s.get("direction") != "WAIT", s.get("score") or 0),
            reverse=True,
        )
        positions = self._positions(signals)

        # The LLM gate. Outside the market window there is no live price for a
        # model to reason about, so calling one spends tokens to learn nothing.
        # The deterministic rule checklist keeps running either way: it is free.
        market_session = session_state(self.market)
        agents = self._agents if market_session.llm_active else [RuleAgent()]

        snapshot_so_far = {
            "ledger": self._ledger_panel(),
            "settings": {"available_capital": self.available_capital},
            "mode": "LIVE" if any(s.get("is_live") for s in signals) else "ESTIMATED",
            "market_open": is_market_open(),
            "minutes_to_square_off": minutes_to_square_off(),
        }
        for signal in signals:
            # Key on what actually changes the answer. Without this an LLM
            # agent is called twice every refresh -- 12 calls a minute at a
            # 10-second interval -- which burns a free tier in an hour, adds
            # latency to every snapshot, and re-asks a question whose inputs
            # have not moved. The signal only changes on a new closed candle.
            cache_key = (
                signal.get("index"),
                signal.get("bar_time"),
                signal.get("direction"),
                int((signal.get("score") or 0) // 5),   # ignore 1-point jitter
                bool((signal.get("plan") or {}).get("affordable", True)),
                snapshot_so_far["mode"],
            )
            cached = self._agent_cache.get(signal["index"])
            if cached and cached[0] == cache_key:
                signal["agent"] = cached[1]
                continue
            try:
                signal["agent"] = review_signal(
                    signal, snapshot_so_far, signal.get("regime"),
                    agents=agents,
                )
                self._agent_cache[signal["index"]] = (cache_key, signal["agent"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Agent review failed for %s: %s", signal["index"], exc)
                signal["agent"] = {"stance": "CAUTION", "summary": f"Review failed: {exc}",
                                   "concerns": [], "supports": [], "provider": "error",
                                   "degraded": True}

        any_live = any(s.get("is_live") for s in signals)
        market = is_market_open()
        mode = "LIVE" if any_live else ("ESTIMATED" if market else "CLOSED")
        events = self._detect_events(signals, positions, mode)

        # The pre-market brief announces itself once per day as a toast.
        premarket = self._premarket(market_session)
        if premarket.pop("_notify", None):
            events.insert(0, Event(
                "PREMARKET_BRIEF",
                {"LEAN_UP": "good", "LEAN_DOWN": "warn",
                 "UNSTABLE": "danger", "UNCLEAR": "info"}.get(
                     premarket.get("lean", "UNCLEAR"), "info"),
                f"Pre-market brief: {premarket.get('lean', 'UNCLEAR').replace('_', ' ')}",
                premarket.get("headline") or "Headlines gathered for today.",
                subject=self.market, sticky=True,
            ))

        session = session_summary(db_path=self.db_path)
        open_pnl = round(sum(p.get("net_pnl") or 0 for p in positions), 2)
        exposure = round(sum(p.get("deployed") or 0 for p in positions), 2)

        # Sanitised before returning, so "a snapshot is valid JSON" is part of
        # this method's contract rather than every caller's problem.
        return json_safe({
            "type": "snapshot",
            "ts": now_ist().isoformat(timespec="seconds"),
            "mode": mode,
            "market_open": market,
            "minutes_to_square_off": minutes_to_square_off(),
            "engine_version": ENGINE_VERSION,
            "interval": self.interval,
            "settings": {
                "available_capital": self.available_capital,
                "max_capital_fraction": self.max_capital_fraction,
                "target_pct": self.target_pct,
                "stop_pct": self.stop_pct,
                "record": self.record,
            },
            "signals": signals,
            "positions": positions,
            "events": [event.as_dict() for event in events],
            "session": {
                **session,
                "open_pnl": open_pnl,
                "exposure": exposure,
                "total_pnl": round(session["realised_pnl"] + open_pnl, 2),
                "capital_free": round(max(0.0, self.available_capital - exposure), 2),
            },
            "ledger": snapshot_so_far["ledger"],
            "regimes": regimes,
            "agent_status": {
                **agent_status(),
                "gated_off": not market_session.llm_active,
                "gate_reason": market_session.llm_reason,
            },
            "market_session": market_session.as_dict(),
            "markets": list_markets(),
            "ticks": live_ticks.status(),
            "index_ticks": index_ticks.latest(self.indices),
            "stocks": self._stocks(),
            "premarket": premarket,
            # What YOU actually made, as distinct from whether the ENGINE has an
            # edge (that is the ledger block). The two can disagree, and the gap
            # between them is itself worth seeing.
            "history": {
                "trades": closed_trades(limit=50, db_path=self.db_path),
                "daily": daily_pnl_series(days=30, db_path=self.db_path),
                "summary": performance_summary(db_path=self.db_path),
            },
            "glossary": GLOSSARY,
            "compute_ms": round((time.perf_counter() - started) * 1000, 1),
        })

    # ------------------------------------------------------------------ ledger
    def _ledger_panel(self) -> dict:
        """Edge measurement for the dashboard, with its honest caveats."""
        try:
            stats = ledger_stats(self.db_path)
            performance = expectancy_report(db_path=self.db_path)
            bands = score_band_report(db_path=self.db_path)
            return {
                **stats,
                "performance": performance,
                "bands": bands.to_dict("records") if not bands.empty else [],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ledger panel failed: %s", exc)
            return {"error": str(exc), "total_signals": 0, "graded": 0,
                    "performance": {"trades": 0, "verdict": "Ledger unavailable."},
                    "bands": []}


def _safe(value: Any) -> float | None:
    """Convert a possibly-NaN pandas value to a JSON-safe float or None."""
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def json_safe(value: Any) -> Any:
    """Recursively replace NaN / Infinity with None so the result is valid JSON.

    Python's ``json.dumps`` happily emits bare ``NaN`` and ``Infinity``, which
    are **not valid JSON**: a browser's ``JSON.parse`` throws a SyntaxError on
    them and drops the whole message. Pandas produces NaN constantly (an
    indicator before its warm-up period, a missing VWAP on a volume-less bar),
    so without this every snapshot containing one early candle is silently
    discarded by the client and the dashboard sits on "CONNECTING" forever --
    with no error anywhere, because the failure is a parse error on the
    receiving side.

    Note that ``float(x) or 0`` does NOT guard against this: NaN is truthy in
    Python, so it passes straight through such a check.
    """
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


_TOKEN_CACHE: dict[tuple, str | None] = {}


def _option_token(index: str, strike: float, option_type: str) -> str | None:
    """Cached NFO token lookup.

    Resolution loads the Angel instrument master, which is far too slow to
    repeat on every refresh. Without a token the grader cannot read that
    contract's premium path, so this is what makes a signal gradeable.
    """
    key = (index, float(strike), option_type)
    if key not in _TOKEN_CACHE:
        try:
            from intraday_advisor.tick_stream import resolve_option_token
            _TOKEN_CACHE[key] = resolve_option_token(index, strike, option_type)
        except Exception:  # noqa: BLE001
            _TOKEN_CACHE[key] = None
    return _TOKEN_CACHE[key]
