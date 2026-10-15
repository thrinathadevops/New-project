"""All-strikes option scanner for NIFTY & BANKNIFTY.

The single-index dashboard only ever names the ATM contract. This scanner casts
a wide net: it scores EVERY strike's CALL and PUT in the chain (ATM ± ~10) for
both indices, ranks them, and hands back the best intraday buy candidates so you
can pick by budget.

Each contract is scored 0-100 on three transparent factors:

  * Direction alignment — does this side (CALL/PUT) match the index's confluence
    direction? Aligned = big plus, counter-trend = big minus.
  * Moneyness — ATM and 1-2 strikes OTM have the best intraday gamma/leverage;
    far-OTM lottery tickets and expensive deep-ITM are penalised.
  * Liquidity — higher open interest = easier fills / tighter spreads
    (only differentiates when the feed provides OI; live LTP-only data may not).

It also attaches the rupee reality: premium, capital per lot, and the profit/
loss per lot at your target/stop — so budget filtering is exact.

HONESTY: this ranks probability and leverage, it does not predict winners. A
high score means "good structure for an intraday move", not "this will pay".
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from intraday_advisor.index_options import (
    DEFAULT_STOP_PCT,
    DEFAULT_TARGET_PCT,
    INDEX_CONFIG,
    analyze_index_option,
)
from intraday_advisor.nse_option_chain import suggest_option_trade

# Exchange lot sizes change periodically — these are defaults the UI can override.
DEFAULT_LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 15}


@dataclass
class OptionCandidate:
    index: str
    strike: float | int
    option_type: str            # CE / PE
    contract_direction: str     # CALL (CE) / PUT (PE)
    premium: float
    lot_size: int
    capital_per_lot: float
    target_premium: float
    stop_premium: float
    profit_per_lot: float       # rupees if target hit
    loss_per_lot: float         # rupees if stop hit
    moneyness: str              # ATM / ITM / OTM
    strikes_from_atm: int
    open_interest: float
    score: int
    aligned: bool               # matches the index's confluence direction
    index_direction: str        # CALL / PUT / WAIT for the parent index
    target_pct: float
    stop_pct: float
    data_source: str
    is_live: bool
    reasons: list[str] = field(default_factory=list)

    def affordable_lots(self, budget: float) -> int:
        if self.capital_per_lot <= 0:
            return 0
        return int(budget // self.capital_per_lot)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _score_contract(
    *, opt: str, strike, premium: float, oi: float, oi_rank: float, atm, step,
    index_direction: str,
) -> tuple[int, str, int, bool, list[str]]:
    """Return (score, moneyness, strikes_from_atm, aligned, reasons)."""
    reasons: list[str] = []
    contract_dir = "CALL" if opt == "CE" else "PUT"
    aligned = contract_dir == index_direction
    counter = index_direction in ("CALL", "PUT") and not aligned

    strikes_from_atm = int(round((strike - atm) / step)) if step else 0
    # OTM steps in the option's own direction: +ve = OTM, 0 = ATM, -ve = ITM
    otm_steps = strikes_from_atm if opt == "CE" else -strikes_from_atm
    if otm_steps == 0:
        moneyness = "ATM"
    elif otm_steps > 0:
        moneyness = "OTM"
    else:
        moneyness = "ITM"

    score = 50.0

    # 1. direction alignment
    if aligned:
        score += 25
        reasons.append(f"Aligned with index {index_direction} bias")
    elif counter:
        score -= 30
        reasons.append(f"Counter-trend vs index {index_direction} bias")
    else:
        reasons.append("Index has no clean direction (WAIT)")

    # 2. moneyness (favour ATM and slightly OTM for intraday)
    if otm_steps == 0:
        score += 15
        reasons.append("ATM — best intraday gamma")
    elif 1 <= otm_steps <= 2:
        score += 10
        reasons.append(f"{otm_steps} strike(s) OTM — good leverage")
    elif otm_steps == 3:
        score += 3
    elif otm_steps > 3:
        score -= 12
        reasons.append("Far OTM — low probability")
    else:  # ITM
        depth = min(abs(otm_steps), 3)
        score -= 5 * depth
        reasons.append("ITM — expensive, smaller % move")

    # 3. liquidity (only meaningful when OI is provided)
    if oi > 0:
        score += oi_rank * 12
        if oi_rank >= 0.7:
            reasons.append("High open interest — liquid")
        elif oi_rank <= 0.2:
            reasons.append("Thin open interest — wider spreads")

    return int(max(0, min(100, round(score)))), moneyness, strikes_from_atm, aligned, reasons


def scan_option_contracts(
    *,
    indices: tuple[str, ...] = ("NIFTY", "BANKNIFTY"),
    candles_by_index: dict | None = None,
    chains_by_index: dict | None = None,
    target_pct: float = DEFAULT_TARGET_PCT,
    stop_pct: float = DEFAULT_STOP_PCT,
    lot_sizes: dict | None = None,
) -> list[OptionCandidate]:
    """Scan and rank every CE/PE strike for the given indices, best first.

    ``candles_by_index`` / ``chains_by_index`` let tests inject data and let the
    UI avoid duplicate fetches. Otherwise data is fetched live.
    """
    lot_sizes = {**DEFAULT_LOT_SIZES, **(lot_sizes or {})}
    candles_by_index = candles_by_index or {}
    chains_by_index = chains_by_index or {}
    out: list[OptionCandidate] = []

    for index in indices:
        if index not in INDEX_CONFIG:
            continue
        candles = candles_by_index.get(index)
        if candles is None:
            try:
                from intraday_advisor.data import fetch_yahoo_ohlcv
                candles = fetch_yahoo_ohlcv(INDEX_CONFIG[index]["yahoo"], period="5d", interval="5m")
            except Exception:  # noqa: BLE001
                candles = None

        spot = float(candles["Close"].iloc[-1]) if (candles is not None and not candles.empty) else 0.0
        chain = chains_by_index.get(index) or suggest_option_trade(index, spot=spot)
        sig = analyze_index_option(index, candles=candles, chain=chain, target_pct=target_pct, stop_pct=stop_pct)

        df = chain.get("df")
        if df is None or df.empty:
            continue
        step = INDEX_CONFIG[index]["step"]
        atm = chain.get("atm", round(sig.spot))
        lot = int(lot_sizes.get(index, 50))
        data_source = chain.get("data_source", "unknown")
        is_live = "live" in data_source.lower()

        ce_total_oi = float(df.get("ce_oi", pd.Series(dtype=float)).sum())
        pe_total_oi = float(df.get("pe_oi", pd.Series(dtype=float)).sum())
        ce_rank = df["ce_oi"].rank(pct=True) if ce_total_oi > 0 else None
        pe_rank = df["pe_oi"].rank(pct=True) if pe_total_oi > 0 else None

        for pos, row in df.iterrows():
            strike = row["strike"]
            for opt, prem_col, oi_col, rank in (
                ("CE", "ce_ltp", "ce_oi", ce_rank),
                ("PE", "pe_ltp", "pe_oi", pe_rank),
            ):
                premium = float(row.get(prem_col, 0) or 0)
                if premium <= 0:
                    continue
                oi = float(row.get(oi_col, 0) or 0)
                oi_rank = float(rank.loc[pos]) if rank is not None else 0.0
                score, moneyness, sfa, aligned, reasons = _score_contract(
                    opt=opt, strike=strike, premium=premium, oi=oi, oi_rank=oi_rank,
                    atm=atm, step=step, index_direction=sig.direction,
                )
                target = round(premium * (1 + target_pct), 2)
                stop = round(premium * (1 - stop_pct), 2)
                out.append(OptionCandidate(
                    index=index, strike=strike, option_type=opt,
                    contract_direction="CALL" if opt == "CE" else "PUT",
                    premium=round(premium, 2), lot_size=lot,
                    capital_per_lot=round(premium * lot, 2),
                    target_premium=target, stop_premium=stop,
                    profit_per_lot=round((target - premium) * lot, 2),
                    loss_per_lot=round((premium - stop) * lot, 2),
                    moneyness=moneyness, strikes_from_atm=sfa, open_interest=oi,
                    score=score, aligned=aligned, index_direction=sig.direction,
                    target_pct=target_pct, stop_pct=stop_pct,
                    data_source=data_source, is_live=is_live, reasons=reasons,
                ))

    out.sort(key=lambda c: (c.aligned, c.score), reverse=True)
    return out
