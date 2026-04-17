"""
NSE Top Stock Lists for Intraday Scanning.
Covers Nifty 50, Nifty Bank, Nifty Midcap high-liquidity names.
These are fetched live from Yahoo Finance with the .NS suffix.
"""
from __future__ import annotations

# Nifty 50 constituents (high liquidity, options available)
NIFTY50 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY",
    "HINDUNILVR", "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK",
    "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "HCLTECH", "SUNPHARMA", "TITAN", "ULTRACEMCO", "NESTLEIND",
    "WIPRO", "POWERGRID", "NTPC", "TECHM", "INDUSINDBK",
    "ONGC", "JSWSTEEL", "TATASTEEL", "COALINDIA", "BPCL",
    "GRASIM", "CIPLA", "DIVISLAB", "DRREDDY", "HINDALCO",
    "ADANIENT", "ADANIPORTS", "BAJAJ-AUTO", "BAJAJFINSV", "BRITANNIA",
    "EICHERMOT", "HEROMOTOCO", "M&M", "SHREECEM", "TATACONSUM",
    "TATAMOTORS", "UPL", "VEDL", "APOLLOHOSP", "SBILIFE",
]

# Nifty Bank (high volume options)
NIFTY_BANK = [
    "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK",
    "INDUSINDBK", "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB", "PNB",
]

# High-volume F&O stocks with active options chains
FNO_ACTIVES = [
    "TATAMOTORS", "INFY", "RELIANCE", "SBIN", "ICICIBANK",
    "HDFCBANK", "AXISBANK", "WIPRO", "TCS", "BAJFINANCE",
    "HINDUNILVR", "ITC", "SUNPHARMA", "ONGC", "TATASTEEL",
    "JSWSTEEL", "LT", "TECHM", "HCLTECH", "MARUTI",
    "BAJAJ-AUTO", "HEROMOTOCO", "TITAN", "M&M", "ADANIPORTS",
    "COALINDIA", "NTPC", "POWERGRID", "BPCL", "VEDL",
]

def get_scan_universe(mode: str = "nifty50") -> list[str]:
    """
    Return the list of symbols to scan based on mode.
    mode: 'nifty50' | 'banknifty' | 'fno' | 'all'
    """
    if mode == "banknifty":
        return NIFTY_BANK
    elif mode == "fno":
        return FNO_ACTIVES
    elif mode == "all":
        combined = list(dict.fromkeys(NIFTY50 + NIFTY_BANK + FNO_ACTIVES))
        return combined
    else:  # default nifty50
        return NIFTY50
