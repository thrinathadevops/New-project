"""Scheduled events that move the index, so a trade is not placed blind into one.

The gap this closes
-------------------
The checklist learned about expiry on 2026-11-05 and still knew nothing about
anything else on the calendar. NIFTY is cap-weighted, so the day Reliance or
HDFC Bank reports is a day the index can gap on one name, and RBI policy
decisions land mid-session by design. An intraday bought option held through
either is paying for a two-way move nobody has priced for you.

This is the index equivalent of the `day-trader-skill` pack's "Tesla reports
this week, so a day trade is out before the close" check -- the one idea in its
news layer that this project had no answer to.

What it deliberately does NOT claim
-----------------------------------
**A date, never a time.** Yahoo returns earnings timestamps in a US timezone
for Indian companies -- Reliance's last result reads ``2026-10-30 10:00-04:00``,
which is 19:30 IST, after the close. The hour is not trustworthy, so only the
date is used and the day either side is treated as "near". Pretending to know
the hour would let a trade be cleared at 14:00 on the strength of a timestamp
that was never meant to be read that way.

**No sentiment, no direction.** It says an event is scheduled. It does not say
which way it will go, because nothing does -- the same discipline
:mod:`intraday_advisor.news` already applies to headlines.

**A static policy list that admits when it is stale.** RBI MPC dates are
published a year ahead and there is no free endpoint worth depending on, so
they are listed here. When the list runs out it reports *that*, loudly, rather
than reporting "no events" -- which is the failure mode that makes a calendar
worse than none.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("event_calendar")

#: Where the fetched earnings dates are cached. One file, refreshed daily:
#: twelve yfinance calls per snapshot would dominate the loop, and a results
#: date does not change inside a day.
CACHE_PATH = Path("data/event_calendar.json")

#: Hours before the cache is refetched.
CACHE_HOURS = 12.0

#: A result this many days away still counts as "near", because the date
#: itself carries a day of uncertainty (see the module docstring).
NEAR_DAYS = 1

#: RBI Monetary Policy Committee decision dates. Published a year ahead.
#: **Update this list.** :func:`policy_horizon` reports how far it reaches and
#: :func:`events_for` warns once it is exhausted.
RBI_MPC_DATES: tuple[str, ...] = (
    "2026-02-06", "2026-04-08", "2026-06-05", "2026-08-06",
    "2026-10-01", "2026-12-04",
    "2027-02-05", "2027-04-07", "2027-06-04", "2027-08-05",
)


@dataclass
class Event:
    """One scheduled event, and how close it is."""

    kind: str                  # "earnings" | "policy"
    label: str
    event_date: str
    days_away: int

    @property
    def near(self) -> bool:
        return abs(self.days_away) <= NEAR_DAYS

    def as_dict(self) -> dict:
        from dataclasses import asdict
        data = asdict(self)
        data["near"] = self.near
        return data


@dataclass
class EventRisk:
    """What is on the calendar around one session."""

    session_date: str = ""
    events: list = field(default_factory=list)
    policy_today: bool = False
    earnings_today: list = field(default_factory=list)
    stale_policy_list: bool = False
    note: str = ""

    @property
    def has_event(self) -> bool:
        return bool(self.events)

    def as_dict(self) -> dict:
        return {
            "session_date": self.session_date,
            "events": [e.as_dict() for e in self.events],
            "policy_today": self.policy_today,
            "earnings_today": list(self.earnings_today),
            "stale_policy_list": self.stale_policy_list,
            "has_event": self.has_event,
            "note": self.note,
        }


def _today() -> date:
    from intraday_advisor.ledger.recorder import now_ist
    return now_ist().date()


# ================================================================= earnings ==

def _read_cache(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001 - a missing or corrupt cache is not an error
        return {}


def _cache_is_fresh(cache: dict, *, hours: float) -> bool:
    stamp = cache.get("fetched_at")
    if not stamp:
        return False
    try:
        age = datetime.now().timestamp() - float(stamp)
    except (TypeError, ValueError):
        return False
    return age < hours * 3600


def earnings_calendar(
    symbols=None, *, cache_path: Path | str = CACHE_PATH,
    cache_hours: float = CACHE_HOURS, refresh: bool = False,
) -> dict:
    """``{symbol: 'YYYY-MM-DD'}`` for the next scheduled result per name.

    Cached on disk for ``cache_hours``. A symbol whose date cannot be fetched is
    **omitted rather than guessed**: a calendar that invents a date would clear
    or block trades on fiction, and an absent entry is visibly absent.
    """
    from intraday_advisor.regime import BREADTH_BASKET

    names = tuple(symbols) if symbols is not None else BREADTH_BASKET
    path = Path(cache_path)
    cache = _read_cache(path)
    if not refresh and _cache_is_fresh(cache, hours=cache_hours):
        cached = cache.get("earnings") or {}
        if all(name in cached for name in names):
            return {name: cached[name] for name in names if cached.get(name)}

    found: dict[str, str] = {}
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        logger.debug("yfinance unavailable for the earnings calendar: %s", exc)
        return {name: cache.get("earnings", {}).get(name) for name in names
                if cache.get("earnings", {}).get(name)}

    for name in names:
        try:
            calendar = yf.Ticker(name).calendar or {}
            dates = calendar.get("Earnings Date") or []
            if isinstance(dates, (str, date, datetime)):
                dates = [dates]
            upcoming = []
            for item in dates:
                if isinstance(item, datetime):
                    upcoming.append(item.date())
                elif isinstance(item, date):
                    upcoming.append(item)
                else:
                    try:
                        upcoming.append(datetime.fromisoformat(str(item)).date())
                    except (TypeError, ValueError):
                        continue
            if upcoming:
                # The date NEAREST today, not the earliest. Yahoo's calendar
                # goes stale for some names -- TCS still listed 2026-10-08 as
                # its "Earnings Date" a month later -- and `min()` then pins a
                # result that already happened. Proximity is what this module
                # judges, in either direction: a result tomorrow is event risk,
                # a result yesterday is IV crush. A date weeks away in either
                # direction simply never enters the window, which is the
                # correct outcome for a stale entry.
                today = _today()
                found[name] = min(
                    upcoming, key=lambda d: abs((d - today).days)
                ).isoformat()
        except Exception as exc:  # noqa: BLE001 - one name must not break the set
            logger.debug("No earnings date for %s: %s", name, exc)

    if found:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            merged = dict(cache.get("earnings") or {})
            merged.update(found)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"fetched_at": datetime.now().timestamp(),
                           "earnings": merged}, handle, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not write the event cache: %s", exc)
    return found


# =================================================================== policy ==

def policy_horizon(dates=RBI_MPC_DATES) -> str:
    """The last policy date on the list, so staleness can be reported."""
    parsed = sorted(d for d in dates if d)
    return parsed[-1] if parsed else ""


def policy_dates(dates=RBI_MPC_DATES) -> list[date]:
    out = []
    for item in dates:
        try:
            out.append(datetime.fromisoformat(str(item)).date())
        except (TypeError, ValueError):
            continue
    return sorted(out)


# ============================================================== the calendar ==

def events_for(
    session_date=None, *, symbols=None, horizon: int = NEAR_DAYS,
    cache_path: Path | str = CACHE_PATH, earnings: dict | None = None,
) -> EventRisk:
    """What is scheduled within ``horizon`` days of this session.

    ``earnings`` may be supplied directly, which keeps this function pure for
    tests and lets a caller that already fetched the calendar avoid refetching.
    """
    target = session_date or _today()
    if isinstance(target, str):
        try:
            target = datetime.fromisoformat(target).date()
        except (TypeError, ValueError):
            return EventRisk(session_date=str(session_date),
                             note="Unreadable session date.")

    risk = EventRisk(session_date=target.isoformat())

    table = earnings if earnings is not None else earnings_calendar(
        symbols, cache_path=cache_path)
    for symbol, when in (table or {}).items():
        try:
            day = datetime.fromisoformat(str(when)).date()
        except (TypeError, ValueError):
            continue
        away = (day - target).days
        if abs(away) <= horizon:
            name = str(symbol).replace(".NS", "")
            risk.events.append(Event("earnings", f"{name} results",
                                     day.isoformat(), away))
            if away == 0:
                risk.earnings_today.append(name)

    for day in policy_dates():
        away = (day - target).days
        if abs(away) <= horizon:
            risk.events.append(Event("policy", "RBI MPC decision",
                                     day.isoformat(), away))
            if away == 0:
                risk.policy_today = True

    horizon_end = policy_horizon()
    if horizon_end and horizon_end < target.isoformat():
        risk.stale_policy_list = True
        risk.note = (
            f"The RBI policy list ends {horizon_end}, before this session, so "
            f"policy days are NOT being checked. Update RBI_MPC_DATES — an "
            f"empty calendar must not read as a clear one."
        )
    elif not risk.events:
        risk.note = "Nothing scheduled within a day of this session."
    else:
        risk.events.sort(key=lambda e: (abs(e.days_away), e.kind))
        risk.note = "; ".join(
            f"{e.label} {'today' if e.days_away == 0 else f'{e.days_away:+d}d'}"
            for e in risk.events
        )
    return risk


def describe(risk: EventRisk) -> str:
    """Plain text for a runbook or a log line."""
    lines = ["=" * 60, f"SCHEDULED EVENTS around {risk.session_date}", "=" * 60]
    if risk.stale_policy_list:
        lines += ["  !! " + risk.note, "=" * 60]
        return "\n".join(lines)
    if not risk.events:
        lines += ["  nothing within a day", "=" * 60]
        return "\n".join(lines)
    for event in risk.events:
        when = "TODAY" if event.days_away == 0 else f"{event.days_away:+d} days"
        lines.append(f"  {event.kind:9} {event.label:28} {when}")
    lines += ["", "  A date, not a time: Yahoo reports Indian results in a US",
              "  timezone, so the day either side counts as near.", "=" * 60]
    return "\n".join(lines)
