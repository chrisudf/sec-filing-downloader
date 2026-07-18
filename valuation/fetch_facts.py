# -*- coding: utf-8 -*-
"""SEC XBRL companyfacts 取数：年度+季度序列，自动推导 Q4，输出 TTM。

用法: python fetch_facts.py TICKER OUT.json EMAIL
- 损益类 TTM = 最近四个离散季度加总（校验连续性）
- 现金流类 TTM = 最新年度 + 本财年YTD - 上年同期YTD（10-Q 现金流表是累计口径）
- 公司会更换 XBRL 标签，同一科目合并所有候选标签、同期取 filed 最新值
"""
import json
import sys
import time
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
# 缺 us-gaap 时重试一次排除偶发坏响应，再区分两种真实原因给出可诊断的报错
for _attempt in range(2):
    _resp = httpx.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK:010d}.json",
                      headers=H, timeout=90).json()
    _all = _resp["facts"]
    if "us-gaap" in _all:
        break
    time.sleep(2)
if "us-gaap" not in _all:
    raise SystemExit(
        f"{TICKER}（CIK {CIK}，{_resp.get('entityName', '?')}）没有 us-gaap 数据。两种常见原因：\n"
        f"- 外国发行人按 IFRS 申报（ifrs-full 暂不支持，如 TSM）\n"
        f"- ticker 映射指向重组后的新实体（如控股公司架构调整），历史财务留在旧 CIK 下——"
        f"可在 https://www.sec.gov/cgi-bin/browse-edgar 按公司名搜旧实体确认")
facts = _all["us-gaap"]


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
    # 刚发完 10-K、尚无本财年 10-Q 时没有 YTD 可用——此刻 TTM 恰好等于最新年度，
    # 返回 None 会让整条估值流水线在这 ~3 个月里崩掉
    if not cur:
        return {"value": a_val, "note": f"= FY({a_end_s})（尚无本财年 YTD）"}
    s, e, v_cur = max(cur, key=lambda x: x[1])
    span = (date.fromisoformat(e) - date.fromisoformat(s)).days
    prior = [v for (ps, pe), v in ytd.items()
             if abs((date.fromisoformat(e) - date.fromisoformat(pe)).days - 364) <= 10
             and abs((date.fromisoformat(pe) - date.fromisoformat(ps)).days - span) <= 10]
    if not prior:
        return {"value": a_val, "note": f"= FY({a_end_s})（缺上年同期 YTD，退回最新年度口径）"}
    return {"value": a_val + v_cur - prior[0], "note": f"FY({a_end_s}) + YTD至{e} - 上年同期YTD"}


# Q4 = 年度 - 前三季 只对可加总的流量科目成立；加权平均股数/EPS 不可加总
# （FY 加权股数 ≈ 四季平均而非加总，硬推会得到大负数并被当成最新股数消费）
DERIVE_Q4 = {"revenue", "op_income", "net_income"}

out = {"ticker": TICKER, "cik": CIK}
for name in TAGS:
    if name in INSTANT:
        out[name + "_instant"] = pick(TAGS[name], "instant")
        continue
    annual = pick(TAGS[name], "annual")
    quarterly = pick(TAGS[name], "quarterly")
    if name in DERIVE_Q4:
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

# ---- 发行人适配性诊断：当前框架假设"美国本土 + 非金融 + 申报 OperatingIncomeLoss"----
# 实测 25 只样本中 16 只不满足（全部银行/券商/保险、能源巨头 XOM、REIT 部分、外国发行人）。
# 数据写完再退出：单独取数仍可拿到部分 facts，流水线则拿到分类后的人话原因。
problems = []
_latest = lambda d: max(d) if d else ""
op_latest = max(_latest(out["op_income_annual"]), _latest(out["op_income_quarterly"]))
rev_latest = max(_latest(out["revenue_annual"]), _latest(out["revenue_quarterly"]))
is_financial = "RevenuesNetOfInterestExpense" in facts or "InterestIncomeExpenseNet" in facts
if not op_latest or (rev_latest and
        (date.fromisoformat(rev_latest) - date.fromisoformat(op_latest)).days > 400):
    if is_financial:
        problems.append("金融类发行人（银行/券商/保险口径）：不申报营业利润，且 CFO 含贷款投放、"
                        "FCF=CFO-Capex 无意义——需要 financials 模式（P/E + P/TBV×ROTE）")
    else:
        problems.append(f"OperatingIncomeLoss 缺失或停报（最新 {op_latest or '无'}，"
                        "能源/REIT 等行业常见），OPM 情景与 SOTP 无法计算")
if is_financial and out["revenue_annual"]:
    rn = pick(["RevenuesNetOfInterestExpense"], "annual")
    if rn:
        a_val = sorted(out["revenue_annual"].items())[-1][1]
        rn_val = sorted(rn.items())[-1][1]
        if a_val < 0.8 * rn_val:
            problems.append(f"营收 tag 只覆盖部分收入（{a_val/1e9:.1f}B，总净收入应为 {rn_val/1e9:.1f}B）"
                            "——静默错误风险，金融类应改用 RevenuesNetOfInterestExpense")
if len(q) < 4:
    problems.append("季度营收不足 4 期（外国发行人 6-K 通常无 XBRL 季度数据），TTM 无法计算")
if problems:
    print(f"\n{TICKER} 不适配当前估值框架：", file=sys.stderr)
    for p in problems:
        print(f"- {p}", file=sys.stderr)
    sys.exit(1)
