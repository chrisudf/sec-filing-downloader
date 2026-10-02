# -*- coding: utf-8 -*-
"""Watchlist 批量表：当前 PE 在近 10/5/3 年的历史分位 + 前瞻 PE，一票一行。

用法:
  python valuation/pe_rank.py [--tickers A,B] [--watchlist PATH] [--out-dir DIR]

票单默认读同级目录 ../watchlist-scanner/watchlist.toml；kind=etf/index 跳过（SEC 无 EPS）。
结果写 reports/pe_rank/pe_rank_YYYY-MM-DD.{md,csv,json}（reports/ 已 gitignore），同时打印到终端；
json 供网页 /watchlist.html 读（app/pe_rank_service.py），原子写入。
节奏：分母一季度才跳一次，每周跑一次 + 财报季补跑就够，天天跑没有信息量。
美股盘中跑的话 yfinance 末根是未收盘 K 线，「收盘」其实是盘中价——报告头会标出来。

列的口径:
  TTM PE / 分位 —— pe_band trailing 口径（XBRL 已公告 TTM EPS × yfinance 日收盘，
    防前视/拆股归一/畸变剔除/近零剔除全在 pe_band）。10y、5y 各用自己窗口重算的带
    （近零地板按窗口中位数定，与 pe_band CLI 一致）；3y = 5y 带里近 3 年那段的秩，
    与 pe_band 的 recent 子窗同源。有效样本 < MIN_DAYS 的不给分位（SOFI/HOOD 型刚转盈）。
  † —— 带子末点比最新收盘早 > STALE_DAYS 天 = 当前 TTM 窗口被剔（一次性畸变/亏损/
    近零），显示的是末个有效点及其分位。AMZN/GOOG 2026Q2 私募重估就是这种，看营业线那组。
  营业线 PE —— metric=opeps，分母 = 营业利润×(1−21%)÷股数，营业线以下的一次性免疫。
  本财年 / 下财年 PE —— 收盘 ÷ yfinance 一致预期 0y / +1y（分析师口径，通常是调整后，
    与 TTM 的 GAAP 不同口径）。Yahoo 的 forwardPE 就是「下财年」这一列，不是 NTM——
    错位财年（NVDA 1 月底）会领先约 16 个月，看起来特别便宜。区间 = 收盘÷预期 high …
    收盘÷预期 low，low ≤ 0 的上沿写「含亏损」。
  ⚠ —— 标在「本财年 PE」格上：本财年一致预期 90 天内大跳而下财年没动（ref_table 的
    一次性判据），疑似一次性收益进了预期。只污染这一格，不波及营业线分位。
    也标在「TTM PE」格上：当前 TTM 窗口的营业外收支 / 有效税率偏离自身常态，还原后
    GAAP PE 偏离超过 ONETIME_IMPACT（gaap_onetime / onetime_reading）。这时 GAAP 那组
    整组换成还原口径（≈ 标记、网页上分位格加斜纹）：当前值和历史逐窗用同一公式扣除
    一次性项后再算分位（clean_band），原值进脚注 / 悬停。
  分位与前瞻 PE 回答的是两个问题（历史位置 vs 预期兑现后的倍数），要一起读。
"""
import argparse
import csv
import json
import math
import os
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
import pe_band as pb  # noqa: E402
from ref_table import fetch_consensus, fy_labels, one_time_warning  # noqa: E402
from app.edgar import contact_email  # noqa: E402

DEFAULT_WATCHLIST = ROOT.parent / "watchlist-scanner" / "watchlist.toml"
OUT_DIR = ROOT / "reports" / "pe_rank"
STALE_DAYS = 7    # 带子末点落后最新收盘超过这么多天 = 当前窗口被剔
MIN_DAYS = 250    # 与 pe_band 的 thin_coverage 同一门槛（约一个财年的交易日）
SKIP_KINDS = ("etf", "index")
METRICS = (("gaap", "eps", "pe_trailing"), ("op", "opeps", "peop_trailing"))
ET = ZoneInfo("America/New_York")
# GAAP TTM 窗口的一次性成分（gaap_onetime）。税前利润两个 tag 与 fetch_facts 同序
PRETAX_TAGS = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]
TAX_TAGS = ["IncomeTaxExpenseBenefit"]
ONETIME_BASE_Q = 12   # 常态 = 截至窗口末 12 季的中位：窗口 4 季全畸变也拖不动
ONETIME_MIN_Q = 8
ONETIME_IMPACT = 0.10


def us_market_open(now=None):
    """美股常规时段（周一至五 9:30–16:00 ET，不计节假日）。"""
    t = (now or datetime.now(ET)).astimezone(ET)
    return t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)


def one_time_flag(trend):
    """ref_table 的一次性判据命中 -> {j0, j1}（本/下财年一致预期 90 天变动），否则 None。
    判据复用，文案不复用（那边的建议是钉 overrides，这张表不适用）。"""
    if not one_time_warning(trend):
        return None
    j0, j1 = (trend[p]["current"] / trend[p]["90daysAgo"] - 1 for p in ("0y", "+1y"))
    return {"j0": j0, "j1": j1}


def one_time_note(flag):
    return (f"本财年一致预期 90 天内 {flag['j0']:+.0%}、下财年仅 {flag['j1']:+.0%}——"
            "疑似一次性收益进了预期，本财年 PE 偏低不可信，看下财年")


def _quarterly(facts, tags):
    return pb.derive_q4(pb.pick(facts, tags, "quarterly", {"USD"}),
                        pb.pick(facts, tags, "annual", {"USD"}))


def _onetime_series(facts):
    """gaap_onetime 用到的四条季度序列，一次取好供逐窗复用。"""
    ni, pt, tx = (_quarterly(facts, t) for t in (pb.NI_TAGS, PRETAX_TAGS, TAX_TAGS))
    return ni, pt, tx, pb.op_income_rows(facts)[1]


def _onetime_at(S, ttm_end, impact):
    ni, pt, tx, opq = S
    keys = [k for k in sorted(pt) if k <= ttm_end and k in opq and k in tx][-ONETIME_BASE_Q:]
    win = keys[-4:]
    if (len(keys) < ONETIME_MIN_Q or win[-1] != ttm_end or any(k not in ni for k in win)
            or (date.fromisoformat(win[-1]) - date.fromisoformat(win[0])).days > 300):
        return None
    below = {k: pt[k]["val"] - opq[k]["val"] / (1 - pb.OP_TAX) for k in keys}
    base = median(below.values())
    excess = sum(below[k] - base for k in win)
    pre, tax, n = (sum(s[k]["val"] for k in win) for s in (pt, tx, ni))
    etrs = [tx[k]["val"] / pt[k]["val"] for k in keys if pt[k]["val"] > 0]
    if len(etrs) < ONETIME_MIN_Q or pre <= 0 or n <= 0:
        return None
    etr_n = median(etrs)
    clean = n + tax - excess - etr_n * (pre - excess)
    ratio = n / clean if clean > 0 else None
    if ratio is not None and abs(ratio - 1) <= impact:
        return None
    return {"ttm_end": ttm_end, "excess": excess, "base_q": base, "pretax": pre,
            "etr": tax / pre, "etr_norm": etr_n, "ratio": ratio}


def gaap_onetime(facts, ttm_end, impact=ONETIME_IMPACT):
    """GAAP TTM 窗口（截至 ttm_end 的四季）里营业线以下 / 税项的一次性成分 -> dict 或 None。

    为什么不靠 pe_band 的畸变守卫：它只看净利自身的形状，单季偏离同窗中位季 > 1.25 倍
    才整窗剔。低于门槛的收益照样进窗口（AMZN Q3'25 $10.2B、Q1'26 $15.7B Anthropic
    重估，† 停住的「末个有效点」本身仍含这两笔）；往下偏离的上限是 −100%（净利归零），
    要转成实打实的亏损才够得着门槛（META Q3'25 OBBBA 一次性税费，净利 −86% 仍放行）。
    改剔窗门槛会让 GAAP 列整年停在旧点，还会撞零售 Q4 的季节性——所以不剔，直接读造成
    畸变的两个科目，还原成「营业外取常态、税率取常态」的口径：

      营业外 = 税前利润 − 营业利润；常态 = 截至窗口末 12 季的中位（每季营业外、季度税率）
      干净净利 = 净利 + 税 − 超常营业外 − 常态税率 × (税前 − 超常营业外)
      ratio = 净利 ÷ 干净净利；GAAP PE × ratio = 干净口径 PE（clean ≤ 0 时 None）

    常态只用截至该窗口末的季度，逐窗算不偷看未来，所以同一公式能拿来还原整段历史
    （clean_band）。税后、线以下的项目（少数股东、权益法）原样保留。|ratio − 1| ≤ impact
    返回 None；科目取不齐（金融股没有营业利润、外国发行人没有季度序列、窗口缺季）也返回
    None——宁可不标也不瞎标。纯函数（facts = companyfacts 的 us-gaap 节）。
    """
    return _onetime_at(_onetime_series(facts), ttm_end, impact)


def clean_band(series, ratios, key, x):
    """还原口径的分位：历史每个交易日的 PE × 它所用窗口的 ratio，当前值 x 在其中的秩。纯函数。

    只还原当前点、拿它去比没还原的历史是两种口径相比：GOOG/AMZN 近两年的 GAAP 历史本身
    就被股权重估压低，还原后的当前值去比它会系统性偏高（GOOG P88/P100/P99，营业线才
    P51/P73/P48）。所以历史逐窗用同一公式还原（_onetime_at 的常态只看窗口末之前，不偷看）。
    取不到 ratio 的窗口（早年季度不足 8 季、还原后亏损）那几天不进分布。
    series = compute_band(include_series=True) 的逐日记录（带 ttm_period）。"""
    pts = [(s["date"], s[key] * ratios[s["ttm_period"]]) for s in series
           if key in s and ratios.get(s.get("ttm_period"))]
    out = {"days": len(pts)}
    for n in (10, 5, 3):
        start = pb.years_ago(n).isoformat()
        sv = sorted(v for d, v in pts if d >= start)
        ok = len(sv) >= MIN_DAYS
        out[f"r{n}"] = pb.rank_of(sv, x) if ok else None
        if n == 3:
            out["p3"] = {q: pb.pctile(sv, q) for q in (10, 25, 50, 75, 90)} if ok else None
    return out


def onetime_reading(facts, g, b10, close, px_date, key="pe_trailing"):
    """GAAP 读数 g -> 还原口径 dict（pe / asof / clean 分位）或 None。纯函数。

    † 行（当前窗口被 pe_band 畸变守卫剔了）优先还原**最新那扇被剔的窗口**、用当前收盘：
    显示的末个有效点是旧窗口 × 旧价格（AMZN 7/30），拿它还原会差出一个季度和两个月的
    股价（AMZN 35.3x vs 最新窗口 33.9x、GOOG 33.6x vs 31.5x）。最新窗口还原不出偏离
    （畸变在营业线以内，这里管不到）才退回旧点——旧点本身也可能含门槛下的一次性项。
    b10 带 series 时附 clean（clean_band），否则只给还原后的 PE。"""
    S = _onetime_series(facts)
    tries = []
    if not g["fresh"]:
        newer = [w for w in b10.get("anom_windows", [])
                 if w["period_end"] > (g["ttm_period"] or "")
                 and w["known_from"] <= str(px_date) and w["ttm_eps"] > 0]
        if newer:
            w = max(newer, key=lambda w: w["period_end"])
            tries.append((w["period_end"], close / w["ttm_eps"], str(px_date)))
    tries.append((g["ttm_period"], g["pe"], g["date"]))
    for end, pe, asof in tries:
        ot = _onetime_at(S, end, ONETIME_IMPACT) if end else None
        if ot:
            break
    else:
        return None
    ot = dict(ot, pe=pe * ot["ratio"] if ot["ratio"] else None, asof=asof)
    if ot["pe"] and "series" in b10:
        ends = {s.get("ttm_period") for s in b10["series"]} - {None}
        ratios = {e: (r or {}).get("ratio") for e in ends for r in [_onetime_at(S, e, -1)]}
        ot["clean"] = clean_band(b10["series"], ratios, key, ot["pe"])
    return ot


def onetime_note(ot, g):
    """还原口径的脚注一句：一次性项明细 + 还原值 + 原值（表里这组显示的是还原口径）。"""
    def bn(v, plus=False):
        return f"{'−' if v < 0 else '+' if plus else ''}${abs(v) / 1e9:.1f}B"
    parts = []
    if abs(ot["excess"]) >= 0.03 * ot["pretax"]:
        parts.append(f"营业外超常 {bn(ot['excess'], True)}（税前，常态每季 {bn(ot['base_q'])}）")
    if abs(ot["etr"] - ot["etr_norm"]) >= 0.02:
        parts.append(f"有效税率 {ot['etr']:.0%}（常态 {ot['etr_norm']:.0%}）")
    adj = ("扣除后 TTM 亏损" if ot.get("pe") is None
           else f"扣除后约 {ot['pe']:.1f}x（{ot.get('asof') or '—'} 收盘）")
    raw = (f"{g['pe']:.1f}x" + ("" if g["fresh"] else f"†{g['date']}")
           + " · " + "/".join(_pc(g.get(k)) for k in ("r10", "r5", "r3")))
    return (f"TTM 窗口（至 {ot['ttm_end']}）疑含一次性项：{'；'.join(parts) or '营业外与税项合计'}"
            f"——{adj}。表中 GAAP 这组是还原口径（历史逐窗同样扣除后重算分位）；原值 {raw}")


def load_watchlist(path, tickers=None):
    """-> [(ticker, kind)]。--tickers 给了就按它（kind 仍从票单查，查不到当 stock）。"""
    wl = {}
    if Path(path).exists():
        wl = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("tickers", {})
    elif not tickers:
        raise SystemExit(f"找不到票单 {path}（用 --watchlist 指定，或 --tickers 直接给）")
    names = [t.strip().upper() for t in tickers.split(",") if t.strip()] if tickers else list(wl)
    return [(t, wl.get(t, {}).get("kind", "stock")) for t in names]


def summarize(b10, b5, last_px_date, key):
    """两条带（10y/5y，trailing）-> 当前值与三个窗口的分位。纯函数。

    b5 可为 None（5 年窗口样本不足时 compute_band 抛错）；b5 需带 series。"""
    cur = b10["current"]
    x = cur[key]
    r10 = None if b10["thin_coverage"] else pb.rank_of(b10["_sorted"], x)
    r5 = r3 = p3 = None
    if b5 is not None:
        if not b5["thin_coverage"]:
            r5 = pb.rank_of(b5["_sorted"], x)
        start3 = pb.years_ago(3).isoformat()
        sv3 = sorted(s[key] for s in b5["series"] if s["date"] >= start3)
        if len(sv3) >= MIN_DAYS:
            r3 = pb.rank_of(sv3, x)
        p3 = (b5.get("recent") or {}).get("pctiles")
    lag = (last_px_date - date.fromisoformat(cur["date"])).days
    return {"pe": x, "date": cur["date"], "fresh": lag <= STALE_DAYS,
            "ttm_period": cur.get("ttm_period"), "days10": b10["days"],
            "thin": b10["thin_coverage"], "r10": r10, "r5": r5, "r3": r3, "p3": p3}


def band_reading(t, email, inputs, metric, key, last_px_date, series=False):
    try:
        b10 = pb.compute_band(t, email, 10, "trailing", metric=metric, inputs=inputs,
                              include_series=series)
    except RuntimeError as e:
        return {"err": str(e).splitlines()[0][:100]}, None
    try:
        b5 = pb.compute_band(t, email, 5, "trailing", include_series=True,
                             metric=metric, inputs=inputs)
    except RuntimeError:
        b5 = None
    return summarize(b10, b5, last_px_date, key), b10


def forward(close, cons):
    """收盘 ÷ 一致预期 -> 本财年/下财年 PE 与下财年区间。纯函数。

    预期 ≤0 或 NaN（NaN 比较恒假）的 PE 记 None；区间两端方向相反：
    预期最高的分析师给出最低的 PE。"""
    pe = lambda eps: close / eps if eps is not None and eps > 0 else None  # noqa: E731
    out = {}
    for k in ("fy1", "fy2"):
        c = cons.get(k)
        if c:
            out[f"{k}_eps"], out[f"{k}_pe"], out[f"{k}_n"] = c["avg"], pe(c["avg"]), c["n"]
    c2 = cons.get("fy2")
    if c2:
        out["fy2_pe_lo"] = pe(c2["high"])
        out["fy2_pe_hi"] = pe(c2["low"])
        # NaN（无预期）不算亏损：NaN <= 0 恒假，区间上沿留「—」
        out["fy2_low_nonpos"] = c2["low"] <= 0
    return out


def yahoo_info(t):
    import yfinance as yf
    try:
        i = yf.Ticker(t).info
    except Exception:
        return {}
    fye = i.get("lastFiscalYearEnd")
    return {"tpe": i.get("trailingPE"), "peg": i.get("trailingPegRatio"),
            "last_fy_end": date.fromtimestamp(fye).isoformat() if fye else None}


def last_close(hist):
    """最后一个有效收盘 -> (close, date)。纯函数。

    yfinance 的当天日线在 Yahoo 结算前会给出 Open/Volume 齐全、Close=NaN 的残行
    （2026-10-02 全表实测：10/01 行 Close 全 NaN）。直接取 iloc[-1] 会让 close 与
    全部前瞻 PE 变 NaN -> JSON null，网页 toFixed 直接抛。退回上一个有效收盘。"""
    px = hist["Close"].dropna()
    if px.empty:
        raise RuntimeError("yfinance 历史价没有有效收盘")
    return float(px.iloc[-1]), px.index[-1].date()


def collect(t, kind, email):
    """一只票的全部读数（联网）。异常逐项兜住，一票失败不拖垮整张表。"""
    if kind in SKIP_KINDS:
        return {"ticker": t, "kind": kind, "skip": ETF_SKIP}
    row = {"ticker": t, "notes": []}
    try:
        inputs = pb.load_inputs(t, email, 10)
        row["close"], row["px_date"] = last_close(inputs["hist"])
    except Exception as e:
        return {"ticker": t, "skip": str(e).splitlines()[0][:100]}
    b10_eps = None
    for name, metric, key in METRICS:
        # GAAP 带要逐日序列：一次性项还原口径的分位要逐窗还原整段历史（clean_band）
        row[name], b10 = band_reading(t, email, inputs, metric, key, row["px_date"],
                                      series=metric == "eps")
        if metric == "eps":
            b10_eps = b10
            ot = None if "err" in row[name] else onetime_reading(
                inputs["facts"], row[name], b10, row["close"], row["px_date"], key)
            if ot:
                row[name]["onetime"] = ot
                row["notes"].append(onetime_note(ot, row[name]))
    row["yh"] = yahoo_info(t)
    try:
        cons, trend, _ = fetch_consensus(t)
        row["fwd"] = forward(row["close"], cons)
        flag = one_time_flag(trend)
        if flag:
            row["fwd"]["fy1_suspect"] = flag
            row["notes"].append(one_time_note(flag))
    except (SystemExit, Exception) as e:   # fetch_consensus 取不到时 raise SystemExit
        row["fwd"] = {}
        row["notes"].append(f"一致预期取不到：{str(e).splitlines()[0][:80]}")
    # 财年标签：Yahoo 的 lastFiscalYearEnd 覆盖所有票（XBRL 带缺席的 TSM/RKLB 也有）
    lfy = row["yh"].get("last_fy_end")
    src = {"fiscal_years": [{"fy_end": lfy}]} if lfy else (b10_eps or {})
    row["fy_labels"] = fy_labels(src)
    return row


def _f(v, d=1, suffix=""):
    if v is None:
        return "—"
    # 分母趋零（RKLB/SPCX 型）的四位数倍数没有信息量，只说明「极高」
    return f">999{suffix}" if v >= 1000 else f"{v:.{d}f}{suffix}"


def _pc(v):
    return "—" if v is None else f"P{v:.0f}"


def _band_cells(m):
    if "err" in m:
        return ["n/a", "—", "—", "—", "—"]
    ot = m.get("onetime")
    if ot:      # 还原口径：PE、三窗分位、近 3 年带全换成扣除一次性项后的同一口径（≈ 标记）
        c = ot.get("clean") or {}
        pe = "亏损⚠" if ot["pe"] is None else f"≈{ot['pe']:.1f}x⚠"
        rs, p3 = [c.get(k) for k in ("r10", "r5", "r3")], c.get("p3")
    else:
        pe = f"{m['pe']:.1f}x" + ("" if m["fresh"] else f"†{m['date'][5:]}")
        rs, p3 = [m["r10"], m["r5"], m["r3"]], m["p3"]
    b = "—" if not p3 else f"{p3[10]:.0f}/{p3[50]:.0f}/{p3[90]:.0f}"
    return [pe] + [_pc(v) for v in rs] + [b]


def _fwd_cells(fw):
    if not fw:
        return ["—", "—", "—"]
    lo, hi = fw.get("fy2_pe_lo"), fw.get("fy2_pe_hi")
    if "fy2_pe_lo" not in fw:
        rng = "—"
    elif fw.get("fy2_low_nonpos"):
        rng = f"{_f(lo)}–含亏损"
    else:
        rng = f"{_f(lo)}–{_f(hi)}"
    fy1 = _f(fw.get("fy1_pe"), suffix="x") + ("⚠" if fw.get("fy1_suspect") else "")
    return [fy1, _f(fw.get("fy2_pe"), suffix="x"), rng]


HEADER = ["票", "收盘", "TTM PE", "10y", "5y", "3y", "3y P10/50/90",
          "营业线 PE", "10y", "5y", "3y", "3y P10/50/90",
          "本财年 PE", "下财年 PE", "下财年区间", "下财年", "Yahoo tPE", "PEG"]


INTRADAY_NOTE = "⚠ 运行时美股在盘中：「收盘」列是盘中价，分位与前瞻 PE 随之浮动"
ETF_SKIP = "ETF/指数：SEC 没有 EPS"


def skipped_line(rows):
    """不进表格的票汇成一行（按原因分组）；没有则 None。ETF/指数和取数失败的票
    一整行全是「—」没有信息量，还把真正要看的行挤开。纯函数。"""
    groups = {}
    for r in rows:
        if "skip" in r:
            groups.setdefault(r["skip"], []).append(r["ticker"])
    if not groups:
        return None
    return "未纳入：" + "；".join(f"{'、'.join(ts)}（{why}）" for why, ts in groups.items())


def render(rows, asof, intraday=False):
    """-> (markdown 全文, 脚注列表)。纯函数。"""
    md = [f"# Watchlist PE 分位 · {asof}", "",
          "TTM PE 与分位 = SEC XBRL 已公告 TTM EPS × yfinance 收盘（pe_band trailing）；"
          "营业线 PE = P/NOPAT（市值 ÷ 营业利润×(1−21%)）；"
          "P10/50/90 = 近 3 年 10%/一半/90% 的交易日 PE 低于该值；"
          "† = 当前窗口被剔，显示末个有效点；本/下财年 PE = 收盘 ÷ yfinance 一致预期"
          "（Yahoo forwardPE = 下财年列，不是 NTM）；⚠ = 疑含一次性项（本财年预期 / "
          "GAAP TTM 窗口的营业外与税项，见脚注的还原值）。"
          "口径细节见 valuation/pe_rank.py 文件头。", ""]
    if intraday:
        md += [f"**{INTRADAY_NOTE}**", ""]
    md += ["| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    notes = []
    for r in rows:
        t = r["ticker"]
        if "skip" in r:
            continue
        yh = r.get("yh", {})
        md.append("| " + " | ".join(
            [t, f"{r['close']:.2f}"] + _band_cells(r["gaap"]) + _band_cells(r["op"])
            + _fwd_cells(r.get("fwd")) + [r["fy_labels"][1],
                                          _f(yh.get("tpe")), _f(yh.get("peg"), 2)]) + " |")
        for lbl, m in (("TTM", r["gaap"]), ("营业线", r["op"])):
            if "err" in m:
                notes.append(f"{t} {lbl}: {m['err']}")
            elif not m["fresh"]:
                notes.append(f"{t} {lbl}: 当前 TTM 窗口被剔（一次性畸变/亏损/近零），"
                             f"末个有效点 {m['date']}")
            elif m["thin"]:
                notes.append(f"{t} {lbl}: 有效样本仅 {m['days10']} 天，不给分位")
        notes += [f"{t}: {n}" for n in r.get("notes", [])]
    sk = skipped_line(rows)
    if sk:
        md += ["", sk]
    md += ["", "## 脚注", ""] + [f"- {n}" for n in notes]
    return "\n".join(md) + "\n", notes


def csv_rows(rows):
    out = []
    for r in rows:
        rec = {"ticker": r["ticker"], "skip": r.get("skip"),
               "close": r.get("close"), "px_date": r.get("px_date")}
        for name, _, _ in METRICS:
            m = r.get(name) or {}
            for k in ("pe", "date", "fresh", "r10", "r5", "r3", "err"):
                rec[f"{name}_{k}"] = m.get(k)
            p3 = m.get("p3") or {}
            for q in (10, 50, 90):
                rec[f"{name}_3y_p{q}"] = p3.get(q)
        fw = r.get("fwd") or {}
        for k in ("fy1_eps", "fy1_pe", "fy1_n", "fy2_eps", "fy2_pe", "fy2_n",
                  "fy2_pe_lo", "fy2_pe_hi"):
            rec[k] = fw.get(k)
        rec["fy1_suspect"] = bool(fw.get("fy1_suspect"))
        ot = (r.get("gaap") or {}).get("onetime") or {}
        rec["gaap_onetime"], rec["gaap_clean_pe"] = bool(ot), ot.get("pe")
        for k in ("r10", "r5", "r3"):
            rec[f"gaap_clean_{k}"] = (ot.get("clean") or {}).get(k)
        labels = r.get("fy_labels") or (None, None)
        rec["fy1_label"], rec["fy2_label"] = labels
        yh = r.get("yh") or {}
        rec["yahoo_tpe"], rec["yahoo_peg"] = yh.get("tpe"), yh.get("peg")
        out.append(rec)
    return out


def _jsonable(o):
    """NaN/inf -> None（JSON 规范没有 NaN，浏览器 JSON.parse 直接抛）、date -> ISO、
    tuple -> list。yfinance 的 info 字段偶发 NaN。"""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


def payload(rows, notes, asof, generated_at, intraday, watchlist):
    """网页读的 JSON（纯函数）。px_date = 各票最新价格日的最大值（=「数据截至」）。"""
    px = [r["px_date"] for r in rows if r.get("px_date")]
    return _jsonable({"asof": asof, "generated_at": generated_at, "intraday": intraday,
                      "px_date": max(px) if px else None, "watchlist": str(watchlist),
                      "header": HEADER, "rows": rows, "notes": notes,
                      "skipped": skipped_line(rows)})


def write_atomic(path, text):
    """先写临时文件再 os.replace：网页刷新和定时任务可能撞车，读者不能读到半截文件。"""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="逗号分隔，覆盖票单")
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    a = ap.parse_args()
    # Windows 控制台默认 cp1252，打印中文表会 UnicodeEncodeError（文件已写完也会丢终端输出）
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")

    email = contact_email()
    intraday = us_market_open()
    tickers = load_watchlist(a.watchlist, a.tickers)
    rows = []
    for i, (t, kind) in enumerate(tickers, 1):
        # 「[i/n] 票」是 app/pe_rank_service.py 解析进度的格式，改这里要同步改那边
        print(f"[{i}/{len(tickers)}] {t}", file=sys.stderr, flush=True)
        rows.append(collect(t, kind, email))

    asof = date.today().isoformat()
    text, notes = render(rows, asof, intraday)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"pe_rank_{asof}.md").write_text(text, encoding="utf-8")
    recs = csv_rows(rows)
    with open(out / f"pe_rank_{asof}.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(recs[0]))
        w.writeheader()
        w.writerows(recs)
    gen = datetime.now().astimezone().isoformat(timespec="seconds")
    write_atomic(out / f"pe_rank_{asof}.json", json.dumps(
        payload(rows, notes, asof, gen, intraday, a.watchlist), ensure_ascii=False))
    print(text)
    print(f"已写出 {out / f'pe_rank_{asof}'}.md / .csv / .json", file=sys.stderr)


if __name__ == "__main__":
    main()
