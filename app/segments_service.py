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
from .common import (FREQ_PATTERN, YEARS_MAX, YEARS_MIN, get_or_fetch,
                     period_label, validate_ticker)
from .zh_labels import (AXIS_LABELS, CONC_PARTY_ZH, CONC_TYPE_ZH,
                        CONC_BENCH_ZH, MAX_MEMBERS, _AGG_RE, _member_base,
                        _member_label, _party_norm, _zh)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from valuation.fetch_segments import SegmentsError, build_segments  # noqa: E402

router = APIRouter()

SEG_TTL = 6 * 3600
CACHE_MAX = 128
_seg_cache: dict[str, tuple[float, tuple[dict, dict]]] = {}
_seg_locks: dict[str, asyncio.Lock] = {}

async def _segments_cached(ticker: str, email: str) -> tuple[dict, dict]:
    async def fetch():
        info = await edgar.company_info(ticker, email)
        try:
            data = await asyncio.to_thread(
                build_segments, ticker, email, info["cik"], 10)
        except SegmentsError as e:
            # 上游瞬态错误（限速/维护）不能报成「没有数据」
            raise edgar.EdgarError(502 if e.transient else 404, str(e))
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as e:
            raise edgar.EdgarError(502, f"SEC 数据请求失败：{type(e).__name__}，请稍后重试")
        return data, info

    return await get_or_fetch(_seg_cache, _seg_locks, ticker,
                              SEG_TTL, CACHE_MAX, fetch)


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
            elif "Range" in axis:
                continue  # 区间注记（Min/Max 两条一组），LLY「三大批发商各占 16-24%」
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
        if bench_m is None:
            # 无基准轴的行不可解读（GS 的信贷组合构成披露曾灌进十几行
            # 「占未注明基准 100%」），宁缺勿错整行丢弃
            continue
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
            "pct": round(pct, 1), "pct_lo": None,
            "start": e["start"], "end": e["end"], "days": e["days"],
            "annual": e["days"] >= 340,
        })
    if not rows:
        return None

    # Min/Max 区间归并：同（对手,类型,基准,期末,跨度）取上限为 pct、
    # 下限记 pct_lo（风险分级用上限，展示为「16-24%」）
    grouped: dict = {}
    for r in rows:
        k = (r["party"], r["type"], r["benchmark"], r["end"], r["days"])
        g = grouped.get(k)
        if g is None:
            grouped[k] = r
        else:
            hi, lo = max(g["pct"], r["pct"]), min(g["pct"], r["pct"])
            g["pct"] = hi
            if hi != lo:
                g["pct_lo"] = lo
    rows = list(grouped.values())

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
                    key=lambda r: -r["pct"])[:15]

    # 趋势：营收基准的单一客户按年度期加总；先按对手方去重再求和——
    # 跨申报改名（CustomerA vs CustomerOne）和比较期重复会双倍计数
    trend_map: dict = {}
    for r in rows:
        if r["annual"] and is_rev(r) and r["type"] == "客户" and not r["aggregate"]:
            trend_map.setdefault(r["end"], {})
            prev = trend_map[r["end"]].get(r["party"])
            if prev is None or r["pct"] > prev:
                trend_map[r["end"]][r["party"]] = r["pct"]
    trend = [{"end": k, "label": period_label(k),
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


def _reshape_axis(data: dict, freq: str, years: int) -> dict | None:
    kind = "quarterly" if freq == "quarterly" else "annual"
    cells = data[kind]
    if not cells:
        return None
    n = years if freq == "annual" else 4 * years
    ends = sorted(cells)[-n:]
    # 窗口约束：Q4 被宁缺勿错丢弃后不许拿窗口外老季度凑数（GOOG 曾把
    # 「3 年」拉成 3.8 年且四个 Dec 旺季无痕消失，与损益卡期间错位）
    latest_end = ends[-1]
    cutoff = (date.fromisoformat(latest_end)
              - timedelta(days=int(years * 366))).isoformat()
    ends = [e for e in ends if e >= cutoff]
    # 窗口内缺失的期补 null 占位（与集中度趋势的断线呈现对齐），
    # 类目轴等距画柱时缺季才有视觉痕迹
    span = 95 if kind == "quarterly" else 365
    grid: list[tuple[str, bool]] = []
    prev = None
    for e in ends:
        if prev is not None:
            gap = (date.fromisoformat(e) - date.fromisoformat(prev)).days
            k = min(round(gap / span), 8)
            # 占位期末在两个真实期之间线性插值（固定步进会把 Dec 30 推成 Jan 3）
            for j in range(1, k):
                pe = (date.fromisoformat(prev)
                      + timedelta(days=round(gap * j / k))).isoformat()
                grid.append((pe, True))
        grid.append((e, False))
        prev = e
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

    def cell_vals(fn):
        return [None if ph else fn(e) for e, ph in grid]

    series = {m: cell_vals(lambda e, _m=m: merged[e].get(_m)) for m in kept}
    other = cell_vals(lambda e: sum(merged[e].get(m, 0) for m in folded) or None) \
        if folded else None
    return {
        "periods": [{"end": e, "label": period_label(e), "placeholder": ph}
                    for e, ph in grid],
        "members": [{"key": m, "label": _member_label(m)} for m in kept],
        "series": [series[m] for m in kept],
        "other": other,
        "total": cell_vals(lambda e: cells[e]["total"]),
        "reconciled": cell_vals(lambda e: cells[e]["reconciled"]),
        "derived": [False if ph else cells[e]["derived"] for e, ph in grid],
    }


@router.get("/api/segments/{ticker}")
async def segments(
    ticker: str,
    freq: str = Query(default="quarterly", pattern=FREQ_PATTERN),
    years: int = Query(default=3, ge=YEARS_MIN, le=YEARS_MAX),
):
    ticker = validate_ticker(ticker)
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
    # 结构性缺 instance 的申报被逐份跳过（fetch_segments），这里如实上浮：
    # 沉默截断会让「已覆盖全部申报」成为假象
    warning = None
    skipped = data.get("skipped") or []
    if skipped:
        warning = (f"{len(skipped)} 份申报缺 XBRL instance 已跳过"
                   f"（{skipped[0][0]} 等），对应期间可能缺柱")
    return {"ticker": ticker, "name": info.get("name") or "",
            "freq": freq, "years": years, "axes": axes,
            "concentration": concentration, "warning": warning}
