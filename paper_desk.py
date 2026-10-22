"""An autonomous paper desk: trades the whole session by itself, records everything.

Why this exists
---------------
The ledger says this strategy loses money -- 142 graded trades, a 9.9% win rate
and an expectancy of -Rs 1,142 per lot with the entire confidence interval below
zero. The right response to that is not to trade it harder with real money; it
is to let it trade itself at zero risk and keep measuring, because there is one
open question the pooled numbers cannot answer:

    **Do the trades the agent VETOED lose, while the ones it approved do not?**

If so, the edge is already there and the veto is the thing finding it. If not,
the layer is costing attention for nothing. Until 2026-10-22 the agent's verdict
was never recorded, so the question was unanswerable. Now it is stored on every
signal, and this desk deliberately takes **both** sides -- the vetoed setups as
well as the approved ones -- because a sample conditioned on the system's own
decision cannot measure that decision.

What it does and does not do
----------------------------
* **Paper only.** It writes to its own ledger file and never touches a broker
  order path. ``intraday_advisor.angel_one.place_intraday_order`` is not
  imported here and must not be.
* **Fixed, small size.** One lot. The instruction that prompted this run was
  "take how much amount you want, there is no restriction", and a measured
  expectancy of -Rs 1,142 per lot is precisely the condition under which
  removing a size limit destroys an account. Size stays fixed so the day
  measures the *strategy*, not a bet on it.
* **Honest exits.** Stop, target and the 15:15 square-off, priced from the live
  tick or the chain, through the same cost model as everything else.
* **It runs unattended.** Launch it before the open and read the summary after
  the close; it needs nothing from anybody in between.

Run it:

    python paper_desk.py --capital 20000 --max-open 4
"""

from __future__ import annotations

import argparse
import json
import logging
import signal as signal_module
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("paper_desk")

#: Stop opening new positions this many minutes before square-off. A trade
#: opened at 15:10 cannot reach a target; it can only pay costs.
NO_NEW_TRADES_BEFORE_CLOSE = 25

#: Square off everything this many minutes before 15:15, so the exit is priced
#: while the book is still liquid rather than in the closing scramble.
FLATTEN_BEFORE_CLOSE = 5


@dataclass
class DeskState:
    opened: int = 0
    closed: int = 0
    realised: float = 0.0
    by_stance: dict = field(default_factory=dict)
    errors: int = 0
    started_at: str = ""
    last_snapshot: str = ""

    def note(self, stance: str, net: float) -> None:
        entry = self.by_stance.setdefault(
            stance or "UNKNOWN", {"trades": 0, "net": 0.0, "wins": 0}
        )
        entry["trades"] += 1
        entry["net"] = round(entry["net"] + net, 2)
        if net > 0:
            entry["wins"] += 1


def _chain_for(index: str, cache: dict):
    """The option chain for one index, fetched at most once per cycle.

    Fetched here rather than read off the snapshot: the engine does not publish
    its chain frame, and my first version of this looked for a key
    (``_chain_frame``) that does not exist. The effect would have been silent
    and bad -- every position whose tick was unavailable would have returned
    "unavailable" forever, so no stop or target could ever fire and the whole
    book would have ridden unpriced to square-off.
    """
    key = index.upper()
    if key in cache:
        return cache[key]
    try:
        from intraday_advisor.nse_option_chain import suggest_option_trade

        cache[key] = (suggest_option_trade(key) or {}).get("df")
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Chain fetch failed for %s: %s", key, exc)
        cache[key] = None
    return cache[key]


def _premium_now(position, chain_cache: dict) -> tuple[float | None, str]:
    """Live premium for a held contract: tick first, chain second.

    A held position MUST be priceable, or it can never be exited on its own
    terms. The tick stream needs an option token, which is only recorded when
    recording is on; the chain lookup is the fallback that makes an exit
    possible regardless.
    """
    from intraday_advisor import live_ticks

    if position.option_token:
        price, age = live_ticks.live_price(position.option_token)
        if price:
            return float(price), f"tick {age:.0f}s"

    frame = _chain_for(position.index_name, chain_cache)
    if frame is not None and not getattr(frame, "empty", True):
        column = "ce_ltp" if position.option_type.upper() == "CE" else "pe_ltp"
        row = frame[frame["strike"] == float(position.strike)]
        if not row.empty:
            value = float(row[column].iloc[0])
            if value > 0:
                return value, "chain"
    return None, "unavailable"


def run_session(
    *, capital: float, db_path: str, max_open: int, interval_seconds: float,
    lots: int, dry_run: bool = False,
) -> DeskState:
    from intraday_advisor.engine import (
        Engine,
        is_market_open,
        minutes_to_square_off,
        now_ist,
    )
    from intraday_advisor.ledger.schema import init_ledger
    from intraday_advisor.positions import (
        close_position,
        evaluate_live,
        load_open_positions,
        open_position,
    )

    init_ledger(db_path)
    engine = Engine(available_capital=capital, record=True, db_path=db_path)
    state = DeskState(started_at=now_ist().isoformat(timespec="seconds"))

    stopping = {"flag": False}

    def _stop(signum, frame):  # noqa: ANN001, ARG001
        LOG.info("Signal received; flattening and stopping.")
        stopping["flag"] = True

    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        try:
            signal_module.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    LOG.info("Paper desk starting. capital=Rs %s max_open=%s lots=%s db=%s",
             f"{capital:,.0f}", max_open, lots, db_path)

    while not stopping["flag"]:
        try:
            snapshot = engine.snapshot()
        except Exception as exc:  # noqa: BLE001
            state.errors += 1
            LOG.warning("Snapshot failed (%s); retrying.", exc)
            time.sleep(interval_seconds)
            continue

        state.last_snapshot = now_ist().isoformat(timespec="seconds")
        remaining = minutes_to_square_off()
        open_now = load_open_positions(db_path=db_path)

        # One chain fetch per index per cycle, shared by every position on it.
        chain_cache: dict = {}

        # --- manage what is already on --------------------------------- #
        flatten = (
            remaining is not None and remaining <= FLATTEN_BEFORE_CLOSE
        ) or not is_market_open()

        for position in open_now:
            premium, source = _premium_now(position, chain_cache)
            if premium is None:
                # Loud, because an unpriceable position cannot be exited on its
                # own terms and will ride to square-off whatever it does.
                LOG.warning(
                    "UNPRICEABLE %s %.0f %s -- no tick and no chain quote; it "
                    "cannot hit its stop or target.",
                    position.index_name, float(position.strike),
                    position.option_type,
                )
                continue
            live = evaluate_live(
                position, current_premium=premium, premium_source=source,
                underlying_direction="WAIT", costs=engine._costs,
            )
            reason = None
            if live.status == "TARGET_HIT":
                reason = "target"
            elif live.status == "STOP_HIT":
                reason = "stop"
            elif flatten:
                reason = "square-off"

            if reason and not dry_run:
                result = close_position(
                    position.position_id, exit_premium=premium,
                    exit_reason=reason, db_path=db_path,
                )
                if result:
                    net = float(result.get("net_pnl") or 0.0)
                    state.closed += 1
                    state.realised = round(state.realised + net, 2)
                    stance = getattr(position, "notes", "") or "UNKNOWN"
                    state.note(stance, net)
                    LOG.info(
                        "CLOSE %-9s %s%s %s @ %.2f (%s) net Rs %+,.0f | "
                        "day Rs %+,.0f",
                        reason, position.index_name, f" {position.strike:.0f}",
                        position.option_type, premium, source, net,
                        state.realised,
                    )

        # --- open new ones, while there is time for them to work --------- #
        can_open = (
            is_market_open()
            and remaining is not None
            and remaining > NO_NEW_TRADES_BEFORE_CLOSE
            and len(load_open_positions(db_path=db_path)) < max_open
        )
        if can_open:
            for sig in snapshot.get("signals", []):
                if len(load_open_positions(db_path=db_path)) >= max_open:
                    break
                if sig.get("direction") not in ("CALL", "PUT"):
                    continue
                plan = sig.get("plan") or {}
                if not plan.get("affordable"):
                    continue
                entry = float(sig.get("entry_premium") or 0)
                if entry <= 0:
                    continue

                # Both sides, deliberately: a sample conditioned on the
                # system's own veto cannot measure the veto.
                stance = ((sig.get("agent") or {}).get("stance")) or "UNKNOWN"

                # One position per contract at a time.
                already = any(
                    p.index_name == sig["index"]
                    and p.option_type == sig["option_type"]
                    and float(p.strike) == float(sig["strike"])
                    for p in load_open_positions(db_path=db_path)
                )
                if already:
                    continue

                if dry_run:
                    LOG.info("WOULD OPEN %s %s %s @ %.2f (stance %s)",
                             sig["index"], sig["strike"], sig["option_type"],
                             entry, stance)
                    continue

                try:
                    open_position(
                        index_name=sig["index"], option_type=sig["option_type"],
                        strike=float(sig["strike"]), entry_premium=entry,
                        lots=lots, lot_size=int(sig.get("lot_size") or 0) or 65,
                        target_pct=float(sig.get("target_pct") or 0.25),
                        stop_pct=float(sig.get("stop_pct") or 0.20),
                        option_token=sig.get("option_token"),
                        signal_id=sig.get("signal_id"),
                        notes=stance,          # the verdict, for the breakdown
                        db_path=db_path,
                    )
                    state.opened += 1
                    LOG.info(
                        "OPEN  %-9s %s %.0f %s @ %.2f  score %s  stance %s",
                        sig.get("agreement", ""), sig["index"],
                        float(sig["strike"]), sig["option_type"], entry,
                        sig.get("score"), stance,
                    )
                except Exception as exc:  # noqa: BLE001
                    state.errors += 1
                    LOG.warning("Could not open %s: %s", sig.get("index"), exc)

        # --- stop when the session is genuinely over --------------------- #
        if not is_market_open() and not load_open_positions(db_path=db_path):
            if state.opened or state.closed:
                LOG.info("Market closed and flat. Session over.")
                break

        time.sleep(interval_seconds)

    return state


def grade_its_own_day(db_path: str) -> dict:
    """Grade the signals this desk recorded, before reporting.

    Without this the day's work sits ungraded in the paper ledger and the
    summary can only report the positions the desk happened to take -- not the
    score-versus-outcome relationship, which is the measurement that matters.
    It is the same mistake that left 151 signals ungraded in the live ledger
    for days: a grading step nobody runs is a grading step that does not exist.
    """
    try:
        from intraday_advisor import auto_grade

        return auto_grade.grade_due(db_path)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not grade the session: %s", exc)
        return {"errors": [{"error": str(exc)[:200]}]}


def summarise(state: DeskState, db_path: str) -> str:
    from intraday_advisor.ledger.report import expectancy_report
    from intraday_advisor.positions import session_summary

    lines = [
        "=" * 68,
        "PAPER SESSION SUMMARY",
        "=" * 68,
        f"  opened            {state.opened}",
        f"  closed            {state.closed}",
        f"  realised net      Rs {state.realised:+,.2f}",
        f"  snapshot errors   {state.errors}",
    ]
    if state.by_stance:
        lines += ["", "  BY AGENT STANCE (the question this run exists to ask)"]
        for stance, entry in sorted(state.by_stance.items()):
            wins = entry["wins"]
            n = entry["trades"]
            lines.append(
                f"    {stance:9} {n:3} trade(s)  {wins} win(s)  "
                f"net Rs {entry['net']:+,.2f}"
            )
        lines.append(
            "    If VETO is the losing bucket, the agent is already finding "
            "the edge."
        )
    try:
        today = session_summary(db_path=db_path)
        lines += ["", f"  ledger today      {today}"]
        report = expectancy_report(db_path=db_path)
        if report.get("trades"):
            lines += [f"  all-time verdict  {report.get('verdict')}"]
    except Exception:  # noqa: BLE001
        pass
    lines.append("=" * 68)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital", type=float, default=20_000.0)
    parser.add_argument("--lots", type=int, default=1,
                        help="fixed; see the module docstring on why")
    parser.add_argument("--max-open", type=int, default=4)
    parser.add_argument("--interval", type=float, default=30.0,
                        help="seconds between snapshots")
    parser.add_argument("--db", default="data/paper_desk.sqlite",
                        help="a SEPARATE ledger; never the live one by default")
    parser.add_argument("--dry-run", action="store_true",
                        help="log decisions without recording positions")
    parser.add_argument("--log", default="logs/paper_desk.log")
    args = parser.parse_args()

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(args.log, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    from intraday_advisor.envfile import load_env
    load_env()

    state = run_session(
        capital=args.capital, db_path=args.db, max_open=args.max_open,
        interval_seconds=args.interval, lots=args.lots, dry_run=args.dry_run,
    )
    graded = grade_its_own_day(args.db)
    for entry in graded.get("graded", []):
        logging.getLogger("paper_desk").info(
            "Graded %s: %s signal(s)", entry["session"], entry["graded"]
        )

    report = summarise(state, args.db)
    print(report)
    logging.getLogger("paper_desk").info("\n%s", report)
    Path(args.log).with_suffix(".summary.json").write_text(
        json.dumps({"opened": state.opened, "closed": state.closed,
                    "realised": state.realised,
                    "by_stance": state.by_stance,
                    "errors": state.errors}, indent=1),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
