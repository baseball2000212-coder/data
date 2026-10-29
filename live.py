"""
1분 간격 GEX (시세 자체는 CBOE 무료 시세라 15분 지연). am/pm 잡이 회차 사이 대기 시간에 돌린다.
결과는 이력 없는 브랜치 `live` 에 매번 덮어써서(force push) 저장소가 커지지 않게 하고,
대시보드는 raw.githubusercontent.com 에서 바로 읽는다(GitHub Pages 빌드 제한과 무관).
  live/<종목>.json = {"live": 최신 GEX·거래량, "series": 그날 분 단위 추이}
"""
import os, sys, json, time, subprocess
import requests
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import gex
from us_calendar import session_of

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
SYMS = ["_SPX", "TSLA"]
EVERY = 60                       # 초. CBOE 파일이 1분 남짓마다 새로 만들어져서 이보다 줄여도 소용없다
FLOW_WIN = timedelta(minutes=5)  # 유입 거래량 = 최근 5분 동안 늘어난 거래량
BRANCH = "live"
SERIES_MAX = 500


def chart_bars(sym):
    """CBOE 지연 시세 1분봉(그날 정규장). GEX와 같은 15분 지연이라 시각이 맞는다.
    [시각, 시, 고, 저, 종, 콜 거래량, 풋 거래량]"""
    r = requests.get(f"https://cdn.cboe.com/api/global/delayed_quotes/charts/intraday/{sym}.json",
                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=30)
    r.raise_for_status()
    out = []
    for b in r.json().get("data") or []:
        p, v = b.get("price") or {}, b.get("volume") or {}
        out.append([b["datetime"][:16], round(p.get("open", 0), 2), round(p.get("high", 0), 2), round(p.get("low", 0), 2),
                    round(p.get("close", 0), 2), int(v.get("calls_volume") or 0), int(v.get("puts_volume") or 0)])
    return out


def git(*args, inp=None, env=None):
    return subprocess.run(["git", *args], input=inp, capture_output=True, text=True, check=True,
                          env=env or os.environ).stdout.strip()


class Live:
    def __init__(self, fetch, syms=None):
        self.fetch = fetch
        self.syms = syms or SYMS                      # 이번 잡이 갱신할 종목 (야간 잡은 SPX만)
        self.snaps = {s: deque() for s in SYMS}   # (시세 시각, option·volume·quote_et)
        self.last_q = {}
        self.docs = {}
        self.series = {s: [] for s in SYMS}
        self._restore()

    def _restore(self):
        """앞 잡(am)이 남긴 분 단위 추이를 이어받는다."""
        try:
            git("fetch", "-q", "--depth", "1", "origin", BRANCH)
        except Exception:
            return
        for s in SYMS:
            name = s.lstrip("_")
            try:
                d = json.loads(git("show", f"FETCH_HEAD:{name}.json"))
                self.docs[name] = d
                self.series[s] = d.get("series", [])
            except Exception:
                pass

    def tick(self):
        changed = False
        for s in self.syms:
            name = s.lstrip("_")
            try:
                df, ts = self.fetch(s)
                qt = gex.quote_time(df)
                if self.last_q.get(s) == qt:      # CBOE 파일이 아직 안 바뀜
                    continue
                self.last_q[s] = qt
                _, day = session_of(qt, name in gex.GTH)      # 야간 거래는 다음 거래일로
                snaps = self.snaps[s]
                if snaps and snaps[-1][0].date() != day:
                    snaps.clear()
                # 5분 이상 지난 스냅샷 중 가장 최근 것만 남긴다 → snaps[0]이 유입 계산 기준
                while len(snaps) > 1 and qt - snaps[1][0] >= FLOW_WIN:
                    snaps.popleft()
                prev = snaps[0][1] if snaps else None
                snaps.append((qt, df[["option", "volume", "quote_et"]].copy()))
                m = gex.metrics(df, day, prev, name, "live")
                t = m.get("total") or {}
                ser = [x for x in self.series[s] if x.get("date") == str(day) and x.get("t") != m["quote_et"]]
                ser.append({"date": str(day), "t": m["quote_et"], "spot": m["spot"],
                            "net_m": (t.get("monthly") or {}).get("net"), "net_all": (t.get("all") or {}).get("net"),
                            "flip_m": (m.get("flip") or {}).get("monthly"), "vol": m.get("vol_total")})
                self.series[s] = ser[-SERIES_MAX:]
                opening = (self.docs.get(name) or {}).get("open")
                if not opening or opening.get("date") != str(day):   # 그날 첫 계산 = 비교 기준(점)
                    opening = {"date": str(day), "t": m["quote_et"], "strikes": m.get("strikes")}
                try:
                    bars = chart_bars(s)
                except Exception as e:
                    print(f"[live] {name} 1분봉 실패: {e}", file=sys.stderr, flush=True)
                    bars = (self.docs.get(name) or {}).get("bars") or []
                self.docs[name] = {"symbol": name, "gex": name not in gex.NO_GEX, "open": opening, "bars": bars,
                                   "updated_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                                   "cboe_ts_et": ts.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S"),
                                   "live": m, "series": self.series[s]}
                changed = True
                print(f"[live] {name} 시세 {m['quote_et']} ET spot {m['spot']} "
                      f"월물 순GEX {(t.get('monthly') or {}).get('net')}M 유입창 {m.get('flow_min')}분", flush=True)
            except Exception as e:
                print(f"[live FAIL] {name}: {e}", file=sys.stderr, flush=True)
        if changed:
            self.push()

    def push(self):
        """docs 를 부모 없는 커밋 하나로 만들어 live 브랜치에 덮어쓴다."""
        try:
            env = dict(os.environ, GIT_INDEX_FILE=os.path.join(git("rev-parse", "--git-dir"), "live.idx"))
            git("read-tree", "--empty", env=env)
            for name, doc in self.docs.items():
                blob = git("hash-object", "-w", "--stdin", inp=json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
                git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}.json", env=env)
            tree = git("write-tree", env=env)
            commit = git("commit-tree", tree, "-m", f"live {datetime.now(ET):%Y-%m-%d %H:%M} ET")
            git("push", "-q", "-f", "origin", f"{commit}:refs/heads/{BRANCH}")
        except subprocess.CalledProcessError as e:
            print(f"[live push FAIL] {e.stderr.strip()[:300]}", file=sys.stderr, flush=True)

    def run_until(self, end):
        """end(ET)까지 EVERY초마다 갱신."""
        while datetime.now(ET) < end:
            t0 = time.time()
            self.tick()
            rest = min(EVERY - (time.time() - t0), (end - datetime.now(ET)).total_seconds())
            if rest > 0:
                time.sleep(rest)
