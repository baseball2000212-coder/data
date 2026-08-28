"""
CBOE 지연 옵션 체인 수집기 — 티커별 '모든 만기'를 한 번에 CSV로 저장.
사이트의 Download CSV 버튼이 쓰는 것과 같은 JSON을 직접 받는다.
"""
import os, re, sys, time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

TICKERS = ["SPY", "TSLA", "AAPL", "NVDA", "IBIT"]   # 지수는 "_SPX"처럼 앞에 _ 붙임
OUT_DIR = "data"
KST = timezone(timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PAT = re.compile(r"^(\D+)(\d{6})([CP])(\d{8})$")   # OCC 심볼: SPY260918C00450000


def fetch(sym: str) -> pd.DataFrame:
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            break
        except Exception as e:
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
    df["collected_kst"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    return df.sort_values(["expiry", "cp", "strike"]).reset_index(drop=True)


def main():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    failed = []
    for sym in TICKERS:
        try:
            df = fetch(sym)
            name = sym.lstrip("_")
            os.makedirs(f"{OUT_DIR}/{name}", exist_ok=True)
            path = f"{OUT_DIR}/{name}/{name}_{today}.csv"
            df.to_csv(path, index=False)
            print(f"[ok] {name}: {len(df):>6} rows, {df['expiry'].nunique():>3} expiries -> {path}")
        except Exception as e:
            failed.append(sym)
            print(f"[FAIL] {sym}: {e}", file=sys.stderr)
        time.sleep(2)   # 요청 사이 텀
    if failed:
        sys.exit(1)     # Actions에서 실패로 표시되어 알림이 옴


if __name__ == "__main__":
    main()
