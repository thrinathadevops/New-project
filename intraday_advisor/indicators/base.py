from __future__ import annotations
import pandas as pd
from typing import Callable, Dict, Any

class IndicatorRegistry:
    def __init__(self):
        self._indicators: Dict[str, Callable[[pd.DataFrame], pd.DataFrame]] = {}

    def register(self, name: str):
        def decorator(func: Callable[[pd.DataFrame], pd.DataFrame]):
            self._indicators[name] = func
            return func
        return decorator

    def apply_all(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        for name, func in self._indicators.items():
            result = func(result)
        return result

# Global registry for the custom indicator engine
registry = IndicatorRegistry()
