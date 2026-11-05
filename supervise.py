"""Keep the desk and the dashboard alive, and notice when they go quiet.

Why this exists
---------------
Five trading sessions were lost between 26 and 30 October. The processes did
not crash -- they were alive in the task list the whole time, from 23 October
16:46 onward, and recorded **nothing**. The last line in their log was a
websocket reconnect at 19:06 that night, then ten days of silence.

That is the failure mode worth engineering against. A crash is loud: the
process is gone, and anybody looking notices. A stalled loop looks exactly like
a healthy one from outside, and the data simply stops.

So this supervisor does two things a plain restart-on-exit cannot:

* **restarts a process that dies**, which is the easy half;
* **restarts a process that is alive and not working**, judged by whether the
  ledger is still growing during market hours. Liveness is measured by output,
  not by existence.

The quiet detector only runs while the market is open, because a desk that
records nothing at 2am is behaving correctly.

Deliberately not a service
--------------------------
This is a foreground script you start once a day, or point a scheduled task
at. It writes what it does to a log you can read afterwards. Installing a
Windows service would hide it further behind an abstraction, and the lesson of
those five sessions is that hidden things fail silently.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("supervise")

#: Declare a process stalled if the ledger has not grown in this long while
#: the market is open. A slow snapshot is ~40s at worst after the 2026-11-05
#: performance fix, so ten minutes is many missed cycles, not one slow one.
QUIET_SECONDS = 600.0

#: Wait this long after a restart before judging it again.
GRACE_SECONDS = 180.0

#: Never restart more often than this, so a process that dies instantly does
#: not become a fork bomb.
MIN_RESTART_INTERVAL = 60.0

#: A poll that arrives this many times later than scheduled means the machine
#: was suspended, not that the loop ran slowly.
SUSPEND_FACTOR = 3.0


@dataclass
class Child:
    """One supervised process and the file that proves it is working."""

    name: str
    command: list[str]
    db_path: str
    process: "subprocess.Popen | None" = None
    started_at: float = 0.0
    restarts: int = 0
    last_count: int = -1
    last_progress: float = 0.0
    log_path: str = ""
    _handle: object = field(default=None, repr=False)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


def _signal_count(db_path: str) -> int:
    """Proof that a child is still working: how many market samples it holds.

    This counts ``observations``, not ``signals``, and the difference cost a
    day of needless restarts.

    A signal row is written only when the engine proposes a CALL or a PUT. On a
    day it declines to trade -- which is most hours of most days, and exactly
    what it is supposed to do when nothing qualifies -- the signal count does
    not move at all. Using it as a heartbeat meant "working correctly and
    finding nothing" was indistinguishable from "stalled", and on 2026-11-05
    this supervisor restarted both children **nine times** on that mistake.

    ``observations`` is written every cycle regardless of the verdict, so it
    beats even when the answer is no. Signals are kept as a fallback for a
    ledger too old to have the table, since a missing table must not read as a
    dead process.
    """
    try:
        from intraday_advisor.ledger.schema import connect

        with connect(db_path) as conn:
            try:
                row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
                return int(row[0]) if row else 0
            except Exception:  # noqa: BLE001 - no table yet; fall back
                row = conn.execute("SELECT COUNT(*) FROM signals").fetchone()
                return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 - a locked DB is not a stall
        return -1


def _market_open() -> bool:
    try:
        from intraday_advisor.engine import is_market_open

        return bool(is_market_open())
    except Exception:  # noqa: BLE001
        return False


def start(child: Child) -> None:
    """Launch, with stdout captured to the child's own log."""
    Path(child.log_path).parent.mkdir(parents=True, exist_ok=True)
    handle = open(child.log_path, "a", encoding="utf-8", buffering=1)
    child._handle = handle
    handle.write(f"\n--- supervisor start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    child.process = subprocess.Popen(
        child.command, stdout=handle, stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).resolve().parent),
    )
    child.started_at = time.time()
    child.last_progress = time.time()
    child.last_count = _signal_count(child.db_path)
    LOG.info("started %s (pid %s): %s", child.name, child.process.pid,
             " ".join(child.command[-4:]))


def stop(child: Child) -> None:
    if child.alive():
        LOG.info("stopping %s (pid %s)", child.name, child.process.pid)
        child.process.terminate()
        try:
            child.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.process.kill()
    if child._handle:
        try:
            child._handle.close()
        except Exception:  # noqa: BLE001
            pass
        child._handle = None


def check(child: Child, *, now: float, market_open: bool) -> str | None:
    """Return a reason to restart, or None. Pure enough to test."""
    if not child.alive():
        return "process exited"

    if not market_open:
        # A desk that records nothing overnight is behaving correctly.
        child.last_progress = now
        return None

    if (now - child.started_at) < GRACE_SECONDS:
        return None

    count = _signal_count(child.db_path)
    if count < 0:
        return None                 # could not read; not evidence of a stall
    if count > child.last_count:
        child.last_count = count
        child.last_progress = now
        return None

    quiet = now - child.last_progress
    if quiet > QUIET_SECONDS:
        return (
            f"alive but silent for {quiet / 60:.0f} min during market hours "
            f"(ledger stuck at {count} market samples)"
        )
    return None


#: Windows SetThreadExecutionState flags. ES_SYSTEM_REQUIRED keeps the machine
#: from sleeping; ES_CONTINUOUS makes that persist until it is cleared.
#: Deliberately NOT ES_DISPLAY_REQUIRED -- the screen may sleep, the CPU may not.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(enable: bool = True) -> bool:
    """Ask Windows not to sleep while the desk is running. Returns success.

    The right scope for this is the process that needs it, not the machine's
    global power policy. On 2026-11-05 this laptop slept 09:41 to 12:37 IST and
    the first half of the session was lost. The AC setting was already "never
    sleep" -- it slept because it was on **battery**, where the idle timeout is
    five minutes.

    Setting the battery timeout to never would fix that session and flatten the
    battery on every other day. So the request is held for exactly as long as
    the supervisor runs and released when it exits: awake while recording,
    normal power behaviour the rest of the time.

    The display is deliberately left alone. Keeping the screen on all session
    wastes power and is not what was needed -- the CPU was.
    """
    try:
        import ctypes

        state = (_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED) if enable else _ES_CONTINUOUS
        # Returns the previous state, or 0 on failure.
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(state))
    except Exception:  # noqa: BLE001 - not Windows, or the call is unavailable
        return False


def suspended_for(now: float, expected: float, poll: float) -> float:
    """Seconds the machine was suspended, or 0.0 if it just ran late.

    A sleeping laptop is invisible from inside the loop: ``time.sleep(60)``
    returns three hours later and every child looks like it stalled. On
    2026-11-05 this machine slept 09:41 to 12:37 IST and the entire first half
    of the session was lost -- not to any defect, and not to anything a restart
    could have fixed.

    So an unexplained jump in the wall clock is reported as what it is. The
    children are exonerated (their quiet clocks reset) and the gap is logged in
    minutes of **market** data, because that is the number that matters and
    nothing else in the system was in a position to notice it.
    """
    late = now - expected
    return late if late > poll * SUSPEND_FACTOR else 0.0


def supervise(children: list[Child], *, poll: float = 60.0,
              max_restarts: int = 20) -> None:
    for child in children:
        start(child)
    try:
        while True:
            scheduled = time.time() + poll
            time.sleep(poll)
            now = time.time()
            market_open = _market_open()
            slept = suspended_for(now, scheduled, poll)
            if slept:
                LOG.warning(
                    "the machine was suspended for %.0f min%s — no process "
                    "could have recorded anything through that, and a restart "
                    "would not have helped",
                    slept / 60,
                    " DURING MARKET HOURS" if market_open else "",
                )
                for child in children:
                    child.last_progress = now
                    child.started_at = max(child.started_at, now - GRACE_SECONDS)
                continue
            for child in children:
                reason = check(child, now=now, market_open=market_open)
                if not reason:
                    continue
                if child.restarts >= max_restarts:
                    LOG.error("%s: %s — restart limit reached, leaving it down",
                              child.name, reason)
                    continue
                if (now - child.started_at) < MIN_RESTART_INTERVAL:
                    continue
                LOG.warning("%s: %s — restarting", child.name, reason)
                stop(child)
                child.restarts += 1
                start(child)
    except KeyboardInterrupt:
        LOG.info("supervisor interrupted; stopping children")
    finally:
        for child in children:
            stop(child)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Keep the desk and dashboard alive, and restart one that "
                    "is running but no longer recording during market hours.")
    parser.add_argument("--capital", type=float, default=20_000.0)
    parser.add_argument("--max-open", type=int, default=6)
    parser.add_argument("--interval", type=float, default=45.0)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--poll", type=float, default=60.0)
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--log", default="logs/supervisor.log")
    parser.add_argument("--allow-sleep", action="store_true",
                        help="do not hold the machine awake (a suspend then "
                             "loses the session's data silently)")
    args = parser.parse_args()

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(args.log, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    from intraday_advisor.log_redaction import install as install_redaction
    install_redaction()

    python = sys.executable
    children = [
        Child(
            name="paper_desk",
            command=[python, "paper_desk.py", "--capital", str(args.capital),
                     "--lots", "1", "--max-open", str(args.max_open),
                     "--interval", str(args.interval),
                     "--db", "data/paper_desk.sqlite"],
            db_path="data/paper_desk.sqlite",
            log_path="logs/supervised_desk.log",
        ),
    ]
    if not args.no_dashboard:
        children.append(Child(
            name="dashboard",
            command=[python, "dashboard_server.py", "--port", str(args.port),
                     "--capital", str(args.capital), "--refresh", "15",
                     "--db", "data/ledger.sqlite"],
            db_path="data/ledger.sqlite",
            log_path="logs/supervised_dashboard.log",
        ))

    LOG.info("supervising %d process(es); quiet threshold %.0f min",
             len(children), QUIET_SECONDS / 60)
    if args.allow_sleep:
        LOG.info("sleep request NOT held (--allow-sleep); a suspend will cost "
                 "you the data for as long as it lasts")
    elif keep_awake(True):
        LOG.info("holding the machine awake while this runs (display may "
                 "still sleep); released on exit")
    else:
        LOG.warning("could not ask the system to stay awake — if this machine "
                    "sleeps mid-session that data is simply gone")
    try:
        supervise(children, poll=args.poll)
    finally:
        keep_awake(False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
