# -*- coding: utf-8 -*-
"""内部人交易卡片服务。

GET /api/insider/{ticker}?years=1|2
返回已分类的交易行 (新的在前)、画买卖点用的日收盘价、各类汇总。冷取数要
逐份下载 Form 4 (大票一年 50-250 份, 约 10-60 秒), fetch_insider 按 accession
落盘缓存, 这里再加 1 小时内存缓存 —— Form 4 每个交易日都有新的, 不能像
财报那样缓存 6 小时。
"""
from __future__ import annotations

import asyncio
import bisect
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Query

from . import edgar
from .common import get_or_fetch, validate_ticker

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from valuation.fetch_insider import InsiderError, build_insider  # noqa: E402

router = APIRouter()

INS_TTL = 3600
# 股价只用来定位买卖点; Yahoo 卡住时降级成不画点, 不能让整个接口跟着挂住。
# 线程没法取消, 超时后它在后台自己结束 (同 valuation_service 取价的做法)
PRICE_TIMEOUT = 30
CACHE_MAX = 64
YEARS_MAX = 2
CLUSTER_DAYS = 30
# 图上画点的类别; 其余 (扣税/行权/授予/赠与/员工购股/其他) 只进表格
MARKED = ("buy", "private", "sell", "sell_plan")
_cache: dict[str, tuple[float, dict]] = {}
_locks: dict = {}


def _has_cluster(rows: list[dict], days: int = CLUSTER_DAYS) -> bool:
    """是否有 ≥2 个不同的人, 买入日期相距 ≤ days 天。"""
    pts = sorted((date.fromisoformat(r["date"]), r["owner_key"]) for r in rows)
    for i, (d0, k0) in enumerate(pts):
        for d1, k1 in pts[i + 1:]:
            if (d1 - d0).days > days:
                break
            if k1 != k0:
                return True
    return False


def summarize(rows: list[dict]) -> dict:
    """各类别的笔数 / 人数 / 金额; 买入另给最近一笔与是否多人买入。"""
    out = {}
    for kind in MARKED:
        rs = [r for r in rows if r["kind"] == kind]
        out[kind] = {"n": len(rs),
                     "people": len({r["owner_key"] for r in rs}),
                     "value": sum(r["value"] or 0 for r in rs)}
    buys = [r for r in rows if r["kind"] == "buy"]
    out["buy"]["cluster"] = _has_cluster(buys)
    last = max(buys, key=lambda r: (r["date"], r["filed"]), default=None)
    out["buy"]["last"] = ({k: last[k] for k in ("date", "owner", "role", "value")}
                          if last else None)
    out["other_n"] = sum(1 for r in rows if r["kind"] not in MARKED)
    return out


def close_at(dates: list[str], closes: list[float], d: str) -> float | None:
    """d 当天或之前最近一个交易日的收盘 (赠与等可能落在周末)。"""
    i = bisect.bisect_right(dates, d) - 1
    return closes[i] if i >= 0 else None


def markers(rows: list[dict], prices: list[list]) -> list[dict]:
    """图上的点: 同一天同一类别合成一个点 (几位高管同日卖出不叠成一团),
    画在当天收盘价上 —— 申报价格可能是另一种单位 (TSM 台股普通股 vs ADR)
    或拆股前的旧价, 不拿来定位。"""
    if not prices:
        return []
    dates = [p[0] for p in prices]
    closes = [p[1] for p in prices]
    groups: dict[tuple[str, str], dict] = {}
    for i, r in enumerate(rows):
        if r["kind"] not in MARKED:
            continue
        y = close_at(dates, closes, r["date"])
        if y is None:
            continue
        g = groups.setdefault((r["date"], r["kind"]),
                              {"date": r["date"], "kind": r["kind"], "y": y,
                               "value": 0.0, "rows": []})
        g["value"] += r["value"] or 0
        g["rows"].append(i)
    return sorted(groups.values(), key=lambda g: (g["date"], g["kind"]))


def _prices(ticker: str, since: str) -> list[list]:
    """日收盘 [[date, close], ...]; 取不到返回 [] (卡片照出, 只是不画点)。"""
    import yfinance as yf
    start = (date.fromisoformat(since) - timedelta(days=7)).isoformat()
    hist = yf.Ticker(ticker).history(start=start, auto_adjust=False, timeout=15)
    if hist.empty:
        return []
    close = hist["Close"].dropna()
    return [[ts.date().isoformat(), round(float(v), 4)] for ts, v in close.items()]


def filing_url(cik: int, acc: str) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{acc.replace('-', '')}/{acc}-index.htm")


async def _load(ticker: str, years: int, email: str) -> dict:
    async def fetch():
        info = await edgar.company_info(ticker, email)
        since = (date.today() - timedelta(days=365 * years)).isoformat()
        ins_task = asyncio.to_thread(build_insider, ticker, email, info["cik"], years)
        px_task = asyncio.wait_for(asyncio.to_thread(_prices, ticker, since), PRICE_TIMEOUT)
        ins, px = await asyncio.gather(ins_task, px_task, return_exceptions=True)
        if isinstance(ins, InsiderError):
            raise edgar.EdgarError(502 if ins.transient else 404, str(ins))
        if isinstance(ins, (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError)):
            raise edgar.EdgarError(502, f"SEC 数据请求失败：{type(ins).__name__}，请稍后重试")
        if isinstance(ins, BaseException):
            raise ins
        warnings = []
        if isinstance(px, BaseException):
            px = []
        if not px:
            warnings.append("取不到股价，图上不画买卖点（表格照常）")
        if ins["missing"]:
            warnings.append(f"有 {ins['missing']} 份申报本次没拉到，数据不完整，稍后刷新可补齐")
        rows = ins["rows"]
        for r in rows:
            r["url"] = filing_url(info["cik"], r["acc"])
        return {"ticker": ticker, "cik": info["cik"], "name": info["name"],
                "since": ins["since"], "years": years, "prices": px,
                "rows": rows, "markers": markers(rows, px),
                "summary": summarize(rows),
                "warning": "；".join(warnings) or None}

    return await get_or_fetch(_cache, _locks, f"{ticker}:{years}",
                              INS_TTL, CACHE_MAX, fetch)


@router.get("/api/insider/{ticker}")
async def insider(ticker: str, years: int = Query(1, ge=1, le=YEARS_MAX)):
    ticker = validate_ticker(ticker)
    return await _load(ticker, years, edgar.contact_email())
