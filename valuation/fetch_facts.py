# -*- coding: utf-8 -*-
"""SEC XBRL companyfacts 取数：年度+季度序列，自动推导 Q4，输出 TTM。

用法: python fetch_facts.py TICKER OUT.json EMAIL
- 损益类 TTM = 最近四个离散季度加总（校验连续性）；无季度 XBRL 时退回最新财年并注明
- 现金流类 TTM = 最新年度 + 本财年YTD - 上年同期YTD（10-Q 现金流表是累计口径）
- 公司会更换 XBRL 标签，同一科目合并所有候选标签、同期取 filed 最新值

发行人适配（自动检测，输出 mode/taxonomy/currency 字段）：
- standard  : 美国经营性公司（us-gaap，报 OperatingIncomeLoss）
- financials: 银行/券商/fintech（us-gaap，营收=RevenuesNetOfInterestExpense，
              盈利=税前利润/净利，附股东权益/商誉/无形资产供 P/TBV 法）
- IFRS 外国发行人（ifrs-full，如 TSM）与非美元申报（如 ASML 的 EUR）：
  tag 映射 + 现汇折算美元（yfinance），历史序列按同一现汇折算（恒定汇率口径）
"""
import json
import sys
import time
from datetime import date, timedelta

import httpx

TICKER, OUT, EMAIL = sys.argv[1].upper(), sys.argv[2], sys.argv[3]
H = {"User-Agent": f"sec-filing-downloader valuation ({EMAIL})"}

TAGS_STANDARD = {
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
INSTANT_STANDARD = {"cash", "st_securities", "lt_securities", "lt_debt",
                    "current_debt", "commercial_paper"}

# 银行/券商/fintech：损益表没有"营业利润"，顶线是总净收入（含净利息收入），
# 估值走 P/E + P/TBV×ROTE，权益/商誉/无形给 TBV 用
TAGS_FINANCIALS = {
    "revenue": ["RevenuesNetOfInterestExpense"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],  # 仅参考，银行 CFO 含贷款投放
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
INSTANT_FINANCIALS = {"equity", "goodwill", "intangibles", "cash"}

# IFRS 外国发行人（20-F）：概念名映射到与 standard 相同的输出键，下游无感知
TAGS_IFRS = {
    "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
    "op_income": ["ProfitLossFromOperatingActivities"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "eps_diluted": ["DilutedEarningsLossPerShare"],
    "cfo": ["CashFlowsFromUsedInOperatingActivities"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
              "PurchaseOfPropertyPlantAndEquipment"],
    "cash": ["CashAndCashEquivalents"],
    "lt_debt": ["NoncurrentBorrowings", "LongtermBorrowings"],
    "current_debt": ["CurrentBorrowings", "ShorttermBorrowings"],
    "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
    "shares_diluted": ["AdjustedWeightedAverageShares", "WeightedAverageShares"],
}
INSTANT_IFRS = {"cash", "lt_debt", "current_debt", "equity"}


def resolve_cik(ticker: str) -> int:
    m = httpx.get("https://www.sec.gov/files/company_tickers.json", headers=H, timeout=60).json()
    for v in m.values():
        if v["ticker"].upper() == ticker:
            return int(v["cik_str"])
    raise SystemExit(f"SEC EDGAR 中未找到 {ticker}")


CIK = resolve_cik(TICKER)
# 缺可用税则时重试一次排除偶发坏响应，再区分真实原因给出可诊断的报错
for _attempt in range(2):
    _resp = httpx.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK:010d}.json",
                      headers=H, timeout=90).json()
    _all = _resp["facts"]
    if "us-gaap" in _all or "ifrs-full" in _all:
        break
    time.sleep(2)

if "us-gaap" in _all:
    facts, TAXONOMY = _all["us-gaap"], "us-gaap"
elif "ifrs-full" in _all:
    facts, TAXONOMY = _all["ifrs-full"], "ifrs-full"
else:
    raise SystemExit(
        f"{TICKER}（CIK {CIK}，{_resp.get('entityName', '?')}）没有 us-gaap/ifrs-full 数据。"
        f"常见原因：ticker 映射指向重组后的新实体（如控股公司架构调整），历史财务留在旧 CIK 下"
        f"——可在 https://www.sec.gov/cgi-bin/browse-edgar 按公司名搜旧实体确认")


def _newest_end(tag: str) -> str:
    if tag not in facts:
        return ""
    return max((f.get("end", "") for u in facts[tag]["units"].values() for f in u), default="")


# ---- 发行人模式检测：银行/券商申报 RevenuesNetOfInterestExpense、不报（或早已停报）营业利润
if TAXONOMY == "us-gaap":
    _rn_end, _op_end = _newest_end("RevenuesNetOfInterestExpense"), _newest_end("OperatingIncomeLoss")
    if _rn_end and (not _op_end or
                    (date.fromisoformat(_rn_end) - date.fromisoformat(_op_end)).days > 400):
        MODE, TAGS, INSTANT = "financials", TAGS_FINANCIALS, INSTANT_FINANCIALS
    else:
        MODE, TAGS, INSTANT = "standard", TAGS_STANDARD, INSTANT_STANDARD
else:
    MODE, TAGS, INSTANT = "standard", TAGS_IFRS, INSTANT_IFRS

# ---- 货币检测与折算：按营收 tag 里年度期数最多的货币为申报货币，非美元按现汇折算
def _detect_currency() -> str:
    counts: dict[str, int] = {}
    for tag in TAGS["revenue"]:
        if tag not in facts:
            continue
        for unit, rows in facts[tag]["units"].items():
            if unit == "shares" or "/" in unit:
                continue
            n = sum(1 for f in rows if f.get("start") and f.get("end") and
                    350 <= (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days <= 380)
            counts[unit] = counts.get(unit, 0) + n
    if not counts:
        return "USD"
    # 期数最多者胜；打平优先 USD（部分 20-F 附带便利折算的 USD 列）
    return max(counts, key=lambda u: (counts[u], u == "USD"))


CURRENCY = _detect_currency()
FX = 1.0
if CURRENCY != "USD":
    import yfinance as yf
    FX = float(yf.Ticker(f"{CURRENCY}USD=X").fast_info["lastPrice"])
    print(f"{TICKER} 申报货币 {CURRENCY}，按现汇 {FX:.5f} 折算美元（历史序列为恒定汇率口径）",
          file=sys.stderr)

_UNIT_PRIORITY = (CURRENCY, f"{CURRENCY}/shares", "shares")


def pick(tag_names, kind):
    rows = {}
    for tag in tag_names:
        if tag not in facts:
            continue
        units = facts[tag]["units"]
        unit = next((u for u in _UNIT_PRIORITY if u in units), None)
        if unit is None:
            continue
        scale = 1.0 if unit == "shares" else FX
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
                rows[key] = {"val": f["val"] * scale, "filed": f.get("filed", "")}
    return {k: v["val"] for k, v in sorted(rows.items())}


def ttm_via_ytd(tag_names, annual):
    if not annual:
        return None
    a_end_s, a_val = sorted(annual.items())[-1]
    a_end = date.fromisoformat(a_end_s)
    ytd = pick(tag_names, "ytd")
    cur = [(s, e, v) for (s, e), v in ytd.items()
           if 0 <= (date.fromisoformat(s) - a_end).days <= 8 and e > a_end_s]
    # 刚发完年报、尚无本财年 YTD 时，TTM 恰好等于最新年度
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
DERIVE_Q4 = {"revenue", "op_income", "net_income", "pretax_income"}
INCOME_ITEMS = ("revenue", "pretax_income", "net_income") if MODE == "financials" \
    else ("revenue", "op_income", "net_income")

out = {"ticker": TICKER, "cik": CIK, "mode": MODE, "taxonomy": TAXONOMY,
       "currency": CURRENCY, "fx_to_usd": FX}
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
for name in INCOME_ITEMS:
    q = out[name + "_quarterly"]
    a = out[name + "_annual"]
    done = False
    if len(q) >= 4:
        last4 = list(q.items())[-4:]
        ends = [date.fromisoformat(k) for k, _ in last4]
        gaps = [(ends[i + 1] - ends[i]).days for i in range(3)]
        if all(80 <= g <= 100 for g in gaps):
            ttm[name] = {"value": sum(v for _, v in last4), "quarters": [k for k, _ in last4]}
            done = True
    if not done and a:
        # 外国发行人（20-F/6-K）常无季度 XBRL：退回最新财年并注明口径
        a_end, a_val = sorted(a.items())[-1]
        ttm[name] = {"value": a_val, "note": f"= FY({a_end})（无季度 XBRL，TTM 退回最新财年）"}
    elif not done:
        ttm[name] = {"value": None, "error": "无季度亦无年度数据"}
if MODE == "standard":
    for name in ("cfo", "capex"):
        if name in TAGS:
            r = ttm_via_ytd(TAGS[name], out[name + "_annual"])
            if r:
                ttm[name] = r
out["ttm"] = ttm
out["data_latest"] = max(max(out["revenue_annual"], default=""),
                         max(out["revenue_quarterly"], default=""))

json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
q = out["revenue_quarterly"]
print(f"{TICKER} (CIK {CIK}): mode={MODE} taxonomy={TAXONOMY} currency={CURRENCY} "
      f"年度 {len(out['revenue_annual'])} 期, 季度 {len(q)} 期, "
      f"最新 {out['data_latest'] or '-'}")
print("TTM:", {k: v.get("value") for k, v in ttm.items()})

# ---- 适配性诊断：能修的已自动路由（financials/IFRS/非美元），剩下的给可诊断的报错
problems = []
warnings = []
if any((ttm.get(k) or {}).get("value") is None for k in INCOME_ITEMS):
    missing = [k for k in INCOME_ITEMS if (ttm.get(k) or {}).get("value") is None]
    problems.append(f"核心损益科目无数据：{', '.join(missing)}（保险/REIT 等行业的科目体系暂不支持）")
if MODE == "financials" and not out.get("equity_instant"):
    problems.append("缺股东权益时点数据，P/TBV 法无法计算")
if out["data_latest"]:
    staleness = (date(2000, 1, 1).today() - date.fromisoformat(out["data_latest"])).days
    if staleness > 550:
        warnings.append(f"最新申报期为 {out['data_latest']}（{staleness} 天前）——"
                        "外国发行人 6-K 无 XBRL 时结构化数据只到上一份年报，"
                        "判断层请以财报原文章节里的最新季度数字为准")
for w in warnings:
    print(f"警告: {w}", file=sys.stderr)
if problems:
    print(f"\n{TICKER} 不适配当前估值框架：", file=sys.stderr)
    for p in problems:
        print(f"- {p}", file=sys.stderr)
    sys.exit(1)
