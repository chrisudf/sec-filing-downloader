# -*- coding: utf-8 -*-
"""SEC XBRL companyfacts 取数：年度+季度序列，自动推导 Q4，输出 TTM。

用法: python fetch_facts.py TICKER OUT.json EMAIL
- 损益类 TTM = 最近四个离散季度加总（校验连续性）
- 现金流类 TTM = 最新年度 + 本财年YTD - 上年同期YTD（10-Q 现金流表是累计口径）
- 公司会更换 XBRL 标签，同一科目合并所有候选标签、同期取 filed 最新值
"""
import json
import sys
from datetime import date, timedelta

import httpx

TICKER, OUT, EMAIL = sys.argv[1].upper(), sys.argv[2], sys.argv[3]
H = {"User-Agent": f"sec-filing-downloader valuation ({EMAIL})"}

TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "SalesRevenueNet"],
    "op_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
              "PaymentsToAcquireProductiveAssets"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "st_securities": ["MarketableSecuritiesCurrent", "ShortTermInvestments",
                      "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    # 长期有价证券：AAPL 这类公司大头在这里，漏掉会把净现金算成净负债
    "lt_securities": ["MarketableSecuritiesNoncurrent",
                      "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
                      "LongTermInvestments"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    # 短期债务两个口径分开给（相互不可加总合并），判断层参考后以 10-Q 原文为准
    "current_debt": ["DebtCurrent", "LongTermDebtCurrent"],
    "commercial_paper": ["CommercialPaper"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
INSTANT = {"cash", "st_securities", "lt_securities", "lt_debt", "current_debt", "commercial_paper"}


def resolve_cik(ticker: str) -> int:
    m = httpx.get("https://www.sec.gov/files/company_tickers.json", headers=H, timeout=60).json()
    for v in m.values():
        if v["ticker"].upper() == ticker:
            return int(v["cik_str"])
    raise SystemExit(f"SEC EDGAR 中未找到 {ticker}")


CIK = resolve_cik(TICKER)
facts = httpx.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK:010d}.json",
                  headers=H, timeout=90).json()["facts"]["us-gaap"]


def pick(tag_names, kind):
    rows = {}
    for tag in tag_names:
        if tag not in facts:
            continue
        units = facts[tag]["units"]
        unit = next((u for u in ("USD", "USD/shares", "shares") if u in units), None)
        if unit is None:
            continue
        for f in units[unit]:
            end, start = f.get("end"), f.get("start")
            if kind == "instant":
                if start is not None:
                    continue
                key = end
            else:
                if start is None:
                    continue
                days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                if kind == "annual" and not 350 <= days <= 380:
                    continue
                if kind == "quarterly" and not 80 <= days <= 100:
                    continue
                key = (start, end) if kind == "ytd" else end
            if key not in rows or f.get("filed", "") > rows[key]["filed"]:
                rows[key] = {"val": f["val"], "filed": f.get("filed", "")}
    return {k: v["val"] for k, v in sorted(rows.items())}


def ttm_via_ytd(tag_names, annual):
    if not annual:
        return None
    a_end_s, a_val = sorted(annual.items())[-1]
    a_end = date.fromisoformat(a_end_s)
    ytd = pick(tag_names, "ytd")
    cur = [(s, e, v) for (s, e), v in ytd.items()
           if 0 <= (date.fromisoformat(s) - a_end).days <= 8 and e > a_end_s]
    if not cur:
        return None
    s, e, v_cur = max(cur, key=lambda x: x[1])
    span = (date.fromisoformat(e) - date.fromisoformat(s)).days
    prior = [v for (ps, pe), v in ytd.items()
             if abs((date.fromisoformat(e) - date.fromisoformat(pe)).days - 364) <= 10
             and abs((date.fromisoformat(pe) - date.fromisoformat(ps)).days - span) <= 10]
    if not prior:
        return None
    return {"value": a_val + v_cur - prior[0], "note": f"FY({a_end_s}) + YTD至{e} - 上年同期YTD"}


out = {"ticker": TICKER, "cik": CIK}
for name in TAGS:
    if name in INSTANT:
        out[name + "_instant"] = pick(TAGS[name], "instant")
        continue
    annual = pick(TAGS[name], "annual")
    quarterly = pick(TAGS[name], "quarterly")
    for a_end, a_val in annual.items():
        ae = date.fromisoformat(a_end)
        in_year = {k: v for k, v in quarterly.items()
                   if timedelta(days=0) < ae - date.fromisoformat(k) < timedelta(days=340)}
        if len(in_year) == 3 and a_end not in quarterly:
            quarterly[a_end] = a_val - sum(in_year.values())
    out[name + "_annual"] = annual
    out[name + "_quarterly"] = dict(sorted(quarterly.items()))

ttm = {}
for name in ("revenue", "op_income", "net_income"):
    q = out[name + "_quarterly"]
    if len(q) >= 4:
        last4 = list(q.items())[-4:]
        ends = [date.fromisoformat(k) for k, _ in last4]
        gaps = [(ends[i + 1] - ends[i]).days for i in range(3)]
        if all(80 <= g <= 100 for g in gaps):
            ttm[name] = {"value": sum(v for _, v in last4), "quarters": [k for k, _ in last4]}
        else:
            ttm[name] = {"value": None, "error": f"季度不连续: gaps={gaps}"}
for name in ("cfo", "capex"):
    r = ttm_via_ytd(TAGS[name], out[name + "_annual"])
    if r:
        ttm[name] = r
out["ttm"] = ttm

json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
q = out["revenue_quarterly"]
print(f"{TICKER} (CIK {CIK}): 年度 {len(out['revenue_annual'])} 期, 季度 {len(q)} 期, "
      f"最新 {list(q)[-1] if q else '-'}")
print("TTM:", {k: v.get("value") for k, v in ttm.items()})
