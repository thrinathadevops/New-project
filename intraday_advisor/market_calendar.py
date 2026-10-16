"""World market sessions, and the gate that stops the LLM burning tokens.

Two jobs:

1. **Know when a market is actually open**, in its own timezone, including the
   things a naive implementation gets wrong: lunch breaks (most of Asia), and
   the Gulf and Israeli markets that trade **Sunday to Thursday**. A Mon-Fri
   assumption reports Tadawul as closed on its busiest day and open on its
   weekend.

2. **Gate the LLM agent to a window around the session.** An LLM reviewer
   polling all night costs money and tells you nothing: there is no live price
   to review. The agent is enabled from ``llm_lead_minutes`` before the open
   until the close, and is off otherwise. The deterministic rule checklist
   keeps running regardless, because it is free.

Holidays are deliberately not modelled. Exchange holiday calendars change
yearly and a stale hardcoded list is worse than none -- it would confidently
report a closed market as open. The live data feed already reveals a holiday
(no ticks, stale candles), and the dashboard shows data freshness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

# Weekday numbers follow datetime.weekday(): Monday 0 ... Sunday 6.
MON_FRI = (0, 1, 2, 3, 4)
SUN_THU = (6, 0, 1, 2, 3)


@dataclass(frozen=True)
class Market:
    """One exchange's trading session, in its own timezone."""

    key: str
    name: str
    exchange: str
    timezone: str
    open_time: time
    close_time: time
    trading_days: tuple[int, ...] = MON_FRI
    # Asian markets mostly break for lunch; price does not move during it.
    lunch_start: time | None = None
    lunch_end: time | None = None
    # The index the dashboard charts for this market.
    index_symbol: str = ""
    index_name: str = ""
    currency: str = ""
    # Search terms used to gather pre-market news for this market.
    news_terms: tuple[str, ...] = ()
    # How long before the open the LLM should start working.
    llm_lead_minutes: int = 30
    # Intraday square-off, where the broker forces it (India).
    square_off: time | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def as_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "exchange": self.exchange,
            "timezone": self.timezone,
            "open": self.open_time.strftime("%H:%M"),
            "close": self.close_time.strftime("%H:%M"),
            "lunch": (f"{self.lunch_start:%H:%M}-{self.lunch_end:%H:%M}"
                      if self.lunch_start else None),
            "trading_days": "Sun-Thu" if self.trading_days == SUN_THU else "Mon-Fri",
            "index_symbol": self.index_symbol, "index_name": self.index_name,
            "currency": self.currency,
            "llm_lead_minutes": self.llm_lead_minutes,
            "square_off": self.square_off.strftime("%H:%M") if self.square_off else None,
        }


# --------------------------------------------------------------------------- #
# The markets
# --------------------------------------------------------------------------- #
# Times are local to each exchange. Where an exchange runs several boards the
# main equity board is used.

MARKETS: dict[str, Market] = {m.key: m for m in (
    # ---------------------------------------------------------------- Asia --
    Market("india", "India", "NSE / BSE", "Asia/Kolkata",
           time(9, 15), time(15, 30), index_symbol="^NSEI", index_name="NIFTY 50",
           currency="INR", square_off=time(15, 15),
           news_terms=("Nifty Sensex India stock market", "RBI India economy",
                       "FII DII India equity flows")),
    Market("singapore", "Singapore", "SGX", "Asia/Singapore",
           time(9, 0), time(17, 0), lunch_start=time(12, 0), lunch_end=time(13, 0),
           index_symbol="^STI", index_name="Straits Times", currency="SGD",
           news_terms=("Singapore STI stock market", "MAS Singapore economy")),
    Market("japan", "Japan", "TSE", "Asia/Tokyo",
           time(9, 0), time(15, 30), lunch_start=time(11, 30), lunch_end=time(12, 30),
           index_symbol="^N225", index_name="Nikkei 225", currency="JPY",
           news_terms=("Nikkei Japan stock market", "Bank of Japan yen")),
    Market("hongkong", "Hong Kong", "HKEX", "Asia/Hong_Kong",
           time(9, 30), time(16, 0), lunch_start=time(12, 0), lunch_end=time(13, 0),
           index_symbol="^HSI", index_name="Hang Seng", currency="HKD",
           news_terms=("Hang Seng Hong Kong stocks", "China policy markets")),
    Market("china", "China", "SSE / SZSE", "Asia/Shanghai",
           time(9, 30), time(15, 0), lunch_start=time(11, 30), lunch_end=time(13, 0),
           index_symbol="000001.SS", index_name="Shanghai Composite", currency="CNY",
           news_terms=("China stock market Shanghai", "PBOC China economy")),
    Market("korea", "South Korea", "KRX", "Asia/Seoul",
           time(9, 0), time(15, 30), index_symbol="^KS11", index_name="KOSPI",
           currency="KRW", news_terms=("KOSPI Korea stock market",)),
    Market("taiwan", "Taiwan", "TWSE", "Asia/Taipei",
           time(9, 0), time(13, 30), index_symbol="^TWII", index_name="TAIEX",
           currency="TWD", news_terms=("Taiwan TAIEX semiconductor stocks",)),
    Market("indonesia", "Indonesia", "IDX", "Asia/Jakarta",
           time(9, 0), time(15, 50), lunch_start=time(12, 0), lunch_end=time(13, 30),
           index_symbol="^JKSE", index_name="Jakarta Composite", currency="IDR",
           news_terms=("Indonesia IDX stock market",)),
    Market("malaysia", "Malaysia", "Bursa Malaysia", "Asia/Kuala_Lumpur",
           time(9, 0), time(17, 0), lunch_start=time(12, 30), lunch_end=time(14, 30),
           index_symbol="^KLSE", index_name="FTSE Bursa KLCI", currency="MYR",
           news_terms=("Malaysia Bursa stock market",)),
    Market("thailand", "Thailand", "SET", "Asia/Bangkok",
           time(10, 0), time(16, 30), lunch_start=time(12, 30), lunch_end=time(14, 30),
           index_symbol="^SET.BK", index_name="SET Index", currency="THB",
           news_terms=("Thailand SET stock market",)),
    Market("philippines", "Philippines", "PSE", "Asia/Manila",
           time(9, 30), time(15, 30), lunch_start=time(12, 0), lunch_end=time(13, 30),
           index_symbol="PSEI.PS", index_name="PSEi", currency="PHP",
           news_terms=("Philippines PSE stock market",)),
    Market("vietnam", "Vietnam", "HOSE", "Asia/Ho_Chi_Minh",
           time(9, 0), time(15, 0), lunch_start=time(11, 30), lunch_end=time(13, 0),
           index_symbol="^VNINDEX", index_name="VN-Index", currency="VND",
           news_terms=("Vietnam VN-Index stock market",)),

    # ------------------------------------------- Middle East (Sunday-Thursday)
    Market("saudi", "Saudi Arabia", "Tadawul", "Asia/Riyadh",
           time(10, 0), time(15, 0), trading_days=SUN_THU,
           index_symbol="^TASI.SR", index_name="TASI", currency="SAR",
           news_terms=("Saudi Tadawul TASI stock market", "oil prices OPEC")),
    Market("qatar", "Qatar", "QSE", "Asia/Qatar",
           time(9, 30), time(13, 15), trading_days=SUN_THU,
           index_symbol="^QSI", index_name="QE Index", currency="QAR",
           news_terms=("Qatar stock exchange QSE",)),
    Market("kuwait", "Kuwait", "Boursa Kuwait", "Asia/Kuwait",
           time(9, 0), time(12, 30), trading_days=SUN_THU,
           index_name="Kuwait All Share", currency="KWD",
           news_terms=("Kuwait Boursa stock market",)),
    Market("israel", "Israel", "TASE", "Asia/Jerusalem",
           time(9, 59), time(17, 14), trading_days=SUN_THU,
           index_symbol="^TA125.TA", index_name="TA-125", currency="ILS",
           news_terms=("Israel TASE stock market",)),
    Market("uae", "UAE", "DFM / ADX", "Asia/Dubai",
           # Moved to a Monday-Friday week in January 2022.
           time(10, 0), time(15, 0),
           index_name="DFM General", currency="AED",
           news_terms=("Dubai DFM Abu Dhabi stock market", "oil prices")),
    Market("egypt", "Egypt", "EGX", "Africa/Cairo",
           time(10, 0), time(14, 30), trading_days=SUN_THU,
           index_symbol="^CASE30", index_name="EGX 30", currency="EGP",
           news_terms=("Egypt EGX stock market",)),

    # -------------------------------------------------------------- Europe --
    Market("uk", "United Kingdom", "LSE", "Europe/London",
           time(8, 0), time(16, 30), index_symbol="^FTSE", index_name="FTSE 100",
           currency="GBP", news_terms=("FTSE London stock market", "Bank of England")),
    Market("germany", "Germany", "XETRA", "Europe/Berlin",
           time(9, 0), time(17, 30), index_symbol="^GDAXI", index_name="DAX",
           currency="EUR", news_terms=("DAX Germany stocks", "ECB eurozone economy")),
    Market("france", "France", "Euronext Paris", "Europe/Paris",
           time(9, 0), time(17, 30), index_symbol="^FCHI", index_name="CAC 40",
           currency="EUR", news_terms=("CAC 40 France stocks",)),
    Market("netherlands", "Netherlands", "Euronext Amsterdam", "Europe/Amsterdam",
           time(9, 0), time(17, 30), index_symbol="^AEX", index_name="AEX",
           currency="EUR", news_terms=("AEX Amsterdam stocks",)),
    Market("spain", "Spain", "BME", "Europe/Madrid",
           time(9, 0), time(17, 30), index_symbol="^IBEX", index_name="IBEX 35",
           currency="EUR", news_terms=("IBEX Spain stocks",)),
    Market("italy", "Italy", "Borsa Italiana", "Europe/Rome",
           time(9, 0), time(17, 30), index_symbol="FTSEMIB.MI", index_name="FTSE MIB",
           currency="EUR", news_terms=("FTSE MIB Italy stocks",)),
    Market("switzerland", "Switzerland", "SIX", "Europe/Zurich",
           time(9, 0), time(17, 30), index_symbol="^SSMI", index_name="SMI",
           currency="CHF", news_terms=("SMI Swiss stocks",)),
    Market("sweden", "Sweden", "Nasdaq Stockholm", "Europe/Stockholm",
           time(9, 0), time(17, 30), index_symbol="^OMX", index_name="OMX 30",
           currency="SEK", news_terms=("OMX Stockholm stocks",)),
    Market("turkey", "Turkey", "Borsa Istanbul", "Europe/Istanbul",
           time(10, 0), time(18, 0), lunch_start=time(13, 0), lunch_end=time(14, 0),
           index_symbol="XU100.IS", index_name="BIST 100", currency="TRY",
           news_terms=("BIST Istanbul stock market",)),

    # ------------------------------------------------------------ Americas --
    Market("usa", "United States", "NYSE / NASDAQ", "America/New_York",
           time(9, 30), time(16, 0), index_symbol="^GSPC", index_name="S&P 500",
           currency="USD",
           news_terms=("US stock market S&P 500 Nasdaq", "Federal Reserve rates inflation",
                       "US economy jobs CPI")),
    Market("canada", "Canada", "TSX", "America/Toronto",
           time(9, 30), time(16, 0), index_symbol="^GSPTSE", index_name="S&P/TSX",
           currency="CAD", news_terms=("TSX Canada stock market",)),
    Market("brazil", "Brazil", "B3", "America/Sao_Paulo",
           time(10, 0), time(17, 0), index_symbol="^BVSP", index_name="Bovespa",
           currency="BRL", news_terms=("Bovespa Brazil stock market",)),
    Market("mexico", "Mexico", "BMV", "America/Mexico_City",
           time(8, 30), time(15, 0), index_symbol="^MXX", index_name="IPC",
           currency="MXN", news_terms=("Mexico BMV IPC stock market",)),

    # ------------------------------------------------- Africa and Oceania --
    Market("southafrica", "South Africa", "JSE", "Africa/Johannesburg",
           time(9, 0), time(17, 0), index_symbol="^J203.JO", index_name="JSE All Share",
           currency="ZAR", news_terms=("JSE South Africa stock market",)),
    Market("australia", "Australia", "ASX", "Australia/Sydney",
           time(10, 0), time(16, 0), index_symbol="^AXJO", index_name="ASX 200",
           currency="AUD", news_terms=("ASX Australia stock market", "RBA Australia")),
    Market("newzealand", "New Zealand", "NZX", "Pacific/Auckland",
           time(10, 0), time(16, 45), index_symbol="^NZ50", index_name="NZX 50",
           currency="NZD", news_terms=("NZX New Zealand stock market",)),
)}

DEFAULT_MARKET = "india"

# Session states.
WEEKEND = "WEEKEND"
CLOSED = "CLOSED"
PRE_MARKET = "PRE_MARKET"
OPEN = "OPEN"
LUNCH = "LUNCH"
POST_MARKET = "POST_MARKET"


def get_market(key: str | None) -> Market:
    return MARKETS.get((key or DEFAULT_MARKET).lower(), MARKETS[DEFAULT_MARKET])


def list_markets() -> list[dict]:
    """Every market, grouped sensibly for a dropdown."""
    return [m.as_dict() for m in MARKETS.values()]


def _combine(moment: datetime, at: time) -> datetime:
    return moment.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)


@dataclass
class SessionState:
    """Where a market is in its day, and whether the LLM should be working."""

    market: str
    market_name: str
    state: str
    local_time: str
    timezone: str
    is_trading_day: bool
    minutes_to_open: int | None = None
    minutes_to_close: int | None = None
    # The gate: should an LLM agent run right now?
    llm_active: bool = False
    llm_reason: str = ""
    llm_opens_in_minutes: int | None = None
    next_open: str | None = None
    detail: str = ""
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = self.__dict__.copy()
        return data


def session_state(market_key: str | None = None,
                  now: datetime | None = None) -> SessionState:
    """Classify a market's current session and decide the LLM gate.

    The gate opens ``llm_lead_minutes`` before the bell so the pre-market brief
    can be built while it is still useful, and shuts at the close. Outside that
    window an LLM has no live prices to reason about, so calling one spends
    money to learn nothing.
    """
    market = get_market(market_key)
    moment = (now.astimezone(market.tz) if now else market.now())

    # AGENT_LEAD_MINUTES widens or narrows the pre-open window without a code
    # change. NSE's bell is 09:15 (pre-open runs 09:00-09:15), so the default
    # 30 opens the gate at 08:45; set 45 for an 08:30 start.
    lead = market.llm_lead_minutes
    try:
        import os

        override = os.getenv("AGENT_LEAD_MINUTES")
        if override:
            lead = max(0, min(240, int(override)))
    except (TypeError, ValueError):
        pass

    is_trading_day = moment.weekday() in market.trading_days
    open_at = _combine(moment, market.open_time)
    close_at = _combine(moment, market.close_time)
    llm_open_at = open_at - timedelta(minutes=lead)

    state = CLOSED
    minutes_to_open: int | None = None
    minutes_to_close: int | None = None
    detail = ""

    if not is_trading_day:
        state = WEEKEND
        detail = (
            f"{market.name} does not trade today "
            f"({'Sun-Thu market' if market.trading_days == SUN_THU else 'weekend'})."
        )
    elif moment < llm_open_at:
        state = CLOSED
        minutes_to_open = int((open_at - moment).total_seconds() // 60)
        detail = f"{market.exchange} opens in {minutes_to_open} minutes."
    elif moment < open_at:
        state = PRE_MARKET
        minutes_to_open = int((open_at - moment).total_seconds() // 60)
        detail = (
            f"Pre-market: {minutes_to_open} minutes to the bell. This is when "
            f"the news brief is worth reading."
        )
    elif moment <= close_at:
        in_lunch = (
            market.lunch_start is not None
            and _combine(moment, market.lunch_start) <= moment
            < _combine(moment, market.lunch_end)
        )
        state = LUNCH if in_lunch else OPEN
        minutes_to_close = int((close_at - moment).total_seconds() // 60)
        detail = (
            f"Lunch break until {market.lunch_end:%H:%M}; price is not trading."
            if in_lunch else
            f"{market.exchange} is open. {minutes_to_close} minutes to the close."
        )
    else:
        state = POST_MARKET
        detail = f"{market.exchange} closed at {market.close_time:%H:%M}."

    # --- the gate -------------------------------------------------------- #
    llm_active = is_trading_day and llm_open_at <= moment <= close_at
    if llm_active:
        llm_reason = (
            f"Market window open ({llm_open_at:%H:%M}-{close_at:%H:%M} "
            f"{market.timezone.split('/')[-1]})."
        )
        llm_opens_in = 0
    else:
        llm_opens_in = None
        if is_trading_day and moment < llm_open_at:
            llm_opens_in = int((llm_open_at - moment).total_seconds() // 60)
            llm_reason = (
                f"LLM paused to save tokens — it starts in {llm_opens_in} minutes, "
                f"{lead} minutes before the open."
            )
        elif not is_trading_day:
            llm_reason = "LLM paused — not a trading day for this market."
        else:
            llm_reason = "LLM paused — the market has closed for the day."

    return SessionState(
        market=market.key, market_name=f"{market.name} ({market.exchange})",
        state=state, local_time=moment.strftime("%Y-%m-%d %H:%M:%S"),
        timezone=market.timezone, is_trading_day=is_trading_day,
        minutes_to_open=minutes_to_open, minutes_to_close=minutes_to_close,
        llm_active=llm_active, llm_reason=llm_reason,
        llm_opens_in_minutes=llm_opens_in,
        next_open=_next_open(market, moment),
        detail=detail,
        extras={
            "open": market.open_time.strftime("%H:%M"),
            "close": market.close_time.strftime("%H:%M"),
            "square_off": market.square_off.strftime("%H:%M") if market.square_off else None,
            "index_name": market.index_name,
            "currency": market.currency,
            "llm_lead_minutes": lead,
        },
    )


def _next_open(market: Market, moment: datetime) -> str:
    """When this market next opens, in its own local time."""
    candidate = moment
    for _ in range(8):           # a week is enough for any weekly schedule
        open_at = _combine(candidate, market.open_time)
        if candidate.weekday() in market.trading_days and open_at > moment:
            return open_at.strftime("%a %d %b, %H:%M")
        candidate = (candidate + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return "unknown"


def llm_should_run(market_key: str | None = None,
                   now: datetime | None = None) -> tuple[bool, str]:
    """The gate, as a one-liner for callers that only need the decision."""
    state = session_state(market_key, now)
    return state.llm_active, state.llm_reason
