from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pandas as pd

from intraday_advisor.execution import PaperOrder


class TradeJournal:
    def __init__(self, path: str | Path = "logs/trade_journal.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_order(self, order: PaperOrder, rationale: str) -> None:
        row = asdict(order)
        row["rationale"] = rationale
        existing = pd.read_csv(self.path) if self.path.exists() else pd.DataFrame()
        pd.concat([existing, pd.DataFrame([row])], ignore_index=True).to_csv(self.path, index=False)

