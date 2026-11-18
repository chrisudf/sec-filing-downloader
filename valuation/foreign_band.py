# -*- coding: utf-8 -*-
"""外国发行人（20-F，无季度 XBRL）的 TTM PE 带：Yahoo 季度 EPS 出数，SEC 20-F 年度 EPS 核对。

为什么要这条路：pe_band 的 TTM 需要季度净利，20-F 发行人（TSM、ASML）在 companyfacts 里只有
年度、本币的数。只用年度数 TTM 一年才跳一次，增速快的票（TSM 一年 +50%）PE 会被系统性
高估；解析 6-K 季报格式不固定。所以季度节奏取 Yahoo 的 earnings dates（每份 ADR、带公告日），
口径由 20-F 年度数把关。

口径规则（不是混用的前提）：
  - 一条带的分子从头到尾只用这一个来源，不和 SEC 季度数拼接；只在「外国发行人形态」
    （ifrs-full / 有年度无季度）时启用——取数失败、新上市票照旧报错，不悄悄换源。
  - Yahoo 的 EPS 币种不固定：TSM 是每 ADR 美元；ASML 2013 年起是欧元、之前是美元。所以
    逐财年判币种：20-F 年度摊薄 EPS（本币 / 普通股）× ADR 换股比，按「Yahoo 是美元」
    （÷ 该财年平均汇率）和「Yahoo 是本币」两种假设分别与 Yahoo 同一财年 4 季之和比，
    偏差小的那个假设 |偏差| ≤ XCHECK_TOL 才算过，并把这年的季度记成该币种；
    对不上、凑不齐 4 季的年份整年剔。
  - 本币季度换美元用**当日**汇率（收盘价是当日美元，分子分母同一天的汇率）；美元季度原样。
  - 最新一份 20-F 之后的季度（年报未出）无从核对，沿用最近一个可核对年份的币种，且该年
    通过才放行；最近一年就对不上 -> 整组不出（宁缺毋滥）。
  - 防前视：公告日（美东）当天不算已知，次一交易日起才进 TTM（TSM 常在美东凌晨 / 前一晚
    公告，保守一天）。近零剔除、250 天门槛、10y/5y 各自窗口与 pe_band 一致。
  - 营业线 / 一次性项还原不做：Yahoo 没有季度营业利润的长历史，不硬凑。
"""
import bisect
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 与 pe_rank 同：同目录平级 import
import pe_band as pb  # noqa: E402

XCHECK_TOL = 0.05     # Yahoo 年度 4 季之和 vs 20-F 折算，汇率用年均价，季度汇率差在 1–2% 内
Q_WIN_LAG = 70        # 财年 (end−365+LAG, end+LAG] 内公告的季度归该财年：Q4 最晚 end+70 天公告
Q_GAP = (45, 140)     # 相邻两次公告的间隔（天）：超出 = 中间缺季，这扇 TTM 窗口不算
NICE_RATIOS = (0.1, 0.2, 0.25, 0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 50, 100)
ADR_RATIO = {"TSM": 5, "ASML": 1}   # 1 ADR = N 股普通股（20-F 封面）；表外的票按年度数推断并标注


def foreign_shape(inputs):
    """ifrs-full taxonomy，或净利有年度、无任何季度（20-F 年报节奏）-> True。纯函数。"""
    if inputs.get("taxonomy") == "ifrs-full":
        return True
    facts = inputs["facts"]
    units = {u for t in pb.NI_TAGS for u in (facts.get(t) or {}).get("units", {})}
    return bool(pb.pick(facts, pb.NI_TAGS, "annual", units)) and \
        not pb.pick(facts, pb.NI_TAGS, "quarterly", units)


def parse_yahoo_quarters(df):
    """get_earnings_dates 的表 -> [{"date": 公告日(美东) ISO, "eps": 每 ADR}]，升序（币种待核对
    时判定）。没公布（Reported EPS 为 NaN）的未来行丢掉；同一天重复行保留一条。纯函数。"""
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


def _hypotheses(sec_local, fx, ccy):
    """20-F 每普通股本币 EPS -> {Yahoo 币种假设: 每普通股在该币种下的值}。美元申报只有一种。"""
    out = {"USD": sec_local / fx}
    if ccy != "USD":
        out[ccy] = sec_local
    return out


def _nice(k):
    best = min(NICE_RATIOS, key=lambda r: abs(k / r - 1))
    return best if abs(k / best - 1) <= XCHECK_TOL else None


def infer_ratio(quarters, annual, fx, ccy):
    """ADR 换股比推断：每个财年在两种币种假设下各算 Yahoo 4 季之和 ÷ 20-F，能就近落到「整」
    比例的记一票，取票数最多的比例；没有一年能落到整比例 -> None。纯函数。"""
    votes = Counter()
    for end, a in annual.items():
        qs = quarters_of_year(quarters, end)
        if len(qs) != 4 or not fx.get(end) or not a["val"]:
            continue
        ys = sum(q["eps"] for q in qs)
        for v in _hypotheses(a["val"], fx[end], ccy).values():
            r = _nice(ys / v) if v else None
            if r:
                votes[r] += 1
    return votes.most_common(1)[0][0] if votes else None


def crosscheck(quarters, annual, fx, ratio, ccy):
    """逐财年核对 -> [{"fy_end", "unit", "sec", "yahoo", "dev", "ok", "why"}]：unit = 判定的
    Yahoo 币种（偏差小的假设），sec = 20-F 折成该币种的每 ADR 值。凑不齐 4 季 / 缺汇率的年份
    ok=False 并写 why。纯函数。"""
    out = []
    for end, a in annual.items():
        qs = quarters_of_year(quarters, end)
        rec = {"fy_end": end, "unit": None, "sec": None, "yahoo": None, "dev": None, "ok": False}
        if len(qs) != 4:
            rec["why"] = f"Yahoo 该财年 {len(qs)} 季"
        elif not fx.get(end):
            rec["why"] = "缺年均汇率"
        else:
            ys = sum(q["eps"] for q in qs)
            devs = {u: ys / (v * ratio) - 1 for u, v in _hypotheses(a["val"], fx[end], ccy).items()
                    if v}
            if not devs:
                rec["why"] = "20-F EPS 为 0"
            else:
                u = min(devs, key=lambda u: abs(devs[u]))
                rec.update(unit=u, yahoo=ys, dev=devs[u],
                           sec=_hypotheses(a["val"], fx[end], ccy)[u] * ratio,
                           ok=abs(devs[u]) <= XCHECK_TOL)
                if not rec["ok"]:
                    rec["why"] = "偏差超限"
        out.append(rec)
    return out


def accepted_quarters(quarters, checks):
    """核对通过的财年里的季度 + 最新 20-F 之后的季度（沿用最近可核对年份的币种，且该年通过
    才放）。-> ({公告日: 币种}, 年报未出的季度数) 或 (None, 原因)。纯函数。"""
    checked = [c for c in checks if c["yahoo"] is not None]
    if not checked:
        return None, "没有一年凑齐 4 季可与 20-F 核对"
    last = checked[-1]
    if not last["ok"]:
        return None, (f"最近可核对财年（至 {last['fy_end']}）Yahoo 与 20-F 偏差 "
                      f"{last['dev']:+.1%}，超出 ±{XCHECK_TOL:.0%}")
    ok = {q["date"]: c["unit"] for c in checks if c["ok"]
          for q in quarters_of_year(quarters, c["fy_end"])}
    tail_from = (date.fromisoformat(max(c["fy_end"] for c in checks))
                 + timedelta(days=Q_WIN_LAG)).isoformat()
    tail = [q["date"] for q in quarters if q["date"] > tail_from]
    ok.update(dict.fromkeys(tail, last["unit"]))
    return ok, len(tail)


def fx_lookup(fx_daily):
    """[(date, 1 美元 = ? 本币)] 升序 -> fn(date) = 当日或之前最近一个汇率（之前没有 -> None）。"""
    ds = [d for d, _ in fx_daily]

    def at(d):
        i = bisect.bisect_right(ds, d) - 1
        return fx_daily[i][1] if i >= 0 else None
    return at


def build_series(quarters, ok, closes, fx_at=None, key="pe_trailing"):
    """逐日 TTM PE：公告日之后的交易日才知道这季；最近 4 季必须全部放行且相邻公告间隔在
    Q_GAP 内。ok = {公告日: 币种}；本币季度按当日汇率 fx_at(date) 换美元（取不到汇率的日子
    跳过）。closes = [(date, close)]。-> [{"date", key, "ttm_eps", "ttm_period"}]。纯函数。"""
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
        ttm = 0.0
        for q in win:
            if ok[q["date"]] == "USD":
                ttm += q["eps"]
                continue
            r = fx_at(d) if fx_at else None
            if not r:
                ttm = None
                break
            ttm += q["eps"] / r
        if not ttm or ttm <= 0:
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

def fx_history(ccy, start, end):
    """[(date, 1 美元 = ? 本币)] 日序列（yfinance 的 {ccy}=X）。USD -> []。

    方向：Yahoo 的 {ccy}=X 对所有币种都是「1 美元 = ? 本币」（2026-11-18 实测 EUR=X 0.911、
    TWD=X 32.9、GBP=X 0.759、JPY=X 159.4；对应 {ccy}USD=X 是倒数 1.097 / 0.0304 / 1.317）。
    本模块一律「本币 ÷ 汇率 = 美元」。fetch_facts 用的是 {ccy}USD=X（美元/本币，乘）——两边
    方向相反但各自自洽，不要混用；sanity_fx 兜住拿错报价的情况。"""
    if ccy == "USD":
        return []
    import yfinance as yf
    px = yf.Ticker(f"{ccy}=X").history(start=start, end=end)["Close"].dropna()
    out = [(d.date(), float(v)) for d, v in px.items()]
    if out:
        inv = yf.Ticker(f"{ccy}USD=X").history(period="5d")["Close"].dropna()
        sanity_fx(out[-1][1], float(inv.iloc[-1]) if len(inv) else None, ccy)
    return out


def sanity_fx(local_per_usd, usd_per_local, ccy, tol=0.05):
    """{ccy}=X 与 {ccy}USD=X 应互为倒数：不是 -> 报价方向拿错了，抛 RuntimeError（整组不出，
    不带着反向汇率往下算）。对照取不到时不拦。纯函数。"""
    if usd_per_local and abs(local_per_usd * usd_per_local - 1) > tol:
        raise RuntimeError(f"{ccy} 汇率方向对不上：{ccy}=X {local_per_usd:.5g} × "
                           f"{ccy}USD=X {usd_per_local:.5g} ≠ 1")


def fx_annual(annual, fx_daily, ccy):
    """各财年平均汇率（1 美元 = ? 本币）。USD -> 1；该财年日数据不足 200 天的不给。纯函数。"""
    if ccy == "USD":
        return {e: 1.0 for e in annual}
    out = {}
    for e in annual:
        a = date.fromisoformat(e) - timedelta(days=365)
        seg = [v for d, v in fx_daily if a < d <= date.fromisoformat(e)]
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
        lo = (date.fromisoformat(min(annual)) - timedelta(days=370)).isoformat()
        fx_daily = fx_history(ccy, lo, (last_px_date + timedelta(days=1)).isoformat())
        fx = fx_annual(annual, fx_daily, ccy)
        ratio, inferred = ADR_RATIO.get(t), False
        if ratio is None:
            ratio, inferred = infer_ratio(quarters, annual, fx, ccy), True
            if ratio is None:
                return {"err": f"{t} 推不出 ADR 换股比（Yahoo 与 20-F 换算对不上）"}
        checks = crosscheck(quarters, annual, fx, ratio, ccy)
        ok, tail = accepted_quarters(quarters, checks)
        if ok is None:
            return {"err": f"{t} Yahoo 季度 EPS 未通过 20-F 核对：{tail}"}
        closes = [(d.date(), float(v)) for d, v in inputs["hist"]["Close"].dropna().items()]
        m = summarize_series(build_series(quarters, ok, closes, fx_lookup(fx_daily)),
                             last_px_date, stale_days=stale_days, min_days=min_days)
    except Exception as e:  # noqa: BLE001
        msg = str(e).splitlines()[0][:100] if str(e) else type(e).__name__
        return {"err": f"{t} Yahoo 季度兜底失败：{msg}"}
    m["src"] = "yahoo"
    m["xcheck"] = {"ccy": ccy, "ratio": ratio, "ratio_inferred": inferred, "tail": tail,
                   "years": checks[-6:], "n_ok": sum(c["ok"] for c in checks),
                   "n": len(checks), "units": sorted({c["unit"] for c in checks if c["ok"]})}
    return m


def source_note(m):
    """来源脚注一句（md 与网页悬停同源）。纯函数。"""
    x = m["xcheck"]
    yrs = [c for c in x["years"] if c["yahoo"] is not None]
    last = yrs[-1] if yrs else None
    units = x.get("units") or ["USD"]
    loc = [u for u in units if u != "USD"]
    unit_s = ("美元" if not loc else
              f"币种逐年判定：{'美元与 ' if 'USD' in units else ''}{'/'.join(loc)}，"
              f"{'/'.join(loc)} 季度按当日汇率换美元")
    s = (f"外国发行人（20-F 无季度 XBRL）：TTM 分子 = Yahoo 季度 EPS（每 ADR，{unit_s}），"
         f"经 20-F 年度 EPS 核对 {x['n_ok']}/{x['n']} 年通过（±{XCHECK_TOL:.0%}，"
         f"{x['ccy']} 按年均汇率、1 ADR = {x['ratio']:g} 股{'（推断）' if x['ratio_inferred'] else ''}）")
    if last:
        s += (f"；最近核对 FY 至 {last['fy_end']}：Yahoo {last['yahoo']:.2f} vs 20-F "
              f"{last['sec']:.2f} {last.get('unit') or ''}").rstrip()
    if x["tail"]:
        s += f"；20-F 之后 {x['tail']} 季年报未出、暂未核对"
    return s + "。营业线 / 一次性项还原不适用"
