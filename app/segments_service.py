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
from datetime import date
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
    if not axes:
        raise edgar.EdgarError(
            404, f"{ticker} 的申报里没有可用的{('季度' if freq == 'quarterly' else '年度')}分部营收数据")
    return {"ticker": ticker, "name": info.get("name") or "",
            "freq": freq, "years": years, "axes": axes}
