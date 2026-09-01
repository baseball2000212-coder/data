"""
CBOE 지연 옵션 체인 수집기 — 정확한 시각(ET 매시 45분)에 맞춰 수집.
잡을 일찍 띄우고 이 스크립트가 목표 시각까지 기다렸다가 받는다.
  python collect_cboe.py am    → 9:45, 10:45, 11:45, 12:45 ET
  python collect_cboe.py pm    → 13:45, 14:45, 15:45, 16:15, 16:45 ET
  python collect_cboe.py eod   → 즉시 1회 (장마감 후용)
GitHub Actions cron은 UTC 고정이지만 목표 시각은 ET로 계산하므로 서머타임은 자동 처리.
"""
import os, re, sys, time
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TICKERS = ["SPY", "TSLA", "AAPL", "NVDA", "IBIT"]   # 지수는 "_SPX"처럼 앞에 _
OUT_DIR = "data"
ET = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PAT = re.compile(r"^(\D+)(\d{6})([CP])(\d{8})$")

SCHEDULE = {
    "am": [(9, 45), (10, 45), (11, 45), (12, 45)],
    "pm": [(13, 45), (14, 45), (15, 45), (16, 15), (16, 45)],   # 16:15 = 주식종가(16:00) 시세, 16:45 = 옵션마감(16:15) 이후
}


def fetch(sym):
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    js = r.json()
    d = js["data"]
    df = pd.DataFrame(d["options"])
    if df.empty:
        return df
    m = df["option"].str.extract(PAT)
    df.insert(1, "root", m[0])
    df.insert(2, "expiry", pd.to_datetime("20" + m[1], format="%Y%m%d").dt.date)
    df.insert(3, "cp", m[2])
    df.insert(4, "strike", m[3].astype(int) / 1000)
    df.insert(0, "underlying", sym.lstrip("_"))
    df["spot"] = d.get("current_price")
    df["cboe_timestamp"] = js.get("timestamp")
    df["collected_et"] = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    return df.sort_values(["expiry", "cp", "strike"]).reset_index(drop=True)


def collect(tag):
    """5종목 한 번 수집. 실패한 종목 수를 반환."""
    day = datetime.now(ET).strftime("%Y-%m-%d")
    fails = 0
    for sym in TICKERS:
        try:
            df = fetch(sym)
            name = sym.lstrip("_")
            os.makedirs(f"{OUT_DIR}/{name}/{day}", exist_ok=True)
            path = f"{OUT_DIR}/{name}/{day}/{name}_{day}_{tag}.csv"
            df.to_csv(path, index=False)
            print(f"[ok] {datetime.now(ET):%H:%M:%S} ET  {name} {tag}: {len(df):>6} rows", flush=True)
        except Exception as e:
            fails += 1
            print(f"[FAIL] {sym} {tag}: {e}", file=sys.stderr, flush=True)
        time.sleep(1)
    return fails


def sleep_until(target):
    """target(ET datetime)까지 대기. 이미 지났으면 바로 반환."""
    while True:
        remaining = (target - datetime.now(ET)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "eod"
    now = datetime.now(ET)
    if now.weekday() >= 5:
        print("skip: 주말")
        return

    if mode == "eod":
        sys.exit(1 if collect("eod") else 0)

    total_fails = 0
    for h, mnt in SCHEDULE[mode]:
        target = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
        if datetime.now(ET) > target + timedelta(minutes=20):
            print(f"skip {h:02d}{mnt:02d}: 잡이 너무 늦게 시작됨", flush=True)
            continue
        print(f"waiting for {target:%H:%M} ET ...", flush=True)
        sleep_until(target)
        total_fails += collect(f"{h:02d}{mnt:02d}")
    sys.exit(1 if total_fails else 0)


if __name__ == "__main__":
    main()
