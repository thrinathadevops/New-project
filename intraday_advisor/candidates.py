"""Several tradeable contracts per index, not one.

The engine names a single ATM contract per index. That is the right default --
ATM has the cleanest delta and the tightest spread -- but it is not always the
right trade, and it is never the only one. A ₹560 BANKNIFTY ATM put is
unaffordable on a ₹20,000 account; two strikes OTM might cost ₹300 and still
express the same view.

This builds a ranked shortlist of CE and PE contracts for each index, each with
its own sizing, cost drag and net reward:risk, so the choice is visible rather
than decided for you.

Ranking
-------
Three factors, all inspectable:

* **Direction alignment** with the engine's read. Counter-trend contracts are
  shown (you may disagree with the engine) but ranked last.
* **Moneyness.** ATM and 1-2 strikes OTM carry the best intraday movement per
  rupee. Far OTM is a lottery ticket whose cost drag swamps the target; deep
  ITM ties up capital for a smaller percentage move.
* **Affordability and cost drag at YOUR capital.** A contract you cannot buy,
  or whose round-trip costs eat the target, is ranked down however good the
  structure looks -- which is a thing a pure "signal quality" score will
  happily ignore.

What this is not
----------------
Not a prediction that any of these will work. It is the set of ways to express
one view, priced honestly. The engine's own signal is still the one that gets
recorded and graded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from intraday_advisor.ledger.costs import OptionsCostModel
from intraday_advisor.ledger.sizing import plan_position

# Beyond this many strikes from the money, an option is a lottery ticket: the
# probability of finishing in the money is small and cost drag is large.
MAX_OTM_STEPS = 5
MAX_ITM_STEPS = 3


@dataclass
class Candidate:
    """One tradeable contract, priced and sized against your capital."""

    index: str
    strike: float
    option_type: str          # CE / PE
    direction: str            # CALL / PUT
    premium: float
    target_premium: float
    stop_premium: float
    expiry: str | None
    lot_size: int
    open_interest: float
    moneyness: str            # ATM / ITM / OTM
    steps_from_atm: int       # signed in the option's own favour
    aligned: bool             # matches the engine's direction
    score: int                # 0-100, ranking only
    # sizing at the operator's actual capital
    lots: int = 0
    affordable: bool = False
    capital_per_lot: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    reward_amount: float = 0.0
    net_rr: float = 0.0
    breakeven: float = 0.0
    cost_drag: float = 0.0
    risk_band: str = ""
    # The cost of entry, measured from the book rather than assumed. ``None``
    # where the feed gave no two-sided quote -- which is itself information
    # about the contract, and must never be shown as a tight spread.
    bid: float | None = None
    ask: float | None = None
    spread_pct: float | None = None          # round trip, % of premium
    spread_measured: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _moneyness(option_type: str, strike: float, atm: float, step: float) -> tuple[str, int]:
    """Classify a strike, with steps signed in the option's own direction."""
    if not step:
        return "ATM", 0
    raw = int(round((strike - atm) / step))
    steps = raw if option_type == "CE" else -raw
    if steps == 0:
        return "ATM", 0
    return ("OTM", steps) if steps > 0 else ("ITM", steps)


def build_candidates(
    index: str,
    chain: dict,
    *,
    direction: str,
    available_capital: float,
    target_pct: float = 0.25,
    stop_pct: float = 0.20,
    lot_size: int | None = None,
    step: float = 100.0,
    costs: OptionsCostModel | None = None,
    limit: int = 8,
    max_capital_fraction: float = 1.0,
) -> list[dict]:
    """Rank tradeable contracts from a live option chain.

    ``direction`` is the engine's read (CALL / PUT / WAIT). On WAIT both sides
    are returned unaligned, because the chain is still worth seeing.

    ``max_capital_fraction`` is the deploy cap from the UI. It was previously
    not accepted at all, so this table sized every contract against the FULL
    account while the signal card above it sized against the cap -- at a 5%
    cap the card read "UNAFFORDABLE, needs Rs 11,089" while this table
    cheerfully offered eight contracts at 11-20% risk. Two answers to the same
    question on one screen, and the riskier one looked tradeable.
    """
    frame = chain.get("df")
    if frame is None or getattr(frame, "empty", True):
        return []

    # The OI walls, so a strike can be checked against the level it would have
    # to trade through rather than only against its own liquidity.
    spot = float(chain.get("spot", 0) or 0)
    ce_wall = float(chain.get("max_ce_wall", 0) or 0)
    pe_wall = float(chain.get("max_pe_wall", 0) or 0)

    atm = float(chain.get("atm") or 0)
    expiry = chain.get("expiry")
    if lot_size is None:
        from intraday_advisor.ledger.sizing import lot_size_for

        try:
            lot_size = lot_size_for(index)
        except ValueError:
            return []

    # Open interest, used only to rank liquidity -- and only when the feed
    # actually provides it. The Angel LTP path sometimes returns OI of zero,
    # in which case it must not silently become a ranking signal.
    ce_oi_total = float(frame.get("ce_oi").sum()) if "ce_oi" in frame else 0.0
    pe_oi_total = float(frame.get("pe_oi").sum()) if "pe_oi" in frame else 0.0

    out: list[Candidate] = []
    for _, row in frame.iterrows():
        strike = float(row.get("strike", 0) or 0)
        if not strike:
            continue
        for option_type, premium_col, oi_col, oi_total in (
            ("CE", "ce_ltp", "ce_oi", ce_oi_total),
            ("PE", "pe_ltp", "pe_oi", pe_oi_total),
        ):
            premium = float(row.get(premium_col, 0) or 0)
            # 0.5 is the synthetic floor this project emits when a feed is
            # unavailable; it is not a tradeable price.
            if premium <= 0.75:
                continue

            moneyness, steps = _moneyness(option_type, strike, atm, step)
            if steps > MAX_OTM_STEPS or steps < -MAX_ITM_STEPS:
                continue

            contract_direction = "CALL" if option_type == "CE" else "PUT"
            aligned = contract_direction == direction

            target = round(premium * (1 + target_pct), 2)
            stop = round(premium * (1 - stop_pct), 2)

            # ---- the measured spread, where the feed gave us one ---------- #
            #
            # Every contract used to be priced with the same assumed 0.25%
            # half-spread. That is defensible for an ATM weekly and badly wrong
            # for a wing: a Rs 2 spread is 1.25% of a Rs 160 premium and 16.7%
            # of a Rs 12 one. Ranking contracts by a cost model that cannot
            # tell them apart ranks them by everything except the thing that
            # decides whether they can repay their own entry.
            #
            # Where the book is missing the assumed constant still applies and
            # `spread_measured` is False, so an unquoted contract is never
            # flattered into looking cheap.
            measured_half = row.get(f"{option_type.lower()}_half_spread_pct")
            contract_costs = costs
            spread_measured = False
            if measured_half is not None and float(measured_half) > 0:
                base = costs or OptionsCostModel()
                contract_costs = base.with_measured_spread(float(measured_half))
                spread_measured = True

            plan = plan_position(
                available_capital=available_capital,
                entry_premium=premium, stop_premium=stop, target_premium=target,
                lot_size=lot_size, costs=contract_costs,
                max_capital_fraction=max_capital_fraction,
            )

            # ---- ranking ------------------------------------------------- #
            score = 50.0
            notes: list[str] = []

            if aligned:
                score += 25
                notes.append(f"Aligned with the engine's {direction} read")
            elif direction in ("CALL", "PUT"):
                score -= 30
                notes.append(f"Counter-trend to the engine's {direction} read")
            else:
                notes.append("Engine reads WAIT — no directional support")

            if steps == 0:
                score += 15
                notes.append("ATM — cleanest delta and tightest spread")
            elif 1 <= steps <= 2:
                score += 10
                notes.append(f"{steps} strike(s) OTM — good movement per rupee")
            elif steps == 3:
                score += 2
            elif steps > 3:
                score -= 15
                notes.append("Far OTM — low probability, high cost drag")
            else:
                score -= 5 * min(abs(steps), 3)
                notes.append("ITM — more capital for a smaller percentage move")

            # Affordability and costs are ranking factors, not footnotes: a
            # contract you cannot buy is not a candidate.
            if not plan.affordable:
                score -= 25
            if plan.cost_drag > 0.05:
                score -= 15
                notes.append(f"Costs are {plan.cost_drag * 100:.1f}% of the position")
            if plan.net_rr and plan.net_rr < 1.0:
                score -= 10

            oi = float(row.get(oi_col, 0) or 0)
            if oi_total > 0 and oi > 0:
                share = oi / oi_total
                if share > 0.08:
                    score += 5
                    notes.append("Heavy open interest — liquid")
                elif share < 0.01:
                    score -= 5
                    notes.append("Thin open interest — wider spreads")

            # ---- the spread, measured rather than inferred from OI -------- #
            #
            # The block above uses open interest as a PROXY for the spread
            # ("thin open interest — wider spreads"). Where the book is
            # actually quoted there is no need to proxy it, and the measurement
            # outranks the proxy: a strike can carry heavy OI and still be
            # quoted two rupees wide.
            if spread_measured:
                round_trip = float(measured_half) * 2
                if round_trip >= 0.05:
                    score -= 30
                    notes.append(
                        f"Spread is {round_trip * 100:.1f}% of the premium "
                        f"round trip — measured, and larger than the move most "
                        f"targets ask for"
                    )
                elif round_trip >= 0.02:
                    score -= 15
                    notes.append(
                        f"Wide book: {round_trip * 100:.1f}% round trip in "
                        f"spread alone (measured)"
                    )
                elif round_trip <= 0.004:
                    score += 8
                    notes.append(
                        f"Tight book: {round_trip * 100:.2f}% round trip "
                        f"(measured, not assumed)"
                    )
                # Can one lot even be filled at the quoted offer?
                ask_qty = float(row.get(f"{option_type.lower()}_ask_qty", 0) or 0)
                if ask_qty and lot_size and ask_qty < lot_size:
                    score -= 10
                    notes.append(
                        f"Only {ask_qty:,.0f} offered at the touch against a "
                        f"{lot_size} lot — you would walk the book"
                    )

            # ---- the wall you would have to trade THROUGH ---------------- #
            #
            # Heavy open interest means two things at once -- a liquid
            # contract, and a level option writers are defending -- and the
            # block above only ever credited the flattering one. Observed
            # 2026-10-21: NIFTY spot 22,722 with the largest put OI on the
            # board at 22,500 (9.27M). The 22500 PE was offered as a PUT
            # candidate and scored +5 for "Heavy open interest - liquid",
            # when buying it is a bet that price breaks the most defended
            # support on the chain.
            #
            # Which reading applies depends on direction and where the strike
            # sits relative to spot: a wall you take profit INTO is support
            # for your exit, a wall you must break THROUGH is working against
            # you the whole way.
            if spot and spot > 0:
                if (contract_direction == "PUT" and pe_wall and pe_wall > 0
                        and strike <= pe_wall < spot):
                    score -= 20
                    notes.append(
                        f"Strike sits at or below the {pe_wall:.0f} PUT wall — "
                        f"this needs the most defended support to break"
                    )
                elif (contract_direction == "CALL" and ce_wall and ce_wall > 0
                        and strike >= ce_wall > spot):
                    score -= 20
                    notes.append(
                        f"Strike sits at or above the {ce_wall:.0f} CALL wall — "
                        f"this needs the heaviest resistance to break"
                    )
                elif (contract_direction == "PUT" and pe_wall and pe_wall > 0
                        and strike > pe_wall):
                    notes.append(
                        f"Room down to the {pe_wall:.0f} PUT wall — "
                        f"that level is a sensible place to book"
                    )
                elif (contract_direction == "CALL" and ce_wall and ce_wall > 0
                        and strike < ce_wall):
                    notes.append(
                        f"Room up to the {ce_wall:.0f} CALL wall — "
                        f"that level is a sensible place to book"
                    )

            out.append(Candidate(
                index=index, strike=strike, option_type=option_type,
                direction=contract_direction, premium=round(premium, 2),
                target_premium=target, stop_premium=stop, expiry=expiry,
                lot_size=lot_size, open_interest=oi,
                moneyness=moneyness, steps_from_atm=steps, aligned=aligned,
                score=int(max(0, min(100, round(score)))),
                lots=plan.lots, affordable=plan.affordable,
                capital_per_lot=plan.capital_per_lot,
                risk_amount=plan.risk_amount,
                risk_pct=plan.risk_pct_of_capital,
                reward_amount=plan.reward_amount,
                net_rr=plan.net_rr, breakeven=plan.breakeven,
                cost_drag=plan.cost_drag, risk_band=plan.risk_band,
                bid=row.get(f"{option_type.lower()}_bid"),
                ask=row.get(f"{option_type.lower()}_ask"),
                spread_pct=(round(float(measured_half) * 200, 3)
                            if spread_measured else None),
                spread_measured=spread_measured,
                notes=notes, warnings=plan.warnings,
            ))

    # Affordable first, then by score: an unaffordable contract is information,
    # not an option, so it should never head the list.
    out.sort(key=lambda c: (c.affordable, c.score), reverse=True)

    # Guarantee both sides are represented. Pure ranking fills every slot with
    # the aligned side, so a CALL read would hide the PUT chain entirely -- and
    # seeing both is the point of a shortlist. Each side gets at least a third
    # of the slots, with the remainder going to whatever ranks highest.
    per_side_floor = max(1, limit // 3)
    calls = [c for c in out if c.option_type == "CE"]
    puts = [c for c in out if c.option_type == "PE"]

    picked: list[Candidate] = []
    picked.extend(calls[:per_side_floor])
    picked.extend(puts[:per_side_floor])
    for candidate in out:
        if len(picked) >= limit:
            break
        if candidate not in picked:
            picked.append(candidate)

    picked.sort(key=lambda c: (c.affordable, c.score), reverse=True)
    return [c.as_dict() for c in picked[:limit]]
