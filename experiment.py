"""Two ways to trade the same signal, run side by side on paper.

The question this answers
-------------------------
Every option this project has graded was **bought**, and the measured record
is a loss of about Rs 300 per lot across 597 signals. Two explanations fit:

* the *signals* are no good -- direction on a 5-minute chart is close to
  unforecastable, so nothing built on them can work; or
* the signals are passable and the *structure* is wrong -- a bought option
  pays the variance premium (measured at +3.6 to +5.9 volatility points to the
  seller) and bleeds time, so a correct direction still loses.

Those two say opposite things about what to do next, and one more backtest
cannot separate them. Running both structures on the **same signal at the same
moment** can: whatever the signal's quality, it is identical for both arms, so
the difference between them is the structure and nothing else.

  Arm A  LONG    buy the engine's contract; +25% target, -20% stop.
  Arm B  SPREAD  sell the first out-of-the-money option on the *other* side of
                 the view and buy one two strikes further out -- a bull put
                 spread on a CALL signal, a bear call spread on a PUT. Take
                 half the credit as profit; stop when the spread costs twice
                 the credit to buy back. Loss is capped by the bought wing.

Both flatten at 15:10. Both are one lot. Neither is ever sized by confidence.

What it deliberately does not do
--------------------------------
It does not obey the "measured negative expectancy" veto, because that veto is
the long-option record this experiment exists to re-measure -- obeying it would
mean the test never runs. Every other blocker (costs above target, expiry day,
estimated data, past square-off) is respected, for both arms.

It writes to its **own ledger** (``data/experiment.sqlite``), never the live
one, so the test cannot touch the positions in the portal or the statistics the
dashboard reports.

How to read the result
----------------------
``python experiment.py report``. A **session** is the unit, not a trade: trades
inside one day share that day's regime and are not independent, the error this
project has made twice. Until there are roughly 25 sessions the report says the
difference is not established, whatever the totals look like.

    python experiment.py run        # runs every session; the supervisor starts it
    python experiment.py report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import uuid
from pathlib import Path

LOG = logging.getLogger("experiment")

DB_PATH = "data/experiment.sqlite"
DESK_URL = "ws://127.0.0.1:8765/ws"

TARGET_PCT, STOP_PCT = 0.25, 0.20
#: Spread exits, as multiples of the credit received.
SPREAD_TAKE = 0.50          # buy it back at half the credit: keep the other half
SPREAD_STOP = 2.00          # buy it back at twice the credit: lose one credit
WING_STEPS = 2              # the bought wing sits this many strikes further out

NO_ENTRY_AFTER = "14:45"
FLATTEN_AT = "15:10"
COOLDOWN_MINUTES = 30
MAX_PAIRS_PER_DAY = 4
MIN_PREMIUM = 5.0           # below this the quote is the synthetic floor
MIN_SESSIONS = 25           # before the difference is called anything

#: The one blocker this experiment overrides, and only this one.
IGNORED_BLOCKER = "Risk checklist"


# ================================================================ decisions ==

def entry_allowed(signal: dict, *, now_hhmm: str, market_open: bool,
                  mode: str) -> tuple[bool, str]:
    """Whether this signal opens a pair. Returns (allowed, reason)."""
    direction = str(signal.get("direction") or "").upper()
    if direction not in ("CALL", "PUT"):
        return False, "no signal"
    if not market_open or mode != "LIVE" or not signal.get("is_live"):
        return False, "not live"
    if now_hhmm >= NO_ENTRY_AFTER:
        return False, "too late in the session"
    thesis = signal.get("thesis") or {}
    blockers = [b for b in (thesis.get("blockers") or []) if b != IGNORED_BLOCKER]
    if blockers:
        return False, "blocked: " + ", ".join(blockers)
    agent = signal.get("agent") or {}
    if IGNORED_BLOCKER in (thesis.get("blockers") or []):
        concern = " ".join(str(c) for c in (agent.get("concerns") or []))
        # The checklist vetoes for many reasons. Only the expectancy veto is
        # the thing under test; any other veto stands.
        if "expectancy" not in concern.lower():
            return False, "vetoed: " + concern[:80]
    if float(signal.get("entry_premium") or 0) < MIN_PREMIUM:
        return False, "premium below the tradeable floor"
    return True, "ok"


def pick_spread(ladder: list[dict], spot: float, direction: str) -> dict | None:
    """The credit spread that expresses the same view as the signal.

    CALL (bullish) -> bull PUT spread: sell the first put strike below spot,
    buy the put ``WING_STEPS`` strikes lower. PUT -> the mirror with calls.
    Returns None when the ladder cannot supply both legs at a real price, or
    when the credit is not positive -- a spread that pays nothing to open is
    not the trade being tested.
    """
    rows = sorted(ladder or [], key=lambda r: r["strike"])
    if not rows or not spot:
        return None
    strikes = [r["strike"] for r in rows]
    by_strike = {r["strike"]: r for r in rows}

    if direction == "CALL":
        below = [s for s in strikes if s < spot]
        if len(below) < 1 + WING_STEPS:
            return None
        short, long_ = below[-1], below[-1 - WING_STEPS]
        side, key = "PE", "pe"
    elif direction == "PUT":
        above = [s for s in strikes if s > spot]
        if len(above) < 1 + WING_STEPS:
            return None
        short, long_ = above[0], above[WING_STEPS]
        side, key = "CE", "ce"
    else:
        return None

    short_px = float(by_strike[short].get(key) or 0)
    long_px = float(by_strike[long_].get(key) or 0)
    if short_px < MIN_PREMIUM or long_px <= 0:
        return None
    credit = short_px - long_px
    if credit <= 0:
        return None
    width = abs(short - long_)
    return {
        "option_type": side, "short_strike": short, "long_strike": long_,
        "short_premium": short_px, "long_premium": long_px,
        "credit": round(credit, 2), "width": width,
        "max_loss_per_unit": round(width - credit, 2),
    }


def long_exit(entry: float, now: float | None, *, now_hhmm: str) -> str | None:
    """Why the bought option should be closed now, or None."""
    if now is None or now <= 0:
        return None
    if now >= entry * (1 + TARGET_PCT):
        return "target"
    if now <= entry * (1 - STOP_PCT):
        return "stop"
    if now_hhmm >= FLATTEN_AT:
        return "square-off"
    return None


def spread_exit(credit: float, value: float | None, *, now_hhmm: str) -> str | None:
    """Why the spread should be bought back now, or None.

    ``value`` is what it costs to close: short premium minus long premium.
    """
    if value is None:
        return None
    if value <= credit * SPREAD_TAKE:
        return "target"
    if value >= credit * SPREAD_STOP:
        return "stop"
    if now_hhmm >= FLATTEN_AT:
        return "square-off"
    return None


# ================================================================== ledger ==

def _note(arm: str, pair: str, direction: str, extra: dict | None = None) -> str:
    return json.dumps({"arm": arm, "pair": pair, "direction": direction,
                       **(extra or {})})


def _parse_note(text) -> dict:
    try:
        data = json.loads(text or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def open_pair(signal: dict, spread: dict, *, db_path: str = DB_PATH) -> str:
    """Open both arms on one signal. Returns the pair id."""
    from intraday_advisor.ledger.structures import open_structure
    from intraday_advisor.positions import open_position

    pair = uuid.uuid4().hex[:10]
    direction = signal["direction"]
    lot_size = int(signal["lot_size"])
    open_position(
        index_name=signal["index"], option_type=signal["option_type"],
        strike=float(signal["strike"]), lot_size=lot_size, lots=1,
        entry_premium=float(signal["entry_premium"]),
        target_pct=TARGET_PCT, stop_pct=STOP_PCT, expiry=signal.get("expiry"),
        notes=_note("long", pair, direction), db_path=db_path,
    )
    open_structure(
        index_name=signal["index"], lot_size=lot_size,
        legs=[
            {"option_type": spread["option_type"], "strike": spread["short_strike"],
             "action": "SELL", "entry_premium": spread["short_premium"]},
            {"option_type": spread["option_type"], "strike": spread["long_strike"],
             "action": "BUY", "entry_premium": spread["long_premium"]},
        ],
        notes=_note("spread", pair, direction,
                    {"credit": spread["credit"], "width": spread["width"],
                     "expiry": signal.get("expiry")}),
        db_path=db_path,
    )
    return pair


def _premium(ladder_by_index: dict, index: str, strike: float, side: str):
    row = next((r for r in ladder_by_index.get(index, [])
                if r["strike"] == strike), None)
    if row is None:
        return None
    price = float(row.get("ce" if side == "CE" else "pe") or 0)
    return price if price > 0 else None


def manage(ladders: dict, *, now_hhmm: str, db_path: str = DB_PATH) -> list[str]:
    """Close whatever has hit its exit. Returns one line per close."""
    from intraday_advisor.ledger.structures import (
        MULTI, close_structure, load_open_structures)
    from intraday_advisor.positions import close_position, load_open_positions

    lines: list[str] = []
    for position in load_open_positions(db_path=db_path):
        if position.option_type == MULTI:
            continue
        now = _premium(ladders, position.index_name, position.strike,
                       position.option_type)
        reason = long_exit(position.entry_premium, now, now_hhmm=now_hhmm)
        if reason:
            result = close_position(position.position_id, exit_premium=now,
                                    exit_reason=reason, db_path=db_path)
            if result:
                lines.append(
                    f"LONG   {position.index_name} {position.strike:.0f}"
                    f"{position.option_type} {reason}: {position.entry_premium} "
                    f"-> {now}  net Rs {result.get('net_pnl'):,.0f}")

    for structure in load_open_structures(db_path=db_path):
        exits, value = {}, 0.0
        for leg in structure.legs:
            price = _premium(ladders, structure.index_name, leg.strike,
                             leg.option_type)
            if price is None:
                exits = None
                break
            exits[leg.leg_index] = price
            value += -leg.sign * price          # cost to close: short - long
        if exits is None:
            continue
        credit = float(_parse_note(structure.notes).get("credit")
                       or -structure.net_entry_premium)
        reason = spread_exit(credit, value, now_hhmm=now_hhmm)
        if reason:
            result = close_structure(structure.position_id, exits,
                                     exit_reason=reason, db_path=db_path)
            if result and result.get("ok"):
                lines.append(
                    f"SPREAD {structure.index_name} credit {credit:.2f} "
                    f"{reason}: now {value:.2f}  net Rs {result['net_pnl']:,.0f}")
    return lines


# ================================================================== report ==

def results(*, db_path: str = DB_PATH) -> dict:
    """Closed results by arm and by session."""
    from intraday_advisor.ledger.schema import connect, init_ledger

    if not Path(db_path).exists():
        return {"arms": {}, "sessions": 0, "pairs": 0}
    init_ledger(db_path)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT session_date, notes, net_pnl, exit_reason, status "
            "FROM positions")]
    arms: dict = {}
    open_count = 0
    for row in rows:
        note = _parse_note(row["notes"])
        arm = note.get("arm")
        if arm not in ("long", "spread"):
            continue
        if row["status"] != "CLOSED" or row["net_pnl"] is None:
            open_count += 1
            continue
        bucket = arms.setdefault(arm, {"trades": 0, "net": 0.0, "wins": 0,
                                       "by_session": {}, "reasons": {}})
        pnl = float(row["net_pnl"])
        bucket["trades"] += 1
        bucket["net"] += pnl
        bucket["wins"] += 1 if pnl > 0 else 0
        day = bucket["by_session"].setdefault(row["session_date"], 0.0)
        bucket["by_session"][row["session_date"]] = day + pnl
        reason = row["exit_reason"] or "?"
        bucket["reasons"][reason] = bucket["reasons"].get(reason, 0) + 1

    sessions = sorted({d for a in arms.values() for d in a["by_session"]})
    out = {"arms": arms, "sessions": len(sessions), "session_dates": sessions,
           "open": open_count,
           "pairs": max((a["trades"] for a in arms.values()), default=0)}

    # The paired difference per session: spread minus long, same days.
    if {"long", "spread"} <= set(arms):
        diffs = [arms["spread"]["by_session"].get(d, 0.0)
                 - arms["long"]["by_session"].get(d, 0.0) for d in sessions]
        n = len(diffs)
        mean = sum(diffs) / n if n else 0.0
        if n >= 2:
            sd = math.sqrt(sum((x - mean) ** 2 for x in diffs) / (n - 1))
            se = sd / math.sqrt(n)
            out["difference"] = {"mean": round(mean, 2), "se": round(se, 2),
                                 "t": round(mean / se, 2) if se else 0.0,
                                 "sessions": n}
        else:
            out["difference"] = {"mean": round(mean, 2), "se": None, "t": None,
                                 "sessions": n}
    return out


def render(result: dict) -> str:
    lines = ["=" * 70, "LONG OPTION vs CREDIT SPREAD  (same signals, same moments)",
             "=" * 70]
    arms = result.get("arms") or {}
    if not arms:
        lines += ["  No closed pairs yet.", "=" * 70]
        return "\n".join(lines)
    lines.append(f"  {'arm':8} {'trades':>7} {'wins':>6} {'net Rs':>11} "
                 f"{'per trade':>10}  exits")
    for name in ("long", "spread"):
        arm = arms.get(name)
        if not arm:
            continue
        per = arm["net"] / arm["trades"] if arm["trades"] else 0.0
        exits = ", ".join(f"{k} {v}" for k, v in sorted(arm["reasons"].items()))
        lines.append(f"  {name:8} {arm['trades']:7} {arm['wins']:6} "
                     f"{arm['net']:11,.0f} {per:10,.0f}  {exits}")
    lines.append("")
    lines.append(f"  {'session':12} {'long':>10} {'spread':>10} {'spread-long':>12}")
    for day in result.get("session_dates", []):
        a = arms.get("long", {}).get("by_session", {}).get(day, 0.0)
        b = arms.get("spread", {}).get("by_session", {}).get(day, 0.0)
        lines.append(f"  {day:12} {a:10,.0f} {b:10,.0f} {b - a:12,.0f}")

    diff = result.get("difference")
    n = result.get("sessions", 0)
    lines.append("")
    if diff and diff.get("t") is not None:
        lines.append(f"  spread minus long: Rs {diff['mean']:,.0f} per session "
                     f"(t = {diff['t']:+.2f} over {diff['sessions']} sessions)")
    if n < MIN_SESSIONS:
        lines.append(
            f"  VERDICT: not established. {n} session(s) recorded and about "
            f"{MIN_SESSIONS} are needed — until then the totals above describe "
            f"these particular days, not either structure.")
    elif diff and diff.get("t") is not None and abs(diff["t"]) >= 2:
        better = "the spread" if diff["mean"] > 0 else "the long option"
        lines.append(f"  VERDICT: {better} has done better on the same signals, "
                     f"by more than chance would explain. Check that it is "
                     f"also positive on its own before trading it.")
    else:
        lines.append("  VERDICT: no reliable difference between the two "
                     "structures on these signals.")
    if result.get("open"):
        lines.append(f"  ({result['open']} leg(s) still open and not counted.)")
    lines.append("=" * 70)
    return "\n".join(lines)


# ================================================================== runner ==

def todays_entries(*, db_path: str = DB_PATH, today: str | None = None) -> tuple:
    """(pairs opened today, {index: minute-of-day of its latest entry}).

    Read from the ledger rather than kept in memory, so a restart mid-session
    cannot forget the cooldown and open a second pair on the same signal.
    """
    from intraday_advisor.ledger.recorder import now_ist
    from intraday_advisor.ledger.schema import connect, init_ledger

    init_ledger(db_path)
    day = today or now_ist().date().isoformat()
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT index_name, opened_at, notes FROM positions "
            "WHERE session_date = ?", (day,))]
    pairs, latest = 0, {}
    for row in rows:
        if _parse_note(row["notes"]).get("arm") != "long":
            continue
        pairs += 1
        stamp = str(row["opened_at"])[11:16]
        minute = int(stamp[:2]) * 60 + int(stamp[3:])
        latest[row["index_name"]] = max(latest.get(row["index_name"], 0), minute)
    return pairs, latest


async def _follow(ws, db_path: str) -> None:
    seen_bars: dict[str, str] = {}
    while True:
        message = json.loads(await ws.recv())
        if message.get("type") != "snapshot":
            continue
        now_hhmm = message["ts"][11:16]
        minute_of_day = int(now_hhmm[:2]) * 60 + int(now_hhmm[3:])
        signals = message.get("signals") or []
        ladders = {s["index"]: s.get("ladder") or [] for s in signals}

        for line in manage(ladders, now_hhmm=now_hhmm, db_path=db_path):
            LOG.info("%s closed %s", now_hhmm, line)

        pairs_today, last_entry = todays_entries(
            db_path=db_path, today=message["ts"][:10])
        for signal in signals:
            index = signal.get("index")
            bar = str(signal.get("bar_time"))
            if seen_bars.get(index) == bar:
                continue                       # one decision per candle
            seen_bars[index] = bar
            allowed, _reason = entry_allowed(
                signal, now_hhmm=now_hhmm,
                market_open=bool(message.get("market_open")),
                mode=str(message.get("mode") or ""))
            if not allowed or pairs_today >= MAX_PAIRS_PER_DAY:
                continue
            if minute_of_day - last_entry.get(index, -10_000) < COOLDOWN_MINUTES:
                continue
            spread = pick_spread(signal.get("ladder") or [],
                                 float(signal.get("spot") or 0),
                                 signal["direction"])
            if spread is None:
                LOG.info("%s %s %s: no spread available at these strikes",
                         now_hhmm, index, signal["direction"])
                continue
            pair = open_pair(signal, spread, db_path=db_path)
            pairs_today += 1
            last_entry[index] = minute_of_day
            LOG.info(
                "%s OPENED pair %s on %s %s | LONG %s %s @ %.2f | SPREAD "
                "sell %s %s @ %.2f, buy %s @ %.2f, credit %.2f",
                now_hhmm, pair, index, signal["direction"],
                signal["strike"], signal["option_type"],
                signal["entry_premium"], spread["short_strike"],
                spread["option_type"], spread["short_premium"],
                spread["long_strike"], spread["long_premium"],
                spread["credit"])


async def run(db_path: str = DB_PATH) -> None:
    """Follow the running desk's snapshots, day after day.

    It does not exit at the close: the supervisor restarts anything that
    exits, and a process that quits at 15:15 would be restarted straight into
    quitting again. Outside market hours ``entry_allowed`` simply refuses, so
    staying connected overnight costs nothing. A dropped connection (the
    dashboard restarting) is retried rather than fatal.
    """
    import websockets

    from intraday_advisor.ledger.schema import init_ledger

    init_ledger(db_path)
    LOG.info("experiment started; ledger %s", db_path)
    while True:
        try:
            async with websockets.connect(DESK_URL, max_size=None,
                                          ping_interval=None) as ws:
                await _follow(ws, db_path)
        except (OSError, websockets.WebSocketException) as exc:
            LOG.info("desk connection lost (%s); retrying in 15s", exc)
            await asyncio.sleep(15)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Long option vs credit spread on the same signals, on paper.")
    parser.add_argument("command", choices=("run", "report"))
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    if args.command == "report":
        print(render(results(db_path=args.db)))
        return 0

    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.FileHandler("logs/experiment.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])
    try:
        asyncio.run(run(args.db))
    except KeyboardInterrupt:
        pass
    print(render(results(db_path=args.db)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
