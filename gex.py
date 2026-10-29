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
만기 구분: monthly = 셋째 금요일 월물(휴장이면 앞당김), 0dte = 오늘 만기 비월물, weekly = 나머지.
"""
import os, re, sys, glob, json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo
from us_calendar import is_monthly, is_trading_day, is_fresh

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
R = 0.04                         # 무위험이자율 근사 (감마는 금리에 거의 둔감)
BAND = 0.12                      # 대시보드에 그리는 행사가: 현재가 ±12%
GRID = np.linspace(0.88, 1.12, 97)   # 가격 시나리오(감마 프로파일)
NO_GEX = {"VIX"}                 # VIX 옵션은 VIX 선물이 기초자산이라 현물 감마 계산이 맞지 않음
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


def metrics(df, day, prev, sym, tag):
    spot = float(df["spot"].iloc[0])
    qt = quote_time(df)
    df = df.copy()
    mon = df["expiry"].map(is_monthly)
    df["bucket"] = np.where(mon, "monthly", np.where(df["expiry"] == day, "0dte", "weekly"))
    df["is_call"] = df["cp"] == "C"
    oi = df["open_interest"].fillna(0)
    vol = df["volume"].fillna(0)
    if prev is not None:
        pv = df["option"].map(prev.set_index("option")["volume"].fillna(0)).fillna(0)
        df["vh"] = (vol - pv).clip(lower=0)
        if df["vh"].sum() == 0:     # 장 마감 뒤 회차: 늘어난 거래가 없으니 누적 기준으로 보여준다
            prev = None
            df["vh"] = np.nan
    else:
        df["vh"] = np.nan
    out = {"tag": tag, "quote_et": qt.strftime("%H:%M"), "quote_kst": qt.astimezone(KST).strftime("%m/%d %H:%M"),
           "spot": round(spot, 2)}

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

    # 거래량 상위 (이번 회차 증가분이 있으면 그걸로, 첫 회차면 누적으로)
    key = "vh" if prev is not None else "volume"
    top = df[df[key].fillna(0) > 0].nlargest(12, key)
    out["top_contracts"] = [{
        "expiry": str(r.expiry), "bucket": r.bucket, "cp": r.cp, "strike": float(r.strike),
        "vh": None if prev is None else int(r.vh), "vol": int(r.volume or 0), "oi": int(r.open_interest or 0),
        "iv": round(float(r.iv) * 100, 1) if pd.notna(r.iv) else None,
        "last": float(r.last_trade_price) if pd.notna(r.last_trade_price) else None} for r in top.itertuples()]
    agg = {"vol": ("volume", "sum"), "vh": ("vh", "sum"), "oi": ("open_interest", "sum")}
    ex = df.groupby(["expiry", "bucket"]).agg(**agg).reset_index()
    ex_c = df[df["is_call"]].groupby("expiry")["volume"].sum()
    ex = ex.nlargest(8, key if key == "vh" else "vol")
    out["top_expiries"] = [{"expiry": str(r.expiry), "bucket": r.bucket, "vol": int(r.vol),
                            "vh": None if prev is None else int(r.vh), "oi": int(r.oi),
                            "call_share": round(float(ex_c.get(r.expiry, 0)) / r.vol, 3) if r.vol else None}
                           for r in ex.itertuples()]
    st = df.groupby(["strike", "cp"]).agg(**agg).reset_index()
    st = st.nlargest(10, key if key == "vh" else "vol")
    out["top_strikes"] = [{"strike": float(r.strike), "cp": r.cp, "vol": int(r.vol),
                           "vh": None if prev is None else int(r.vh), "oi": int(r.oi)} for r in st.itertuples()]

    if sym in NO_GEX:
        out["gex"] = False
        return out

    # 이미 만기가 지난 계약(오늘 만기 + 16:00 이후 시세)은 감마 계산에서 뺀다
    df = df[(df["expiry"] > day) | ((df["expiry"] == day) & (qt.time() < dtime(16, 0)))].copy()
    oi = df["open_interest"].fillna(0)
    if df["gamma"].fillna(0).abs().sum() == 0:
        out["gex"] = False
        out["gex_note"] = "개장 전 시세라 그릭스가 없음"
        return out
    out["gex"] = True
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
    m = df[df["bucket"] == "monthly"]
    cw = m[m["is_call"]].groupby("strike")["gex"].sum()
    pw = m[~m["is_call"]].groupby("strike")["gex"].sum()
    out["call_wall"] = float(cw.idxmax()) if len(cw) and cw.max() > 0 else None
    out["put_wall"] = float(pw.idxmin()) if len(pw) and pw.min() < 0 else None

    # 감마 프로파일: 가격이 S로 움직이면 순GEX가 어떻게 되나 (BS 감마 재계산, IV·OI 고정)
    v = df[(oi > 0) & (df["iv"] > 0.01) & (df["iv"] < 5)].copy()
    exp_dt = {e: datetime.combine(e, dtime(16, 0), ET) for e in v["expiry"].unique()}
    v["T"] = v["expiry"].map(lambda e: max((exp_dt[e] - qt).total_seconds(), 3600) / (365 * 86400))
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
    return sorted(d for d in os.listdir(data_dir)
                  if os.path.isdir(os.path.join(data_dir, d)) and not d.startswith("_") and d != "docs")


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
    order = ["SPY", "QQQ", "TSLA", "NVDA", "AAPL", "IBIT", "VIX"]
    items.sort(key=lambda x: order.index(x["symbol"]) if x["symbol"] in order else 99)
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
                  f"P/C {s.get('pc_vol')} 상위 {s['top_contracts'][:1]}", flush=True)
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
