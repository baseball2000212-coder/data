"""
TSLA 체결 흐름(flow) 분석: 틱(45~50초)마다 늘어난 계약별 거래량(live.py가 남긴 data/TSLA/<날짜>/TSLA_<날짜>_flow.csv.gz)으로
어떤 계약에 갑자기 거래가 몰렸고 그 직후 TSLA가 어떻게 움직였는지 정리한다.
  python flow_study.py               → 가장 최근 날짜
  python flow_study.py 2026-11-04    → 그날
결과: docs/flow/TSLA/<날짜>.json, docs/flow/TSLA/index.json (docs/flow.html이 읽는다). pm 잡이 장 마감 후 자동으로 돌린다.

정의
  premium      = dvol × 체결가 × 100 (달러)
  side         = B(매도호가 근처 체결 = 고객 매수로 봄) · S(매수호가 근처 = 고객 매도) · M(사이) · ?(체결 시각이 그대로거나 호가 없음)
  sdelta       = 고객 기준 주식 환산 델타 = +dvol×delta×100 (B), −dvol×delta×100 (S), 0 (M·?)
                 풋 델타는 음수라 풋 매수는 음(−), 콜 매수는 양(+). 딜러 헤지 = −sdelta (딜러가 사거나 팔아야 할 주식 수)
  x_med        = 그 틱 dvol ÷ 그 계약의 그날 틱당 dvol 중앙값 (얼마나 평소보다 큰지)
  pct          = 그날 전체 행 중 |sdelta| 백분위
  r1·r5·r15    = 그 틱 현재가 대비 1·5·15분 뒤(그 시각 이후 첫 틱) 현재가 수익률(%)
직전 틱과 180초 넘게 벌어진 행(gap_s > 180, 수집이 끊겼던 구간)은 사건·통계에서 빼고 누적 흐름에는 넣는다.
"""
import io, os, re, sys, glob, json, argparse, subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from us_calendar import is_monthly

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
SYM = "TSLA"
MAX_GAP = 180          # 초
N_EVENTS = 40
HORIZONS = (1, 5, 15)  # 분
FWD_TOL = timedelta(minutes=3)   # h분 뒤 시각 이후 첫 틱이 이보다 더 늦으면(수집 공백·장 마감) 수익률을 비운다
BIG_Q = 0.9            # 통계의 '큰 체결' = B·S 행 중 |sdelta| 상위 10%


def path_of(day, data_dir="data", sym=SYM):
    return f"{data_dir}/{sym}/{day}/{sym}_{day}_flow.csv.gz"


def _git_bytes(rel):
    for ref in ("HEAD", "origin/main"):
        try:
            return subprocess.run(["git", "show", f"{ref}:{rel}"], capture_output=True, check=True, timeout=120).stdout
        except Exception:
            continue
    return None


def latest_day(data_dir="data", sym=SYM):
    days = [re.search(r"(\d{4}-\d{2}-\d{2})_flow", f).group(1) for f in glob.glob(f"{data_dir}/{sym}/*/{sym}_*_flow.csv.gz")]
    if not days:
        try:
            out = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD", f"{data_dir}/{sym}/"],
                                 capture_output=True, text=True, check=True, timeout=120).stdout
            days = re.findall(r"(\d{4}-\d{2}-\d{2})_flow\.csv\.gz", out)
        except Exception:
            pass
    return max(days) if days else None


def load(day, data_dir="data", sym=SYM):
    p = path_of(day, data_dir, sym)
    src = p if os.path.exists(p) else _git_bytes(p)
    if src is None:
        return None
    if isinstance(src, bytes):
        src = io.BytesIO(src)
    return pd.read_csv(src, compression="gzip", dtype={"quote_et": str, "option": str, "expiry": str, "cp": str,
                                                     "side": str, "last_trade_time": str})


def r(x, nd=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(x) else (int(round(x)) if nd == 0 else round(x, nd))


def dte_bucket(d):
    return "0DTE" if d <= 0 else "1-7d" if d <= 7 else "8d+"


def exp_badge(exp, day):
    try:
        e = datetime.strptime(exp, "%Y-%m-%d").date()
    except Exception:
        return ""
    return "0dte" if str(e) == day else "monthly" if is_monthly(e) else ""


def prepare(df):
    df = df.copy()
    for k in ("dvol", "last_trade_price", "bid", "ask", "delta", "gamma", "iv", "spot", "strike", "gap_s", "dte"):
        df[k] = pd.to_numeric(df[k], errors="coerce")
    df["side"] = df["side"].fillna("?")
    df["t"] = pd.to_datetime(df["quote_et"], errors="coerce")
    df = df[df["t"].notna() & (df["dvol"] > 0)].sort_values(["t", "option"]).reset_index(drop=True)
    sign = df["side"].map({"B": 1, "S": -1}).fillna(0)
    df["premium"] = (df["dvol"] * df["last_trade_price"].fillna(0) * 100)
    df["sdelta"] = sign * df["dvol"] * df["delta"].fillna(0) * 100
    df["ok"] = df["gap_s"].fillna(0) <= MAX_GAP
    return df


def tick_returns(df):
    """틱별 현재가와 1·5·15분 뒤 수익률(%)."""
    ticks = df.groupby("t", sort=True)["spot"].last().dropna()
    tt = ticks.index.values.astype("datetime64[ns]").astype("int64")
    sp = ticks.values
    out = pd.DataFrame({"spot": sp}, index=ticks.index)
    for h in HORIZONS:
        want = tt + int(h * 60e9)
        j = np.searchsorted(tt, want, side="left")
        ok = j < len(tt)
        jj = np.where(ok, j, 0)
        ok &= (tt[jj] - want) <= int(FWD_TOL.total_seconds() * 1e9)
        out[f"r{h}"] = np.where(ok, (sp[jj] / sp - 1) * 100, np.nan)
    return out


def analyze(df, day):
    df = prepare(df)
    if df.empty:
        return None
    tr = tick_returns(df)
    df = df.join(tr[[f"r{h}" for h in HORIZONS]], on="t")
    df["x_med"] = df["dvol"] / df.groupby("option")["dvol"].transform("median")
    ok = df[df["ok"]]
    df["pct"] = (df["sdelta"].abs().where(df["ok"]).rank(pct=True) * 100)

    # 사건: |sdelta| 순위와 premium 순위 중 더 높은 쪽 기준 상위 N
    rk = pd.concat([ok["sdelta"].abs().rank(ascending=False, method="first"),
                    ok["premium"].rank(ascending=False, method="first")], axis=1).min(axis=1)
    ev = df.loc[rk.nsmallest(N_EVENTS).index].sort_values(["t", "option"])
    events = [{"t": x.t.strftime("%H:%M:%S"), "option": x.option, "expiry": x.expiry, "badge": exp_badge(x.expiry, day),
               "cp": x.cp, "strike": r(x.strike, 2), "dte": r(x.dte, 0), "dvol": r(x.dvol, 0), "side": x.side,
               "last": r(x.last_trade_price, 2), "bid": r(x.bid, 2), "ask": r(x.ask, 2), "iv": r(x.iv, 4),
               "delta": r(x.delta, 3), "premium": r(x.premium, 0), "sdelta": r(x.sdelta, 0), "dealer": r(-x.sdelta, 0),
               "x_med": r(x.x_med, 1), "pct": r(x.pct, 1), "spot": r(x.spot, 2),
               **{f"r{h}": r(getattr(x, f"r{h}"), 3) for h in HORIZONS}}
              for x in ev.itertuples()]

    # 분 단위 추이: 현재가, 고객 순 델타 흐름(누적)
    m = df.assign(minute=df["t"].dt.floor("min")).groupby("minute").agg(
        spot=("spot", "last"), sdelta=("sdelta", "sum"), dvol=("dvol", "sum"))
    m["cum"] = m["sdelta"].cumsum()
    series = [[k.strftime("%H:%M"), r(v.spot, 2), r(v.cum, 0), r(v.sdelta, 0), r(v.dvol, 0)] for k, v in m.iterrows()]

    # 만기(0DTE·1~7일·8일+) × 콜풋 × 매수매도별 평균 수익률
    bs = ok[ok["side"].isin(["B", "S"])].copy()
    bs["db"] = bs["dte"].fillna(-1).map(dte_bucket)
    thr = bs["sdelta"].abs().quantile(BIG_Q) if len(bs) else np.inf

    def stat(g):
        o = {"n": int(len(g))}
        for h in HORIZONS:
            v = g[f"r{h}"].dropna()
            o[f"r{h}"] = r(v.mean(), 3) if len(v) else None
            o[f"n{h}"] = int(len(v))
        return o

    buckets = []
    for db in ("0DTE", "1-7d", "8d+"):
        for c in ("C", "P"):
            for s in ("B", "S"):
                g = bs[(bs["db"] == db) & (bs["cp"] == c) & (bs["side"] == s)]
                buckets.append({"dte": db, "cp": c, "side": s, "all": stat(g), "big": stat(g[g["sdelta"].abs() >= thr])})

    sides = df["side"].value_counts().to_dict()
    ticks = tr.index
    return {"symbol": SYM, "date": day, "generated_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {"rows": int(len(df)), "ticks": int(len(ticks)), "first": ticks[0].strftime("%H:%M:%S"),
                        "last": ticks[-1].strftime("%H:%M:%S"), "dvol": int(df["dvol"].sum()),
                        "premium": r(df["premium"].sum(), 0), "net_sdelta": r(df["sdelta"].sum(), 0),
                        "spot_first": r(tr["spot"].iloc[0], 2), "spot_last": r(tr["spot"].iloc[-1], 2),
                        "sides": {k: int(v) for k, v in sides.items()}, "gap_rows": int((~df["ok"]).sum()),
                        "big_sdelta": r(thr, 0)},
            "events": events, "series": series, "buckets": buckets}


def run(day=None, data_dir="data", out_dir="docs/flow"):
    day = day or latest_day(data_dir)
    if not day:
        print("[flow_study] flow 파일 없음", flush=True)
        return None
    df = load(day, data_dir)
    if df is None or df.empty:
        print(f"[flow_study] {day}: flow 파일 없음/비어 있음", flush=True)
        return None
    doc = analyze(df, day)
    if doc is None:
        print(f"[flow_study] {day}: 쓸 행 없음", flush=True)
        return None
    d = os.path.join(out_dir, SYM)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{day}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    idx_path = os.path.join(d, "index.json")
    try:
        idx = json.load(open(idx_path, encoding="utf-8"))
    except Exception:
        idx = {}
    dates = sorted(set(idx.get("dates", [])) | {day})
    with open(idx_path, "w", encoding="utf-8") as fh:
        json.dump({"symbol": SYM, "dates": dates}, fh, ensure_ascii=False, separators=(",", ":"))
    s = doc["summary"]
    print(f"[flow_study] {day}: {s['rows']}행 {s['ticks']}틱 사건 {len(doc['events'])}개 → {path}", flush=True)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?", help="YYYY-MM-DD (기본: 가장 최근)")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="docs/flow")
    a = ap.parse_args()
    sys.exit(0 if run(a.day, a.data, a.out) else 1)
