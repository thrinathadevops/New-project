"""Liveness measured by output, not by existence.

Five sessions were lost between 26 and 30 October to processes that were alive
in the task list the whole time and recorded nothing. A restart-on-exit
supervisor would not have noticed, because nothing exited. So the decision
these tests pin is the one that matters: a child that is running but not
producing is treated as dead.
"""

from __future__ import annotations

import pytest

from supervise import GRACE_SECONDS, QUIET_SECONDS, Child, check


class FakeProcess:
    """Just enough of Popen for :func:`check`."""

    def __init__(self, running: bool = True) -> None:
        self._running = running
        self.pid = 1234

    def poll(self):
        return None if self._running else 1


def child(*, count: int, running: bool = True, db: str = "x.sqlite") -> Child:
    return Child(name="desk", command=[], db_path=db,
                 process=FakeProcess(running), started_at=0.0,
                 last_count=count, last_progress=0.0)


@pytest.fixture
def ledger(monkeypatch):
    """A controllable signal count, so no database is needed."""
    box = {"count": 100}

    def fake_count(_db_path):
        return box["count"]

    monkeypatch.setattr("supervise._signal_count", fake_count)
    return box


# ================================================================ the easy half ==

def test_a_dead_child_is_restarted(ledger):
    kid = child(count=100, running=False)
    assert check(kid, now=GRACE_SECONDS + 1, market_open=True) == "process exited"


def test_a_dead_child_is_restarted_even_outside_market_hours(ledger):
    """A crashed dashboard should come back at 08:00 so it is up for the open,
    rather than waiting for a market that has no reason to wake it."""
    kid = child(count=100, running=False)
    assert check(kid, now=10.0, market_open=False) == "process exited"


# =========================================================== the half that bit ==

def test_an_alive_but_silent_child_is_restarted_during_market_hours(ledger):
    kid = child(count=100)
    reason = check(kid, now=QUIET_SECONDS + GRACE_SECONDS + 1, market_open=True)
    assert reason is not None
    assert "silent" in reason
    assert "100 signals" in reason, "the reason must name the evidence"


def test_a_child_that_is_still_recording_is_left_alone(ledger):
    kid = child(count=100)
    ledger["count"] = 101
    assert check(kid, now=QUIET_SECONDS + GRACE_SECONDS + 1,
                 market_open=True) is None
    assert kid.last_count == 101


def test_progress_resets_the_quiet_clock(ledger):
    """Otherwise a child that recorded one signal ten minutes ago would be
    killed for the silence that preceded it."""
    kid = child(count=100)
    now = GRACE_SECONDS + 10
    ledger["count"] = 101
    check(kid, now=now, market_open=True)
    assert kid.last_progress == now
    assert check(kid, now=now + QUIET_SECONDS - 1, market_open=True) is None


# ================================================= when silence means nothing ==

def test_silence_outside_market_hours_is_correct_behaviour(ledger):
    """A desk recording nothing at 02:00 is working as designed. This is the
    guard that stops the supervisor restarting it all night."""
    kid = child(count=100)
    assert check(kid, now=QUIET_SECONDS * 10, market_open=False) is None


def test_the_overnight_clock_is_reset_so_the_open_is_not_a_false_alarm(ledger):
    """Without this, a child alive since yesterday would be declared silent in
    the supervisor's first market-hours poll, before it had a chance to work."""
    kid = child(count=100)
    check(kid, now=50_000.0, market_open=False)
    assert kid.last_progress == 50_000.0
    assert check(kid, now=50_001.0, market_open=True) is None


def test_a_just_started_child_gets_a_grace_period(ledger):
    """The first snapshot of a cold process took 27s measured on 2026-11-05;
    judging it before it has produced anything would restart it forever."""
    kid = child(count=100)
    assert check(kid, now=GRACE_SECONDS - 1, market_open=True) is None


def test_an_unreadable_ledger_is_not_evidence_of_a_stall(monkeypatch):
    """A locked database returns -1. Restarting on that would turn a moment of
    contention into a kill, and the child holding the lock is the one working."""
    monkeypatch.setattr("supervise._signal_count", lambda _p: -1)
    kid = child(count=100)
    assert check(kid, now=QUIET_SECONDS + GRACE_SECONDS + 1,
                 market_open=True) is None


def test_the_quiet_threshold_is_longer_than_a_slow_snapshot():
    """A cold snapshot measured 39.5s on 2026-11-05. The threshold has to be
    many missed cycles, not one slow one, or the supervisor becomes the outage."""
    assert QUIET_SECONDS >= 300
    assert GRACE_SECONDS >= 60
