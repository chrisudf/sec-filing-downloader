# -*- coding: utf-8 -*-
"""外国发行人（20-F，无季度 XBRL）的 TTM PE 带：Yahoo 季度 EPS 出数，SEC 20-F 年度 EPS 核对。

为什么要这条路：pe_band 的 TTM 需要季度净利，20-F 发行人（TSM）在 companyfacts 里只有
年度、IFRS、本币的数。只用年度数 TTM 一年才跳一次，增速快的票（TSM 一年 +50%）PE 会被
系统性高估；解析 6-K 季报格式不固定。所以季度节奏取 Yahoo 的 earnings dates（每份 ADR、
美元、带公告日），口径由 20-F 年度数把关。

口径规则（不是混用的前提）：
  - 一条带的分子从头到尾只用这一个来源，不和 SEC 季度数拼接；只在「外国发行人形态」
    （ifrs-full / 有年度无季度）时启用——取数失败、新上市票照旧报错，不悄悄换源。
  - 核对：20-F 年度摊薄 EPS（本币 / 普通股）× ADR 换股比 ÷ 该财年平均汇率，与 Yahoo 同一
    财年 4 季之和比，|偏差| ≤ XCHECK_TOL 的年份才进分布；对不上、凑不齐 4 季的年份整年剔。
  - 最新一份 20-F 之后的季度（年报未出）无从核对，前提是最近一个可核对年份通过才放行；
    最近一年就对不上 -> 整组不出（宁缺毋滥）。
  - 防前视：公告日（美东）当天不算已知，次一交易日起才进 TTM（TSM 常在美东凌晨 / 前一晚
    公告，保守一天）。近零剔除、250 天门槛、10y/5y 各自窗口与 pe_band 一致。
  - 营业线 / 一次性项还原不做：Yahoo 没有季度营业利润的长历史，不硬凑。
"""
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 与 pe_rank 同：同目录平级 import
import pe_band as pb  # noqa: E402

XCHECK_TOL = 0.05     # Yahoo 年度 4 季之和 vs 20-F 折算，汇率用年均价，季度汇率差在 1–2% 内
Q_WIN_LAG = 70        # 财年 (end−365+LAG, end+LAG] 内公告的季度归该财年：Q4 最晚 end+70 天公告
Q_GAP = (45, 140)     # 相邻两次公告的间隔（天）：超出 = 中间缺季，这扇 TTM 窗口不算
NICE_RATIOS = (0.1, 0.2, 0.25, 0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 50, 100)
ADR_RATIO = {"TSM": 5}   # 1 ADR = N 股普通股（20-F 封面）；表外的票按年度数推断并标注


def foreign_shape(inputs):
    """ifrs-full taxonomy，或净利有年度、无任何季度（20-F 年报节奏）-> True。纯函数。"""
    if inputs.get("taxonomy") == "ifrs-full":
        return True
    facts = inputs["facts"]
    units = {u for t in pb.NI_TAGS for u in (facts.get(t) or {}).get("units", {})}
    return bool(pb.pick(facts, pb.NI_TAGS, "annual", units)) and \
        not pb.pick(facts, pb.NI_TAGS, "quarterly", units)


def parse_yahoo_quarters(df):
    """get_earnings_dates 的表 -> [{"date": 公告日(美东) ISO, "eps": 每 ADR 美元}]，升序。
    没公布（Reported EPS 为 NaN）的未来行丢掉；同一天重复行保留一条。纯函数。"""
    if df is None or getattr(df, "empty", True) or "Reported EPS" not in df.columns:
        return []
    out = {}
    for ts, v in df["Reported EPS"].items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:
            continue
        d = (ts.tz_convert("America/New_York") if getattr(ts, "tzinfo", None) else ts).date()
        out[d.isoformat()] = f
    return [{"date": d, "eps": v} for d, v in sorted(out.items())]


def annual_eps(facts):
    """20-F 年度摊薄 EPS -> (币种, {财年末: {"val", "filed"}})。本币优先：TSM 同时申报一条
    USD/shares，那是按年末汇率的便利折算，不是交易口径。没有 -> (None, {})。纯函数。"""
    units = {u for t in pb.EPS_TAGS for u in (facts.get(t) or {}).get("units", {})
             if u.endswith("/shares")}
    for u in sorted(units, key=lambda u: u == "USD/shares"):
        rows = pb.pick(facts, pb.EPS_TAGS, "annual", {u})
        if rows:
            return u.split("/")[0], rows
    return None, {}


def quarters_of_year(quarters, fy_end):
    """公告日落在 (fy_end−365+LAG, fy_end+LAG] 的季度。纯函数。"""
    e = date.fromisoformat(fy_end)
    lo, hi = (e - timedelta(days=365 - Q_WIN_LAG)).isoformat(), \
        (e + timedelta(days=Q_WIN_LAG)).isoformat()
    return [q for q in quarters if lo < q["date"] <= hi]


def infer_ratio(quarters, annual, fx):
    """ADR 换股比推断：各年 Yahoo 4 季之和 × 年均汇率 ÷ 20-F EPS 的中位，就近取「整」比例；
    离最近的整比例超过 XCHECK_TOL -> None（换算对不上，不推断）。纯函数。"""
    ks = []
    for end, a in annual.items():
        qs = quarters_of_year(quarters, end)
        if len(qs) == 4 and fx.get(end) and a["val"]:
            ks.append(sum(q["eps"] for q in qs) * fx[end] / a["val"])
    if not ks:
        return None
    k = median(ks)
    best = min(NICE_RATIOS, key=lambda r: abs(k / r - 1))
    return best if abs(k / best - 1) <= XCHECK_TOL else None


def crosscheck(quarters, annual, fx, ratio):
    """逐财年核对 -> [{"fy_end", "sec", "yahoo", "dev", "ok", "why"}]（sec = 20-F 折成每 ADR
    美元）。凑不齐 4 季 / 缺汇率的年份 ok=False 并写 why。纯函数。"""
    out = []
    for end, a in annual.items():
        qs = quarters_of_year(quarters, end)
        rec = {"fy_end": end, "sec": None, "yahoo": None, "dev": None, "ok": False}
        if len(qs) != 4:
            rec["why"] = f"Yahoo 该财年 {len(qs)} 季"
        elif not fx.get(end):
            rec["why"] = "缺年均汇率"
        else:
            rec["sec"] = a["val"] * ratio / fx[end]
            rec["yahoo"] = sum(q["eps"] for q in qs)
            rec["dev"] = rec["yahoo"] / rec["sec"] - 1 if rec["sec"] else None
            rec["ok"] = rec["dev"] is not None and abs(rec["dev"]) <= XCHECK_TOL
            if not rec["ok"]:
                rec["why"] = "偏差超限"
        out.append(rec)
    return out


def accepted_quarters(quarters, checks):
    """核对通过的财年里的季度 + 最新 20-F 之后的季度（最近一个可核对年份通过才放）。
    -> (放行的公告日集合, 年报未出的季度数) 或 (None, 原因)。纯函数。"""
    checked = [c for c in checks if c["yahoo"] is not None]
    if not checked:
        return None, "没有一年凑齐 4 季可与 20-F 核对"
    last = checked[-1]
    if not last["ok"]:
        return None, (f"最近可核对财年（至 {last['fy_end']}）Yahoo 与 20-F 偏差 "
                      f"{last['dev']:+.1%}，超出 ±{XCHECK_TOL:.0%}")
    ok = {q["date"] for c in checks if c["ok"] for q in quarters_of_year(quarters, c["fy_end"])}
    tail_from = (date.fromisoformat(max(c["fy_end"] for c in checks))
                 + timedelta(days=Q_WIN_LAG)).isoformat()
    tail = [q["date"] for q in quarters if q["date"] > tail_from]
    return ok | set(tail), len(tail)


def build_series(quarters, ok, closes, key="pe_trailing"):
    """逐日 TTM PE：公告日之后的交易日才知道这季；最近 4 季必须全部放行且相邻公告间隔在
    Q_GAP 内。closes = [(date, close)]。-> [{"date", key, "ttm_eps", "ttm_period"}]。纯函数。"""
    out, i = [], 0
    for d, px in closes:
        ds = d.isoformat()
        while i < len(quarters) and quarters[i]["date"] < ds:
            i += 1
        win = quarters[max(0, i - 4):i]
        if len(win) < 4 or any(q["date"] not in ok for q in win):
            continue
        gaps = [(date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days
                for a, b in zip(win, win[1:])]
        if any(not Q_GAP[0] <= g <= Q_GAP[1] for g in gaps):
            continue
        ttm = sum(q["eps"] for q in win)
        if ttm <= 0:
            continue
        out.append({"date": ds, key: px / ttm, "ttm_eps": ttm, "ttm_period": win[-1]["date"]})
    return out


def _window(series, start, key):
    """窗口内按 pe_band 规则剔近零（EPS < 窗口中位 25%）后的序列。"""
    s = [x for x in series if x["date"] >= start]
    if not s:
        return s
    floor = 0.25 * pb.pctile(sorted(x["ttm_eps"] for x in s), 50)
    return [x for x in s if x["ttm_eps"] >= floor]


def summarize_series(series, last_px_date, key="pe_trailing", stale_days=7, min_days=250):
    """-> 与 pe_rank.summarize 同形的 dict（pe/date/fresh/ttm_period/days10/thin/r10/r5/r3/p3）。
    10y、5y 各自窗口剔近零；3y = 5y 剔后序列的近 3 年段（与 pe_band recent 子窗同源）。纯函数。"""
    s10 = _window(series, pb.years_ago(10).isoformat(), key)
    if not s10:
        raise RuntimeError("Yahoo 季度 EPS 排出的有效 TTM 序列为空")
    cur = s10[-1]
    x = cur[key]
    sv10 = sorted(v[key] for v in s10)
    s5 = _window(series, pb.years_ago(5).isoformat(), key)
    sv5 = sorted(v[key] for v in s5)
    start3 = pb.years_ago(3).isoformat()
    sv3 = sorted(v[key] for v in s5 if v["date"] >= start3)
    ok3 = len(sv3) >= min_days
    lag = (last_px_date - date.fromisoformat(cur["date"])).days
    return {"pe": x, "date": cur["date"], "fresh": lag <= stale_days,
            "ttm_period": cur["ttm_period"], "days10": len(sv10), "thin": len(sv10) < min_days,
            "r10": pb.rank_of(sv10, x) if len(sv10) >= min_days else None,
            "r5": pb.rank_of(sv5, x) if len(sv5) >= min_days else None,
            "r3": pb.rank_of(sv3, x) if ok3 else None,
            "p3": {q: pb.pctile(sv3, q) for q in (10, 25, 50, 75, 90)} if ok3 else None}


# ---- 联网 ----

def fx_annual(ccy, annual, hist_fn=None):
    """各财年平均汇率（1 美元 = ? 本币）。USD -> 1。hist_fn(symbol, start, end) -> Close 序列。"""
    if ccy == "USD":
        return {e: 1.0 for e in annual}
    if hist_fn is None:
        import yfinance as yf
        hist_fn = lambda s, a, b: yf.Ticker(s).history(start=a, end=b)["Close"]  # noqa: E731
    ends = sorted(annual)
    lo = (date.fromisoformat(ends[0]) - timedelta(days=370)).isoformat()
    hi = (date.fromisoformat(ends[-1]) + timedelta(days=2)).isoformat()
    px = hist_fn(f"{ccy}=X", lo, hi).dropna()
    out = {}
    for e in ends:
        a = (date.fromisoformat(e) - timedelta(days=365)).isoformat()
        seg = [float(v) for d, v in px.items() if a < d.date().isoformat() <= e]
        if len(seg) >= 200:
            out[e] = sum(seg) / len(seg)
    return out


def foreign_reading(t, inputs, tk, last_px_date, stale_days=7, min_days=250):
    """外国发行人的 TTM PE 读数 -> summarize 同形 dict + src/xcheck，或 {"err": 原因}。不抛。"""
    try:
        quarters = parse_yahoo_quarters(tk.get_earnings_dates(limit=100))
        if len(quarters) < 4:
            return {"err": f"{t} Yahoo 季度 EPS 不足 4 季"}
        ccy, annual = annual_eps(inputs["facts"])
        if not annual:
            return {"err": f"{t} 20-F 没有年度摊薄 EPS，无从核对 Yahoo 季度数"}
        fx = fx_annual(ccy, annual)
        ratio, inferred = ADR_RATIO.get(t), False
        if ratio is None:
            ratio, inferred = infer_ratio(quarters, annual, fx), True
            if ratio is None:
                return {"err": f"{t} 推不出 ADR 换股比（Yahoo 与 20-F 换算对不上）"}
        checks = crosscheck(quarters, annual, fx, ratio)
        ok, tail = accepted_quarters(quarters, checks)
        if ok is None:
            return {"err": f"{t} Yahoo 季度 EPS 未通过 20-F 核对：{tail}"}
        closes = [(d.date(), float(v)) for d, v in inputs["hist"]["Close"].dropna().items()]
        m = summarize_series(build_series(quarters, ok, closes), last_px_date,
                             stale_days=stale_days, min_days=min_days)
    except Exception as e:  # noqa: BLE001
        msg = str(e).splitlines()[0][:100] if str(e) else type(e).__name__
        return {"err": f"{t} Yahoo 季度兜底失败：{msg}"}
    m["src"] = "yahoo"
    m["xcheck"] = {"ccy": ccy, "ratio": ratio, "ratio_inferred": inferred, "tail": tail,
                   "years": checks[-6:], "n_ok": sum(c["ok"] for c in checks),
                   "n": len(checks)}
    return m


def source_note(m):
    """来源脚注一句（md 与网页悬停同源）。纯函数。"""
    x = m["xcheck"]
    yrs = [c for c in x["years"] if c["yahoo"] is not None]
    last = yrs[-1] if yrs else None
    s = (f"外国发行人（20-F 无季度 XBRL）：TTM 分子 = Yahoo 季度 EPS（每 ADR 美元），"
         f"经 20-F 年度 EPS 核对 {x['n_ok']}/{x['n']} 年通过（±{XCHECK_TOL:.0%}，"
         f"{x['ccy']} 按年均汇率、1 ADR = {x['ratio']:g} 股{'（推断）' if x['ratio_inferred'] else ''}）")
    if last:
        s += f"；最近核对 FY 至 {last['fy_end']}：Yahoo {last['yahoo']:.2f} vs 20-F {last['sec']:.2f}"
    if x["tail"]:
        s += f"；20-F 之后 {x['tail']} 季年报未出、暂未核对"
    return s + "。营业线 / 一次性项还原不适用"
