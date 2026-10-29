"""
옵션 체인 스냅샷 → 딜러 감마 노출(GEX)·거래량 상위 → docs/gex/<종목>.json (대시보드 docs/index.html 이 읽는다).
  python gex.py                 → 오늘(ET) 스냅샷
  python gex.py 2026-10-28      → 그 날짜
  python gex.py --all           → data/ 전체로 history 다시 계산 (1회성, 전체 체크아웃 필요)

GEX(현재가 1% 변동당 달러) = 감마 × 미결제약정 × 100 × 현재가² × 0.01
부호는 통상 가정(딜러가 콜은 매수·풋은 매도한 상태)대로 콜 +, 풋 −. 개별 종목은 개인 콜 매수가 많아
딜러가 콜을 판 상태일 수 있어 부호가 반대일 수 있다. 그래서 콜·풋을 따로 보여준다.
미결제약정(OI)은 전일 장 마감 기준이고 장중에 바뀌지 않는다(OCC가 밤사이 갱신). 장중 GEX 변화는
포지션 변화가 아니라 가격·변동성 변화 때문이고, 오늘 새로 생긴 포지션은 회차별 거래량 증가분으로 본다.
만기 구분: monthly = 셋째 금요일 월물(휴장이면 앞당김. SPX는 AM 정산 월물 루트 `SPX`, 위클리는 `SPXW`),
0dte = 오늘 만기 비월물, weekly = 나머지.
대시보드는 DASH 종목(SPX·TSLA)만 만든다. 다른 종목은 원자료만 쌓는다.
"""
import os, re, sys, glob, json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo
from us_calendar import is_monthly, is_trading_day, is_fresh, session_of

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
R = 0.04                         # 무위험이자율 근사 (감마는 금리에 거의 둔감)
BAND = 0.12                      # 대시보드에 그리는 행사가: 현재가 ±12%
GRID = np.linspace(0.88, 1.12, 97)   # 가격 시나리오(감마 프로파일)
NO_GEX = {"VIX"}                 # VIX 옵션은 VIX 선물이 기초자산이라 현물 감마 계산이 맞지 않음
DASH = ["SPX", "TSLA"]           # 대시보드 종목
AM_ROOTS = {"SPX"}               # 셋째 금요일 아침(09:30) 시가로 정산하는 월물 루트
GTH = {"SPX", "VIX"}             # 야간 거래(GTH)가 있는 옵션
HIST_DAYS = 90
FMT = "%Y-%m-%d %H:%M:%S"


def tag_of(path):
    return os.path.basename(path).split("_")[-1].split(".")[0]


def tag_key(tag):
    d = re.sub(r"\D", "", tag)
    return int(d) if d else 9999      # eod 는 맨 뒤


def load(path):
    df = pd.read_csv(path)
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    return df


def cboe_ts(df):
    return datetime.strptime(str(df["cboe_timestamp"].iloc[0])[:19], FMT).replace(tzinfo=timezone.utc)


def quote_time(df):
    """시세가 반영하는 시장 시각. 옛 파일엔 quote_et 열이 없어서 CBOE 시각 − 15분으로 추정."""
    if "quote_et" in df and pd.notna(df["quote_et"].iloc[0]):
        return datetime.strptime(df["quote_et"].iloc[0], FMT).replace(tzinfo=ET)
    return (cboe_ts(df) - timedelta(minutes=15)).astimezone(ET)


def is_stale(df, day):
    got = datetime.strptime(df["collected_et"].iloc[0], FMT).replace(tzinfo=ET)
    return not is_fresh(cboe_ts(df).astimezone(ET), got, day)


def bs_gamma(S, K, T, iv):
    """S: (g,1) 시나리오 가격, K·T·iv: (n,) → (g,n)"""
    sq = iv * np.sqrt(T)
    d1 = (np.log(S / K) + (R + 0.5 * iv ** 2) * T) / sq
    return np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * np.pi) / (S * sq)


def _flip(xs, ys, spot):
    """순GEX 부호가 바뀌는 가격 중 현재가에 가장 가까운 것 (선형 보간)."""
    best = None
    for i in range(len(xs) - 1):
        if ys[i] == 0 or ys[i] * ys[i + 1] < 0:
            x = xs[i] if ys[i] == 0 else xs[i] - ys[i] * (xs[i + 1] - xs[i]) / (ys[i + 1] - ys[i])
            if best is None or abs(x - spot) < abs(best - spot):
                best = x
    return None if best is None else round(float(best), 2)


def _m(x, nd=1):   # 달러 → 백만 달러
    return round(float(x) / 1e6, nd)


def expiry_dt(df):
    """계약별 만기 시각(ET). AM 정산 월물은 그날 09:30, 나머지는 16:00."""
    am = df["root"].isin(AM_ROOTS) if "root" in df else pd.Series(False, index=df.index)
    keys = pd.Series(list(zip(df["expiry"], am)), index=df.index)
    cache = {k: datetime.combine(k[0], dtime(9, 30) if k[1] else dtime(16, 0), ET) for k in set(keys)}
    return keys.map(cache)


DIV = {"SPX": 0.012}   # 배당수익률 근사 (선도가 → 현물 환산용)


def implied_spot(df, qt, day, sym):
    """가장 가까운 월물의 풋콜 패리티로 지수를 역산한다: F = K + (콜 중간가 − 풋 중간가)·e^(rT), S = F·e^(−(r−q)T).
    야간에는 지수가 계산되지 않아 CBOE 현재가가 전날 종가에 멈춰 있으므로 이 값을 현재가로 쓴다.
    정규장 데이터로 검증하면 실제 가격과 0.1% 안쪽으로 맞는다."""
    d = df[(df["expiry"] > day) & df["expiry"].map(is_monthly)]
    if d.empty:
        return None
    e = d["expiry"].min()
    m = d[d["expiry"] == e]
    T = (datetime.combine(e, dtime(16, 0), ET) - qt).total_seconds() / (365 * 86400)
    s0 = float(df["spot"].iloc[0])
    c = m[m["cp"] == "C"].set_index("strike")
    p = m[m["cp"] == "P"].set_index("strike")
    fs = []
    for k in c.index.intersection(p.index):
        if abs(k / s0 - 1) > 0.04:
            continue
        cb, ca, pb, pa = (float(x) for x in (c.at[k, "bid"], c.at[k, "ask"], p.at[k, "bid"], p.at[k, "ask"]))
        if min(cb, ca, pb, pa) <= 0:
            continue
        cm, pm = (cb + ca) / 2, (pb + pa) / 2
        if (ca - cb) / cm > 0.1 or (pa - pb) / pm > 0.1:
            continue
        fs.append(k + (cm - pm) * np.exp(R * T))
    if len(fs) < 3:
        return None
    return float(np.median(fs) * np.exp(-(R - DIV.get(sym, 0.0)) * T))


def metrics(df, day, prev, sym, tag):
    """prev: 직전 스냅샷(option·volume·quote_et 열만 있어도 됨). 있으면 그 사이 늘어난 거래량(flow)을 계산."""
    spot = float(df["spot"].iloc[0])
    qt = quote_time(df)
    df = df.copy()
    session, _ = session_of(qt, sym in GTH)
    spot_close = None
    if session == "야간":
        imp = implied_spot(df, qt, day, sym)
        if imp:
            spot_close, spot = spot, imp
    if sym in AM_ROOTS and "root" in df:
        mon = df["root"].isin(AM_ROOTS)
    else:
        mon = df["expiry"].map(is_monthly)
    df["bucket"] = np.where(mon, "monthly", np.where(df["expiry"] == day, "0dte", "weekly"))
    df["is_call"] = df["cp"] == "C"
    df["exp_dt"] = expiry_dt(df)
    oi = df["open_interest"].fillna(0)
    vol = df["volume"].fillna(0)
    flow_min = None
    if prev is not None:
        pv = df["option"].map(prev.set_index("option")["volume"].fillna(0)).fillna(0)
        df["vh"] = (vol - pv).clip(lower=0)
        flow_min = round((qt - quote_time(prev)).total_seconds() / 60)
        if df["vh"].sum() == 0 or flow_min <= 0:   # 장 마감 뒤: 늘어난 거래가 없으니 누적만 보여준다
            prev, flow_min = None, None
            df["vh"] = np.nan
    else:
        df["vh"] = np.nan
    out = {"tag": tag, "date": str(day), "session": session, "quote_et": qt.strftime("%H:%M"),
           "quote_date_et": str(qt.date()),
           "quote_kst": qt.astimezone(KST).strftime("%m/%d %H:%M"), "spot": round(spot, 2), "flow_min": flow_min}
    if spot_close is not None:
        out["spot_close"] = round(spot_close, 2)
        out["spot_note"] = f"야간: 현재가는 옵션 가격으로 역산한 지수 (정규장 종가 {spot_close:,.2f})"
    elif session != "정규장":
        out["spot_note"] = "현재가는 직전 정규장 종가"

    cv, pvv = vol[df["is_call"]].sum(), vol[~df["is_call"]].sum()
    co, po = oi[df["is_call"]].sum(), oi[~df["is_call"]].sum()
    out["pc_vol"] = round(pvv / cv, 3) if cv else None
    out["pc_oi"] = round(po / co, 3) if co else None
    out["vol_total"] = int(cv + pvv)
    out["vh_total"] = None if prev is None else int(df["vh"].sum())

    # 최근 월물 ATM IV
    fut = df[mon & (df["expiry"] > day)]
    if len(fut):
        e0 = fut["expiry"].min()
        near = fut[fut["expiry"] == e0]
        k0 = near.iloc[(near["strike"] - spot).abs().argsort()[:1]]["strike"].iloc[0]
        iv = near[near["strike"] == k0]["iv"].mean()
        out["atm_iv"] = {"expiry": str(e0), "strike": float(k0), "iv": round(float(iv) * 100, 2)}

    # 거래량: 누적 상위(그날 가장 많이 거래된 계약)와 유입 상위(직전 스냅샷 이후 늘어난 거래량)
    def rows(t):
        return [{"expiry": str(r.expiry), "bucket": r.bucket, "cp": r.cp, "strike": float(r.strike),
                 "vh": None if prev is None else int(r.vh), "vol": int(r.volume or 0), "oi": int(r.open_interest or 0),
                 "iv": round(float(r.iv) * 100, 1) if pd.notna(r.iv) else None,
                 "last": float(r.last_trade_price) if pd.notna(r.last_trade_price) else None} for r in t.itertuples()]
    live_k = df["expiry"] >= day          # 지난 만기(야간에 남아 있는 전날 0DTE 등)는 거래량 표에서 뺀다
    out["top_volume"] = rows(df[(vol > 0) & live_k].nlargest(12, "volume"))
    out["top_flow"] = rows(df[(df["vh"].fillna(0) > 0) & live_k].nlargest(12, "vh")) if prev is not None else []
    key = "vh" if prev is not None else "volume"
    agg = {"vol": ("volume", "sum"), "vh": ("vh", "sum"), "oi": ("open_interest", "sum")}
    ex = df[live_k].groupby(["expiry", "bucket"]).agg(**agg).reset_index()
    ex_c = df[df["is_call"]].groupby("expiry")["volume"].sum()
    ex = ex.nlargest(8, key if key == "vh" else "vol")
    out["top_expiries"] = [{"expiry": str(r.expiry), "bucket": r.bucket, "vol": int(r.vol),
                            "vh": None if prev is None else int(r.vh), "oi": int(r.oi),
                            "call_share": round(float(ex_c.get(r.expiry, 0)) / r.vol, 3) if r.vol else None}
                           for r in ex.itertuples()]
    st = df[live_k].groupby(["strike", "cp"]).agg(**agg).reset_index()
    st = st.nlargest(10, key if key == "vh" else "vol")
    out["top_strikes"] = [{"strike": float(r.strike), "cp": r.cp, "vol": int(r.vol),
                           "vh": None if prev is None else int(r.vh), "oi": int(r.oi)} for r in st.itertuples()]

    if sym in NO_GEX:
        out["gex"] = False
        return out

    # 이미 만기가 지난 계약은 감마 계산에서 뺀다
    df = df[df["exp_dt"] > qt].copy()
    oi = df["open_interest"].fillna(0)
    if df["gamma"].fillna(0).abs().sum() == 0:
        out["gex"] = False
        out["gex_note"] = "개장 전 시세라 그릭스가 없음"
        return out
    out["gex"] = True
    if spot_close is not None:   # CBOE 감마는 멈춰 있는 종가 기준이라 역산한 지수로 다시 계산
        T = df["exp_dt"].map(lambda e: max((e - qt).total_seconds(), 3600) / (365 * 86400)).values
        iv = df["iv"].clip(lower=0.01, upper=5).fillna(0.2).values
        df["gamma"] = bs_gamma(np.array([[spot]]), df["strike"].values, T, iv)[0]
    sign = np.where(df["is_call"], 1.0, -1.0)
    unit = 100 * spot ** 2 * 0.01
    df["gex"] = sign * df["gamma"].fillna(0) * oi * unit
    tot = {}
    for b in ("monthly", "weekly", "0dte", "all"):
        s = df if b == "all" else df[df["bucket"] == b]
        c, p = s.loc[s["is_call"], "gex"].sum(), s.loc[~s["is_call"], "gex"].sum()
        tot[b] = {"call": _m(c), "put": _m(p), "net": _m(c + p)}
    out["total"] = tot

    band = df[(df["strike"] >= spot * (1 - BAND)) & (df["strike"] <= spot * (1 + BAND))]
    out["strikes"] = {}
    for b in ("monthly", "all"):
        s = band if b == "all" else band[band["bucket"] == "monthly"]
        g = s.pivot_table(index="strike", columns="cp", values="gex", aggfunc="sum").fillna(0)
        g = g.reindex(columns=["C", "P"], fill_value=0)
        out["strikes"][b] = {"k": [float(k) for k in g.index], "c": [_m(v, 2) for v in g["C"]], "p": [_m(v, 2) for v in g["P"]]}
    # 콜월·풋월: 월물 행사가별 순GEX가 가장 큰(+)·작은(−) 행사가. 같은 행사가에 콜·풋 미결제가 짝으로 쌓인
    # 박스·컨버전 포지션(SPX 7000·8000 같은 정수 행사가)은 순GEX로 보면 서로 상쇄된다
    net = df[df["bucket"] == "monthly"].groupby("strike")["gex"].sum()
    out["call_wall"] = float(net.idxmax()) if len(net) and net.max() > 0 else None
    out["put_wall"] = float(net.idxmin()) if len(net) and net.min() < 0 else None

    # 감마 프로파일: 가격이 S로 움직이면 순GEX가 어떻게 되나 (BS 감마 재계산, IV·OI 고정)
    v = df[(oi > 0) & (df["iv"] > 0.01) & (df["iv"] < 5)].copy()
    v["T"] = v["exp_dt"].map(lambda e: max((e - qt).total_seconds(), 3600) / (365 * 86400))
    S = (spot * GRID)[:, None]
    prof = {}
    for b in ("monthly", "all"):
        s = v if b == "all" else v[v["bucket"] == "monthly"]
        if not len(s):
            prof[b] = None
            continue
        gm = bs_gamma(S, s["strike"].values, s["T"].values, s["iv"].values)
        w = np.where(s["is_call"].values, 1.0, -1.0) * s["open_interest"].values * 100 * 0.01
        prof[b] = (gm * w).sum(axis=1) * (S[:, 0] ** 2)
    xs = (spot * GRID).round(2)
    out["profile"] = {"s": [float(x) for x in xs],
                      **{b: (None if prof[b] is None else [_m(y) for y in prof[b]]) for b in prof}}
    out["flip"] = {b: (None if prof[b] is None else _flip(xs, prof[b], spot)) for b in prof}
    return out


def day_files(data_dir, sym, day):
    fs = glob.glob(f"{data_dir}/{sym}/{day}/{sym}_{day}_*.csv*")
    return sorted(fs, key=lambda p: tag_key(tag_of(p)))


def symbols(data_dir):
    return [s for s in DASH if os.path.isdir(os.path.join(data_dir, s))]


def day_slots(data_dir, sym, day, verbose=False, strict=True):
    slots, prev = [], None
    for f in day_files(data_dir, sym, day):
        try:
            df = load(f)
            if df.empty or (strict and is_stale(df, day)):
                print(f"[gex] {sym} {day} {tag_of(f)}: 묵은 시세라 제외", flush=True)
                continue
            slots.append(metrics(df, day, prev, sym, tag_of(f)))
            prev = df
        except Exception as e:
            print(f"[gex] {sym} {day} {tag_of(f)} 실패: {e}", file=sys.stderr, flush=True)
    return slots


HIST_COLS = ["date", "tag", "quote_et", "spot", "net_monthly", "call_monthly", "put_monthly", "net_all", "net_0dte",
             "flip_monthly", "flip_all", "call_wall", "put_wall", "pc_vol", "pc_oi", "atm_iv"]


def hist_rows(day, slots):
    rows = []
    for s in slots:
        t = s.get("total") or {}
        rows.append({"date": str(day), "tag": s["tag"], "quote_et": s["quote_et"], "spot": s["spot"],
                     "net_monthly": (t.get("monthly") or {}).get("net"), "call_monthly": (t.get("monthly") or {}).get("call"),
                     "put_monthly": (t.get("monthly") or {}).get("put"), "net_all": (t.get("all") or {}).get("net"),
                     "net_0dte": (t.get("0dte") or {}).get("net"),
                     "flip_monthly": (s.get("flip") or {}).get("monthly"), "flip_all": (s.get("flip") or {}).get("all"),
                     "call_wall": s.get("call_wall"), "put_wall": s.get("put_wall"), "pc_vol": s.get("pc_vol"),
                     "pc_oi": s.get("pc_oi"), "atm_iv": (s.get("atm_iv") or {}).get("iv")})
    return rows


def save_history(out_dir, sym, rows, days):
    p = f"{out_dir}/history/{sym}.csv"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    h = pd.read_csv(p, dtype={"tag": str}) if os.path.exists(p) else pd.DataFrame(columns=HIST_COLS)
    h = h[~h["date"].astype(str).isin({str(d) for d in days})]
    h = pd.concat([h, pd.DataFrame(rows, columns=HIST_COLS)], ignore_index=True)
    h = h.sort_values(["date", "tag"], key=lambda c: c.map(tag_key) if c.name == "tag" else c)
    h.to_csv(p, index=False)
    return h


def daily_history(h):
    """하루 한 줄: 마감 회차(eod 제외 마지막 회차)."""
    h = h.copy()
    h["_k"] = h["tag"].astype(str).map(tag_key)
    h = h[h["_k"] < 9999].sort_values(["date", "_k"]).groupby("date").tail(1).tail(HIST_DAYS)
    return json.loads(h.drop(columns="_k").to_json(orient="records"))


def write_json(out_dir, sym, day, slots, h):
    doc = {"symbol": sym, "date": str(day), "gex": sym not in NO_GEX,
           "updated_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "slots": slots, "history": daily_history(h)}
    with open(f"{out_dir}/{sym}.json", "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))


def write_index(out_dir):
    items = []
    for p in sorted(glob.glob(f"{out_dir}/*.json")):
        if os.path.basename(p) == "index.json":
            continue
        d = json.load(open(p, encoding="utf-8"))
        last = d["slots"][-1] if d["slots"] else {}
        items.append({"symbol": d["symbol"], "date": d["date"], "gex": d["gex"], "quote_et": last.get("quote_et"),
                      "spot": last.get("spot"), "net_monthly": ((last.get("total") or {}).get("monthly") or {}).get("net"),
                      "flip": (last.get("flip") or {}).get("monthly")})
    items = [x for x in items if x["symbol"] in DASH]
    items.sort(key=lambda x: DASH.index(x["symbol"]))
    with open(f"{out_dir}/index.json", "w", encoding="utf-8") as fh:
        json.dump({"updated_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "symbols": items}, fh,
                  ensure_ascii=False, separators=(",", ":"))


def build_day(day, data_dir="data", out_dir="docs/gex", verbose=False, strict=True):
    os.makedirs(out_dir, exist_ok=True)
    for sym in symbols(data_dir):
        slots = day_slots(data_dir, sym, day, verbose, strict)
        if not slots:
            continue
        h = save_history(out_dir, sym, hist_rows(day, slots), [day])
        write_json(out_dir, sym, day, slots, h)
        if verbose:
            s = slots[-1]
            t = (s.get("total") or {}).get("monthly")
            print(f"[gex] {sym} {s['tag']} 시세 {s['quote_et']} ET spot {s['spot']} 월물 순GEX {t and t['net']}M "
                  f"flip {(s.get('flip') or {}).get('monthly')} 콜월 {s.get('call_wall')} 풋월 {s.get('put_wall')} "
                  f"P/C {s.get('pc_vol')} 누적상위 {s['top_volume'][:1]}", flush=True)
    write_index(out_dir)


def build_all(data_dir="data", out_dir="docs/gex"):
    os.makedirs(out_dir, exist_ok=True)
    for sym in symbols(data_dir):
        days = sorted(d for d in os.listdir(f"{data_dir}/{sym}") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
        rows, last = [], None
        for ds in days:
            day = datetime.strptime(ds, "%Y-%m-%d").date()
            if not is_trading_day(day):
                continue
            slots = day_slots(data_dir, sym, day)
            if slots:
                rows += hist_rows(day, slots)
                last = (day, slots)
        print(f"[gex] {sym} history {len(rows)}행", flush=True)
        if os.path.exists(f"{out_dir}/history/{sym}.csv"):
            os.remove(f"{out_dir}/history/{sym}.csv")
        h = save_history(out_dir, sym, rows, [])
        if last:
            write_json(out_dir, sym, last[0], last[1], h)
    write_index(out_dir)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--all":
        build_all()
    else:
        d = datetime.strptime(arg, "%Y-%m-%d").date() if arg else datetime.now(ET).date()
        build_day(d, verbose=True)
