# -*- coding: utf-8 -*-
"""财务图表数据服务：把 fetch_facts 的 XBRL 序列重塑成图表用的对齐数组。

GET /api/financials/{ticker}?freq=quarterly|annual&years=N
返回 periods（期末+标签）与各科目等长数组（缺数为 null），
利润率、FCF、总债务等派生口径在这里算好，前端只管画。
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
from valuation.fetch_facts import FactsError, build_facts  # noqa: E402

router = APIRouter()

FACTS_TTL = 6 * 3600   # companyfacts 一天最多更新几次，6 小时内直接用缓存
CACHE_MAX = 256        # 防爬全量代码把内存吃满
_facts_cache: dict[str, tuple[float, dict, dict]] = {}
_fetch_locks: dict[str, asyncio.Lock] = {}  # 按 ticker 加锁：防同票惊群，不同票并行

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


async def _facts_and_info(ticker: str, email: str) -> tuple[dict, dict]:
    hit = _facts_cache.get(ticker)
    if hit and time.time() - hit[0] < FACTS_TTL:
        return hit[1], hit[2]
    lock = _fetch_locks.setdefault(ticker, asyncio.Lock())
    async with lock:
        hit = _facts_cache.get(ticker)
        if hit and time.time() - hit[0] < FACTS_TTL:
            return hit[1], hit[2]
        # 先查公司信息：走 edgar 的 24h 缓存代码表拿 CIK，避免 build_facts
        # 再下载一遍 700KB 的 company_tickers.json
        info = await edgar.company_info(ticker, email)
        try:
            facts = await asyncio.to_thread(build_facts, ticker, email, info["cik"])
        except FactsError as e:
            raise edgar.EdgarError(404, str(e))
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
            raise edgar.EdgarError(502, f"SEC 数据请求失败：{type(e).__name__}，请稍后重试")
        now = time.time()
        for k in [k for k, (ts, *_) in _facts_cache.items() if now - ts > FACTS_TTL]:
            del _facts_cache[k]
        if len(_facts_cache) >= CACHE_MAX:
            del _facts_cache[min(_facts_cache, key=lambda k: _facts_cache[k][0])]
        _facts_cache[ticker] = (now, facts, info)
        return facts, info


def _label(end: str) -> str:
    d = date.fromisoformat(end)
    return f"{MONTHS[d.month - 1]} '{d.year % 100:02d}"


def _nearest_instant(inst: dict, ends: list[str], window: int = 10) -> list:
    """资产负债表时点对齐到损益期末：优先同日，否则取窗口内最近的一天。"""
    keys = sorted(inst)
    out = []
    for e in ends:
        if e in inst:
            out.append(inst[e])
            continue
        ed = date.fromisoformat(e)
        best, best_gap = None, window + 1
        for k in keys:
            gap = abs((date.fromisoformat(k) - ed).days)
            if gap < best_gap:
                best, best_gap = inst[k], gap
        out.append(best)
    return out


def _add(*rows):
    """逐期相加，全部为 null 的期保持 null（区别于 0）。"""
    out = []
    for vals in zip(*rows):
        present = [v for v in vals if v is not None]
        out.append(sum(present) if present else None)
    return out


def _sub(a, b):
    return [x - y if x is not None and y is not None else None for x, y in zip(a, b)]


def _ratio(num, den):
    return [round(x / y, 4) if x is not None and y not in (None, 0) else None
            for x, y in zip(num, den)]


def _total_debt(inst) -> list:
    """总债务的组合规则（四个不可混加口径，见 fetch_facts 注释）：
    - 长期腿：优先真非流动口径；只有 LongTermDebt(含当期到期) 时用它，
      流动腿只补 CP/短借（当期到期已在总口径里，再加 DebtCurrent 会双计）
    - 流动腿：DebtCurrent（含 CP）优先，否则 当期到期+CP+短借 逐项拼
    - 没有任何长期腿数据的期直接给 null：宁缺勿错（KO 换标签曾把
      42B 债务画成只剩 1B 商业票据的假去杠杆悬崖）"""
    noncur = inst("lt_debt_noncurrent")
    lt_total = inst("lt_debt_total")
    lt_cur = inst("lt_debt_current")
    debt_cur = inst("debt_current")
    cp = inst("commercial_paper")
    st_borrow = inst("st_borrowings")

    out = []
    rows = zip(noncur, lt_total, lt_cur, debt_cur, cp, st_borrow)
    for nc, lt, lc, dc, c, sb in rows:
        if nc is not None:
            cur = dc if dc is not None else None
            if cur is None:
                parts = [v for v in (lc, c, sb) if v is not None]
                cur = sum(parts) if parts else None
            out.append(nc + (cur or 0))
        elif lt is not None:
            if dc is not None and lc is not None:
                extra = max(dc - lc, 0)
            else:
                parts = [v for v in (c, sb) if v is not None]
                extra = sum(parts) if parts else 0
            out.append(lt + extra)
        else:
            out.append(None)
    # 最后的兜底：SOFI 2023 起资产负债表 Debt 行只标长短期合并口径
    combined = inst("debt_combined")
    return [t if t is not None else c for t, c in zip(out, combined)]


def _reshape(facts: dict, info: dict, freq: str, years: int) -> dict:
    suffix = "_quarterly" if freq == "quarterly" else "_annual"
    rev = facts["revenue" + suffix]
    if not rev:
        raise edgar.EdgarError(404, f"{facts['ticker']} 没有{('季度' if freq == 'quarterly' else '年度')}营收数据")
    # 陈旧守卫：营收序列若明显落后于净利/现金流（换了未收录的标签），
    # 宁可报错也不能把十年前的数据当最新画出来
    freshest = max((max(facts.get(k + suffix) or {"": ""})
                    for k in ("net_income", "cfo")), default="")
    if freshest and (date.fromisoformat(freshest) - date.fromisoformat(max(rev))).days > 400:
        raise edgar.EdgarError(
            422, f"{facts['ticker']} 的营收标签未收录（最新营收期 {max(rev)}，"
                 f"其他科目已到 {freshest}），图表暂不支持该公司")
    # 按期数截取而不是按天数窗口：53 周财年会让同一设置下不同公司差一根柱
    n = years if freq == "annual" else 4 * years
    ends = sorted(rev)[-n:]

    def dur(name):
        d = facts.get(name + suffix) or {}
        return [d.get(e) for e in ends]

    def inst(name):
        return _nearest_instant(facts.get(name + "_instant") or {}, ends)

    revenue = dur("revenue")
    cogs = dur("cogs")
    gross = dur("gross_profit")
    # GrossProfit 很多公司不标，回退用 营收-营业成本
    gross = [g if g is not None else (r - c if r is not None and c is not None else None)
             for g, r, c in zip(gross, revenue, cogs)]
    # 银行报表没有毛利概念：SOFI 恰好有条成本标签，硬算会得出 82% 的
    # 假毛利率（申报里不存在 gross profit 这一行），整列压掉
    if facts.get("bank_format"):
        cogs = [None] * len(ends)
        gross = [None] * len(ends)
    # SG&A 合并披露优先；拆开披露的公司用 销售营销+管理 合成
    sga = [s if s is not None else (sm + ga if sm is not None and ga is not None else None)
           for s, sm, ga in zip(dur("sga"), dur("sm"), dur("ga"))]
    op_income = dur("op_income")
    # 运营费用：披露值优先，缺了用 毛利-营业利润 倒推；银行格式两者皆无，
    # 退到非利息支出合计（SOFI 等的费用主体）
    nie = dur("noninterest_expense")
    opex = [o if o is not None
            else (g - oi if g is not None and oi is not None else n)
            for o, g, oi, n in zip(dur("opex"), gross, op_income, nie)]
    net_income = dur("net_income")

    ocf = dur("cfo")
    capex = dur("capex")  # PaymentsToAcquire* 是正数（流出）
    fcf = _sub(ocf, capex)

    cash = inst("cash")
    # 证券类：基线口径优先。NVDA 2026 起把该行拆成 债券+股票 两个新标签，
    # 回填时两腿都要（只跟债券腿会把 +18B 的组合增长画成缩水）；
    # 股票腿只在债券腿同期存在（拆分特征成立）时并入，防附注级零散值漏入
    debt_st, eq_st = inst("debt_securities_st"), inst("equity_securities_st")
    st_fill = [d + (e or 0) if d is not None else None
               for d, e in zip(debt_st, eq_st)]
    st_sec = [a if a is not None else b
              for a, b in zip(inst("st_securities"), st_fill)]
    lt_sec = [a if a is not None else b
              for a, b in zip(inst("lt_securities"), inst("debt_securities_lt"))]
    securities = _add(st_sec, lt_sec)
    # 无分类资产负债表的银行（SOFI）整行标 OtherInvestments——该标签太泛，
    # 只在分类口径整列全空时才启用
    if all(v is None for v in securities):
        securities = inst("securities_unclassified")

    return {
        "ticker": facts["ticker"],
        "cik": facts["cik"],
        "name": info.get("name") or "",
        "fiscalYearEnd": info.get("fiscalYearEnd") or "",
        "freq": freq,
        "years": years,
        "periods": [{"end": e, "label": _label(e)} for e in ends],
        "income": {
            "revenue": revenue,
            "cogs": cogs,
            "gross_profit": gross,
            "rnd": dur("rnd"),
            "sga": sga,
            "opex": opex,
            "op_income": op_income,
            "pretax_income": dur("pretax_income"),
            "income_tax": dur("income_tax"),
            "net_income": net_income,
            "margins": {
                "gross": _ratio(gross, revenue),
                "operating": _ratio(op_income, revenue),
                "net": _ratio(net_income, revenue),
            },
        },
        "cashflow": {"ocf": ocf, "capex": capex, "fcf": fcf},
        "balance": {"cash": cash, "securities": securities, "total_debt": _total_debt(inst)},
        # 营业外/一次性组件：前端拆解瀑布图的营业外损益并标记一次性主导的期
        "oneoff": {k: dur(k) for k in
                   ("equity_inv_gain", "interest_income", "interest_expense_nonop",
                    "fx_gain", "other_nonop", "restructuring", "impairment",
                    "litigation", "disposal_gain")},
        "ttm": facts.get("ttm") or {},
    }


@router.get("/api/financials/{ticker}")
async def financials(
    ticker: str,
    freq: str = Query(default="quarterly", pattern="^(quarterly|annual)$"),
    years: int = Query(default=3, ge=1, le=10),
):
    ticker = ticker.strip().upper()
    if not re.match(r"^[A-Z.\-]{1,10}$", ticker):
        raise edgar.EdgarError(400, "股票代码格式不对")
    email = edgar.contact_email()
    facts, info = await _facts_and_info(ticker, email)
    return _reshape(facts, info, freq, years)
