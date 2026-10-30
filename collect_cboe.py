"""
CBOE 지연 옵션 체인 수집기.
CBOE 무료 시세는 15분 늦다. 그래서 목표 시장 시각 + 15분에 받고, 파일 이름은 시장 시각으로 붙인다
(10:00에 받은 파일 = 09:45 시장 상태 → _0945). 2026-10-29 이전 파일은 받은 시각으로 이름이 붙어 있어
실제 시장 상태가 이름보다 15분 앞이다(README 참고).
  python collect_cboe.py am    → 09:45, 10:45, 11:45, 12:45 ET 시장 상태
  python collect_cboe.py pm    → 13:45, 14:45, 15:45, 16:00(주식 종가), 16:15(옵션 마감)
  python collect_cboe.py eod   → 그날 마감 스냅샷이 빠졌을 때만 1회 (예비용)
  python collect_cboe.py test  → 지금 1회, data/_test 에 저장 (커밋하지 않음)
  python collect_cboe.py live  → SPX·TSLA 실시간 GEX 1회 갱신 (live 브랜치)
  python collect_cboe.py gth   → SPX 야간 거래(GTH, 20:15~09:15 ET) 동안 1분마다 GEX 갱신 (잡 하나당 최대 5시간 40분)
PUSH_EACH=1 이면 회차마다 GEX 대시보드(docs/)를 갱신하고 커밋·푸시한다.
LIVE=1 이면 회차 사이 대기 시간에 SPX·TSLA GEX를 1분마다 갱신한다(live.py).
GitHub Actions cron은 UTC 고정이지만 목표 시각은 ET로 계산하므로 서머타임은 자동 처리.
"""
import os, re, sys, time, subprocess, threading
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from us_calendar import is_trading_day, is_fresh, EARLY_CLOSE

TICKERS = ["_SPX", "SPY", "QQQ", "TSLA", "AAPL", "NVDA", "IBIT", "_VIX"]   # 지수는 "_SPX"처럼 앞에 _
OUT_DIR = "data"
ET = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PAT = re.compile(r"^(\D+)(\d{6})([CP])(\d{8})$")
DELAY = timedelta(minutes=15)   # CBOE 지연 시세
LATE = timedelta(minutes=20)    # 이보다 늦게 시작한 회차는 건너뜀

SLOTS = {   # ET 시장 시각
    "am": [(9, 45), (10, 45), (11, 45), (12, 45)],
    "pm": [(13, 45), (14, 45), (15, 45), (16, 0), (16, 15)],
}
EARLY_SLOTS = {   # 13:00 조기 폐장일
    "am": [(9, 45), (10, 45), (11, 45), (12, 45), (13, 0), (13, 15)],
    "pm": [],
}
# 실시간 갱신을 멈추는 시각(ET, 수집 시각). am은 pm 잡이 이어받기 직전까지, pm은 옵션 마감 시세(16:15)를 받을 때까지
LIVE_END = {"am": (13, 50), "pm": (16, 31)}
HANDOFF_WAIT = 25                 # 초. am 실시간이 멈춘 뒤 교대 스냅샷 push가 끝날 시간을 주고 pm이 이어받는다
GTH_MAX = timedelta(hours=5, minutes=40)   # 야간 잡 하나의 최대 길이 (GitHub 잡 6시간 제한 안쪽)
EARLY_LIVE_END = {"am": (13, 31), "pm": (13, 31)}


def slim(name, df):
    """SPX는 체인이 커서 미결제·거래량이 있고 현재가 ±25% 안의 계약만 저장한다."""
    if name != "SPX" or df.empty:
        return df
    spot = float(df["spot"].iloc[0])
    keep = ((df["open_interest"].fillna(0) > 0) | (df["volume"].fillna(0) > 0)) & df["strike"].between(spot * 0.75, spot * 1.25)
    return df[keep]


def close_tag(day):
    return "1315" if day in EARLY_CLOSE else "1615"


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
    ts = datetime.strptime(js["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    d = js["data"]
    df = pd.DataFrame(d["options"])
    if df.empty:
        return df, ts
    m = df["option"].str.extract(PAT)
    df.insert(1, "root", m[0])
    df.insert(2, "expiry", pd.to_datetime("20" + m[1], format="%Y%m%d").dt.date)
    df.insert(3, "cp", m[2])
    df.insert(4, "strike", m[3].astype(int) / 1000)
    df.insert(0, "underlying", sym.lstrip("_"))
    df["spot"] = d.get("current_price")
    pc = d.get("prev_day_close")
    if pc is None and d.get("current_price") is not None and d.get("price_change") is not None:
        pc = d["current_price"] - d["price_change"]
    df["prev_close"] = pc                                          # 전일 종가 (화면의 등락률용)
    df["cboe_timestamp"] = js.get("timestamp")
    df["collected_et"] = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    df["quote_et"] = (ts - DELAY).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")   # 시세가 반영하는 시장 시각(추정)
    return df.sort_values(["expiry", "cp", "strike"]).reset_index(drop=True), ts


def snap_path(out_dir, name, day, tag):
    return f"{out_dir}/{name}/{day}/{name}_{day}_{tag}.csv.gz"


def have(out_dir, name, day, tag):
    p = snap_path(out_dir, name, day, tag)
    return os.path.exists(p) or os.path.exists(p[:-3])


def collect(tag, day, out_dir=OUT_DIR, check_fresh=True):
    """종목별로 한 번 수집. 이미 있는 파일은 건드리지 않는다(뒤늦게 뜬 예비 잡의 덮어쓰기 방지). 실패 수를 반환."""
    fails = 0
    for sym in TICKERS:
        name = sym.lstrip("_")
        if have(out_dir, name, day, tag):
            print(f"[skip] {name} {tag}: 이미 있음", flush=True)
            continue
        try:
            for attempt in range(3):   # CBOE가 묵은 파일을 주면(9/23 사고) 저장하지 않음
                df, ts = fetch(sym)
                if not check_fresh or is_fresh(ts.astimezone(ET), datetime.now(ET), day):
                    break
                print(f"[stale] {name} {tag}: CBOE 파일 시각 {ts.astimezone(ET):%m-%d %H:%M} ET, 30초 뒤 재시도", flush=True)
                time.sleep(30)
            else:
                raise RuntimeError(f"CBOE 파일 시각 {ts.astimezone(ET):%m-%d %H:%M} ET — 묵은 시세라 저장 안 함")
            path = snap_path(out_dir, name, day, tag)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            df = slim(name, df)
            df.to_csv(path, index=False, compression="gzip")
            print(f"[ok] {datetime.now(ET):%H:%M:%S} ET  {name} {tag}: {len(df):>6} rows (시세 {df['quote_et'].iloc[0][11:16]} ET)", flush=True)
        except Exception as e:
            fails += 1
            print(f"[FAIL] {sym} {tag}: {e}", file=sys.stderr, flush=True)
        time.sleep(1)
    return fails


def publish(tag, day):
    """회차마다 대시보드 갱신 + 커밋·푸시. 실패해도 수집은 계속한다."""
    if os.environ.get("PUSH_EACH") != "1":
        return
    try:
        import gex
        gex.build_day(day)
    except Exception as e:
        print(f"[gex FAIL] {e}", file=sys.stderr, flush=True)
    subprocess.run(["bash", "commit.sh", f"collect {tag} {day}"], check=False)


def chain(mode):
    """끝나기 직전에 다음 잡을 띄워 수집이 끊기지 않게 한다(정규장 오전 → 오후 → 야간 → … → 다음 날 오전).
    같은 모드 잡이 이미 대기·실행 중이면 띄우지 않는다(예약 작업·백업 크론과 겹쳐 잡이 불어나지 않게)."""
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        return
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{repo}/actions/workflows/collect.yml"
    try:
        me, runs = os.environ.get("GITHUB_RUN_ID"), []
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            r = requests.get(f"{base}/runs", params={"status": status, "per_page": 30}, headers=h, timeout=20)
            runs += r.json().get("workflow_runs", [])
        if any(x.get("display_title") == f"collect {mode}" and str(x.get("id")) != me for x in runs):
            print(f"[chain] {mode}: 이미 대기·실행 중이라 건너뜀", flush=True)
            return
        r = requests.post(f"{base}/dispatches", json={"ref": "main", "inputs": {"mode": mode}}, headers=h, timeout=20)
        print(f"[chain] 다음 잡 {mode} 띄움 (HTTP {r.status_code})", flush=True)
    except Exception as e:
        print(f"[chain FAIL] {mode}: {e}", file=sys.stderr, flush=True)


def sleep_until(target, live=None):
    """target(ET datetime)까지 대기. live가 있으면 기다리는 동안 1분마다 실시간 GEX를 갱신한다."""
    if live is not None:
        return live.run_until(target)
    while True:
        remaining = (target - datetime.now(ET)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "eod"
    now = datetime.now(ET)
    day = now.date()

    if mode == "test":
        fails = collect(f"test{now:%H%M}", day, out_dir="data/_test", check_fresh=False)   # 새벽에도 경로를 시험할 수 있게
        import gex
        gex.build_day(day, data_dir="data/_test", out_dir="data/_test/docs", verbose=True, strict=False)
        sys.exit(1 if fails else 0)

    if mode == "live":
        from live import Live
        Live(fetch).tick()
        return

    if mode == "gth":
        from live import Live
        end = now + GTH_MAX
        if now.hour < 12:                                   # 아침 잡은 야간 거래가 끝나는 09:15에서 멈춘다
            end = min(end, now.replace(hour=9, minute=15, second=0, microsecond=0) + DELAY)
        Live(fetch, syms=["_SPX"]).run_until(end)
        t = datetime.now(ET)
        morning_end = t.replace(hour=9, minute=15, second=0, microsecond=0) + DELAY
        if t.hour < 12 and t >= morning_end - timedelta(minutes=1):
            if is_trading_day(t.date()):
                chain("am")                                 # 야간이 끝나면 바로 정규장 잡으로
        else:
            chain("gth")                                    # 야간이 남았으면 이어서
        return

    if not is_trading_day(day):
        print(f"skip: {day} 휴장")
        return

    if mode == "eod":
        tag = close_tag(day)
        missing = [s for s in TICKERS if not have(OUT_DIR, s.lstrip("_"), day, tag)]
        if not missing:
            print(f"skip eod: {tag} 마감 스냅샷이 이미 있음")
            return
        fails = collect("eod", day)
        publish("eod", day)
        sys.exit(1 if fails else 0)

    h, mnt = (EARLY_LIVE_END if day in EARLY_CLOSE else LIVE_END)[mode]
    if now >= now.replace(hour=h, minute=mnt, second=0, microsecond=0):
        # GitHub 백업 예약은 몇 시간씩 늦게 뜨기도 한다(10/30 am 예약이 16:31 ET에 떠서 pm 잡을 하나 더 띄웠다)
        print(f"skip {mode}: 그 시간대가 이미 끝남({h:02d}:{mnt:02d} ET). 다음 잡도 띄우지 않는다", flush=True)
        return

    if mode == "pm" and day in EARLY_CLOSE:   # 조기 폐장일은 am 잡이 마감·보관까지 맡는다
        print("skip pm: 조기 폐장일", flush=True)
        return
    if mode == "pm" and os.environ.get("LIVE") == "1":
        # pm은 am과 다른 줄이라 미리 떠 있을 수 있다. am 실시간이 끝나고 교대 스냅샷을 올릴 때까지 기다렸다가 이어받는다
        h0, m0 = LIVE_END["am"]
        handoff = now.replace(hour=h0, minute=m0, second=0, microsecond=0) + timedelta(seconds=HANDOFF_WAIT)
        if datetime.now(ET) < handoff:
            print(f"pm: am 실시간이 끝나는 {handoff:%H:%M:%S} ET까지 대기", flush=True)
            sleep_until(handoff)

    live = None
    if os.environ.get("LIVE") == "1":
        from live import Live
        live = Live(fetch)
    total_fails = 0
    for h, mnt in (EARLY_SLOTS if day in EARLY_CLOSE else SLOTS)[mode]:
        tag = f"{h:02d}{mnt:02d}"
        target = now.replace(hour=h, minute=mnt, second=0, microsecond=0) + DELAY
        if datetime.now(ET) > target + LATE:
            print(f"skip {tag}: 잡이 너무 늦게 시작됨", flush=True)
            continue
        print(f"waiting for {target:%H:%M} ET (시장 {tag}) ...", flush=True)
        sleep_until(target, live)
        if live is None:
            total_fails += collect(tag, day)
            publish(tag, day)
            continue
        # 회차 저장(8종목 받기·GEX 계산·커밋, 40~70초)은 따로 돌리고 그동안에도 실시간 갱신을 이어간다
        res = {}

        def slot_job(tag=tag):
            try:
                res["fails"] = collect(tag, day)
                publish(tag, day)
            except Exception as e:
                res["fails"] = 1
                print(f"[FAIL] 회차 {tag}: {e}", file=sys.stderr, flush=True)

        th = threading.Thread(target=slot_job)
        th.start()
        while th.is_alive():
            live.run_until(datetime.now(ET) + timedelta(seconds=3))
        th.join()
        total_fails += res.get("fails", 0)
    if live is not None:
        live.run_until(now.replace(hour=h, minute=mnt, second=0, microsecond=0))
        if mode == "am" and day not in EARLY_CLOSE:   # pm 잡이 30·60분 창을 바로 채울 수 있게 최근 스냅샷을 넘긴다
            try:
                live.save_handoff()
            except Exception as e:
                print(f"[live FAIL] 교대 스냅샷: {e}", file=sys.stderr, flush=True)
        if mode == "pm" or day in EARLY_CLOSE:   # 장 마감 후 그날 분 단위 기록을 보관
            try:
                live.archive()
                try:   # 그날(또는 지난 날) 보관본에 1분봉이 비어 있으면 채운다
                    import backfill_bars
                    backfill_bars.fill("TSLA")
                except Exception as e:
                    print(f"[bars FAIL] {e}", file=sys.stderr, flush=True)
                subprocess.run(["bash", "commit.sh", f"archive {day}"], check=False)
            except Exception as e:
                print(f"[archive FAIL] {e}", file=sys.stderr, flush=True)
    if mode == "am":
        chain("pm")
    elif mode == "pm" and is_trading_day(day + timedelta(days=1)):
        chain("gth")                                        # 내일이 거래일이면 오늘 밤 야간 거래가 있다. 20:15 ET까지는 기다리며 돈다
    sys.exit(1 if total_fails else 0)


if __name__ == "__main__":
    main()
