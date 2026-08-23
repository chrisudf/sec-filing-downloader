# -*- coding: utf-8 -*-
"""financials 与 segments 两个图表服务共享的样板：TTL 缓存 + 按键锁、
期间标签、参数校验。

缓存/锁/清扫/驱逐是竞态敏感代码，两个服务的行为必须一致——这类
样板只允许存在这一份；期间标签同理，两张卡同页显示，格式漂移
用户会先于开发者发现。
"""
from __future__ import annotations

import asyncio
import re
import time

from . import edgar

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# 两个端点的参数口径必须一致
FREQ_PATTERN = "^(quarterly|annual)$"
YEARS_MIN, YEARS_MAX = 1, 10
_TICKER_RE = re.compile(r"^[A-Z.\-]{1,10}$")


def period_label(end: str) -> str:
    """'2026-06-27' -> \"Jun '26\"。"""
    return f"{MONTHS[int(end[5:7]) - 1]} '{end[2:4]}"


def validate_ticker(ticker: str) -> str:
    ticker = ticker.strip().upper()
    if not _TICKER_RE.match(ticker):
        raise edgar.EdgarError(400, "股票代码格式不对")
    return ticker


async def get_or_fetch(cache: dict, locks: dict, key: str,
                       ttl: float, max_size: int, fetch):
    """双检查 TTL 缓存 + 按键锁（同键防惊群、不同键并行）+
    过期清扫 + 超容量驱逐最旧。fetch 为无参异步闭包，异常原样上抛
    （失败不落缓存，重试可再触发）。"""
    hit = cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    lock = locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = await fetch()
        now = time.time()
        for k in [k for k, (ts, *_) in cache.items() if now - ts > ttl]:
            del cache[k]
        if len(cache) >= max_size:
            del cache[min(cache, key=lambda k: cache[k][0])]
        cache[key] = (now, value)
        return value
