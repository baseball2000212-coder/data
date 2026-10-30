"""
1분 간격 GEX (시세 자체는 CBOE 무료 시세라 15분 지연). am/pm 잡이 회차 사이 대기 시간에 돌린다.
결과는 이력 없는 브랜치 `live` 에 매번 덮어써서(force push) 저장소가 커지지 않게 하고,
대시보드는 raw.githubusercontent.com 에서 바로 읽는다(GitHub Pages 빌드 제한과 무관).
  live/<종목>.json = {"live": 최신 GEX·거래량, "series": 그날 분 단위 추이}
"""
import io, os, sys, json, time, subprocess
import requests
import pandas as pd
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import gex
from us_calendar import session_of, prev_trading_day

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
SYMS = ["_SPX", "TSLA"]
EVERY = 60                       # 초. CBOE 파일이 1분 남짓마다 새로 만들어져서 이보다 줄여도 소용없다
AFTER = -20                      # 다음 파일은 직전 파일 시각 + EVERY + 이 초(= 40초) 뒤부터 받으러 간다. CBOE 파일은 45~50초 간격으로 나온다
FLOW_WIN = timedelta(minutes=5)  # 유입 거래량 = 최근 5분 동안 늘어난 거래량 (만기별·행사가별 표)
FLOW_WINS = {"1": 1, "30": 30, "60": 60}   # Top Flow 표의 비교 창(분). 화면에서 고른다
KEEP = timedelta(minutes=61)     # 메모리에 남기는 분 단위 스냅샷 길이
HANDOFF_GAP = timedelta(minutes=5)   # am 잡이 끝날 때 다음 잡(pm)에 넘기는 스냅샷 간격(30·60분 창이 잡 교대 직후에도 비지 않게)
BRANCH = "live"
SERIES_MAX = 1500
FRAME_BAND = gex.FRAME_BAND                 # 분 단위 사다리(지난 시점 보기)에 남기는 행사가: 현재가 ±이 비율
GTH_FRAME_GAP = timedelta(minutes=5)        # 야간에는 5분마다 한 장만 남긴다
ARCHIVE = "docs/archive"
LIVE_REF = "refs/remotes/origin/live"   # FETCH_HEAD는 회차 커밋(commit.sh의 pull)과 겹치면 덮어써져서 전용 참조를 쓴다


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
        self.disk = {}                             # (종목, 날짜) → 그날 저장된 매시 스냅샷. 잡이 바뀐 직후 30·60분 창용
        self.dlog = {}                             # 30·60분 창 기준을 어디서 찾았는지 진단 로그(15분에 한 번)
        self.pvol = {}                             # (종목, 날짜) → (전 거래일, 그날 마지막 스냅샷 거래량). 전일 대비 유입용
        self.last_q = {}
        self.due = {}                              # 종목별 다음에 받으러 갈 시각(UTC). CBOE 갱신 주기에 맞춘다
        self.miss = {}                             # 아직 안 바뀐 파일을 연달아 받은 횟수(뒤로 물러나기용)
        self.docs = {}
        self.extra = {}                            # 다음 push에 같이 올릴 파일(잡 교대용 스냅샷)
        self.series = {s: [] for s in SYMS}
        self.frames = {s.lstrip("_"): {"date": None, "frames": []} for s in SYMS}
        self._restore()

    def _restore(self):
        """앞 잡(am)이 남긴 분 단위 추이를 이어받는다."""
        try:
            git("fetch", "-q", "--depth", "1", "origin", f"+{BRANCH}:{LIVE_REF}")
        except Exception:
            return
        for s in SYMS:
            name = s.lstrip("_")
            try:
                d = json.loads(git("show", f"{LIVE_REF}:{name}.json"))
                self.docs[name] = d
                self.series[s] = d.get("series", [])
            except Exception:
                pass
            try:
                f = json.loads(git("show", f"{LIVE_REF}:{name}_frames.json"))
                self.frames[name] = {"date": f.get("date"), "frames": f.get("frames", [])}
            except Exception:
                pass
            if s in self.syms:
                self._restore_snaps(s, name)

    def _restore_snaps(self, s, name):
        """앞 잡이 끝나며 남긴 분 단위 스냅샷(<종목>_snaps.json)을 메모리로 이어받는다. 날짜가 다르면 tick에서 비워진다."""
        try:
            h = json.loads(git("show", f"{LIVE_REF}:{name}_snaps.json"))
            day = datetime.strptime(h["day"], "%Y-%m-%d").date()
            for q, vols in zip(h["q"], h["vol"]):
                qt = datetime.fromisoformat(q)
                df = pd.DataFrame({"option": h["option"], "volume": vols, "quote_et": qt.strftime(gex.FMT)})
                self.snaps[s].append((qt, df, day))
            print(f"[live] {name} 앞 잡 스냅샷 {len(h['q'])}개 이어받음 ({h['q'][0][11:16]}~{h['q'][-1][11:16]} ET)", flush=True)
        except Exception:
            pass

    def save_handoff(self):
        """잡이 끝나기 전에 최근 61분 스냅샷을 5분 간격으로 live 브랜치에 남긴다(다음 잡이 _restore에서 읽는다).
        이 잡 종목 파일이라 다음 잡의 첫 push 때 자연히 지워진다."""
        for s in self.syms:
            snaps, name = self.snaps[s], s.lstrip("_")
            if not snaps:
                continue
            keep, last = [], None
            for q, d, day in reversed(snaps):
                if last is None or last - q >= HANDOFF_GAP - timedelta(seconds=20):
                    keep.append((q, d, day))
                    last = q
            keep.reverse()
            day = keep[-1][2]
            keep = [k for k in keep if k[2] == day]
            cur = keep[-1][1]
            opts = cur.loc[cur["volume"].fillna(0) > 0, "option"].drop_duplicates().tolist()   # 거래 없는 계약은 0으로 보면 된다
            vol = [d.drop_duplicates("option").set_index("option")["volume"].reindex(opts).fillna(0).astype(int).tolist()
                   for _, d, _ in keep]
            self.extra[f"{name}_snaps.json"] = {"day": str(day), "option": opts,
                                                "q": [q.isoformat() for q, _, _ in keep], "vol": vol}
            print(f"[live] {name} 다음 잡에 넘길 스냅샷 {len(keep)}개 ({len(opts)}계약)", flush=True)
        if self.extra:
            self.push()

    def tick(self, syms=None):
        changed = False
        for s in syms or self.syms:
            name = s.lstrip("_")
            try:
                df, ts = self.fetch(s)
                qt = gex.quote_time(df)
                now = datetime.now(timezone.utc)
                if self.last_q.get(s) == qt:      # CBOE 파일이 아직 안 바뀜 → 45초 동안은 3초마다, 그 뒤엔 천천히(최대 60초)
                    k = self.miss[s] = self.miss.get(s, 0) + 1   # CBOE 파일은 45~50초 간격이라 촘촘히 본다
                    self.due[s] = now + timedelta(seconds=3 if k <= 15 else min(15 * (k - 15), EVERY))
                    continue
                self.miss[s] = 0
                nxt = ts + timedelta(seconds=EVERY + AFTER)     # 다음 파일이 나올 즈음
                self.due[s] = nxt if nxt > now else now + timedelta(seconds=5)
                self.last_q[s] = qt
                _, day = session_of(qt, name in gex.GTH)      # 야간 거래는 다음 거래일로
                snaps = self.snaps[s]
                if snaps and snaps[-1][2] != day:
                    snaps.clear()
                while len(snaps) > 1 and qt - snaps[1][0] >= KEEP:
                    snaps.popleft()
                prev = self.before(s, name, day, qt, FLOW_WIN)
                prevs = {k: self.before(s, name, day, qt, timedelta(minutes=w)) for k, w in FLOW_WINS.items()}
                snaps.append((qt, df[["option", "volume", "quote_et"]].copy(), day))
                m = gex.metrics(df, day, prev, name, "live", prevs=prevs, prev_day=self.prev_volume(name, day))
                last = (self.docs.get(name) or {}).get("live") or {}
                if m.get("gex") is False and last.get("gex") and last.get("date") == str(day) and name not in gex.NO_GEX:
                    # 장 시작 첫 시세처럼 CBOE 그릭스가 잠깐 비는 분: 차트가 사라지지 않게 직전 계산값을 이어 쓴다
                    for k in ("total", "strikes", "walls", "call_wall", "put_wall", "profile", "flip"):
                        if k in last:
                            m[k] = last[k]
                    m["gex"] = True
                    m["gex_note"] = f"이번 시세({m['quote_et']} ET)는 그릭스가 비어 있어 GEX는 {last.get('quote_et')} ET 계산값"
                t = m.get("total") or {}
                ser = [x for x in self.series[s] if x.get("date") == str(day) and x.get("t") != m["quote_et"]]
                ser.append({"date": str(day), "t": m["quote_et"], "spot": m["spot"],
                            "net_m": (t.get("monthly") or {}).get("net"), "net_all": (t.get("all") or {}).get("net"),
                            "flip_m": (m.get("flip") or {}).get("monthly"), "flip_all": (m.get("flip") or {}).get("all"),
                            "vol": m.get("vol_total")})
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
                      f"순GEX(오늘 만기 제외) {(t.get('all') or {}).get('net')}M 유입창 {m.get('flow_min')}분", flush=True)
            except Exception as e:
                print(f"[live FAIL] {name}: {e}", file=sys.stderr, flush=True)
                self.due[s] = datetime.now(timezone.utc) + timedelta(seconds=15)
        if changed:
            self.push()

    def before(self, s, name, day, qt, win):
        """시세 시각 qt보다 win 이상 앞선 스냅샷 중 가장 최근 것. 메모리(1분 간격)에 없으면 그날 저장된 매시 스냅샷에서 찾는다."""
        need = win - timedelta(seconds=20)      # CBOE 갱신 간격이 정확히 60초가 아니라서 조금 봐준다
        cap = max(win * 2, win + timedelta(minutes=2))   # 너무 오래된 것(1분 창에 1시간 전 등)은 쓰지 않는다
        for q, d, _ in reversed(self.snaps[s]):
            if qt - q >= need:
                return d if qt - q <= cap else None
        disk = self._disk(name, day)
        for q, d in reversed(disk):
            if qt - q >= need:
                self._diag(name, win, f"매시 스냅샷 {q:%H:%M} 사용" if qt - q <= cap else f"매시 스냅샷 {q:%H:%M}는 너무 오래돼 비움")
                return d if qt - q <= cap else None
        if win >= timedelta(minutes=30):
            mem = f"{self.snaps[s][0][0]:%H:%M}~" if self.snaps[s] else "없음"
            self._diag(name, win, f"기준 없음 (메모리 {mem}, 매시 스냅샷 {[f'{q:%H:%M}' for q, _ in disk]})")
        return None

    def _diag(self, name, win, msg):
        key, now = (name, win), time.time()
        if now - self.dlog.get(key, 0) >= 900:
            self.dlog[key] = now
            print(f"[live] {name} {int(win.total_seconds() // 60)}분 창: {msg}", flush=True)

    def prev_volume(self, name, day):
        """전 거래일 마지막 스냅샷(eod 우선)의 계약별 거래량. 저장소 커밋에서 그 파일 하나만 받는다(부분 체크아웃이라 폴더는 없다)."""
        key = (name, str(day))
        if key not in self.pvol:
            self.pvol[key] = None
            d = day
            for _ in range(4):                     # 그날 파일이 없으면(휴장·수집 실패) 하루씩 더 앞으로
                d = prev_trading_day(d)
                try:
                    fs = git("ls-tree", "--name-only", "HEAD", f"data/{name}/{d}/").split()
                    fs = [f for f in fs if "_test" not in f]
                    if not fs:
                        continue
                    f = max(fs, key=lambda p: gex.tag_key(gex.tag_of(p)))
                    raw = subprocess.run(["git", "show", f"HEAD:{f}"], capture_output=True, check=True).stdout
                    pdf = pd.read_csv(io.BytesIO(raw), compression="gzip" if f.endswith(".gz") else None,
                                      usecols=["option", "volume"])
                    self.pvol[key] = (d, pdf)
                    print(f"[live] {name} 전일 거래량 기준: {f}", flush=True)
                    break
                except Exception as e:
                    print(f"[live] {name} 전일 거래량 실패 {d}: {e}", file=sys.stderr, flush=True)
        return self.pvol[key]

    def _disk(self, name, day):
        """그날 저장된 매시 스냅샷. 잡이 도는 동안 새로 저장된 회차도 들어오게 매번 폴더를 다시 본다(읽은 파일은 기억)."""
        have = self.disk.setdefault((name, str(day)), {})
        for f in gex.day_files("data", name, day):
            if f in have:
                continue
            try:
                d = gex.load(f)
                q = gex.quote_time(d)
                have[f] = (q, pd.DataFrame({"option": d["option"], "volume": d["volume"], "quote_et": q.strftime(gex.FMT)}))
            except Exception:
                pass                               # 쓰는 중인 파일일 수 있다 → 다음에 다시 읽는다
        return sorted(have.values(), key=lambda x: x[0])

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
                files.update(self.extra)
                try:   # 이 잡이 맡지 않은 종목 파일은 브랜치에 있는 최신본을 그대로 둔다
                    git("fetch", "-q", "--depth", "1", "origin", f"+{BRANCH}:{LIVE_REF}")
                    base = git("rev-parse", LIVE_REF)
                    for path in git("ls-tree", "--name-only", LIVE_REF).split():
                        if path not in files and path.split("_")[0].split(".")[0] not in mine:
                            files[path] = json.loads(git("show", f"{LIVE_REF}:{path}"))
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
        """end(ET)까지 갱신. 종목마다 CBOE 파일이 새로 나올 즈음(직전 파일 시각 + 40초)부터 3초마다 받으러 가서
        파일이 나온 뒤 몇 초 안에 계산·게시한다(고정 1분 간격이면 평균 30초, 최대 1분을 더 기다린다)."""
        while datetime.now(ET) < end:
            now = datetime.now(timezone.utc)
            due = [s for s in self.syms if self.due.get(s, now) <= now]
            if due:
                self.tick(due)
            now = datetime.now(timezone.utc)
            nxt = min((self.due.get(s, now) for s in self.syms), default=now + timedelta(seconds=EVERY))
            rest = min((nxt - now).total_seconds(), (end - datetime.now(ET)).total_seconds(), EVERY)
            time.sleep(max(rest, 1))
