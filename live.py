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
SERIES_MAX = 1500
FRAME_BAND = gex.FRAME_BAND                 # 분 단위 사다리(지난 시점 보기)에 남기는 행사가: 현재가 ±이 비율
GTH_FRAME_GAP = timedelta(minutes=5)        # 야간에는 5분마다 한 장만 남긴다
ARCHIVE = "docs/archive"


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
        self.frames = {s.lstrip("_"): {"date": None, "frames": []} for s in SYMS}
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
            try:
                f = json.loads(git("show", f"FETCH_HEAD:{name}_frames.json"))
                self.frames[name] = {"date": f.get("date"), "frames": f.get("frames", [])}
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
                self.add_frame(name, day, qt, m)
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

    def add_frame(self, name, day, qt, m):
        """그 분의 행사가별 순 GEX(현재가 근처)와 요약을 한 장으로 남긴다 → 화면에서 지난 시점을 고를 수 있다."""
        fr = self.frames[name]
        if fr["date"] != str(day):
            fr.update(date=str(day), frames=[])
        if m.get("gex") is False or not m.get("strikes"):
            return
        if m.get("session") == "야간" and fr["frames"] and fr["frames"][-1].get("sess") == "야간":
            last = datetime.strptime(fr["frames"][-1]["q"], "%Y-%m-%d %H:%M").replace(tzinfo=ET)
            if qt - last < GTH_FRAME_GAP:
                return
        fr["frames"] = [f for f in fr["frames"] if f["q"] != qt.strftime("%Y-%m-%d %H:%M")]
        fr["frames"].append(gex.frame_of(m, qt, FRAME_BAND.get(name, 0.05)))

    def frames_doc(self, name):
        fr = self.frames[name]
        return {"symbol": name, "date": fr["date"], "bars": (self.docs.get(name) or {}).get("bars") or [],
                "frames": fr["frames"]}

    def archive(self):
        """그날 분 단위 기록을 main 의 docs/archive/<종목>/<날짜>.json 으로 남긴다(장 마감 후 pm 잡이 호출)."""
        os.makedirs(ARCHIVE, exist_ok=True)
        idx_path = os.path.join(ARCHIVE, "index.json")
        try:
            idx = json.load(open(idx_path, encoding="utf-8"))
        except Exception:
            idx = {}
        for name, fr in self.frames.items():
            if not fr["date"] or not fr["frames"]:
                continue
            doc = self.frames_doc(name)
            try:   # 매시 스냅샷(거래량 표·프로파일용)도 같이 묶는다
                h = json.load(open(f"docs/gex/{name}.json", encoding="utf-8"))
                if h.get("date") == fr["date"]:
                    doc["slots"] = h.get("slots", [])
            except Exception:
                pass
            os.makedirs(os.path.join(ARCHIVE, name), exist_ok=True)
            path = os.path.join(ARCHIVE, name, f"{fr['date']}.json")
            try:   # 매시판 보관본이 더 촘촘하면(잡이 늦게 떠서 분 단위가 적은 날) 프레임은 그대로 두고 1분봉만 넣는다
                old = json.load(open(path, encoding="utf-8"))
                if not doc["bars"] and old.get("bars"):
                    doc.update({k: old[k] for k in ("bars", "bars_ival", "bars_src") if k in old})
                if len(old.get("frames", [])) > len(fr["frames"]):
                    if doc["bars"] and not old.get("bars"):
                        old["bars"] = doc["bars"]
                        with open(path, "w", encoding="utf-8") as fh:
                            json.dump(old, fh, ensure_ascii=False, separators=(",", ":"))
                    continue
            except Exception:
                pass
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
            idx[name] = sorted(set(idx.get(name, [])) | {fr["date"]})
            print(f"[archive] {name} {fr['date']} 분 단위 {len(fr['frames'])}장", flush=True)
        with open(idx_path, "w", encoding="utf-8") as fh:
            json.dump(idx, fh, ensure_ascii=False, separators=(",", ":"))

    def push(self, retry=2):
        """docs 를 부모 없는 커밋 하나로 만들어 live 브랜치에 덮어쓴다.
        다른 잡(정규장 잡과 야간 잡이 겹치는 시간)이 그 사이 썼으면 거절되므로, 다시 받아 합친 뒤 재시도한다."""
        mine = {s.lstrip("_") for s in self.syms}
        for attempt in range(retry + 1):
            try:
                base = ""
                files = {f"{n}.json": d for n, d in self.docs.items() if n in mine}
                files.update({f"{n}_frames.json": self.frames_doc(n) for n, fr in self.frames.items() if fr["frames"] and n in mine})
                try:   # 이 잡이 맡지 않은 종목 파일은 브랜치에 있는 최신본을 그대로 둔다
                    git("fetch", "-q", "--depth", "1", "origin", BRANCH)
                    base = git("rev-parse", "FETCH_HEAD")
                    for path in git("ls-tree", "--name-only", "FETCH_HEAD").split():
                        if path not in files and path.split("_")[0].split(".")[0] not in mine:
                            files[path] = json.loads(git("show", f"FETCH_HEAD:{path}"))
                except Exception:
                    pass
                env = dict(os.environ, GIT_INDEX_FILE=os.path.join(git("rev-parse", "--git-dir"), "live.idx"))
                git("read-tree", "--empty", env=env)
                for path, doc in files.items():
                    blob = git("hash-object", "-w", "--stdin", inp=json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
                    git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
                tree = git("write-tree", env=env)
                commit = git("commit-tree", tree, "-m", f"live {datetime.now(ET):%Y-%m-%d %H:%M} ET")
                lease = f"--force-with-lease=refs/heads/{BRANCH}:{base}" if base else "--force"
                git("push", "-q", lease, "origin", f"{commit}:refs/heads/{BRANCH}")
                return
            except subprocess.CalledProcessError as e:
                print(f"[live push 재시도 {attempt + 1}] {e.stderr.strip()[:200]}", file=sys.stderr, flush=True)
                time.sleep(2)

    def run_until(self, end):
        """end(ET)까지 EVERY초마다 갱신."""
        while datetime.now(ET) < end:
            t0 = time.time()
            self.tick()
            rest = min(EVERY - (time.time() - t0), (end - datetime.now(ET)).total_seconds())
            if rest > 0:
                time.sleep(rest)
