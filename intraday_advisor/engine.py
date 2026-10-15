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
    evaluate_live,
    load_open_positions,
    session_summary,
)

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
    db_path: str | Path = DEFAULT_LEDGER_PATH
    indices: tuple[str, ...] = tuple(INDEX_CONFIG)

    # --- internal state for diffing ---
    _previous: dict = field(default_factory=dict, repr=False)
    _announced: set = field(default_factory=set, repr=False)
    _chart_cache: dict = field(default_factory=dict, repr=False)
    _chart_fetched_at: dict = field(default_factory=dict, repr=False)
    _costs: Any = None

    def __post_init__(self) -> None:
        if self._costs is None:
            self._costs = fitted_cost_model(self.db_path)

    # ------------------------------------------------------------------ config
    def configure(self, **changes) -> None:
        """Apply settings from the UI. Unknown keys are ignored deliberately,
        so a stale client cannot crash the server with an old field name."""
        for key in ("available_capital", "max_capital_fraction", "target_pct",
                    "stop_pct", "record"):
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

    # --------------------------------------------------------------- positions
    def _positions(self, signals: list[dict]) -> list[dict]:
        direction_by_index = {s["index"]: s.get("direction", "WAIT") for s in signals}
        spot_by_index = {s["index"]: s.get("spot", 0) for s in signals}

        out = []
        for position in load_open_positions(db_path=self.db_path):
            premium, source = None, "unavailable"
            try:
                premium, source = get_option_premium(
                    position.index_name, position.strike, position.option_type,
                    spot=spot_by_index.get(position.index_name, 0) or 0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Premium fetch failed for %s: %s", position.position_id, exc)
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

        for position in positions:
            key = f"{position['position_id']}:{position['status']}"
            if position["status"] in ("TARGET_HIT", "STOP_HIT", "WARNING"):
                if key in self._announced:
                    continue
                self._announced.add(key)
                severity = {
                    "TARGET_HIT": "good", "STOP_HIT": "danger", "WARNING": "warn",
                }[position["status"]]
                events.append(Event(
                    f"POSITION_{position['status'].replace('_HIT', '')}",
                    severity,
                    f"{position['index_name']} {position['strike']:g} "
                    f"{position['option_type']} — {position['action']}",
                    position["message"],
                    subject=position["position_id"],
                    sticky=position["status"] in ("STOP_HIT", "TARGET_HIT"),
                ))

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
        signals.sort(
            key=lambda s: (s.get("direction") != "WAIT", s.get("score") or 0),
            reverse=True,
        )
        positions = self._positions(signals)

        any_live = any(s.get("is_live") for s in signals)
        market = is_market_open()
        mode = "LIVE" if any_live else ("ESTIMATED" if market else "CLOSED")
        events = self._detect_events(signals, positions, mode)

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
            "ledger": self._ledger_panel(),
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
