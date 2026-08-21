# -*- coding: utf-8 -*-
"""分部营收图表服务：把 fetch_segments 的多申报解析结果重塑成堆叠图数组。

GET /api/segments/{ticker}?freq=quarterly|annual&years=N
按轴（业务线/经营分部/地区）返回 periods 与成员序列；成员按窗口内合计
排序，超过 7 个折叠进「其他」。冷取数要逐份下载解析申报文件（首次
10-60 秒），fetch_segments 内部按 accession 落盘缓存，这里再加 6h 内存缓存。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Query

from . import edgar

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from valuation.fetch_segments import SegmentsError, build_segments  # noqa: E402

router = APIRouter()

SEG_TTL = 6 * 3600
CACHE_MAX = 128
_seg_cache: dict[str, tuple[float, dict, dict]] = {}
_seg_locks: dict[str, asyncio.Lock] = {}

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
AXIS_LABELS = {"product": "按业务线", "segment": "按经营分部", "geo": "按地区"}
MAX_MEMBERS = 7  # 调色板 8 档：7 个成员 + 「其他」

# 常见成员名的中文映射；不在表里的剥掉 Member 后缀按驼峰拆词展示
MEMBER_ZH = {
    "Americas": "美洲", "Europe": "欧洲", "GreaterChina": "大中华区",
    "Japan": "日本", "RestOfAsiaPacific": "亚太其他", "AsiaPacific": "亚太",
    "IPhone": "iPhone", "IPad": "iPad", "Mac": "Mac",
    "WearablesHomeandAccessories": "穿戴/家居/配件", "Service": "服务",
    "Product": "产品", "DataCenter": "数据中心", "Gaming": "游戏",
    "ProfessionalVisualization": "专业可视化", "Automotive": "汽车",
    "OEMAndOther": "OEM及其他", "OemAndOther": "OEM及其他",
    "ComputeAndNetworking": "计算与网络", "Graphics": "图形",
    "Hyperscale": "超大规模云", "AICloudsIndustrialEnterprise": "AI云/行业企业",
    "EdgeComputing": "边缘计算",
    "LaunchServices": "发射服务", "SpaceSystems": "空间系统",
    "US": "美国", "CN": "中国", "TW": "台湾", "JP": "日本", "KR": "韩国",
    "DE": "德国", "GB": "英国", "IE": "爱尔兰", "SG": "新加坡", "IL": "以色列",
    "UnitedStates": "美国", "China": "中国", "ChinaIncludingHongKong": "中国(含香港)",
    "NonUs": "美国以外", "OtherCountries": "其他国家/地区",
    "AllOtherCountries": "其他国家/地区", "International": "国际",
    "Domestic": "本土", "Foreign": "海外",
}


# ---- 集中度分类：类型/基准/交易对手 ----
CONC_TYPE_ZH = (("Customer", "客户"), ("Supplier", "供应商"), ("Vendor", "供应商"),
                ("Credit", "信用"), ("Lender", "贷款人"), ("Geographic", "地域"),
                ("Labor", "用工"), ("Reinsur", "再保险"), ("Product", "产品"))
# 基准轴决定「占什么的百分比」——占应收款和占营收是完全不同的风险，
# 必须分开标注（对标站曾把 AAPL 的应收款集中度标成营收集中度）
CONC_BENCH_ZH = (("NonTradeReceivable", "非贸易应收款"),
                 ("TradeAccountsReceivable", "贸易应收款"),
                 ("AccountsReceivable", "应收账款"),
                 ("Receivable", "应收款"),
                 # 分部营收基准要先于「营收」命中：占分部营收≠占公司总营收
                 ("SalesRevenueSegment", "分部营收"),
                 ("RevenueFromContractWithCustomer", "营收"),
                 ("SalesRevenue", "营收"), ("Revenue", "营收"),
                 ("AccountsPayable", "应付账款"), ("Purchase", "采购额"),
                 ("CostOfGoods", "采购成本"), ("Deposit", "存款"),
                 ("Loans", "贷款"), ("Assets", "资产"))
# 聚合口径的对手方（客户群体/前N大合计/按地域圈定的客户），不能当
# 单一客户进风险分级和趋势加总——NVDA 的「美国终端客户 99%」是群体
_AGG_RE = re.compile(
    r"(Customers|Suppliers|Vendors|Carriers|Largest|Top[A-Z0-9]|Based|"
    r"Aggregate|Combined|Group|Government)")
# 同一客户的两套序数命名归一：NVDA 的 10-K 用 CustomerA/B/C、10-Q 用
# CustomerOne/Two，不归并会在趋势里双倍计数、明细表出两行
_ORD_ZH = {"A": "一", "B": "二", "C": "三", "D": "四", "E": "五", "F": "六",
           "One": "一", "Two": "二", "Three": "三", "Four": "四",
           "Five": "五", "Six": "六"}
_ORD_RE1 = re.compile(r"^(Customer|Client|Vendor|Supplier)"
                      r"(A|B|C|D|E|F|One|Two|Three|Four|Five|Six)$")
_ORD_RE2 = re.compile(r"^(One|Two|Three|Four|Five|Six)"
                      r"(Customer|Client|Vendor|Supplier)$")


def _party_norm(base: str) -> str | None:
    m = _ORD_RE1.match(base)
    kind = ordn = None
    if m:
        kind, ordn = m.group(1), m.group(2)
    else:
        m = _ORD_RE2.match(base)
        if m:
            ordn, kind = m.group(1), m.group(2)
    if kind is None:
        return None
    return ("客户" if kind in ("Customer", "Client") else "供应商") + _ORD_ZH[ordn]
CONC_PARTY_ZH = {
    "CustomerOne": "客户一", "CustomerTwo": "客户二", "CustomerThree": "客户三",
    "CustomerFour": "客户四", "CustomerFive": "客户五",
    "VendorOne": "供应商一", "VendorTwo": "供应商二", "VendorThree": "供应商三",
    "Company": "未具名大客户", "CellularNetworkCarriers": "移动运营商",
}


def _zh(table, name: str) -> str | None:
    for key, zh in table:
        if key in name:
            return zh
    return None


def _conc_reshape(raw: list) -> dict | None:
    """原始集中度事实 -> {risk, latest[], trend[]}。值 0-1 视为小数、
    1-100 视为整数百分比（个别公司整数入标），其余丢弃。"""
    rows = []
    for e in raw:
        v = e["value"]
        pct = v * 100 if 0 < v <= 1 else (v if 1 < v <= 100 else None)
        if pct is None:
            continue
        dims = e["dims"]
        type_m = bench_m = None
        party_axes = []   # 对手方候选轴（客户/供应商等）
        scope_axes = []   # 分母限定轴：产品/地域（分部轴只是归属注记，剥掉即可）
        for axis, m in dims.items():
            if "ConcentrationRiskByType" in axis:
                type_m = m
            elif "Benchmark" in axis:
                bench_m = m
            elif "StatementBusinessSegments" in axis:
                continue  # 归属注记：NVDA「客户A占总营收22%（计算分部）」分母仍是总营收
            elif any(k in axis for k in ("ProductOrService",
                                         "StatementGeographical")):
                scope_axes.append((axis, m))
            else:
                party_axes.append((axis, m))
        # 分母限定披露（NVDA「美国终端客户占新加坡开票的受管制产品营收
        # 99%」）：分母不是公司总营收，画出来必错，整行丢弃
        if party_axes and scope_axes:
            continue
        geo_party = False
        if party_axes:
            party_axis, party_m = party_axes[0][0], party_axes[0][1]
            if len(party_axes) > 1:
                continue  # 多重对手方限定，同属分母限定披露
        elif len(scope_axes) == 1 and "Geographical" in scope_axes[0][0]:
            # 纯地域集中度（RKLB 美国占营收 79%）：地域即对手方，类型强制地域
            party_axis, party_m = scope_axes[0]
            geo_party = True
        elif scope_axes:
            continue  # 纯产品限定的样板行
        else:
            party_axis, party_m = None, None
        if party_m is None and pct >= 99:
            continue  # 「基本全部…」的样板行，无对手方、无信息量
        base = _member_base(party_m) if party_m else None
        aggregate = bool(geo_party or (party_m and _AGG_RE.search(base)))
        party = (_party_norm(base) or CONC_PARTY_ZH.get(base)
                 or _member_label(party_m)) if party_m \
            else (_zh(CONC_TYPE_ZH, type_m or "") or "未具名")
        # 类型：地域轴对手方一律地域（轴身份优先于成员名字符串匹配）
        row_type = "地域" if geo_party \
            else _zh(CONC_TYPE_ZH, (type_m or "") + (party_m or "")) or "集中度"
        rows.append({
            "party": party,
            "type": row_type,
            "benchmark": _zh(CONC_BENCH_ZH, bench_m or "") or (_member_label(bench_m) if bench_m else "未注明基准"),
            "aggregate": aggregate,
            "pct": round(pct, 1),
            "start": e["start"], "end": e["end"], "days": e["days"],
            "annual": e["days"] >= 340,
        })
    if not rows:
        return None

    def is_rev(r):
        return r["benchmark"] == "营收"

    # 最新清单：同 (对手,类型,基准) 取期末最新；同 end 多跨度时年度优先
    # （按 (end, days) 排序，跨度长的后写入胜出），窗口=最新期回看 400 天
    latest_end = max(r["end"] for r in rows)
    cutoff = (date.fromisoformat(latest_end) - timedelta(days=400)).isoformat()
    dedup: dict = {}
    for r in sorted(rows, key=lambda r: (r["end"], r["days"])):
        dedup[(r["party"], r["type"], r["benchmark"])] = r
    latest = sorted((r for r in dedup.values() if r["end"] >= cutoff),
                    key=lambda r: -r["pct"])

    # 趋势：营收基准的单一客户按年度期加总；先按对手方去重再求和——
    # 跨申报改名（CustomerA vs CustomerOne）和比较期重复会双倍计数
    trend_map: dict = {}
    for r in rows:
        if r["annual"] and is_rev(r) and r["type"] == "客户" and not r["aggregate"]:
            trend_map.setdefault(r["end"], {})
            prev = trend_map[r["end"]].get(r["party"])
            if prev is None or r["pct"] > prev:
                trend_map[r["end"]][r["party"]] = r["pct"]
    trend = [{"end": k, "label": _label(k),
              "sum": round(sum(parties.values()), 1), "count": len(parties)}
             for k, parties in sorted(trend_map.items())]

    # 风险分级：单一客户优先；只有群体口径时降为「合计」措辞
    rev_latest = [r for r in latest if is_rev(r) and r["type"] == "客户"]
    singles = [r for r in rev_latest if not r["aggregate"]]
    pool = singles or rev_latest
    top = max((r["pct"] for r in pool), default=0)
    # 陈旧披露：最近一次集中度披露远早于当下（公司多半已无 ≥10% 集中）
    stale = (date.today() - date.fromisoformat(latest_end)).days > 550
    risk = {"level": "stale" if stale
            else "high" if top >= 30 else "medium" if top >= 10 else "low",
            "top_pct": top,
            "top_party": next((r["party"] for r in pool if r["pct"] == top), None),
            "aggregate": bool(pool and not singles),
            "last_end": latest_end}
    return {"risk": risk, "latest": latest, "trend": trend}


def _member_base(name: str) -> str:
    """剥掉 Member/SegmentMember 类后缀作为合并键：同一分部跨申报改名
    （NVDA 的 GraphicsMember -> GraphicsSegmentMember）要归成一个系列，
    否则图上同一分部会中途换色、图例按名去重后对不上色块。"""
    return re.sub(r"(Segments?Member|SegmentMember|Member)$", "", name) or name


def _member_label(name: str) -> str:
    base = _member_base(name)
    if base in MEMBER_ZH:
        return MEMBER_ZH[base]
    # 驼峰拆词："RestOfAsiaPacific" -> "Rest Of Asia Pacific"
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base) or name


def _label(end: str) -> str:
    d = date.fromisoformat(end)
    return f"{MONTHS[d.month - 1]} '{d.year % 100:02d}"


async def _segments_cached(ticker: str, email: str) -> tuple[dict, dict]:
    hit = _seg_cache.get(ticker)
    if hit and time.time() - hit[0] < SEG_TTL:
        return hit[1], hit[2]
    lock = _seg_locks.setdefault(ticker, asyncio.Lock())
    async with lock:
        hit = _seg_cache.get(ticker)
        if hit and time.time() - hit[0] < SEG_TTL:
            return hit[1], hit[2]
        info = await edgar.company_info(ticker, email)
        try:
            data = await asyncio.to_thread(
                build_segments, ticker, email, info["cik"], 10)
        except SegmentsError as e:
            # 上游瞬态错误（限速/维护）不能报成「没有数据」
            raise edgar.EdgarError(502 if e.transient else 404, str(e))
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as e:
            raise edgar.EdgarError(502, f"SEC 数据请求失败：{type(e).__name__}，请稍后重试")
        now = time.time()
        for k in [k for k, (ts, *_) in _seg_cache.items() if now - ts > SEG_TTL]:
            del _seg_cache[k]
        if len(_seg_cache) >= CACHE_MAX:
            del _seg_cache[min(_seg_cache, key=lambda k: _seg_cache[k][0])]
        _seg_cache[ticker] = (now, data, info)
        return data, info


def _reshape_axis(data: dict, freq: str, years: int) -> dict | None:
    kind = "quarterly" if freq == "quarterly" else "annual"
    cells = data[kind]
    if not cells:
        return None
    n = years if freq == "annual" else 4 * years
    ends = sorted(cells)[-n:]
    # 先按剥后缀的 base 归并改名成员（改名的新旧世代不会同期出现，
    # 取先见值），再按窗口内合计排序；超过上限的折叠进「其他」
    merged: dict[str, dict[str, float]] = {}
    for e in ends:
        row = merged.setdefault(e, {})
        for m, v in cells[e]["members"].items():
            row.setdefault(_member_base(m), v)
    sums: dict[str, float] = {}
    for e in ends:
        for m, v in merged[e].items():
            sums[m] = sums.get(m, 0) + v
    ranked = sorted(sums, key=lambda m: -sums[m])
    kept, folded = ranked[:MAX_MEMBERS], ranked[MAX_MEMBERS:]

    series = {m: [merged[e].get(m) for e in ends] for m in kept}
    other = [sum(merged[e].get(m, 0) for m in folded) or None
             for e in ends] if folded else None
    return {
        "periods": [{"end": e, "label": _label(e)} for e in ends],
        "members": [{"key": m, "label": _member_label(m)} for m in kept],
        "series": [series[m] for m in kept],
        "other": other,
        "total": [cells[e]["total"] for e in ends],
        "reconciled": [cells[e]["reconciled"] for e in ends],
        "derived": [cells[e]["derived"] for e in ends],
    }


@router.get("/api/segments/{ticker}")
async def segments(
    ticker: str,
    freq: str = Query(default="quarterly", pattern="^(quarterly|annual)$"),
    years: int = Query(default=3, ge=1, le=10),
):
    ticker = ticker.strip().upper()
    if not re.match(r"^[A-Z.\-]{1,10}$", ticker):
        raise edgar.EdgarError(400, "股票代码格式不对")
    email = edgar.contact_email()
    data, info = await _segments_cached(ticker, email)
    axes = []
    for key in ("product", "segment", "geo"):
        if key not in data["axes"]:
            continue
        shaped = _reshape_axis(data["axes"][key], freq, years)
        if shaped and len(shaped["periods"]) >= 1:
            axes.append({"key": key, "label": AXIS_LABELS[key], **shaped})
    concentration = _conc_reshape(data.get("concentration") or [])
    if not axes and concentration is None:
        raise edgar.EdgarError(
            404, f"{ticker} 的申报里没有可用的{('季度' if freq == 'quarterly' else '年度')}分部营收数据")
    return {"ticker": ticker, "name": info.get("name") or "",
            "freq": freq, "years": years, "axes": axes,
            "concentration": concentration}
