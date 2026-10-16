"""Session Timing, Market Profile Phases & ICT Kill Zones.

Tailored to Indian Market Hours (NSE: 09:15 to 15:30 IST).
Trading edges shift dramatically based on the time of day:
  - Opening Drive (09:15 - 09:45): High momentum price discovery
  - Initial Balance (09:15 - 10:15): Key high/low boundaries established
  - Midday Chop Zone (11:30 - 12:45): Lunch lull, trap zone, low liquidity
  - European Open Impulse (12:45 - 13:45): London open triggers fresh trend wave
  - Power Hour (14:15 - 15:15): Institutional rebalancing, intraday breakout run
  - Square-Off Risk (15:15 - 15:30): Broker auto-square-off, no new entries
"""

from __future__ import annotations

import datetime
import numpy as np
import pandas as pd
from .base import registry


@registry.register("session_context")
def add_session_context(df: pd.DataFrame) -> pd.DataFrame:
    """Classifies current candle into market session phases and Kill Zones."""
    enriched = df.copy()
    n = len(enriched)
    if n == 0:
        return enriched

    is_dt = isinstance(enriched.index, pd.DatetimeIndex)

    if is_dt:
        # Extract time
        times = enriched.index.time
        
        t_915 = datetime.time(9, 15)
        t_945 = datetime.time(9, 45)
        t_1015 = datetime.time(10, 15)
        t_1130 = datetime.time(11, 30)
        t_1245 = datetime.time(12, 45)
        t_1345 = datetime.time(13, 45)
        t_1415 = datetime.time(14, 15)
        t_1515 = datetime.time(15, 15)
        t_1530 = datetime.time(15, 30)

        opening_drive = np.array([(t >= t_915 and t < t_945) for t in times])
        initial_balance = np.array([(t >= t_915 and t <= t_1015) for t in times])
        midday_chop = np.array([(t >= t_1130 and t < t_1245) for t in times])
        european_open = np.array([(t >= t_1245 and t < t_1345) for t in times])
        power_hour = np.array([(t >= t_1415 and t < t_1515) for t in times])
        squareoff_zone = np.array([(t >= t_1515 and t <= t_1530) for t in times])
    else:
        # Fallback if no datetime index
        opening_drive = np.zeros(n, dtype=bool)
        initial_balance = np.zeros(n, dtype=bool)
        midday_chop = np.zeros(n, dtype=bool)
        european_open = np.zeros(n, dtype=bool)
        power_hour = np.zeros(n, dtype=bool)
        squareoff_zone = np.zeros(n, dtype=bool)

    enriched["Session_Opening_Drive"] = opening_drive
    enriched["Session_Initial_Balance"] = initial_balance
    enriched["Session_Midday_Chop"] = midday_chop
    enriched["Session_European_Open"] = european_open
    enriched["Session_Power_Hour"] = power_hour
    enriched["Session_Squareoff_Risk"] = squareoff_zone

    return enriched
