# -*- coding: utf-8 -*-
"""SEC XBRL companyfacts 取数：年度+季度序列，自动推导 Q4，输出 TTM。

用法: python fetch_facts.py TICKER OUT.json EMAIL
也可作为模块导入: from valuation.fetch_facts import build_facts
- 损益类 TTM = 最近四个离散季度加总（校验连续性）
- 现金流类 TTM = 最新年度 + 本财年YTD - 上年同期YTD（10-Q 现金流表是累计口径）
- 现金流类季度序列同理：离散季度值 = 同财年相邻 YTD 差分补全
- 公司会更换 XBRL 标签，同一科目合并所有候选标签、同期取 filed 最新值
"""
import json
import sys
from datetime import date, timedelta

import httpx

TAGS = {
    # 后两个覆盖公用事业等换标签的公司，否则窗口会锚定在十年前的陈旧序列上；
    # 银行的 RevenuesNetOfInterestExpense 走下面的 OVERRIDE_TAGS（必须压过
    # ASC 606 附注子集，不能平级合并）
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "RegulatedAndUnregulatedOperatingRevenue"],
    "cogs": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "rnd": ["ResearchAndDevelopmentExpense"],
    # SG&A：有的公司合并披露，有的拆成 销售营销 + 管理 两条，三者不能混进同一候选列表
    "sga": ["SellingGeneralAndAdministrativeExpense"],
    "sm": ["SellingAndMarketingExpense"],
    "ga": ["GeneralAndAdministrativeExpense"],
    "opex": ["OperatingExpenses"],
    "op_income": ["OperatingIncomeLoss"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "income_tax": ["IncomeTaxExpenseBenefit"],
    # 银行格式没有 OperatingExpenses，非利息支出是对应的费用合计（图表端回退用）
    "noninterest_expense": ["NoninterestExpense"],
    # ---- 营业外/一次性项目组件（图表端拆解与标色用，全部为新增 key）----
    # 股权投资公允价值变动：GOOG 持 SpaceX/Anthropic 类股权的浮盈浮亏，
    # Q2'26 单季 +99B，把净利润污染到 94% 净利率——必须单独拆出来标示
    "equity_inv_gain": ["EquitySecuritiesFvNiGainLoss", "GainLossOnInvestments"],
    "interest_income": ["InvestmentIncomeInterest"],
    "interest_expense_nonop": ["InterestExpense", "InterestExpenseNonoperating"],
    "fx_gain": ["ForeignCurrencyTransactionGainLossBeforeTax"],
    "other_nonop": ["OtherNonoperatingIncomeExpense"],
    "restructuring": ["RestructuringCharges"],
    "impairment": ["GoodwillImpairmentLoss", "AssetImpairmentCharges",
                   "ImpairmentOfIntangibleAssetsExcludingGoodwill"],
    "litigation": ["LitigationSettlementExpense", "LossContingencyLossInPeriod"],
    "disposal_gain": ["GainLossOnDispositionOfBusiness"],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
              "PaymentsToAcquireProductiveAssets"],
    # 股东回报与股权激励（图表端；现金流量表科目，10-Q 为累计口径）
    "buyback": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "sbc": ["ShareBasedCompensation"],
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
    # ---- 以下为图表端专用口径。基线 key（上面的）一律不改动：
    # 它们喂给估值判断层，候选列表变了会静默改变已有输出 ----
    # NVDA 2026 起改用 DebtSecurities* 标签；单独给出、由图表端按期回填
    "debt_securities_st": ["DebtSecuritiesCurrent"],
    "debt_securities_lt": ["DebtSecuritiesNoncurrent"],
    # 债务四个不可混加的口径分开给，总债务的组合规则在图表端：
    # LongTermDebt(总口径) 含当期到期部分，不能与 DebtCurrent 直接相加；
    # KO 2024 起只标 LongTermDebtAndCapitalLeaseObligations
    "lt_debt_noncurrent": ["LongTermDebtNoncurrent",
                           "LongTermDebtAndCapitalLeaseObligations"],
    "lt_debt_total": ["LongTermDebt"],
    "lt_debt_current": ["LongTermDebtCurrent",
                        "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "debt_current": ["DebtCurrent"],
    "st_borrowings": ["ShortTermBorrowings", "OtherShortTermBorrowings"],
    # NVDA 2026 起把「有价证券」拆成 债券+股票 两行，只跟债券腿会把
    # 投资组合画成缩水（实际 +18B）
    "equity_securities_st": ["EquitySecuritiesFvNi"],
    # SOFI 这类无分类资产负债表的银行把投资证券整行标成 OtherInvestments；
    # 该标签太泛，只在所有分类证券标签全空时才启用（服务端把关）
    "securities_unclassified": ["OtherInvestments"],
    # 保险公司的债券组合常只标 AFS 总口径（MET ~$316B），图表端按覆盖度选源
    "afs_securities_total": ["AvailableForSaleSecuritiesDebtSecurities"],
    # SOFI 2023 起资产负债表 Debt 行只标长短期合并口径
    "debt_combined": ["DebtLongtermAndShorttermCombinedAmount",
                      "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
                      "DebtAndCapitalLeaseObligations", "NotesPayable"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
INSTANT = {"cash", "st_securities", "lt_securities", "lt_debt", "current_debt",
           "commercial_paper", "debt_securities_st", "debt_securities_lt",
           "lt_debt_noncurrent", "lt_debt_total", "lt_debt_current",
           "debt_current", "st_borrowings", "equity_securities_st",
           "securities_unclassified", "debt_combined", "afs_securities_total"}
# 只补缺不覆盖的回退标签：主标签已有的期一律不动（估值管道基线稳定），
# 只填缺失期。AVGO 2019 年起季度净利润只标 ProfitLoss（含少数股东权益）
FILL_TAGS = {
    "net_income": ["ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
}
# 报表主线口径覆盖：该标签出现的期直接替换合并结果。银行同一份申报里
# RevenuesNetOfInterestExpense 是损益表第一行「Total net revenue」，而
# RevenueFromContractWithCustomer 只是 ASC 606 附注的合同收入子集——
# 平级合并会让 SOFI 的营收少报 8 倍。非银行公司不标该口径，零影响
OVERRIDE_TAGS = {
    "revenue": ["RevenuesNetOfInterestExpense"],
}
# 现金流表科目：10-Q 只披露财年累计数，离散季度序列要靠 YTD 差分
YTD_FLOW = {"cfo", "capex", "buyback", "dividends", "sbc"}


class FactsError(ValueError):
    """取数失败（代码不存在 / 无 us-gaap 数据等），信息可直接展示给用户。
    transient=True 表示上游瞬态错误（限速/维护），应映射 5xx 而非「没有数据」。"""

    def __init__(self, msg: str, transient: bool = False):
        self.transient = transient
        super().__init__(msg)


def _headers(email: str) -> dict:
    return {"User-Agent": f"sec-filing-downloader valuation ({email})"}


def resolve_cik(ticker: str, headers: dict) -> int:
    r = httpx.get("https://www.sec.gov/files/company_tickers.json",
                  headers=headers, timeout=60)
    if r.status_code != 200:
        raise FactsError(f"SEC 代码表接口返回 {r.status_code}", transient=True)
    for v in r.json().values():
        if v["ticker"].upper() == ticker:
            return int(v["cik_str"])
    raise FactsError(f"SEC EDGAR 中未找到 {ticker}")


def pick(facts: dict, tag_names, kind):
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


def quarterly_from_ytd(facts: dict, tag_names) -> dict:
    """现金流科目的离散季度值：同一财年（相同 start）内按 end 排序，
    相邻两期间隔 80-100 天时差分；首期本身就是季度长度时直接取值。
    年度数与 YTD 同 start，所以 Q4 = FY - 前三季 YTD 也被这里覆盖。"""
    ytd = pick(facts, tag_names, "ytd")
    by_start: dict = {}
    for (s, e), v in ytd.items():
        by_start.setdefault(s, []).append((e, v))
    rows = {}
    for s, ents in by_start.items():
        ents.sort()
        prev_e, prev_v = None, None
        for e, v in ents:
            if prev_e is None:
                span = (date.fromisoformat(e) - date.fromisoformat(s)).days
                if 80 <= span <= 100:
                    rows[e] = v
            elif 80 <= (date.fromisoformat(e) - date.fromisoformat(prev_e)).days <= 100:
                rows[e] = v - prev_v
            prev_e, prev_v = e, v
    return dict(sorted(rows.items()))


def ttm_via_ytd(facts: dict, tag_names, annual):
    if not annual:
        return None
    a_end_s, a_val = sorted(annual.items())[-1]
    a_end = date.fromisoformat(a_end_s)
    ytd = pick(facts, tag_names, "ytd")
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


def build_facts(ticker: str, email: str, cik: int | None = None) -> dict:
    """cik 可选：服务端已有缓存的 ticker->CIK 映射时直接传入，省一次 700KB 下载。"""
    ticker = ticker.upper()
    headers = _headers(email)
    if cik is None:
        cik = resolve_cik(ticker, headers)
    r = httpx.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                  headers=headers, timeout=90)
    if r.status_code == 404:
        raise FactsError(f"SEC 没有 {ticker} 的 XBRL companyfacts 数据")
    if r.status_code != 200:
        raise FactsError(f"SEC companyfacts 接口返回 {r.status_code}（稍后重试）", transient=True)
    all_facts = r.json()["facts"]
    if "us-gaap" not in all_facts:
        raise FactsError(f"{ticker} 没有 us-gaap 口径数据（可能是 IFRS 外国发行人），暂不支持")
    facts = all_facts["us-gaap"]

    out = {"ticker": ticker, "cik": cik,
           # 银行报表格式（损益表第一行是 Total net revenue）：
           # 没有毛利概念，图表端据此不硬算毛利率
           "bank_format": "RevenuesNetOfInterestExpense" in facts}
    for name in TAGS:
        if name in INSTANT:
            out[name + "_instant"] = pick(facts, TAGS[name], "instant")
            continue
        annual = pick(facts, TAGS[name], "annual")
        quarterly = pick(facts, TAGS[name], "quarterly")
        if name in YTD_FLOW:
            for k, v in quarterly_from_ytd(facts, TAGS[name]).items():
                quarterly.setdefault(k, v)
        for fill_tag in FILL_TAGS.get(name, ()):
            for k, v in pick(facts, [fill_tag], "annual").items():
                annual.setdefault(k, v)
            for k, v in pick(facts, [fill_tag], "quarterly").items():
                quarterly.setdefault(k, v)
        for ov_tag in OVERRIDE_TAGS.get(name, ()):
            annual.update(pick(facts, [ov_tag], "annual"))
            quarterly.update(pick(facts, [ov_tag], "quarterly"))
        if name == "revenue":
            # 子集守卫：保险等行业把 ASC 606 附注子集标在 RFCWC 下
            # （MET 曾因此少报 31.6 倍），同期 Revenues 显著更大时以其为准
            for kind_dict, kind in ((annual, "annual"), (quarterly, "quarterly")):
                for k, v_full in pick(facts, ["Revenues"], kind).items():
                    cur = kind_dict.get(k)
                    if cur is not None and cur < 0.8 * v_full:
                        kind_dict[k] = v_full
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
        r = ttm_via_ytd(facts, TAGS[name], out[name + "_annual"])
        if r:
            ttm[name] = r
    out["ttm"] = ttm
    return out


def main() -> None:
    ticker, out_path, email = sys.argv[1].upper(), sys.argv[2], sys.argv[3]
    try:
        out = build_facts(ticker, email)
    except FactsError as e:
        raise SystemExit(str(e))
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    q = out["revenue_quarterly"]
    print(f"{ticker} (CIK {out['cik']}): 年度 {len(out['revenue_annual'])} 期, 季度 {len(q)} 期, "
          f"最新 {list(q)[-1] if q else '-'}")
    print("TTM:", {k: v.get("value") for k, v in out["ttm"].items()})


if __name__ == "__main__":
    main()
