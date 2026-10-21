"""The one dashboard. Serves the page and streams snapshots on a single port.

Run it:

    python dashboard_server.py                 # http://localhost:8700
    python dashboard_server.py --port 9000 --capital 5000

Why a server and not another Streamlit page
-------------------------------------------
Streamlit re-runs the whole script on every interaction, which makes continuous
animation impossible and loses position state on refresh. The analysis also ran
inside the UI process, so each surface ended up with its own slightly different
copy of the logic -- which is how the project arrived at four scorers.

Here the split is strict: :class:`~intraday_advisor.engine.Engine` computes one
snapshot, this process broadcasts it, and the browser only renders. Adding a
second view later cannot introduce a second opinion, because no view does any
analysis.

One port, both protocols: ``websockets.serve`` is given a ``process_request``
hook that answers ordinary GETs with the static files and lets ``/ws`` upgrade.
That keeps it to a single URL to open and a single process to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from http import HTTPStatus
from pathlib import Path

try:
    import websockets
    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response
except ImportError:  # pragma: no cover - dependency guard
    print("websockets is required:  python -m pip install websockets", file=sys.stderr)
    raise SystemExit(1)

from intraday_advisor.envfile import load_env
from intraday_advisor.engine import Engine
from intraday_advisor.ledger.schema import DEFAULT_LEDGER_PATH
from intraday_advisor.positions import close_position, open_position

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dashboard")

STATIC_DIR = Path(__file__).parent / "dashboard"
# Only these files are ever served. An allowlist rather than path arithmetic,
# so there is no traversal surface at all.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

clients: set = set()


# --------------------------------------------------------------------------- #
# Static file serving, on the same port as the socket
# --------------------------------------------------------------------------- #

def process_request(connection, request):
    """Answer plain HTTP; return None to let ``/ws`` upgrade to a WebSocket."""
    path = request.path.split("?", 1)[0]
    if path == "/ws":
        return None

    entry = STATIC_FILES.get(path)
    if entry is None:
        return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")

    filename, content_type = entry
    target = STATIC_DIR / filename
    try:
        body = target.read_bytes()
    except OSError:
        logger.error("Missing static file: %s", target)
        return connection.respond(
            HTTPStatus.INTERNAL_SERVER_ERROR, f"Cannot read {filename}\n"
        )

    # Built explicitly rather than by mutating connection.respond()'s response.
    # ``Headers`` is a multidict, so assigning Content-Type/Content-Length on a
    # response that already has them APPENDS a second copy, and a browser then
    # rejects the reply outright with ERR_RESPONSE_HEADERS_MULTIPLE_CONTENT_LENGTH
    # (curl quietly tolerates it, so this only shows up in a real browser).
    return Response(
        HTTPStatus.OK.value,
        HTTPStatus.OK.phrase,
        Headers([
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            # Always revalidate: the files change under the server in dev.
            ("Cache-Control", "no-cache"),
        ]),
        body,
    )


# --------------------------------------------------------------------------- #
# Snapshot loop
# --------------------------------------------------------------------------- #

class Broadcaster:
    """Computes snapshots and pushes them to every connected browser.

    The engine is synchronous and does network I/O, so it runs in a thread via
    ``to_thread``; otherwise a slow data fetch would stall the socket and the
    UI would look frozen exactly when it matters.
    """

    def __init__(self, engine: Engine, refresh_seconds: float = 10.0) -> None:
        self.engine = engine
        self.refresh_seconds = refresh_seconds
        self.latest: dict | None = None
        self._wake = asyncio.Event()

    def request_refresh(self) -> None:
        """Recompute immediately rather than waiting out the interval.

        Used after a settings change or a position edit, so the screen responds
        to input straight away.
        """
        self._wake.set()

    async def stream_ticks(self, every: float = 1.5) -> None:
        """Push live prices -- index spot *and* open positions -- between
        snapshots.

        A snapshot is ~105 KB and takes seconds to build; this payload is a few
        hundred bytes of pure cache reads. Sending them on the same cadence
        would mean either a stale screen or a pointless amount of work, so they
        run on separate clocks: heavy analysis occasionally, prices
        continuously.

        Positions ride this clock because that is the panel where a cold number
        costs money. Their P&L, status and alarms are computed by the engine
        (the same code the snapshot uses), never in the browser.
        """
        from intraday_advisor import index_ticks

        def gather() -> dict:
            # One hop to the worker thread for both: two would double the
            # scheduling overhead of the most frequent loop in the process.
            payload = {"type": "ticks",
                       "prices": index_ticks.latest(self.engine.indices)}
            try:
                payload.update(self.engine.tick_positions())
            except Exception as exc:  # noqa: BLE001
                logger.debug("Tick position update failed: %s", exc)
            return payload

        while True:
            await asyncio.sleep(every)
            if not clients:
                continue
            try:
                payload = await asyncio.to_thread(gather)
            except Exception:  # noqa: BLE001 - a tick must never break the loop
                continue
            if payload.get("prices") or payload.get("positions") is not None:
                await self.broadcast(payload)
                # An alarm found here must reach the rest of the system too --
                # the risk governor and the stale-row cleanup live in the
                # snapshot, so pull one forward rather than waiting out the
                # interval.
                if payload.get("events"):
                    self.request_refresh()

    async def grade_when_due(self, every: float = 300.0) -> None:
        """Grade the ledger by itself, because the manual step never happens.

        The whole record -> grade -> report pipeline worked and 151 real signals
        sat ungraded, because grading was a command somebody had to remember
        after the close. When it finally ran it produced the most useful number
        in the project. A measurement that depends on being remembered is one
        that does not happen.

        Runs on its own slow clock: grading reads a whole session's premium path
        from the broker, so checking every five minutes is both ample and the
        most this should ever ask of the API.
        """
        from intraday_advisor import auto_grade

        while True:
            try:
                summary = await asyncio.to_thread(
                    auto_grade.grade_due, self.engine.db_path
                )
                for entry in summary.get("graded", []):
                    logger.info(
                        "Auto-graded %s: %s signal(s) %s",
                        entry["session"], entry["graded"], entry.get("outcomes") or "",
                    )
                    await self.broadcast({
                        "type": "ack", "title": "Session graded",
                        "message": (
                            f"{entry['session']}: {entry['graded']} signal(s) "
                            f"graded. The measured verdict has been updated."
                        ),
                    })
                    # The verdict banner reads the ledger, so refresh it now
                    # rather than at the next interval.
                    self.request_refresh()
            except Exception as exc:  # noqa: BLE001 - never take the desk down
                logger.warning("Auto-grade pass failed: %s", exc)
            await asyncio.sleep(every)

    async def run(self) -> None:
        while True:
            try:
                snapshot = await asyncio.to_thread(self.engine.snapshot)
                self.latest = snapshot
                await self.broadcast(snapshot)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                logger.error("Snapshot failed: %s", exc, exc_info=True)
                with contextlib.suppress(Exception):
                    await self.broadcast({
                        "type": "error",
                        "message": f"Snapshot failed: {exc}",
                    })

            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.refresh_seconds)

    @staticmethod
    async def broadcast(message: dict) -> None:
        if not clients:
            return
        # allow_nan=False makes a NaN leak fail loudly here rather than
        # shipping bare NaN, which is valid for Python's json module and
        # rejected outright by the browser's JSON.parse.
        try:
            payload = json.dumps(message, default=str, allow_nan=False)
        except ValueError as exc:
            logger.error("Refusing to send non-JSON-compliant snapshot: %s", exc)
            payload = json.dumps({
                "type": "error",
                "message": "Server produced invalid JSON (NaN/Infinity); "
                           "snapshot withheld. See the server log.",
            })
        dead = set()
        for client in list(clients):
            try:
                await client.send(payload)
            except Exception:  # noqa: BLE001
                dead.add(client)
        clients.difference_update(dead)


# --------------------------------------------------------------------------- #
# Commands from the browser
# --------------------------------------------------------------------------- #

async def handle_command(message: dict, broadcaster: Broadcaster, socket) -> None:
    kind = message.get("type")

    if kind == "settings":
        broadcaster.engine.configure(
            available_capital=message.get("available_capital"),
            max_capital_fraction=message.get("max_capital_fraction"),
            target_pct=message.get("target_pct"),
            stop_pct=message.get("stop_pct"),
            record=message.get("record"),
            market=message.get("market"),
            expiries=message.get("expiries"),
            candidate_count=message.get("candidate_count"),
            stock_universe=message.get("stock_universe"),
            stock_limit=message.get("stock_limit"),
        )
        refresh = message.get("refresh_seconds")
        if refresh:
            broadcaster.refresh_seconds = max(3.0, float(refresh))
        broadcaster.request_refresh()
        return

    if kind == "open_position":
        try:
            position_id = await asyncio.to_thread(
                open_position,
                index_name=message["index_name"],
                option_type=message["option_type"],
                strike=float(message["strike"]),
                lot_size=int(message["lot_size"]),
                lots=int(message["lots"]),
                entry_premium=float(message["entry_premium"]),
                target_pct=float(message.get("target_pct", 0.25)),
                stop_pct=float(message.get("stop_pct", 0.20)),
                option_token=message.get("option_token"),
                signal_id=message.get("signal_id"),
                db_path=broadcaster.engine.db_path,
            )
        except (KeyError, ValueError, TypeError) as exc:
            await socket.send(json.dumps({
                "type": "error", "message": f"Could not open position: {exc}"
            }))
            return
        logger.info("Position opened: %s", position_id)
        await socket.send(json.dumps({
            "type": "ack", "title": "Position added",
            "message": f"{message['index_name']} {message['strike']} "
                       f"{message['option_type']} is now monitored. The stop "
                       f"alarm will fire the moment it is breached.",
        }))
        broadcaster.request_refresh()
        return

    if kind == "close_position":
        try:
            result = await asyncio.to_thread(
                close_position,
                message["position_id"],
                exit_premium=float(message["exit_premium"]),
                exit_reason="manual",
                db_path=broadcaster.engine.db_path,
            )
        except (KeyError, ValueError, TypeError) as exc:
            await socket.send(json.dumps({
                "type": "error", "message": f"Could not close position: {exc}"
            }))
            return
        if result is None:
            await socket.send(json.dumps({
                "type": "error", "message": "That position is not open."
            }))
            return
        logger.info("Position closed: %s net %s", result["position_id"], result["net_pnl"])
        await socket.send(json.dumps({
            "type": "ack", "title": "Position closed",
            "message": f"Net {result['net_pnl']:+,.0f} after "
                       f"{result['cost']:,.0f} in costs.",
        }))
        broadcaster.request_refresh()
        return

    if kind == "protect_position":
        """Pull a stop up to net breakeven, or to an explicit premium.

        A stop may only ever be tightened -- the ledger refuses to loosen one,
        so this endpoint cannot be used to widen a losing trade.
        """
        from intraday_advisor.positions import adjust_stop, breakeven_stop
        try:
            level = message.get("stop_premium")
            if level in (None, ""):
                result = await asyncio.to_thread(
                    breakeven_stop, message["position_id"],
                    costs=broadcaster.engine._costs,
                    db_path=broadcaster.engine.db_path,
                )
            else:
                result = await asyncio.to_thread(
                    adjust_stop, message["position_id"], float(level),
                    reason="manual", db_path=broadcaster.engine.db_path,
                )
        except (KeyError, ValueError, TypeError) as exc:
            await socket.send(json.dumps({
                "type": "error", "message": f"Could not move the stop: {exc}"
            }))
            return
        if not result.get("ok"):
            await socket.send(json.dumps({
                "type": "error",
                "message": result.get("error", "Could not move the stop."),
            }))
            return
        logger.info("Stop moved: %s -> %s", result["old_stop"], result["new_stop"])
        await socket.send(json.dumps({
            "type": "ack", "title": "Stop moved",
            "message": (
                f"Rs {result['old_stop']:,.2f} to Rs {result['new_stop']:,.2f} "
                f"- locks in Rs {result['locked_in']:+,.2f} per unit."
            ),
        }))
        broadcaster.request_refresh()
        return

    if kind == "delete_position":
        from intraday_advisor.positions import delete_position
        try:
            removed = await asyncio.to_thread(
                delete_position, message["position_id"],
                db_path=broadcaster.engine.db_path,
            )
        except (KeyError, TypeError) as exc:
            await socket.send(json.dumps({
                "type": "error", "message": f"Could not delete: {exc}"
            }))
            return
        logger.info("Position deleted: %s (%s)", message.get("position_id"), removed)
        await socket.send(json.dumps({
            "type": "ack", "title": "Row removed" if removed else "Nothing to remove",
            "message": "The trade is gone from your record."
                       if removed else "No such position.",
        }))
        broadcaster.request_refresh()
        return

    await socket.send(json.dumps({
        "type": "error", "message": f"Unknown command {kind!r}"
    }))


async def handler(socket, broadcaster: Broadcaster) -> None:
    clients.add(socket)
    logger.info("Browser connected (%d open)", len(clients))
    try:
        # Send the cached snapshot immediately so a new tab is never blank
        # while it waits for the next interval.
        if broadcaster.latest:
            with contextlib.suppress(ValueError):
                await socket.send(
                    json.dumps(broadcaster.latest, default=str, allow_nan=False)
                )

        async for raw in socket:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                await handle_command(message, broadcaster, socket)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clients.discard(socket)
        logger.info("Browser disconnected (%d open)", len(clients))


# --------------------------------------------------------------------------- #

async def main_async(args) -> None:
    engine = Engine(
        available_capital=args.capital,
        max_capital_fraction=args.max_fraction,
        record=not args.no_record,
        interval=args.interval,
        db_path=args.db,
    )
    broadcaster = Broadcaster(engine, refresh_seconds=args.refresh)

    missing = [name for name, _ in STATIC_FILES.values()
               if not (STATIC_DIR / name).exists()]
    if missing:
        logger.error("Missing dashboard files in %s: %s", STATIC_DIR, sorted(set(missing)))
        raise SystemExit(1)

    async def route(socket):
        await handler(socket, broadcaster)

    async with serve(route, args.host, args.port, process_request=process_request):
        # Prices stream on their own clock, independent of the snapshot loop.
        tick_task = asyncio.create_task(broadcaster.stream_ticks())
        # Grades itself after the close. See Broadcaster.grade_when_due.
        grade_task = asyncio.create_task(broadcaster.grade_when_due())
        shown_host = "localhost" if args.host in ("0.0.0.0", "") else args.host
        logger.info("=" * 62)
        logger.info("  Options Desk ready:  http://%s:%s", shown_host, args.port)
        logger.info("  Capital %s · deploy max %d%% · refresh %ss · recording %s",
                    f"Rs {args.capital:,.0f}", args.max_fraction * 100,
                    args.refresh, "OFF" if args.no_record else "ON")
        logger.info("  Grade after 15:30:   python ledger_cli.py grade")
        logger.info("=" * 62)
        try:
            await broadcaster.run()
        finally:
            tick_task.cancel()
            grade_task.cancel()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Intraday Options Desk dashboard")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1, local only)")
    parser.add_argument("--port", type=int, default=8700)
    parser.add_argument("--capital", type=float, default=20_000.0,
                        help="capital available now, in rupees")
    parser.add_argument("--max-fraction", type=float, default=1.0,
                        dest="max_fraction",
                        help="max fraction of capital to deploy (default 1.0)")
    parser.add_argument("--refresh", type=float, default=10.0,
                        help="seconds between snapshots")
    parser.add_argument("--interval", default="5m", help="candle interval")
    parser.add_argument("--no-record", action="store_true",
                        help="do not write signals to the ledger")
    parser.add_argument("--db", default=str(DEFAULT_LEDGER_PATH))
    args = parser.parse_args(argv)

    # Credentials and agent keys live in .env; nothing read them before.
    load_env()

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logger.info("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
