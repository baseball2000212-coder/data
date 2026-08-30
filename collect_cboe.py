"""
CBOE 지연 옵션 체인 수집기 — 장중 1시간 간격 + 장마감 후 1회.
GitHub Actions cron은 UTC 고정이지만, 실제 실행 여부는 아래에서 미국 동부시간(ET)으로
판단하므로 서머타임 전환은 자동으로 처리된다.
"""
import os, re, sys, time
import requests
import pandas as pd
from datetime import datetime, time as T
from zoneinfo import ZoneInfo

TICKERS = ["SPY", "TSLA", "AAPL", "NVDA", "IBIT"]   # 지수는 "_SPX"처럼 앞에 _
OUT_DIR = "data"
ET = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PAT = re.compile(r"^(\D+)(\d{6})([CP])(\d{8})$")

SESSION_START, SESSION_END = T(9, 30), T(17, 15)   # 장중 스냅샷 창 (16:45 실행이 늦어도 통과)
EOD_START, EOD_END = T(17, 30), T(19, 30)          # 장마감 후 1회 스냅샷 창


def snapshot_tag(now_et):
    """실행 시각이 어느 창에 속하는지. 어느 창도 아니면 None → 아무것도 안 함."""
    if now_et.weekday() >= 5:
        return None
    t = now_et.time()
    if SESSION_START <= t <= SESSION_END:
        return now_et.strftime("%H%M")          # 예: 1030, 1130, ...
    if EOD_START <= t <= EOD_END:
        return "eod"
    return None


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


def main():
    now = datetime.now(ET)
    tag = snapshot_tag(now)
    if tag is None:
        print(f"skip: {now:%Y-%m-%d %H:%M} ET 는 수집 창 밖")
        return
    day = now.strftime("%Y-%m-%d")
    failed = []
    for sym in TICKERS:
        try:
            df = fetch(sym)
            name = sym.lstrip("_")
            os.makedirs(f"{OUT_DIR}/{name}/{day}", exist_ok=True)
            path = f"{OUT_DIR}/{name}/{day}/{name}_{day}_{tag}.csv"
            df.to_csv(path, index=False)
            print(f"[ok] {name} {tag}: {len(df):>6} rows -> {path}")
        except Exception as e:
            failed.append(sym)
            print(f"[FAIL] {sym}: {e}", file=sys.stderr)
        time.sleep(2)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
