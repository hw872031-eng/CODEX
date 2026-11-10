#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股选股引擎 v3 (2026-11-10)
股票池: 沪深300 + 中证500 共 800 只
命令:   fetch1 / fetch2 [n] / report
输出:   ~/ashare/report_YYYY-MM-DD.json, 并复制到 /mnt/files
口径:   过去260个交易日, 同分数十分桶内「10个交易日内先触及 +3ATR 止盈、
        而非先跌破 -2ATR 止损」的历史比例。样本内统计, 不是盈利保证。
数据源: 东方财富 push2his / push2 / datacenter (主), 新浪 (备用)
注意:   /mnt/files 是 s3fs 挂载, 单文件读约4秒, 数据缓存必须放本地盘。
"""
import sys, os, json, time, glob
import requests
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = os.environ.get("ASHARE_BASE") or os.path.expanduser("~/ashare")
SHARED = "/mnt/files"
KL = os.path.join(BASE, "kl")
os.makedirs(KL, exist_ok=True)

UT = "bd1d9ddb04089700cf9c27f6f7426281"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
     "Referer": "https://quote.eastmoney.com/"}
KURL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
LURL = "https://push2.eastmoney.com/api/qt/clist/get"
DURL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SINA = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

IDX = [("上证指数", "1.000001"), ("深证成指", "0.399001"), ("创业板指", "0.399006"),
       ("沪深300", "1.000300"), ("中证500", "1.000905")]


def log(*a):
    print(*a, flush=True)


def sess_get(url, params, tries=3, timeout=15):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=H, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = "HTTP %s" % r.status_code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:80])
        time.sleep(0.3 * (i + 1))
    raise RuntimeError("request failed %s | %s" % (url, last))


def secid_of(code):
    code = str(code).zfill(6)
    return ("1." if code[0] in "56" else "0.") + code


def kline_em(secid, lmt=330, fqt=1):
    j = sess_get(KURL, {"secid": secid, "ut": UT,
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57",
                        "klt": "101", "fqt": str(fqt), "beg": "0",
                        "end": "20500101", "lmt": str(lmt)})
    d = (j or {}).get("data") or {}
    rows = []
    for r in (d.get("klines") or []):
        p = r.split(",")
        rows.append((p[0], float(p[1]), float(p[2]), float(p[3]),
                     float(p[4]), float(p[5]), float(p[6])))
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "amount"])
    df["code"] = d.get("code") or secid.split(".")[-1]
    df["name"] = d.get("name") or ""
    return df


def kline_sina(code, datalen=330):
    sym = ("sh" if str(code)[0] in "56" else "sz") + str(code)
    r = requests.get(SINA, params={"symbol": sym, "scale": 240, "ma": "no", "datalen": datalen},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    if r.status_code != 200:
        raise RuntimeError("sina HTTP %s" % r.status_code)
    data = r.json()
    if not data:
        raise RuntimeError("sina empty")
    df = pd.DataFrame(data).rename(columns={"day": "date"})
    for c in ("open", "close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["amount"] = 0.0
    df["code"] = str(code)
    df["name"] = ""
    return df.sort_values("date")[["date", "open", "close", "high", "low", "volume", "amount", "code", "name"]]


def fetch_index():
    frames = []
    for nm, sid in IDX:
        df = kline_em(sid, lmt=260)
        df["name"] = nm
        frames.append(df)
    pd.concat(frames, ignore_index=True).to_pickle(os.path.join(BASE, "index.pkl"))
    log("  index ok")


def fetch_boards():
    boards = []
    for pn in range(1, 4):
        j = sess_get(LURL, {"pn": pn, "pz": 200, "po": 1, "np": 1, "ut": UT, "fltt": 2,
                            "invt": 2, "fid": "f3", "fs": "m:90+t:2", "fields": "f12,f14"})
        d = (j.get("data") or {})
        diff = d.get("diff") or []
        if not diff:
            break
        boards += [{"bk": x["f12"], "board": x["f14"]} for x in diff]
        if len(boards) >= (d.get("total") or 0):
            break
    boards = pd.DataFrame(boards).drop_duplicates("bk").to_dict("records")
    pd.DataFrame(boards).to_pickle(os.path.join(BASE, "boards.pkl"))

    def perf(b):
        try:
            c = kline_em("90." + b["bk"], lmt=40)["close"].values
            if len(c) < 22:
                return None
            return {"bk": b["bk"], "board": b["board"],
                    "ret20": c[-1] / c[-21] - 1, "ret5": c[-1] / c[-6] - 1, "last": c[-1]}
        except Exception:
            return None

    out = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(perf, boards):
            if r:
                out.append(r)
    pd.DataFrame(out).to_pickle(os.path.join(BASE, "board_perf.pkl"))
    log("  boards %d, perf %d" % (len(boards), len(out)))

    def cons(b):
        try:
            j = sess_get(LURL, {"pn": 1, "pz": 600, "np": 1, "ut": UT, "fltt": 2, "invt": 2,
                                "fid": "f3", "fs": "b:%s+f:!50" % b["bk"], "fields": "f12,f14"})
            return [{"code": str(x["f12"]).zfill(6), "board": b["board"]}
                    for x in (((j.get("data") or {}).get("diff")) or [])]
        except Exception:
            return []

    rows = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(cons, boards):
            rows += r
    pd.DataFrame(rows).drop_duplicates("code").to_pickle(os.path.join(BASE, "board_cons.pkl"))
    log("  board cons %d" % len(rows))


def constituents():
    rows = []
    for typ, nm in (("1", "沪深300"), ("3", "中证500")):
        j = sess_get(DURL, {"reportName": "RPT_INDEX_TS_COMPONENT",
                            "columns": "SECURITY_CODE,SECURITY_NAME_ABBR",
                            "pageSize": 600, "pageNumber": 1,
                            "filter": '(TYPE="%s")' % typ}, timeout=30)
        for d in ((j.get("result") or {}).get("data") or []):
            rows.append({"code": str(d["SECURITY_CODE"]).zfill(6),
                         "name": d["SECURITY_NAME_ABBR"], "index": nm})
    df = pd.DataFrame(rows).drop_duplicates("code")
    df.to_pickle(os.path.join(BASE, "cons.pkl"))
    return df


def fetch_industry():
    def page(pn):
        j = sess_get(LURL, {"pn": pn, "pz": 100, "np": 1, "ut": UT, "fltt": 2, "invt": 2,
                            "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                            "fields": "f12,f100"})
        d = (j.get("data") or {})
        return int(d.get("total") or 0), (d.get("diff") or [])

    def rows_of(diff):
        return [{"code": str(x["f12"]).zfill(6), "industry": x.get("f100") or ""} for x in diff]

    total, first = page(1)
    rows = rows_of(first)
    n = (total + 99) // 100
    if n > 1:
        with ThreadPoolExecutor(max_workers=12) as ex:
            for _t, diff in ex.map(page, range(2, n + 1)):
                rows += rows_of(diff)
    df = pd.DataFrame(rows).drop_duplicates("code")
    df.to_pickle(os.path.join(BASE, "industry.pkl"))
    return df


def cmd_fetch1():
    t0 = time.time()
    log("fetch1: index / boards / constituents / industry")
    fetch_index()
    # 板块强度改由800只样本股按申万二级行业等权合成, 不再依赖东财板块接口
    cons = constituents()
    log("  cons %d (%s)" % (len(cons), cons["index"].value_counts().to_dict()))
    ind = fetch_industry()
    covered = cons["code"].isin(set(ind.loc[ind["industry"] != "", "code"])).sum()
    log("fetch1 done, industry covered %d/%d, %.0fs" % (covered, len(cons), time.time() - t0))


def cmd_fetch2(limit=300):
    t0 = time.time()
    cons = pd.read_pickle(os.path.join(BASE, "cons.pkl"))
    names = dict(zip(cons["code"], cons["name"]))
    done = {f[:-4] for f in os.listdir(KL) if f.endswith(".pkl")}
    todo = [c for c in cons["code"] if c not in done][:limit]
    log("fetch2: have %d/%d, fetching %d" % (len(done), len(cons), len(todo)))

    def one(code):
        for fn in (kline_sina, lambda c: kline_em(secid_of(c), lmt=330)):
            try:
                df = fn(code)
                if df is None or len(df) < 80:
                    continue
                df["name"] = names.get(code, "")
                df.to_pickle(os.path.join(KL, code + ".pkl"))
                return "ok"
            except Exception:
                continue
        return "fail"

    ok = 0
    bad = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for c, st in zip(todo, ex.map(one, todo)):
            if st == "ok":
                ok += 1
            else:
                bad.append(c)
    got = len(glob.glob(os.path.join(KL, "*.pkl")))
    log("fetch2: +%d ok, %d fail, total %d/%d, %.0fs" % (ok, len(bad), got, len(cons), time.time() - t0))
    if bad:
        log("  fail sample: %s" % bad[:8])


PANEL = os.path.join(BASE, "panel.pkl")


def build_panel():
    files = sorted(glob.glob(os.path.join(KL, "*.pkl")))
    stamp = (len(files), round(max(os.path.getmtime(f) for f in files)) if files else 0)
    if os.path.exists(PANEL):
        try:
            if pd.read_pickle(os.path.join(BASE, "panel_stamp.pkl")) == stamp:
                return pd.read_pickle(PANEL)
        except Exception:
            pass
    frames = []
    for f in files:
        try:
            frames.append(pd.read_pickle(f))
        except Exception:
            continue
    px = pd.concat([d[["date", "open", "close", "high", "low", "volume", "code", "name"]]
                    for d in frames], ignore_index=True)
    px["date"] = pd.to_datetime(px["date"])
    try:
        px.to_pickle(PANEL)
        pd.to_pickle(stamp, os.path.join(BASE, "panel_stamp.pkl"))
    except Exception:
        pass
    return px


W = {"trend": 0.25, "mom20": 0.25, "ret60": 0.10, "excess": 0.15,
     "volr": 0.10, "board": 0.15, "atrp": -0.15}
HOLD = 10
TP_ATR, SL_ATR = 1.5, 1.0        # 主口径: 1.5ATR止盈 / 1.0ATR止损 (盈亏比1.5)
TP2_ATR, SL2_ATR = 3.0, 2.0    # 对照口径: 3ATR / 2ATR (宽目标, 用于敏感性)


def zs(s):
    s = pd.to_numeric(s, errors="coerce")
    sd = s.std(ddof=0)
    if not sd or np.isnan(sd):
        return s * 0
    return (s - s.mean()) / sd


def cmd_report():
    t0 = time.time()
    cons = pd.read_pickle(os.path.join(BASE, "cons.pkl"))
    consmap = dict(zip(cons["code"], cons["name"]))
    ind = pd.read_pickle(os.path.join(BASE, "industry.pkl"))
    indmap = dict(zip(ind["code"], ind["industry"]))
    try:
        bc = pd.read_pickle(os.path.join(BASE, "board_cons.pkl"))
        for c, b in zip(bc["code"], bc["board"]):
            if not indmap.get(c):
                indmap[c] = b
    except Exception:
        pass

    px = build_panel()
    close = px.pivot(index="date", columns="code", values="close").sort_index()
    high = px.pivot(index="date", columns="code", values="high").sort_index()
    low = px.pivot(index="date", columns="code", values="low").sort_index()
    vol = px.pivot(index="date", columns="code", values="volume").sort_index()
    cols = sorted(close.columns)
    close, high, low, vol = close[cols], high[cols], low[cols], vol[cols]
    codes = list(close.columns)
    lim = int(os.environ.get("UNIV_LIMIT", "0") or 0)
    if lim:
        codes = codes[:lim]
        close, high, low, vol = close[codes], high[codes], low[codes], vol[codes]
    log("report: %d codes, %d dates" % (len(codes), len(close)))

    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ret20 = close / close.shift(20) - 1
    ret60 = close / close.shift(60) - 1
    mom20 = close.shift(1) / close.shift(21) - 1
    prev = close.shift(1)
    tr = pd.concat([(high - low).stack(), (high - prev).abs().stack(),
                    (low - prev).abs().stack()], axis=1).max(axis=1).unstack()
    atrp = tr.rolling(14).mean() / close
    volr = (vol.rolling(5).mean() / vol.rolling(20).mean()).clip(0, 3)
    trend = close / ma60 - 1
    above60 = close >= ma60

    idxall = pd.read_pickle(os.path.join(BASE, "index.pkl"))
    hsd = idxall[idxall["name"] == "沪深300"].sort_values("date")
    hs = pd.Series(hsd["close"].values, index=pd.to_datetime(hsd["date"].values))
    hs_ret20 = (hs / hs.shift(20) - 1).reindex(close.index).ffill()

    names = pd.Series({c: consmap.get(c, "") for c in codes})
    is_st = names.str.contains("ST", na=False)
    sec_of = pd.Series({c: (indmap.get(c) or "未分类") for c in codes})
    grp = ret20.T.groupby(sec_of).mean()               # 行业等权20日涨幅 (行业 x 日期)
    board_hist = grp.T.reindex(close.index)
    board_mat = board_hist.loc[:, [sec_of[c] for c in codes]]
    board_ret = pd.Series(board_mat.iloc[-1].values, index=codes)
    bt = grp.iloc[:, -1].sort_values(ascending=False)
    brank = {k: i + 1 for i, k in enumerate(bt.index)}

    factor = {"trend": trend, "mom20": mom20, "ret60": ret60,
              "excess": ret20.sub(hs_ret20, axis=0), "volr": volr,
              "board": board_mat, "atrp": atrp}

    cm, hm, lm = close.values, high.values, low.values
    T = cm.shape[0]
    dts = list(close.index)
    volv = vol.values

    def eligible(i):
        ok = ~np.isnan(cm[i]) & (np.nan_to_num(volv[i]) > 0) & (cm[i] >= 2.0)
        ok &= np.sum(~np.isnan(cm[max(0, i - 60):i + 1]), axis=0) >= 60
        ok &= ~is_st.values
        return ok

    def score_at(i):
        tot = np.zeros(len(codes))
        wsum = 0.0
        for k, w in W.items():
            tot += w * zs(factor[k].iloc[i]).fillna(0).values
            wsum += abs(w)
        return tot / wsum

    def levels(i, tp_atr=TP_ATR, sl_atr=SL_ATR):
        a = atrp.iloc[i].values
        a = np.where(np.isnan(a), np.nanmedian(a) if not np.all(np.isnan(a)) else 0.03, a)
        return np.clip(tp_atr * a, 0.03, 0.20), np.clip(sl_atr * a, 0.02, 0.13)

    def outcome(i, tp, sl):
        entry = cm[i]
        tgt = entry * (1 + tp)
        stp = entry * (1 - sl)
        alive = ~np.isnan(entry)
        won = np.zeros(len(entry), bool)
        lost = np.zeros(len(entry), bool)
        for k in range(1, HOLD + 1):
            if i + k >= T:
                break
            hh, ll = hm[i + k], lm[i + k]
            ht = alive & (hh >= tgt)
            hs_ = alive & (ll <= stp)
            both = ht & hs_
            won |= ht & ~both
            lost |= hs_ & ~both
            alive &= ~(ht | hs_)
            if not alive.any():
                break
        return won, lost

    last = T - 1
    start = max(60, last - 260)
    wins, cnt, up, wins2 = np.zeros(10), np.zeros(10), np.zeros(10), np.zeros(10)
    for i in range(start, last - HOLD + 1):
        ok = eligible(i)
        if ok.sum() < 100:
            continue
        sc = score_at(i)
        idx = np.where(ok)[0]
        q = pd.qcut(pd.Series(sc[idx]).rank(method="first"), 10, labels=False).values
        tp, sl = levels(i)
        w, _ = outcome(i, tp, sl)
        tp2, sl2 = levels(i, TP2_ATR, SL2_ATR)
        w2, _ = outcome(i, tp2, sl2)
        f10 = np.zeros(len(codes), bool)
        if i + HOLD < T:
            with np.errstate(divide="ignore", invalid="ignore"):
                rr = cm[i + HOLD] / cm[i] - 1
            f10 = np.nan_to_num(rr, nan=-9.0) > 0.0015
        for b in range(10):
            j = idx[q == b]
            cnt[b] += len(j)
            wins[b] += w[j].sum()
            wins2[b] += w2[j].sum()
            up[b] += f10[j].sum()
    p_bucket = [wins[b] / cnt[b] if cnt[b] else np.nan for b in range(10)]
    p_up = [up[b] / cnt[b] if cnt[b] else np.nan for b in range(10)]
    base = wins.sum() / max(1, cnt.sum())
    base2 = wins2.sum() / max(1, cnt.sum())
    breakeven = SL_ATR / (TP_ATR + SL_ATR)
    log("  calibration %d days, base %.1f%%" % (last - start, base * 100))

    ok = eligible(last)
    live = pd.DataFrame({"code": codes, "score": score_at(last), "price": cm[last],
                         "atrp": atrp.iloc[last].values, "ret20": ret20.iloc[last].values,
                         "trend": trend.iloc[last].values, "board_ret20": board_ret.values,
                         "ok": ok}).set_index("code")
    tp, sl = levels(last)
    live["tp"], live["sl"] = tp, sl
    live = live[live["ok"]].dropna(subset=["score"])
    live["bucket"] = pd.qcut(live["score"].rank(method="first"), 10, labels=False)
    live["p_tgt"] = live["bucket"].map(lambda b: p_bucket[int(b)])
    live["p_up"] = live["bucket"].map(lambda b: p_up[int(b)])
    live["name"] = [consmap.get(c, "") for c in live.index]
    live["board"] = [indmap.get(c, "") or "未分类" for c in live.index]
    live["board_rank"] = [brank.get(b, np.nan) for b in live["board"]]

    f = {"board_collapse": int((live["board_ret20"] < -0.05).sum()),
         "board_hot": int((live["board_ret20"] > 0.40).sum()),
         "board_missing": int(live["board_ret20"].isna().sum()),
         "ret20_negative": int((live["ret20"] < 0).sum()),
         "ret20_hot_over_40pct": int((live["ret20"] > 0.40).sum()),
         "atr_over_9pct": int((live["atrp"] > 0.09).sum()),
         "below_ma60": int((live["trend"] < 0).sum())}
    pool = live[(live["board_ret20"] >= -0.05) & (live["board_ret20"] <= 0.40)
                & (live["ret20"] >= 0) & (live["ret20"] <= 0.40)
                & (live["atrp"] <= 0.09) & (live["trend"] >= 0)]
    pool = pool.dropna(subset=["p_tgt"]).sort_values("score", ascending=False)
    f["pool_after_filters"] = int(len(pool))

    picks, per_board = [], {}
    for code, r in pool.iterrows():
        b = r["board"]
        if per_board.get(b, 0) >= 2:
            continue
        per_board[b] = per_board.get(b, 0) + 1
        picks.append((code, r))
        if len(picks) >= 5:
            break

    idxm = {}
    for nm, _ in IDX:
        d = idxall[idxall["name"] == nm].sort_values("date")
        c = d["close"].values
        if len(c) < 65:
            continue
        s = pd.Series(c)
        idxm[nm] = {"close": round(float(c[-1]), 2),
                    "chg": round(float(c[-1] / c[-2] - 1) * 100, 2),
                    "ret5": round(float(c[-1] / c[-6] - 1) * 100, 2),
                    "ret20": round(float(c[-1] / c[-21] - 1) * 100, 2),
                    "dist_ma20": round(float(c[-1] / s.rolling(20).mean().iloc[-1] - 1) * 100, 2),
                    "dist_ma60": round(float(c[-1] / s.rolling(60).mean().iloc[-1] - 1) * 100, 2)}
    order = [n for n, _ in IDX if n in idxm]
    a20 = float(np.mean([idxm[n]["ret20"] for n in order])) / 100
    a5 = float(np.mean([idxm[n]["ret5"] for n in order])) / 100
    a60 = float(np.mean([idxm[n]["dist_ma60"] for n in order])) / 100
    verdict = ("上行" if a20 >= 0.05 else "偏强" if a20 >= 0.01 else "震荡" if a20 >= -0.03
               else "偏弱" if a20 >= -0.08 else "下行")
    phase = ("趋势上行" if a20 > 0.02 and a5 > 0.01 else
             "超跌反弹" if a20 < -0.02 and a5 > 0.02 else
             "高位回落" if a60 > 0.02 and a5 < -0.02 else
             "加速下行" if a20 < -0.05 and a5 < -0.01 else "横向整理")

    chg = pd.Series(cm[last] / cm[last - 1] - 1)
    chg = chg[~chg.isna()]
    ab = pd.Series(above60.iloc[last].values, index=codes)
    ab = ab[~pd.isna(cm[last])]
    breadth = {"adv": int((chg > 0).sum()), "dec": int((chg < 0).sum()),
               "flat": int((chg == 0).sum()), "n": int(len(chg)),
               "med_chg": round(float(chg.median()) * 100, 2),
               "pct_above_ma60": round(float(ab.mean()) * 100, 1),
               "n_above_ma60": int(ab.sum()),
               "med_ret20": round(float(pd.Series(ret20.iloc[last].values).median()) * 100, 2)}

    top = [{"board": k, "ret20": round(float(v) * 100, 1), "n": int((sec_of == k).sum())}
           for k, v in bt.head(8).items()]
    bot = [{"board": k, "ret20": round(float(v) * 100, 1), "n": int((sec_of == k).sum())}
           for k, v in bt.tail(8).items()]

    pick_list = []
    for code, r in picks:
        pick_list.append({"code": str(code), "name": r["name"], "board": r["board"],
                          "price": round(float(r["price"]), 2),
                          "p_tgt": round(float(r["p_tgt"]) * 100, 1),
                          "p_up": None if pd.isna(r["p_up"]) else round(float(r["p_up"]) * 100, 1),
                          "stop": round(float(r["price"] * (1 - r["sl"])), 2),
                          "target": round(float(r["price"] * (1 + r["tp"])), 2),
                          "stop_pct": round(-float(r["sl"]) * 100, 1),
                          "target_pct": round(float(r["tp"]) * 100, 1),
                          "rr": round(float(r["tp"] / r["sl"]), 2),
                          "board_ret20": None if pd.isna(r["board_ret20"]) else round(float(r["board_ret20"]) * 100, 1),
                          "board_rank": None if pd.isna(r["board_rank"]) else int(r["board_rank"]),
                          "ret20": round(float(r["ret20"]) * 100, 1),
                          "atrp": round(float(r["atrp"]) * 100, 1),
                          "score": round(float(r["score"]), 2)})

    out = {"date": str(pd.Timestamp(dts[last]).date()),
           "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "engine": "v3",
           "universe": {"n": int(len(codes)), "n_eligible": int(ok.sum())},
           "market": {"index": idxm, "verdict": verdict, "phase": phase, "breadth": breadth,
                      "avg_ret20": round(a20 * 100, 2), "avg_ret5": round(a5 * 100, 2),
                      "boards_top": top, "boards_bottom": bot, "n_boards": int(len(bt))},
           "calibration": {"days": int(last - start), "base_rate": round(float(base) * 100, 1),
                           "bucket_p": [None if np.isnan(x) else round(float(x) * 100, 1) for x in p_bucket],
                           "bucket_up": [None if np.isnan(x) else round(float(x) * 100, 1) for x in p_up],
                           "bucket_n": [int(x) for x in cnt],
                           "bucket_p_wide": [None if cnt[b] == 0 else round(float(wins2[b] / cnt[b]) * 100, 1) for b in range(10)],
                           "base_rate_wide": round(float(base2) * 100, 1),
                           "breakeven_pct": round(float(breakeven) * 100, 1),
                           "horizon_days": HOLD, "tp_atr": TP_ATR, "sl_atr": SL_ATR,
                           "wide_tp_atr": TP2_ATR, "wide_sl_atr": SL2_ATR},
           "filters": f, "picks": pick_list,
           "risk_note": "概率是过去260个交易日同分数桶的历史统计值，样本内边缘很薄，不是盈利保证。"}
    fp = os.path.join(BASE, "report_%s.json" % out["date"])
    with open(fp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    try:
        import shutil
        shutil.copy(fp, os.path.join(SHARED, os.path.basename(fp)))
    except Exception:
        pass
    log("report -> %s (%.0fs)" % (fp, time.time() - t0))
    log(json.dumps({"date": out["date"], "verdict": verdict, "phase": phase,
                    "breadth": breadth, "picks": pick_list,
                    "calibration": out["calibration"], "filters": f}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch1":
        cmd_fetch1()
    elif cmd == "fetch2":
        cmd_fetch2(int(sys.argv[2]) if len(sys.argv) > 2 else 300)
    elif cmd == "report":
        cmd_report()
    else:
        log("usage: engine.py fetch1 | fetch2 [n] | report")
