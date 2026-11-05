"""The free agent that needs no API, no key, and no network.

This is deliberately the default. It encodes the checks a disciplined trader
runs before every entry — the ones that are skipped precisely when skipping
them is most expensive, because the setup looks good and the hand is already
moving.

It is free in the way that matters: deterministic, instant, offline, and
reproducible. Run it on the same brief twice and you get the same verdict,
which is more than can be said for any language model, and it means a veto can
be argued with rather than merely obeyed.

An LLM agent (see :mod:`intraday_advisor.agents.llm`) adds judgement about
things that resist encoding — an unusual combination of conditions, a reading
that is technically fine but contextually odd. It does not replace this.
"""

from __future__ import annotations

from intraday_advisor.agents.base import (
    CAUTION,
    PROCEED,
    VETO,
    AgentVerdict,
    TradeBrief,
    timed,
)

# Thresholds. Each is a judgement call, written down so it can be argued with
# and changed, rather than buried in a condition.
MAX_RISK_PCT_VETO = 0.35        # one stop-out taking a third of the account
MAX_RISK_PCT_CAUTION = 0.08
MIN_NET_RR = 1.0                # below this you risk more than you can make
GOOD_NET_RR = 1.2
MAX_COST_DRAG = 0.05            # round-trip costs above 5% of position
LATE_SESSION_MINUTES = 30       # too little time left for the move to arrive
MIN_SCORE = 45


class RuleAgent:
    """Deterministic pre-trade checklist. Free, offline, reproducible."""

    name = "rules"

    @timed
    def review(self, brief: TradeBrief) -> AgentVerdict:
        verdict = AgentVerdict(provider="rules", model="checklist-v1")

        if not brief.tradable:
            verdict.stance = PROCEED
            verdict.summary = "No trade proposed — nothing to check."
            return verdict

        vetoes: list[str] = []
        cautions: list[str] = []
        supports: list[str] = []

        # --- 1. Can this trade make money at all? ---------------------------
        if brief.breakeven and brief.target_premium and brief.breakeven >= brief.target_premium:
            vetoes.append(
                f"Breakeven ₹{brief.breakeven:.2f} is at or above the target "
                f"₹{brief.target_premium:.2f}. After costs this trade cannot "
                f"make money even when it works."
            )
        if brief.net_rr is not None and brief.net_rr < MIN_NET_RR:
            vetoes.append(
                f"Net reward:risk is {brief.net_rr:.2f}:1 after costs — you risk "
                f"more than you stand to make, which needs a win rate above 50% "
                f"just to break even."
            )
        elif brief.net_rr is not None and brief.net_rr >= GOOD_NET_RR:
            supports.append(f"Net reward:risk {brief.net_rr:.2f}:1 after costs.")

        if brief.cost_drag is not None and brief.cost_drag > MAX_COST_DRAG:
            vetoes.append(
                f"Round-trip costs are {brief.cost_drag * 100:.1f}% of the "
                f"position. The contract is too cheap, or the size too small, "
                f"for flat per-order brokerage."
            )

        # --- 2. Can the account survive being wrong? ------------------------
        if not brief.affordable:
            vetoes.append("One lot costs more than the capital available.")
        elif brief.risk_pct_of_capital is not None:
            risk = brief.risk_pct_of_capital
            if risk >= MAX_RISK_PCT_VETO:
                vetoes.append(
                    f"One stop-out costs {risk * 100:.1f}% of the account. Three "
                    f"in a row and there is not enough left to recover from."
                )
            elif risk >= MAX_RISK_PCT_CAUTION:
                cautions.append(
                    f"One stop-out costs {risk * 100:.1f}% of the account. "
                    f"Deploying less is the only lever that changes this."
                )
            else:
                supports.append(f"Risk is a survivable {risk * 100:.1f}% of capital.")

        # --- 3. Does the wider market agree? --------------------------------
        wanted = "UP" if brief.direction == "CALL" else "DOWN"
        if brief.regime_bias not in ("NEUTRAL", "unknown"):
            if brief.regime_bias != wanted and brief.regime_conviction >= 40:
                vetoes.append(
                    f"The market regime reads {brief.regime_bias} with "
                    f"{brief.regime_conviction}% agreement, against this "
                    f"{brief.direction}. Fighting an aligned trend is the most "
                    f"expensive habit in intraday trading."
                )
            elif brief.regime_bias != wanted:
                cautions.append(
                    f"Regime leans {brief.regime_bias}, this trade is "
                    f"{brief.direction} — counter-trend."
                )
            else:
                supports.append(
                    f"Market regime ({brief.regime}) agrees with the direction."
                )

        if brief.regime == "RANGE":
            cautions.append(
                "Timeframes disagree, so this is a range. Directional trades "
                "fail most often here."
            )
        if brief.regime == "VOLATILE":
            cautions.append(
                "High-volatility regime: stops get hit by noise and a correct "
                "direction can still lose if the move is slow."
            )

        if brief.global_lean not in ("unknown", "NEUTRAL") and brief.global_lean != wanted:
            cautions.append(
                f"Global markets lean {brief.global_lean} against this "
                f"{brief.direction}."
            )

        if brief.breadth_divergence:
            cautions.append(
                "Breadth is diverging from the index — the move is narrow, "
                "and narrow moves reverse."
            )

        if brief.vix_level == "high":
            cautions.append(
                f"India VIX {brief.vix} is in a fear regime. Many consistently "
                f"profitable traders simply stand aside here."
            )

        # --- 4. Is there time for this to work? -----------------------------
        if brief.minutes_to_square_off is not None:
            if brief.minutes_to_square_off <= 0:
                vetoes.append("Past the intraday square-off — no time left.")
            elif brief.minutes_to_square_off <= LATE_SESSION_MINUTES:
                cautions.append(
                    f"Only {brief.minutes_to_square_off} minutes to square-off. "
                    f"A bought option needs the move to arrive quickly, and "
                    f"time decay is steepest at the close."
                )

        # --- 4b. Is the contract about to stop existing? ---------------------
        #
        # The checklist knew about minutes to square-off and nothing at all
        # about days to expiry, although `days_to_expiry` has been computed for
        # IV and spread pricing all along. On expiry day that omission is the
        # difference between a stop and a coin toss.
        #
        # An ATM weekly option is almost entirely extrinsic value, and on 0 DTE
        # that value goes to zero over a handful of hours whichever way the
        # index goes. A stop and target set as percentages of the premium --
        # which is how this project sets them -- then measure decay more than
        # direction: the premium can lose 20% without the underlying moving at
        # all, taking the stop out on a trade that was never wrong.
        if brief.days_to_expiry is not None:
            if brief.days_to_expiry <= 0.0:
                vetoes.append(
                    "Expiry day. This contract expires today, so a "
                    "percentage stop on the premium is measuring time decay "
                    "rather than the trade being wrong — the premium can lose "
                    "20% with the index flat."
                )
            elif brief.days_to_expiry < 1.0:
                cautions.append(
                    f"{brief.days_to_expiry:.1f} days to expiry. Decay is now "
                    f"fast enough to hit a percentage stop on its own, so the "
                    f"stop no longer means what it says."
                )

        # --- 4c. Is something scheduled to hit this session? -----------------
        #
        # NIFTY is cap-weighted, so the day one heavyweight reports is a day the
        # index can gap on one name, and RBI policy lands mid-session by design.
        # These CAUTION rather than veto: a scheduled event is elevated two-way
        # risk, not a structurally unsound trade, and a bought option is not
        # always on the wrong side of one.
        if brief.policy_today:
            cautions.append(
                "RBI policy decision today. The move usually arrives in one "
                "burst mid-session and stops set for a normal day are the "
                "wrong size for it, in both directions."
            )
        if brief.events_today:
            names = ", ".join(brief.events_today)
            cautions.append(
                f"{names} reports today. The index is cap-weighted, so one "
                f"heavyweight's result can move it independently of anything "
                f"on the chart. A date, not a time — treat the whole session "
                f"as exposed."
            )
        if brief.event_calendar_stale:
            cautions.append(
                "The RBI policy date list has run out, so policy days are not "
                "being checked at all. Update RBI_MPC_DATES — this is a blind "
                "spot, not an all-clear."
            )

        # --- 5. Is this real? -----------------------------------------------
        if not brief.data_is_live:
            vetoes.append(
                "The broker feed is not live, so these premiums are estimates. "
                "Nothing here is tradable."
            )
        if brief.score < MIN_SCORE:
            cautions.append(
                f"Confluence score is only {brief.score} — a weak setup."
            )

        # --- 6. Is the edge measured, or still assumed? ---------------------
        if brief.graded_trades < 30:
            cautions.append(
                f"The strategy has only {brief.graded_trades} graded trades, so "
                f"its edge is still unmeasured. Treat position sizes as "
                f"experiments, not convictions."
            )
        elif brief.measured_expectancy is not None and brief.measured_expectancy < 0:
            vetoes.append(
                f"Measured expectancy is ₹{brief.measured_expectancy:.0f} per lot "
                f"— this strategy has lost money over its graded history."
            )
        elif brief.measured_expectancy is not None:
            supports.append(
                f"Measured expectancy ₹{brief.measured_expectancy:.0f}/lot over "
                f"{brief.graded_trades} graded trades."
            )

        # --- verdict ---------------------------------------------------------
        if vetoes:
            verdict.stance = VETO
            verdict.summary = vetoes[0]
            verdict.confidence = min(100, 60 + 10 * len(vetoes))
        elif cautions:
            verdict.stance = CAUTION
            verdict.summary = (
                f"Tradable, with {len(cautions)} thing(s) working against it."
            )
            verdict.confidence = max(30, 70 - 10 * len(cautions))
        else:
            verdict.stance = PROCEED
            verdict.summary = "No objection — the checklist is clean."
            verdict.confidence = 70

        verdict.concerns = vetoes + cautions
        verdict.supports = supports
        return verdict
