"""
Market configurations for the Trading Dashboard.
Defines Yahoo Finance suffixes, local execution market open times, and default monitoring symbols.
"""

GLOBAL_MARKETS = {
    "India (NSE)": {
        "suffix": ".NS", 
        "open_hour": 9, 
        "open_minute": 15, 
        "default_symbols": "RELIANCE,HDFCBANK,INFY,TATAMOTORS,JSWSTEEL"
    },
    "USA (NYSE/NASDAQ)": {
        "suffix": "", 
        "open_hour": 9, 
        "open_minute": 30, 
        "default_symbols": "AAPL,MSFT,TSLA,NVDA,META"
    },
    "UK (LSE)": {
        "suffix": ".L", 
        "open_hour": 8, 
        "open_minute": 0, 
        "default_symbols": "HSBA.L,SHEL.L,BP.L,AZN.L,ULVR.L"
    },
    "China (SSE)": {
        "suffix": ".SS", 
        "open_hour": 9, 
        "open_minute": 30, 
        "default_symbols": "600519.SS,601398.SS,601857.SS,600036.SS"
    },
    "Europe (Euronext)": {
        "suffix": ".PA", 
        "open_hour": 9, 
        "open_minute": 0, 
        "default_symbols": "MC.PA,OR.PA,AI.PA,TTE.PA"
    },
    "Japan (JSE)": {
        "suffix": ".T", 
        "open_hour": 9, 
        "open_minute": 0, 
        "default_symbols": "7203.T,9984.T,8306.T,6861.T"
    },
    "Korea (KSE)": {
        "suffix": ".KS", 
        "open_hour": 9, 
        "open_minute": 0, 
        "default_symbols": "005930.KS,000660.KS,373220.KS,207940.KS"
    },
}
