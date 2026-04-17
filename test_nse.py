import requests, time

s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com",
})

print("Step 1: Seeding cookies...")
r1 = s.get("https://www.nseindia.com", timeout=15)
print(f"  Status: {r1.status_code}")
print(f"  Cookies: {list(s.cookies.keys())}")
time.sleep(2)

print("Step 2: Fetching NIFTY option chain...")
r2 = s.get("https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY", timeout=15)
print(f"  Status: {r2.status_code}")
if r2.status_code == 200:
    data = r2.json()
    records = data.get("records", {})
    print(f"  underlyingValue: {records.get('underlyingValue')}")
    print(f"  data rows: {len(records.get('data',[]))}")
    if records.get("data"):
        print(f"  sample row keys: {list(records['data'][0].keys())}")
else:
    print(f"  Body: {r2.text[:300]}")
