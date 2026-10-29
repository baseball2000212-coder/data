"""지난 날 보관본(docs/archive/<종목>/<날짜>.json)에 비어 있는 1분봉을 채운다.

1) live 브랜치에 그날 CBOE 1분봉(콜·풋 거래량 포함)이 남아 있으면 그걸 쓴다.
2) 없으면 Yahoo Finance 차트(정규장 1분봉, 최근 30일까지)로 채운다. 30일보다 오래된 날은 2분봉(60일까지)으로 채운다.
CBOE 1분봉 API는 당일분만 주기 때문에 지난 날은 이렇게 메운다.

  python backfill_bars.py            # TSLA 전체
  python backfill_bars.py TSLA 2026-10-28
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
ARCHIVE = "docs/archive"
YAHOO = {"TSLA": "TSLA"}          # 보관본 이름 → Yahoo 심볼 (SPX는 야간 처리 때문에 채우지 않는다)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def live_bars(sym):
    """live 브랜치의 <종목>.json 1분봉(그날 정규장분)."""
    try:
        subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", "live"], check=True, capture_output=True)
        doc = json.loads(subprocess.run(["git", "show", f"FETCH_HEAD:{sym}.json"], check=True,
                                        capture_output=True, text=True).stdout)
        return doc.get("bars") or []
    except Exception:
        return []


def yahoo_bars(ysym, day, ival):
    """그날 09:30–15:59 ET 봉을 [ET 시각, 시, 고, 저, 종, None, None] 로 돌려준다."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    p1, p2 = int((d + timedelta(hours=9, minutes=30)).timestamp()), int((d + timedelta(hours=16)).timestamp())
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ysym}",
                             params={"interval": ival, "period1": p1, "period2": p2, "includePrePost": "false"},
                             headers=UA, timeout=30)
            j = r.json().get("chart") or {}
        except Exception as e:
            print(f"  {host} {ival}: {e}", flush=True)
            continue
        if j.get("error"):
            print(f"  {host} {ival}: {j['error'].get('description')}", flush=True)
            continue
        res = (j.get("result") or [None])[0] or {}
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        out = []
        for i, ts in enumerate(res.get("timestamp") or []):
            o, h, l, c = (q.get(k, [None] * (i + 1))[i] for k in ("open", "high", "low", "close"))
            t = datetime.fromtimestamp(ts, ET)
            if None in (o, h, l, c) or t.strftime("%Y-%m-%d") != day or not ("09:30" <= t.strftime("%H:%M") <= "15:59"):
                continue
            out.append([t.strftime("%Y-%m-%dT%H:%M"), round(o, 2), round(h, 2), round(l, 2), round(c, 2), None, None])
        return out
    return []


def fill(sym, days=None):
    idx = json.load(open(f"{ARCHIVE}/index.json", encoding="utf-8"))
    lb = live_bars(sym)
    changed = 0
    for day in days or idx.get(sym, []):
        path = f"{ARCHIVE}/{sym}/{day}.json"
        if not os.path.exists(path):
            continue
        doc = json.load(open(path, encoding="utf-8"))
        if doc.get("bars"):
            continue
        bars, ival, src = [b for b in lb if b[0][:10] == day], 1, "CBOE"
        if len(bars) < 300:
            bars, src = [], "Yahoo"
            for ival in (1, 2):
                bars = yahoo_bars(YAHOO[sym], day, f"{ival}m")
                if bars:
                    break
                time.sleep(1)
        if not bars:
            print(f"[bars] {sym} {day}: 없음", flush=True)
            continue
        doc["bars"], doc["bars_ival"], doc["bars_src"] = bars, ival, src
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        changed += 1
        print(f"[bars] {sym} {day}: {src} {ival}분봉 {len(bars)}개 ({bars[0][0][11:]}–{bars[-1][0][11:]})", flush=True)
        time.sleep(0.5)
    return changed


if __name__ == "__main__":
    syms = [a for a in sys.argv[1:] if not a[:1].isdigit()] or list(YAHOO)
    days = [a for a in sys.argv[1:] if a[:1].isdigit()] or None
    print("채운 날:", sum(fill(s, days) for s in syms))
