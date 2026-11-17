"""Run slow pure-Python work in another process, so it cannot stall the desk.

Why a process and not a thread
------------------------------
Python runs one thread's bytecode at a time. The watchlist scan (about a minute
of pandas for 50 symbols) and the indicator scorecard (about 73 seconds) were
both moved to background *threads* so a snapshot would not wait for them -- and
that only looked like it worked. A thread doing a minute of pandas holds the
interpreter lock for most of that minute, so the snapshot thread still crawled:
measured on 2026-11-17, snapshots took 44-51 seconds while a scan was running
and under one second when it was not. The same mechanism cost five sessions in
October, when the tick stream's decoding thread starved the loop.

A separate process has its own interpreter and its own lock. The desk's thread
simply waits on the result, and waiting does not hold the lock.

One worker, reused, started on first use. If the pool cannot be started (or the
work cannot be sent to it), the call runs in the caller's thread exactly as
before -- slower, never broken.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ProcessPoolExecutor

logger = logging.getLogger("offload")

_LOCK = threading.Lock()
_POOL: ProcessPoolExecutor | None = None
_BROKEN = False

#: Longer than the slowest job here by a wide margin; a job past this is stuck.
TIMEOUT_SECONDS = 600.0


def _pool() -> ProcessPoolExecutor | None:
    global _POOL, _BROKEN
    with _LOCK:
        if _BROKEN:
            return None
        if _POOL is None:
            try:
                _POOL = ProcessPoolExecutor(max_workers=1)
            except Exception as exc:  # noqa: BLE001
                logger.warning("No worker process available (%s); slow jobs "
                               "will run in-thread.", exc)
                _BROKEN = True
                return None
        return _POOL


def call(function, *args, **kwargs):
    """``function(*args, **kwargs)``, computed in the worker process.

    Blocks the calling thread until the result is back, so call it from a
    background thread, never from the snapshot path. ``function`` must be a
    module-level function and its arguments and result picklable.
    """
    global _POOL, _BROKEN
    pool = _pool()
    if pool is None:
        return function(*args, **kwargs)
    try:
        return pool.submit(function, *args, **kwargs).result(TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - includes a crashed worker
        logger.warning("Worker process failed for %s (%s); running in-thread.",
                       getattr(function, "__name__", function), exc)
        with _LOCK:
            try:
                if _POOL is not None:
                    _POOL.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
            _POOL = None
        return function(*args, **kwargs)
