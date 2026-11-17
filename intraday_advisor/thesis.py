"""One decision brief per signal: why take it, why not, and what decides it.

The dashboard already computed every input below and scattered them across a
dozen panels -- regime in one card, the chain in another, costs in a hint line,
the agent's objections in a collapsed block, the measured record at the bottom
of the page. Reading a trade meant assembling the argument yourself, which is
the part most likely to be skipped when the setup looks good.

This composes them into a single argument in a fixed order: each check states
what was looked at, what it found, and whether that supports the trade, and the
verdict is derived from the checks rather than written separately -- so the
headline can never say something the evidence underneath does not.

What the confidence figure is, and is not
-----------------------------------------
``agreement`` is the share of checks that support the trade. It is **agreement
among the evidence, not a probability of profit** -- the same distinction
``regime.py`` draws for conviction. Nothing here knows the probability of
profit; the closest measured thing is the forward-odds check, which is reported
as its own row with its own number. No amount of analysis makes an intraday
option trade certain, and a brief that claimed otherwise would be the one
component of this system that was lying.

Like the agent layer, this can only describe a trade. It never creates one and
never changes a score.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("thesis")

PASS, FAIL, NEUTRAL = "pass", "fail", "neutral"

#: Forward-odds replays for the live brief. Fewer than the CLI's 10,000 because
#: this runs inside the snapshot loop; the Monte Carlo error at 2,000 is about
#: one point on a ~30% probability, well inside what anyone should act on.
LIVE_REPLAYS = 2_000

#: Fallback when a contract's own delta cannot be computed. An at-the-money
#: option moves about half as far as its underlying -- true only at the money,
#: which is why :func:`option_delta` is used wherever a price is available.
ATM_DELTA = 0.5

#: Deltas outside this band are clamped. Below it the replay would need an
#: index move so large that every path ends "undecided"; a deep wing's delta is
#: also the least stable number Black-Scholes produces.
MIN_DELTA, MAX_DELTA = 0.08, 0.95


def option_delta(*, spot, strike, premium, days_to_expiry, option_type) -> tuple:
    """This contract's own delta, from its own traded price.

    Returns ``(delta, basis)`` where delta is a positive magnitude.

    The first version of the odds check used 0.5 for every contract. That is
    right at the money and wrong everywhere else, in the direction that
    flatters cheap options: a Rs 12 wing with a real delta near 0.15 needs
    about **three times** the index move the 0.5 assumption gave it, so its
    target looked far more reachable than it is. The implied volatility is
    backed out of the contract's premium (bisection, as elsewhere here) and
    the delta read off that, so the translation from premium to index levels
    uses the option actually being bought.
    """
    import math

    spot_v, strike_v = _num(spot), _num(strike)
    price, days = _num(premium), _num(days_to_expiry)
    if not (spot_v and strike_v and price and days and days > 0):
        return ATM_DELTA, "assumed 0.50 (no price or expiry to compute it from)"
    try:
        from intraday_advisor.options_math import (
            RISK_FREE_RATE, _norm_cdf, implied_vol)

        years = days / 365.0
        vol = implied_vol(price, spot_v, strike_v, years, option_type)
        if not vol or vol <= 0:
            return ATM_DELTA, "assumed 0.50 (price implies no sane volatility)"
        d1 = (math.log(spot_v / strike_v)
              + (RISK_FREE_RATE + vol * vol / 2.0) * years) / (vol * math.sqrt(years))
        call = _norm_cdf(d1)
        raw = call if str(option_type).upper() == "CE" else 1.0 - call
        delta = min(MAX_DELTA, max(MIN_DELTA, raw))
        return round(delta, 3), f"delta {delta:.2f} from this contract's own price"
    except Exception as exc:  # noqa: BLE001
        logger.debug("Delta unavailable: %s", exc)
        return ATM_DELTA, "assumed 0.50 (could not be computed)"

_ODDS_CACHE: dict = {}


def _check(key: str, label: str, state: str, detail: str, *,
           blocker: bool = False) -> dict:
    return {"key": key, "label": label, "state": state, "detail": detail,
            "blocker": bool(blocker and state == FAIL)}


def _num(value, default=None):
    try:
        if value is None:
            return default
        out = float(value)
        return default if out != out else out
    except (TypeError, ValueError):
        return default


# ================================================================== checks ==

def _trend(signal: dict, bullish: bool) -> dict:
    regime = signal.get("regime") or {}
    bias = str(regime.get("bias") or "NEUTRAL").upper()
    name = str(regime.get("regime") or "UNKNOWN").replace("_", " ").title()
    conviction = _num(regime.get("conviction"), 0) or 0
    want = "BULLISH" if bullish else "BEARISH"
    against = "BEARISH" if bullish else "BULLISH"
    if want in bias:
        return _check("trend", "Market trend", PASS,
                      f"{name}, leaning the same way as this trade "
                      f"({conviction:.0f}% of timeframe inputs agree).")
    if against in bias:
        return _check("trend", "Market trend", FAIL,
                      f"{name}, leaning AGAINST this trade "
                      f"({conviction:.0f}% agreement). Buying into a trend "
                      f"running the other way is the most common losing setup.")
    return _check("trend", "Market trend", NEUTRAL,
                  f"{name} with no clear lean — the wider market neither "
                  f"helps nor hurts this trade.")


def _price(signal: dict, bullish: bool) -> dict:
    score = _num(signal.get("underlying_score"))
    if score is None:
        return _check("price", "Price action", NEUTRAL,
                      "No price score available for this bar.")
    strong = score >= 60 if bullish else score <= 40
    weak = score <= 45 if bullish else score >= 55
    side = "bullish" if score >= 55 else "bearish" if score <= 45 else "mixed"
    if strong:
        return _check("price", "Price action", PASS,
                      f"Indicators read {side} ({score:.0f}/100) on the last "
                      f"closed 5-minute candle.")
    if weak:
        return _check("price", "Price action", FAIL,
                      f"Indicators read {side} ({score:.0f}/100) — they do not "
                      f"support this direction.")
    return _check("price", "Price action", NEUTRAL,
                  f"Indicators are mixed ({score:.0f}/100).")


def _chain(signal: dict, bullish: bool) -> dict:
    bias = str(signal.get("chain_bias") or "NEUTRAL").upper()
    pcr = _num(signal.get("pcr"))
    tail = f" Put-call ratio {pcr:.2f}." if pcr else ""
    want = "BULLISH" if bullish else "BEARISH"
    against = "BEARISH" if bullish else "BULLISH"
    if want in bias:
        return _check("chain", "Option chain", PASS,
                      f"Open interest is positioned the same way.{tail}")
    if against in bias:
        return _check("chain", "Option chain", FAIL,
                      f"Open interest is positioned AGAINST this trade — the "
                      f"writers are betting the other way.{tail}")
    return _check("chain", "Option chain", NEUTRAL,
                  f"Open interest shows no clear side.{tail}")


def _volatility(signal: dict) -> dict:
    premium = signal.get("variance_premium") or {}
    points = _num(premium.get("premium_points") if isinstance(premium, dict)
                  else None)
    if points is None:
        return _check("volatility", "Option price", NEUTRAL,
                      "Not enough data to judge whether options are dear or "
                      "cheap right now.")
    if points > 2:
        return _check("volatility", "Option price", FAIL,
                      f"Options are DEAR: the market charges about "
                      f"{points:.1f} volatility points more than the index has "
                      f"actually been moving. A buyer pays that premium.")
    if points < -1:
        return _check("volatility", "Option price", PASS,
                      f"Options are cheap relative to how much the index has "
                      f"been moving ({points:+.1f} points).")
    return _check("volatility", "Option price", NEUTRAL,
                  f"Options are fairly priced against recent movement "
                  f"({points:+.1f} points).")


def _cost(signal: dict) -> dict:
    plan = signal.get("plan") or {}
    if not plan:
        return _check("cost", "Costs", NEUTRAL, "No sized plan to cost.")
    if not plan.get("affordable", True):
        return _check("cost", "Costs", FAIL,
                      "One lot costs more than the capital available.",
                      blocker=True)
    rr = _num(plan.get("net_rr"), 0) or 0
    drag = (_num(plan.get("cost_drag"), 0) or 0) * 100
    breakeven = _num(plan.get("breakeven"))
    target = _num(signal.get("target_premium"))
    if breakeven and target and breakeven >= target:
        return _check("cost", "Costs", FAIL,
                      f"Breakeven ₹{breakeven:.2f} is at or above the target "
                      f"₹{target:.2f} — this trade cannot make money even if "
                      f"it works.", blocker=True)
    if rr < 1:
        return _check("cost", "Costs", FAIL,
                      f"After brokerage and spread the reward is only "
                      f"{rr:.2f}× the risk. You would be risking more than you "
                      f"can win.", blocker=True)
    return _check("cost", "Costs", PASS,
                  f"After all costs the reward is {rr:.2f}× the risk; costs "
                  f"take {drag:.2f}% of the position.")


def _risk(signal: dict) -> dict:
    plan = signal.get("plan") or {}
    pct = _num(plan.get("risk_pct_of_capital"))
    if pct is None:
        return _check("risk", "Account risk", NEUTRAL, "No sized plan.")
    if plan.get("stop_protects_gain"):
        return _check("risk", "Account risk", PASS,
                      "The stop sits above breakeven, so it locks in a gain.")
    share = pct * 100
    if share >= 35:
        return _check("risk", "Account risk", FAIL,
                      f"One stop-out would take {share:.0f}% of your capital. "
                      f"The account cannot survive being wrong twice.",
                      blocker=True)
    if share >= 10:
        return _check("risk", "Account risk", FAIL,
                      f"One stop-out costs {share:.1f}% of capital — heavy. "
                      f"Lots are indivisible, so the only way to cut this is a "
                      f"cheaper contract.")
    return _check("risk", "Account risk", PASS,
                  f"One stop-out costs {share:.1f}% of capital.")


def _time(signal: dict, minutes_left) -> dict:
    days = _num((signal.get("volatility") or {}).get("days_to_expiry"))
    minutes = _num(minutes_left)
    if minutes is not None and minutes <= 0:
        return _check("time", "Time left", FAIL,
                      "Past the intraday square-off.", blocker=True)
    if days is not None and days <= 0:
        return _check("time", "Time left", FAIL,
                      "Expiry day: the premium decays by the hour whichever "
                      "way the index goes, so a percentage stop measures time "
                      "passing, not the trade being wrong.", blocker=True)
    if minutes is not None and minutes <= 45:
        return _check("time", "Time left", FAIL,
                      f"Only {minutes:.0f} minutes to square-off — the move "
                      f"has to arrive almost immediately.")
    if days is not None and days < 1:
        return _check("time", "Time left", FAIL,
                      f"{days:.1f} days to expiry: decay alone can hit the "
                      f"stop.")
    parts = []
    if minutes is not None:
        parts.append(f"{minutes:.0f} minutes to square-off")
    if days is not None:
        parts.append(f"{days:.1f} days to expiry")
    return _check("time", "Time left", PASS if parts else NEUTRAL,
                  (", ".join(parts) + ".") if parts else "Session time unknown.")


def _events(event_risk: dict) -> dict:
    risk = event_risk or {}
    notes = []
    if risk.get("policy_today"):
        notes.append("RBI policy decision today")
    names = risk.get("earnings_today") or []
    if names:
        notes.append(f"{', '.join(names)} report(s) today")
    if notes:
        return _check("events", "Scheduled events", FAIL,
                      "; ".join(notes) + " — the index can jump on news that "
                      "is nothing to do with the chart.")
    if risk.get("stale_policy_list") or risk.get("degraded"):
        return _check("events", "Scheduled events", NEUTRAL,
                      "The event calendar could not be checked today.")
    return _check("events", "Scheduled events", PASS,
                  "No heavyweight results or policy decision today.")


def _agent(signal: dict) -> dict:
    agent = signal.get("agent") or {}
    stance = str(agent.get("stance") or "").upper()
    concerns = agent.get("concerns") or []
    first = str(concerns[0]) if concerns else ""
    if stance == "VETO":
        return _check("agent", "Risk checklist", FAIL,
                      f"VETO. {first}", blocker=True)
    if stance == "CAUTION":
        return _check("agent", "Risk checklist", NEUTRAL,
                      f"Caution. {first}" if first else "Caution.")
    if stance == "PROCEED":
        return _check("agent", "Risk checklist", PASS,
                      "No structural objection found.")
    return _check("agent", "Risk checklist", NEUTRAL,
                  "The risk checklist has not ruled on this bar.")


def _record(performance: dict) -> dict:
    perf = performance or {}
    trades = int(_num(perf.get("trades"), 0) or 0)
    expectancy = _num(perf.get("expectancy"))
    if trades < 30 or expectancy is None:
        return _check("record", "Measured track record", NEUTRAL,
                      f"Only {trades} graded signal(s) so far — too few to "
                      f"say whether these signals make money.")
    if expectancy < 0:
        return _check("record", "Measured track record", FAIL,
                      f"Across {trades} graded signals this system has LOST "
                      f"an average of ₹{abs(expectancy):,.0f} per lot after "
                      f"costs. That is the measured record, not an opinion.")
    return _check("record", "Measured track record", PASS,
                  f"Across {trades} graded signals the average is "
                  f"₹{expectancy:,.0f} per lot after costs.")


def _odds(signal: dict, bullish: bool, minutes_left, db_path) -> dict | None:
    """Replay the rest of the session; compare against what the costs demand.

    Cached per bar: the answer cannot change until a new candle closes, and the
    replay is the one expensive thing in the brief.
    """
    plan = signal.get("plan") or {}
    entry = _num(signal.get("entry_premium"))
    stop = _num(signal.get("stop_premium"))
    target = _num(signal.get("target_premium"))
    spot = _num(signal.get("spot"))
    lot = _num(signal.get("lot_size"))
    if not (entry and stop and target and spot and lot) or db_path is None:
        return None
    if not (stop < entry < target):
        return None

    key = (signal.get("index"), signal.get("bar_time"), bullish,
           round(entry, 1), round(stop, 1), round(target, 1))
    if key in _ODDS_CACHE:
        return _ODDS_CACHE[key]

    try:
        from intraday_advisor.forward_odds import assess, pool_bars
        from intraday_advisor.index_options import INDEX_CONFIG

        bars = pool_bars(INDEX_CONFIG[signal["index"]]["yahoo"], db_path=db_path)
        delta, delta_basis = option_delta(
            spot=spot, strike=signal.get("strike"), premium=entry,
            days_to_expiry=(signal.get("volatility") or {}).get("days_to_expiry"),
            option_type=signal.get("option_type") or ("CE" if bullish else "PE"),
        )
        up = (target - entry) / delta
        down = (entry - stop) / delta
        minutes = _num(minutes_left, 200) or 200
        result = assess(
            bars, entry_premium=entry, stop_premium=stop,
            target_premium=target,
            quantity=lot * int(_num(plan.get("lots"), 1) or 1),
            entry=spot,
            stop=spot - down if bullish else spot + down,
            target=spot + up if bullish else spot - up,
            bars_left=max(1, int(minutes // 5)), replays=LIVE_REPLAYS,
        )
    except Exception as exc:  # noqa: BLE001 - the brief must survive without it
        logger.debug("Forward odds unavailable: %s", exc)
        return None

    if not result.odds or not result.odds.replays:
        check = None
    else:
        hit = result.odds.hit_rate * 100
        need = result.breakeven_rate_net * 100
        gap = result.edge_points
        open_share = result.odds.neither * 100
        detail = (
            f"Replaying the rest of today {LIVE_REPLAYS:,} times from the "
            f"last {result.odds.sessions_in_pool} sessions, the target came "
            f"first {hit:.0f}% of the time it was decided; costs demand "
            f"{need:.0f}%. That is {gap:+.1f} points"
            f"{' — and ' + format(open_share, '.0f') + '% of replays reached the close undecided' if open_share > 40 else ''}"
            f". The index must move about {up:,.0f} points for the target "
            f"and {down:,.0f} against for the stop ({delta_basis})."
        )
        state = PASS if gap >= 5 else FAIL if gap <= -5 else NEUTRAL
        check = _check("odds", "Measured odds", state, detail)
        check["hit_rate"] = round(hit, 1)
        check["needed"] = round(need, 1)

    if len(_ODDS_CACHE) > 64:
        _ODDS_CACHE.clear()
    _ODDS_CACHE[key] = check
    return check


# ================================================================== thesis ==

def build_thesis(
    signal: dict, *, event_risk: dict | None = None,
    performance: dict | None = None, minutes_to_square_off=None,
    mode: str = "", db_path=None,
) -> dict:
    """The decision brief for one signal. Never raises.

    Returns ``{verdict, tone, headline, summary, agreement, checks, for,
    against, blockers, disclaimer}``.
    """
    direction = str(signal.get("direction") or "WAIT").upper()
    index = signal.get("index", "")

    if direction not in ("CALL", "PUT"):
        reasons = [str(w) for w in (signal.get("warnings") or [])[:3]]
        score = _num(signal.get("score"), 0) or 0
        return {
            "verdict": "NO TRADE", "tone": "neutral",
            "headline": f"No trade on {index} right now",
            "summary": (
                f"The evidence does not line up on either side (score "
                f"{score:.0f}/100). Waiting is a position too — most of the "
                f"day looks like this, and a forced trade pays the costs "
                f"without the setup."
            ),
            "agreement": None, "checks": [], "for": [], "against": reasons,
            "blockers": [], "disclaimer": _DISCLAIMER,
        }

    bullish = direction == "CALL"
    checks = [
        _trend(signal, bullish),
        _price(signal, bullish),
        _chain(signal, bullish),
        _volatility(signal),
        _cost(signal),
        _risk(signal),
        _time(signal, minutes_to_square_off),
        _events(event_risk or {}),
        _agent(signal),
        _record(performance or {}),
    ]
    odds = _odds(signal, bullish, minutes_to_square_off, db_path)
    if odds:
        checks.insert(4, odds)

    if mode and mode != "LIVE":
        checks.insert(0, _check(
            "data", "Data", FAIL,
            ("The market is closed — these are the last traded prices, not "
             "ones you can trade at." if mode == "CLOSED" else
             "Prices are estimated. Nothing on this screen is tradeable "
             "until live data returns."), blocker=True))

    passed = [c for c in checks if c["state"] == PASS]
    failed = [c for c in checks if c["state"] == FAIL]
    blockers = [c for c in checks if c["blocker"]]
    agreement = round(len(passed) / len(checks) * 100) if checks else 0

    contract = f"{index} {signal.get('strike'):g} {signal.get('option_type')}" \
        if signal.get("strike") else index
    if blockers:
        verdict, tone = "DO NOT TAKE", "danger"
        headline = f"Do not take {contract}"
        summary = (
            f"{len(blockers)} check(s) rule this trade out on their own, "
            f"whatever the others say: {blockers[0]['detail']}"
        )
    elif agreement >= 70 and not failed:
        verdict, tone = "EVIDENCE SUPPORTS", "good"
        headline = f"The evidence supports {contract}"
        summary = (
            f"{len(passed)} of {len(checks)} checks support it and none "
            f"object. That is as good as this screen can say — it is still a "
            f"trade that can lose, so the stop is not optional."
        )
    elif agreement >= 50:
        verdict, tone = "MIXED", "warn"
        headline = f"Mixed evidence on {contract}"
        summary = (
            f"{len(passed)} of {len(checks)} checks support it, "
            f"{len(failed)} object. If you take it, take the smallest size "
            f"and treat the objections below as the reasons it may fail."
        )
    else:
        verdict, tone = "WEAK", "warn"
        headline = f"Weak case for {contract}"
        summary = (
            f"Only {len(passed)} of {len(checks)} checks support it. The "
            f"signal exists, but most of the evidence does not back it."
        )

    return {
        "verdict": verdict, "tone": tone, "headline": headline,
        "summary": summary, "agreement": agreement, "checks": checks,
        "for": [f"{c['label']}: {c['detail']}" for c in passed],
        "against": [f"{c['label']}: {c['detail']}" for c in failed],
        "blockers": [c["label"] for c in blockers],
        "disclaimer": _DISCLAIMER,
    }


_DISCLAIMER = (
    "Agreement is how many of the checks support the trade. It is not the "
    "chance of profit — no analysis makes an intraday option trade certain."
)


def quote_leg(
    signal: dict, *, strike: float, option_type: str, premium: float,
    lots: int, target_pct: float, stop_pct: float, available_capital: float,
    costs=None, event_risk: dict | None = None, performance: dict | None = None,
    minutes_to_square_off=None, mode: str = "", db_path=None,
) -> dict:
    """Exact numbers and a brief for ANY contract, not only the engine's.

    The order ticket first showed a flat fee estimate and, for any strike the
    engine had not named, the sentence "its costs and odds were not analysed".
    That left the contracts most worth checking -- the ones you chose yourself
    -- as the only ones with no analysis behind them. This runs the same cost
    model, sizing and checks on whatever leg is in the ticket.

    The index context (trend, chain, events, record) is the signal's; the
    contract-specific rows (costs, risk, odds, time) are recomputed for this
    leg. The agent's verdict is carried over only when the leg *is* the
    engine's contract, because that verdict was about that contract.
    """
    from intraday_advisor.ledger.sizing import plan_position

    side = str(option_type or "CE").upper()
    direction = "CALL" if side == "CE" else "PUT"
    entry = float(premium)
    lots = max(1, int(lots))
    lot_size = int(signal.get("lot_size") or 0)
    stop = round(entry * (1 - float(stop_pct)), 2)
    target = round(entry * (1 + float(target_pct)), 2)
    out: dict = {"index": signal.get("index"), "strike": float(strike),
                 "option_type": side, "premium": entry, "lots": lots,
                 "stop_premium": stop, "target_premium": target, "notes": []}
    if entry <= 0 or lot_size <= 0:
        out["error"] = "A premium and a lot size are needed to price this."
        return out

    needed = entry * lot_size * lots
    # Sized at the lots ASKED for: the question in the ticket is "what does
    # this order cost me", not "what would you have sized".
    try:
        plan = plan_position(
            available_capital=max(float(available_capital or 0), needed),
            entry_premium=entry, stop_premium=stop, target_premium=target,
            lot_size=lot_size, max_lots=lots, costs=costs,
        ).as_dict()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"Could not size this order: {exc}"
        return out

    capital = float(available_capital or 0)
    if capital:
        plan["risk_pct_of_capital"] = round(plan["risk_amount"] / capital, 4)
        plan["affordable"] = needed <= capital
        if needed > capital:
            out["notes"].append(
                f"This order needs ₹{needed:,.0f}; the capital set is "
                f"₹{capital:,.0f}.")
    out["plan"] = {k: plan.get(k) for k in (
        "lots", "quantity", "deployed_capital", "risk_amount", "reward_amount",
        "risk_pct_of_capital", "net_rr", "cost_drag", "breakeven",
        "round_trip_cost", "affordable", "stop_protects_gain", "locked_gain")}

    same = (float(signal.get("strike") or 0) == float(strike)
            and str(signal.get("option_type") or "").upper() == side
            and str(signal.get("direction") or "").upper() == direction)
    leg = dict(signal)
    leg.update({
        "direction": direction, "option_type": side, "strike": float(strike),
        "entry_premium": entry, "stop_premium": stop, "target_premium": target,
        "plan": plan,
        "agent": signal.get("agent") if same else {
            "stance": "", "concerns": []},
    })
    thesis = build_thesis(
        leg, event_risk=event_risk, performance=performance,
        minutes_to_square_off=minutes_to_square_off, mode=mode, db_path=db_path)
    engine_direction = str(signal.get("direction") or "WAIT").upper()
    if engine_direction not in ("CALL", "PUT"):
        out["notes"].append(
            f"The engine reads no trade on {signal.get('index')} right now — "
            f"this order is your own decision, not a signal.")
    elif engine_direction != direction:
        out["notes"].append(
            f"This is AGAINST the engine's {engine_direction} read.")
    elif not same:
        out["notes"].append(
            f"Same direction as the engine's read, on a different strike.")
    out["thesis"] = thesis
    out["is_engine_contract"] = same
    return out


def ladder(chain: dict, *, around: int = 6) -> list[dict]:
    """A compact strike ladder from a chain result, for the order ticket.

    The engine names one contract; this is what lets the desk open any other
    strike on either side without the engine having to recommend it.
    """
    try:
        frame = chain.get("df") if isinstance(chain, dict) else None
        if frame is None or len(frame) == 0:
            return []
        atm = _num(chain.get("atm") or chain.get("rec_strike"))
        ordered = frame.sort_values("strike")
        if atm is not None:
            position = int((ordered["strike"] - atm).abs().values.argmin())
            ordered = ordered.iloc[max(0, position - around):position + around + 1]
        rows = []
        for _, row in ordered.iterrows():
            rows.append({
                "strike": float(row["strike"]),
                "ce": round(float(row.get("ce_ltp") or 0), 2),
                "pe": round(float(row.get("pe_ltp") or 0), 2),
                "ce_oi": float(row.get("ce_oi") or 0),
                "pe_oi": float(row.get("pe_oi") or 0),
                "atm": atm is not None and float(row["strike"]) == atm,
            })
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.debug("Strike ladder unavailable: %s", exc)
        return []
