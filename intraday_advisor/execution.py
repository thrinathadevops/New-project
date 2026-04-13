from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from intraday_advisor.risk import TradePlan


@dataclass
class PaperOrder:
    timestamp: str
    ticker: str
    direction: str
    shares: int
    entry: float
    stop: float
    target: float
    status: str = "ACCEPTED"


class PaperBroker:
    def __init__(self) -> None:
        self.orders: list[PaperOrder] = []

    def place_order(self, plan: TradePlan) -> PaperOrder:
        if plan.shares <= 0:
            raise ValueError("Paper order rejected: position size is zero")
        order = PaperOrder(
            timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
            ticker=plan.ticker,
            direction=plan.direction,
            shares=plan.shares,
            entry=plan.entry,
            stop=plan.stop,
            target=plan.target,
        )
        self.orders.append(order)
        return order

    def order_log(self) -> list[dict]:
        return [asdict(order) for order in self.orders]
