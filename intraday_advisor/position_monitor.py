"""Live monitoring for an open option position.

You add a trade (what you bought, at what premium, how many units). On every
refresh the dashboard re-fetches the current premium and the fresh underlying
direction, then this module decides one of:

  * 🎯 TARGET_HIT  — premium is up >= your target%, book it.
  * 🚨 STOP_HIT    — premium is down <= your loss limit (default -20%), exit now.
  * ⚠️ WARNING     — not at a hard level yet, but the move has turned against
                     your option (a CE while the index now reads bearish, or a
                     PE while it reads bullish), or you are closing in on the
                     stop. This is the early "it will not go your way" nudge.
  * ✅ ON_TRACK    — still working in your favour.

It cannot predict the future; it reports live P&L and flags when your original
reason for the trade has broken. Cutting losers early is the whole job here.
"""

from __future__ import annotations

from dataclasses import dataclass

# Fraction of the stop distance at which we start warning (e.g. -12% when the
# hard stop is -20%). Gives you a heads-up before the limit is actually hit.
WARN_AT_STOP_FRACTION = 0.6


@dataclass
class PositionStatus:
    status: str            # TARGET_HIT / STOP_HIT / WARNING / ON_TRACK / NO_DATA
    pnl_pct: float         # signed, e.g. -0.12 == -12%
    pnl_amount: float      # signed rupees
    current_premium: float
    message: str
    action: str            # BOOK PROFIT / EXIT NOW / CONSIDER EXIT / HOLD / —
    is_alarm: bool         # True for STOP_HIT (drives the loud UI warning)


def evaluate_position(
    entry_premium: float,
    current_premium: float,
    option_type: str,           # CE / PE
    qty: float,
    underlying_direction: str,  # fresh CALL / PUT / WAIT for the index
    *,
    target_pct: float = 0.25,
    stop_pct: float = 0.20,
) -> PositionStatus:
    if entry_premium <= 0 or current_premium is None or current_premium <= 0:
        return PositionStatus("NO_DATA", 0.0, 0.0, current_premium or 0.0,
                              "Live premium unavailable — cannot evaluate.", "—", False)

    pnl_pct = (current_premium - entry_premium) / entry_premium
    pnl_amount = (current_premium - entry_premium) * qty
    opt = (option_type or "").upper()

    # --- hard levels first ---
    if pnl_pct >= target_pct:
        return PositionStatus(
            "TARGET_HIT", pnl_pct, pnl_amount, current_premium,
            f"🎯 Target reached: +{pnl_pct*100:.1f}% (₹{pnl_amount:,.0f}). Book the profit.",
            "BOOK PROFIT", False,
        )
    if pnl_pct <= -stop_pct:
        return PositionStatus(
            "STOP_HIT", pnl_pct, pnl_amount, current_premium,
            f"🚨 Loss limit hit: {pnl_pct*100:.1f}% (₹{pnl_amount:,.0f}). EXIT NOW — do not average down.",
            "EXIT NOW", True,
        )

    # --- soft signals: thesis broken or nearing the stop ---
    thesis_against = (
        (opt == "CE" and underlying_direction == "PUT")
        or (opt == "PE" and underlying_direction == "CALL")
    )
    nearing_stop = pnl_pct <= -(stop_pct * WARN_AT_STOP_FRACTION)

    if thesis_against:
        side = "bearish" if opt == "CE" else "bullish"
        return PositionStatus(
            "WARNING", pnl_pct, pnl_amount, current_premium,
            f"⚠️ The index has turned {side} against your {opt}. Your reason for the trade is "
            f"weakening — consider exiting before the stop. (now {pnl_pct*100:+.1f}%)",
            "CONSIDER EXIT", False,
        )
    if nearing_stop:
        return PositionStatus(
            "WARNING", pnl_pct, pnl_amount, current_premium,
            f"⚠️ Down {pnl_pct*100:.1f}% — closing in on your -{stop_pct*100:.0f}% stop. Stay ready to exit.",
            "CONSIDER EXIT", False,
        )

    return PositionStatus(
        "ON_TRACK", pnl_pct, pnl_amount, current_premium,
        f"✅ On track: {pnl_pct*100:+.1f}% (₹{pnl_amount:,.0f}). Hold while the move continues.",
        "HOLD", False,
    )
