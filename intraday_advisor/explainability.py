from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class TheoryInfo:
    name: str
    purpose: str
    buy_watch: str
    sell_watch: str
    avoid: str
    fields: list[str]


THEORY_INFOS: dict[str, TheoryInfo] = {
    "fundamentals": TheoryInfo(
        "Fundamental Quality",
        "Filters weak businesses before technical analysis starts.",
        "Prefer stocks passing ROE, ROCE, growth, low debt, promoter holding, OPM, EPS, and market-cap rules.",
        "This layer is not for short selling; it is mainly a stock-quality shortlist.",
        "Avoid stocks failing the screen unless you intentionally want a speculative trade.",
        ["MarketCapCr", "ROE", "ROCE", "DebtToEquity", "SalesGrowth", "PromoterHolding", "ProfitGrowth", "OPM", "EPS"],
    ),
    "ema": TheoryInfo(
        "EMA 9/21 Swing Breakout",
        "Finds momentum entry after fast EMA crosses above slow EMA, then waits for swing-high breakout.",
        "BUY WATCH when EMA9 is above EMA21 and price breaks recent swing high.",
        "SELL/exit watch when EMA9 crosses below EMA21.",
        "Avoid buying immediately on crossover without swing-high breakout confirmation.",
        ["EMA9", "EMA21", "BreakoutLevel", "SwingHigh", "SwingLow"],
    ),
    "ema200": TheoryInfo(
        "EMA 200 Trend Filter",
        "Keeps trades aligned with the larger trend.",
        "Prefer long setups only when Close is above EMA200.",
        "Below EMA200 means weak long trend context.",
        "Avoid long trades below EMA200 unless you are deliberately trading a reversal.",
        ["Close", "EMA200", "AboveEMA200"],
    ),
    "price_action": TheoryInfo(
        "Price Action Psychology",
        "Reads buyer/seller behavior through trend structure, support/resistance, candle pattern, and volume.",
        "BUY WATCH when trend is up, price breaks/respects support, candle shows buyer control, and volume confirms.",
        "SELL WATCH when rejection appears near resistance or sellers dominate.",
        "Avoid unclear trend, weak breakout volume, bearish rejection near highs, and inside-range signals.",
        ["TrendStructure", "Support", "Resistance", "CandlePattern", "VolumeConfirmation", "BreakoutState"],
    ),
    "smart_money": TheoryInfo(
        "FVG, Liquidity, VWAP, Order Flow",
        "Adds institutional-style context around imbalance, liquidity sweeps, VWAP, and volume profile.",
        "BUY WATCH when bullish FVG aligns with EMA/VWAP and order flow is not seller-dominant.",
        "SELL WATCH when bearish sweep, below VWAP, or seller-dominant order flow warns against longs.",
        "Avoid if FVG is against the trade, price is below VWAP, or the move lacks order-flow support.",
        ["FVG", "LiquiditySweep", "OrderFlow", "VWAPRelation", "VolumePOC", "ValueAreaLow", "ValueAreaHigh"],
    ),
    "situational": TheoryInfo(
        "Weekday Low-Revisit Rules",
        "Marks likely revisit levels from Friday/Monday and Wednesday/Thursday behavior.",
        "BUY WATCH only if price reaches the situational low and other confirmations support a bounce.",
        "SELL WATCH only if price rejects after revisit; this rule alone is not a short signal.",
        "Avoid treating the revisit rule alone as an entry trigger.",
        ["SituationalRule", "SituationalLevel", "SituationalTargetDate", "SituationalVisited"],
    ),
    "box": TheoryInfo(
        "Box Theory",
        "Uses previous day high/low as a decision box and avoids the middle.",
        "BUY WATCH at the bottom of the box only with bullish long-wick rejection.",
        "SELL WATCH at the top of the box only with bearish long-wick rejection.",
        "Avoid the middle line because false signals are common there.",
        ["BoxHigh", "BoxLow", "BoxMiddle", "BoxZone", "BoxWickSignal", "BoxBias"],
    ),
    "risk": TheoryInfo(
        "Risk Plan",
        "Turns a setup into capital risk, stop level, target, and share quantity.",
        "Consider only if risk amount fits your per-trade budget and stop is logical.",
        "Exit/invalidate if EMA9 crosses below EMA21 or planned stop is hit.",
        "Avoid trades where risk is too large, stop is too far, or target is not worth the risk.",
        ["entry", "stop", "target", "shares", "risk_amount", "reward_amount"],
    ),
}


def theory_info_rows() -> list[dict[str, object]]:
    return [
        {
            "Theory": info.name,
            "Purpose": info.purpose,
            "Buy Watch": info.buy_watch,
            "Sell Watch": info.sell_watch,
            "Avoid": info.avoid,
            "Fields": ", ".join(info.fields),
        }
        for info in THEORY_INFOS.values()
    ]


def decision_guide(row: dict) -> tuple[str, str]:
    blockers: list[str] = []
    positives: list[str] = []

    if not row.get("AboveEMA200", False):
        blockers.append("below EMA200")
    else:
        positives.append("above EMA200")

    signal = row.get("Signal", "HOLD")
    if signal == "BUY":
        positives.append("EMA9/EMA21 swing-high breakout is active")
    elif signal == "SELL":
        blockers.append("EMA9 crossed below EMA21")
    else:
        blockers.append("no active EMA breakout entry")

    if row.get("BoxZone") == "middle no-trade zone":
        blockers.append("price is near box middle")
    elif row.get("BoxBias") == "BUY WATCH":
        positives.append("box bottom bullish wick rejection")
    elif row.get("BoxBias") == "SELL WATCH":
        blockers.append("box top bearish wick rejection")

    if row.get("VWAPRelation") == "above VWAP":
        positives.append("above VWAP")
    elif row.get("VWAPRelation"):
        blockers.append("below VWAP")

    if row.get("OrderFlow") == "seller-dominant order flow":
        blockers.append("seller-dominant order flow")
    elif row.get("OrderFlow") == "buyer-dominant order flow":
        positives.append("buyer-dominant order flow")

    if row.get("VolumeConfirmation") == "bullish confirmation":
        positives.append("volume confirms move")
    elif row.get("VolumeConfirmation") in {"weak breakout risk", "insufficient volume history"}:
        blockers.append("weak volume confirmation")

    if blockers:
        action = "WAIT / AVOID"
    elif signal == "BUY":
        action = "BUY WATCH"
    elif signal == "SELL":
        action = "SELL WATCH"
    else:
        action = "HOLD"
        
    current_price = row.get("Close", 0)
    option_suggestion = "No Options Trade"
    
    if action == "BUY WATCH" and current_price > 0:
        # Determine step size based on NSE basic rules
        step = 10 if current_price < 1000 else 20 if current_price <= 3000 else 50
        strike = round(current_price / step) * step
        if strike < current_price:
            strike += step # Lean slightly out of money to keep premium cheaper
        option_suggestion = f"BUY {strike} CE (CALL OPTION)"
        
    elif action == "SELL WATCH" and current_price > 0:
        step = 10 if current_price < 1000 else 20 if current_price <= 3000 else 50
        strike = round(current_price / step) * step
        if strike > current_price:
            strike -= step
        option_suggestion = f"BUY {strike} PE (PUT OPTION)"

    why = "; ".join(positives + blockers) if positives or blockers else "No strong combined signal."
    return action, why, option_suggestion

