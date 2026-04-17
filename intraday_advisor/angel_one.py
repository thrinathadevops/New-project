from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from intraday_advisor.data import clean_ohlcv
from intraday_advisor.risk import TradePlan


@dataclass(frozen=True)
class AngelCredentials:
    api_key: str
    client_code: str
    pin: str
    totp_secret: str

    @classmethod
    def from_env(cls) -> "AngelCredentials":
        values = {
            "api_key": os.getenv("ANGEL_ONE_API_KEY", ""),
            "client_code": os.getenv("ANGEL_ONE_CLIENT_CODE", ""),
            "pin": os.getenv("ANGEL_ONE_PIN", ""),
            "totp_secret": os.getenv("ANGEL_ONE_TOTP_SECRET", ""),
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"Missing Angel One environment values: {', '.join(missing)}")
        return cls(**values)


class AngelOneClient:
    def __init__(self, credentials: AngelCredentials | None = None) -> None:
        self.credentials = credentials or AngelCredentials.from_env()
        self.smart_api = None
        self.refresh_token = ""
        self.feed_token = ""

    def login(self) -> None:
        try:
            import pyotp
            from SmartApi import SmartConnect
        except ImportError as exc:
            raise RuntimeError("Install Angel One dependencies with: python -m pip install smartapi-python pyotp logzero websocket-client") from exc

        self.smart_api = SmartConnect(self.credentials.api_key)
        totp = pyotp.TOTP(self.credentials.totp_secret).now()
        session = self.smart_api.generateSession(self.credentials.client_code, self.credentials.pin, totp)
        if not session.get("status"):
            raise RuntimeError(f"Angel One login failed: {session}")
        self.refresh_token = session["data"]["refreshToken"]
        self.feed_token = self.smart_api.getfeedToken()

    def get_candles(
        self,
        symbol_token: str,
        exchange: str = "NSE",
        interval: str = "FIVE_MINUTE",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> pd.DataFrame:
        if self.smart_api is None:
            self.login()
        params = {
            "exchange": exchange,
            "symboltoken": symbol_token,
            "interval": interval,
            "fromdate": from_date or datetime.now().strftime("%Y-%m-%d 09:15"),
            "todate": to_date or datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        response = self.smart_api.getCandleData(params)
        if not response.get("status"):
            raise RuntimeError(f"Angel One candle request failed: {response}")
        frame = pd.DataFrame(response["data"], columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        return clean_ohlcv(frame.set_index("Date"))

    def search_symbol_token(self, symbol: str, exchange: str = "NSE") -> str:
        if self.smart_api is None:
            self.login()
        query = symbol.upper().replace("-EQ", "")
        response = self.smart_api.searchScrip(exchange, query)
        if not response.get("status"):
            raise RuntimeError(f"Angel One symbol search failed: {response}")

        matches = response.get("data") or []
        for item in matches:
            trading_symbol = str(item.get("tradingsymbol", "")).upper()
            name = str(item.get("name", "")).upper()
            if trading_symbol == f"{query}-EQ" or name == query:
                token = item.get("symboltoken") or item.get("token")
                if token:
                    return str(token)
        raise LookupError(f"No exact NSE equity token found for {symbol}")

    def get_open_positions(self) -> list[dict]:
        """Fetch real-time open positions from Angel One."""
        if self.smart_api is None:
            try:
                self.login()
            except Exception as e:
                # Silently catch offline errors to act as a fallback network failure handler
                print(f"Angel One Login Failed inside get_open_positions: {e}")
                return []
                
        try:
            response = self.smart_api.position()
            if not response.get("status"):
                return []
                
            positions = response.get("data")
            if not positions:
                return []

            active_positions = []
            for pos in positions:
                net_qty = int(pos.get("netqty", 0))
                if net_qty != 0: # Only open positions
                    avg_price = float(pos.get("avgnetprice", 0))
                    # Fallback to buy price if parsing fails
                    if avg_price == 0:
                        avg_price = float(pos.get("buyavgprice", 0))
                    
                    symbol = pos.get("tradingsymbol", "UNKNOWN")
                    # Clean up -EQ if it exists
                    display_ticker = symbol.replace("-EQ", "")
                    
                    active_positions.append({
                        "Ticker": display_ticker,
                        "Option": symbol,
                        "Entry Underlying Level": avg_price,
                        "Target Level": avg_price * 1.20,  # 20% Profit Target default
                        "Stop Loss Level": avg_price * 0.90, # 10% Stop Loss default
                        "Source": "ANGEL_ONE_API",
                        "Qty": net_qty
                    })
            return active_positions
        except Exception as e:
            print(f"Failed to fetch Angel One positions: {e}")
            return []

    def place_intraday_order(
        self,
        plan: TradePlan,
        symbol_token: str,
        live_confirmed: bool = False,
        exchange: str = "NSE",
        order_type: str = "LIMIT",
    ) -> dict:
        if os.getenv("ENABLE_LIVE_TRADING", "NO").upper() != "YES" or not live_confirmed:
            raise PermissionError("Live trading is blocked. Set ENABLE_LIVE_TRADING=YES and pass live_confirmed=True.")
        if plan.shares <= 0:
            raise ValueError("Order blocked: share quantity is zero.")
        if self.smart_api is None:
            self.login()

        orderparams = {
            "variety": "NORMAL",
            "tradingsymbol": f"{plan.ticker}-EQ",
            "symboltoken": symbol_token,
            "transactiontype": plan.direction,
            "exchange": exchange,
            "ordertype": order_type,
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(plan.entry if order_type == "LIMIT" else 0),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(plan.shares),
        }
        return self.smart_api.placeOrderFullResponse(orderparams)
