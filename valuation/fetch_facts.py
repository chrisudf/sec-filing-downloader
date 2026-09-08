# -*- coding: utf-8 -*-
"""SEC XBRL companyfacts 取数：年度+季度序列，自动推导 Q4，输出 TTM。

用法: python fetch_facts.py TICKER OUT.json EMAIL
也可作为模块导入: from valuation.fetch_facts import build_facts
- 损益类 TTM = 最近四个离散季度加总（校验连续性）；无季度 XBRL 时退回最新财年并注明
- 现金流类 TTM = 最新年度 + 本财年YTD - 上年同期YTD（10-Q 现金流表是累计口径）
- 现金流类季度序列同理：离散季度值 = 同财年相邻 YTD 差分补全
- 公司会更换 XBRL 标签，同一科目合并所有候选标签、同期取 filed 最新值

发行人适配（自动检测，输出 mode/taxonomy/currency 字段）：
- standard  : 美国经营性公司（us-gaap，报 OperatingIncomeLoss）
- financials: 银行/券商/fintech（us-gaap，营收=RevenuesNetOfInterestExpense，
              盈利=税前利润/净利，附股东权益/商誉/无形资产供 P/TBV 法）
- IFRS 外国发行人（ifrs-full，如 TSM）与非美元申报（如 ASML 的 EUR）：
  tag 映射 + 现汇折算美元（yfinance），历史序列按同一现汇折算（恒定汇率口径）
"""
import json
import os
import sys
import time
from datetime import date, timedelta

import httpx


# 科目注册表：每个科目一行内聚声明——tags=候选标签（合并时同期取 filed
# 最新）、i=资产负债表时点、y=现金流累计口径（季度靠 YTD 差分）、
# fill=只补缺不覆盖的回退、override=报表主线口径覆盖。
# 曾经的五个平行注册表（TAGS/INSTANT/FILL/OVERRIDE/YTD_FLOW）会漏改：
# 新时点科目忘加 INSTANT 不报错、只静默产出空序列
def _item(tags, i=False, y=False, fill=(), override=(), no_q4=False):
    return {"tags": list(tags), "instant": i, "ytd_flow": y,
            "fill": list(fill), "override": list(override), "no_q4": no_q4}


SPEC = {
    # 后两个覆盖公用事业等换标签的公司，否则窗口会锚定在十年前的陈旧
    # 序列上；银行的 RevenuesNetOfInterestExpense 走 override（必须压过
    # ASC 606 附注子集，不能平级合并——SOFI 曾少报 8 倍）
    "revenue": _item(["RevenueFromContractWithCustomerExcludingAssessedTax",
                      "Revenues", "SalesRevenueNet",
                      "RevenueFromContractWithCustomerIncludingAssessedTax",
                      "RegulatedAndUnregulatedOperatingRevenue"],
                     override=["RevenuesNetOfInterestExpense"]),
    "cogs": _item(["CostOfGoodsAndServicesSold", "CostOfRevenue",
                   "CostOfGoodsSold"]),
    "gross_profit": _item(["GrossProfit"]),
    "rnd": _item(["ResearchAndDevelopmentExpense"]),
    # SG&A：有的公司合并披露，有的拆成 销售营销+管理 两条，不能混进同一候选列表
    "sga": _item(["SellingGeneralAndAdministrativeExpense"]),
    "sm": _item(["SellingAndMarketingExpense"]),
    "ga": _item(["GeneralAndAdministrativeExpense"]),
    "opex": _item(["OperatingExpenses"]),
    "op_income": _item(["OperatingIncomeLoss"]),
    "pretax_income": _item([
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    "income_tax": _item(["IncomeTaxExpenseBenefit"]),
    # 银行格式没有 OperatingExpenses，非利息支出是对应的费用合计（图表端回退用）
    "noninterest_expense": _item(["NoninterestExpense"]),
    # ---- 营业外/一次性项目组件（图表端拆解与标色用）----
    # 股权投资公允价值变动：GOOG 持 SpaceX/Anthropic 类股权的浮盈浮亏，
    # Q2'26 单季 +99B 把净利率推到 94%——必须单独拆出来标示
    "equity_inv_gain": _item(["EquitySecuritiesFvNiGainLoss",
                              "GainLossOnInvestments"]),
    "interest_income": _item(["InvestmentIncomeInterest"]),
    "interest_expense_nonop": _item(["InterestExpense",
                                     "InterestExpenseNonoperating"]),
    "fx_gain": _item(["ForeignCurrencyTransactionGainLossBeforeTax"]),
    "other_nonop": _item(["OtherNonoperatingIncomeExpense"]),
    "restructuring": _item(["RestructuringCharges"]),
    "impairment": _item(["GoodwillImpairmentLoss", "AssetImpairmentCharges",
                         "ImpairmentOfIntangibleAssetsExcludingGoodwill"]),
    "litigation": _item(["LitigationSettlementExpense",
                         "LossContingencyLossInPeriod"]),
    "disposal_gain": _item(["GainLossOnDispositionOfBusiness"]),
    # AVGO 2019 年起季度净利润只标 ProfitLoss（含少数股东权益），
    # fill=只补缺不覆盖：主标签已有的期一律不动（估值管道基线稳定）
    "net_income": _item(["NetIncomeLoss"],
                        fill=["ProfitLoss",
                              "NetIncomeLossAvailableToCommonStockholdersBasic"]),
    "eps_diluted": _item(["EarningsPerShareDiluted"]),
    "cfo": _item(["NetCashProvidedByUsedInOperatingActivities"], y=True),
    "capex": _item(["PaymentsToAcquirePropertyPlantAndEquipment",
                    "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
                    "PaymentsToAcquireProductiveAssets"], y=True),
    # 股东回报与股权激励（图表端；现金流量表科目，10-Q 为累计口径）
    "buyback": _item(["PaymentsForRepurchaseOfCommonStock"], y=True),
    # PaymentsOfOrdinaryDividends 走 fill（只补缺不覆盖）：PFE **只**用这个标签，
    # 主列表两个全空 -> 分红整列为 None；GOOGL 2026-06-30 也只出现在它下面
    # （此前的期用 PaymentsOfDividends），不补就缺最新一季。
    # 不进主列表的原因：它与 PaymentsOfDividends/...CommonStock 在少数重叠期
    # 口径不同（普通股 vs 含优先股，MO 2007 两者差 2 倍），平级合并会让
    # "同期取 filed 最新"在两个口径间随机跳；fill 保证已有值的期一律不动。
    "dividends": _item(["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
                       y=True, fill=["PaymentsOfOrdinaryDividends"]),
    # 股权激励（现金流量表加回项）：卖方 non-GAAP EPS 多半是剔了 SBC 的口径，
    # 而 SBC 是真实成本（稀释已体现在稀释股数里，费用还被剔一次）。取来供判断层
    # 做「SBC 调整后 PE」对照——AMZN/META 这类占净利两位数百分比。后两个候选
    # 覆盖只标分配口径的公司（Rule of 40 的剔 SBC 分需要它们）。
    "sbc": _item(["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense",
                  "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost"],
                 y=True),
    "cash": _item(["CashAndCashEquivalentsAtCarryingValue"], i=True),
    "st_securities": _item(["MarketableSecuritiesCurrent", "ShortTermInvestments",
                            "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
                           i=True),
    # 长期有价证券：AAPL 这类公司大头在这里，漏掉会把净现金算成净负债
    "lt_securities": _item(["MarketableSecuritiesNoncurrent",
                            "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
                            "LongTermInvestments"], i=True),
    "lt_debt": _item(["LongTermDebtNoncurrent", "LongTermDebt"], i=True),
    # 短期债务两个口径分开给（相互不可加总合并），判断层参考后以 10-Q 原文为准
    "current_debt": _item(["DebtCurrent", "LongTermDebtCurrent"], i=True),
    "commercial_paper": _item(["CommercialPaper"], i=True),
    # ---- P/TBV 法（financials 模式）用的净资产口径。standard 模式一并取：
    # 多几个 key 不影响既有消费方，缺了则银行票整条 P/TBV 腿算不出 ----
    "equity": _item(["StockholdersEquity",
                     "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
                    i=True),
    "goodwill": _item(["Goodwill"], i=True),
    "intangibles": _item(["IntangibleAssetsNetExcludingGoodwill",
                          "FiniteLivedIntangibleAssetsNet"], i=True),
    # 时点流通股 = 每股 TBV 的正确分母。TBV 是时点存量，除以当季**加权平均**稀释
    # 股数等于拿流量均值配存量——增发季（SOFI 型一次性摊薄）加权数≈期末股数的
    # 一半，每股 TBV 会被高估近一倍。P/TBV 历史带自 0058 起已改用时点股数
    # （pe_band.ptbv_band），引擎当前点必须同源，否则「当前倍数 vs 历史锚」是两个口径。
    "shares_outstanding": _item(["CommonStockSharesOutstanding"], i=True),
    # ---- 以下为图表端专用口径。基线 key（上面的）候选列表一律不改动：
    # 它们喂给估值判断层，变了会静默改变已有输出 ----
    # NVDA 2026 起改用 DebtSecurities* 标签；单独给出、由图表端按期回填
    "debt_securities_st": _item(["DebtSecuritiesCurrent"], i=True),
    "debt_securities_lt": _item(["DebtSecuritiesNoncurrent"], i=True),
    # 债务四个不可混加的口径分开给，总债务的组合规则在图表端：
    # LongTermDebt(总口径) 含当期到期部分，不能与 DebtCurrent 直接相加；
    # KO 2024 起只标 LongTermDebtAndCapitalLeaseObligations
    "lt_debt_noncurrent": _item(["LongTermDebtNoncurrent",
                                 "LongTermDebtAndCapitalLeaseObligations"], i=True),
    "lt_debt_total": _item(["LongTermDebt"], i=True),
    "lt_debt_current": _item(["LongTermDebtCurrent",
                              "LongTermDebtAndCapitalLeaseObligationsCurrent"],
                             i=True),
    "debt_current": _item(["DebtCurrent"], i=True),
    "st_borrowings": _item(["ShortTermBorrowings", "OtherShortTermBorrowings"],
                           i=True),
    # NVDA 2026 起把「有价证券」拆成 债券+股票 两行，只跟债券腿会把
    # 投资组合画成缩水（实际 +18B）
    "equity_securities_st": _item(["EquitySecuritiesFvNi"], i=True),
    # SOFI 这类无分类资产负债表的银行把投资证券整行标成 OtherInvestments；
    # 该标签太泛，只在所有分类证券标签全空时才启用（服务端把关）
    "securities_unclassified": _item(["OtherInvestments"], i=True),
    # 保险公司的债券组合常只标 AFS 总口径（MET ~$316B），图表端按覆盖度选源
    "afs_securities_total": _item(["AvailableForSaleSecuritiesDebtSecurities"],
                                  i=True),
    # SOFI 2023 起资产负债表 Debt 行只标长短期合并口径；后三个覆盖
    # REIT/保险的无分类资产负债表（O $25B、MET $14.5B 曾整列 null）
    "debt_combined": _item(["DebtLongtermAndShorttermCombinedAmount",
                            "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
                            "DebtAndCapitalLeaseObligations", "NotesPayable"],
                           i=True),
    # 加权平均股本是均值不可加减：Q4=年度-前三季 会推出 -30B 的负股数
    # （原仓库就有的 bug，估值管道取序列末值时可能踩中）
    "shares_diluted": _item(["WeightedAverageNumberOfDilutedSharesOutstanding"],
                            no_q4=True),
}

# IFRS 外国发行人（20-F）：概念名映射到与 standard 相同的输出键，下游无感知。
# Q4 = 年度 - 前三季 只对可加总的流量科目成立，其余科目 no_q4。
SPEC_IFRS = {
    "revenue": _item(["Revenue", "RevenueFromContractsWithCustomers"]),
    "op_income": _item(["ProfitLossFromOperatingActivities"]),
    "net_income": _item(["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"]),
    "eps_diluted": _item(["DilutedEarningsLossPerShare"], no_q4=True),
    "cfo": _item(["CashFlowsFromUsedInOperatingActivities"], no_q4=True),
    "capex": _item(["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                    "PurchaseOfPropertyPlantAndEquipment",
                    "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets"],
                   no_q4=True),
    "cash": _item(["CashAndCashEquivalents"], i=True),
    "lt_debt": _item(["NoncurrentBorrowings", "LongtermBorrowings"], i=True),
    "current_debt": _item(["CurrentBorrowings", "ShorttermBorrowings"], i=True),
    "equity": _item(["EquityAttributableToOwnersOfParent", "Equity"], i=True),
    "shares_diluted": _item(["AdjustedWeightedAverageShares", "WeightedAverageShares"],
                            no_q4=True),
}

# 兼容视图：估值管道、图表服务与测试引用的旧接口，全部由 SPEC 推导
TAGS = {k: v["tags"] for k, v in SPEC.items()}
INSTANT = {k for k, v in SPEC.items() if v["instant"]}
FILL_TAGS = {k: v["fill"] for k, v in SPEC.items() if v["fill"]}
OVERRIDE_TAGS = {k: v["override"] for k, v in SPEC.items() if v["override"]}
YTD_FLOW = {k for k, v in SPEC.items() if v["ytd_flow"]}

USD_UNITS = ("USD", "USD/shares", "shares")


class FactsError(ValueError):
    """取数失败（代码不存在 / 无可用税则数据等），信息可直接展示给用户。
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
    mapping = r.json()
    for v in mapping.values():
        if v["ticker"].upper() == ticker:
            return int(v["cik_str"])
    # 与「接口失败」严格分开（0020，DRAM 实测被读成取数问题）：映射本身取到了，
    # 只是没有这个代码——退市/改名/不申报 companyfacts 的 ETF 三类都长这样
    raise FactsError(
        f"SEC ticker 映射已取得（{len(mapping):,} 个代码）但无 {ticker}——"
        "可能已退市/改名，或是不进 ticker 映射的 ETF/基金/信托；"
        "若已知 CIK，可用 build_facts(ticker, email, cik=...) 绕过映射重试")


def pick(facts: dict, tag_names, kind, prefer_max=False, units=USD_UNITS, fx=1.0):
    """多候选 tag 合并：同期取 filed 最新值。

    prefer_max（仅营收使用）：同期同 filed 日打平时取较大者——总营收 tag 与其
    分项 tag（合同收入）可能同时申报（CRCL：Revenues $2,747M vs
    RevenueFromContractWithCustomer $110M），总营收 ⊇ 分项，取小值是静默错误。
    units/fx：非美元申报时按申报货币取单位并按现汇折算（股数不折算）。"""
    rows = {}
    for tag in tag_names:
        if tag not in facts:
            continue
        tag_units = facts[tag]["units"]
        unit = next((u for u in units if u in tag_units), None)
        if unit is None:
            continue
        scale = 1.0 if unit == "shares" else fx
        for f in tag_units[unit]:
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
            filed = f.get("filed", "")
            val = f["val"] * scale
            if (key not in rows or filed > rows[key]["filed"]
                    or (prefer_max and filed == rows[key]["filed"]
                        and val > rows[key]["val"])):
                rows[key] = {"val": val, "filed": filed}
    return {k: v["val"] for k, v in sorted(rows.items())}


def quarterly_from_ytd(facts: dict, tag_names, units=USD_UNITS, fx=1.0) -> dict:
    """现金流科目的离散季度值：同一财年（相同 start）内按 end 排序，
    相邻两期间隔 80-100 天时差分；首期本身就是季度长度时直接取值。
    年度数与 YTD 同 start，所以 Q4 = FY - 前三季 YTD 也被这里覆盖。"""
    ytd = pick(facts, tag_names, "ytd", units=units, fx=fx)
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


def ttm_via_ytd(facts: dict, tag_names, annual, fy_end="", units=USD_UNITS, fx=1.0):
    if not annual:
        return None
    a_end_s, a_val = sorted(annual.items())[-1]
    a_end = date.fromisoformat(a_end_s)
    # AMZN 类公司按季直接申报 twelve-months-ended：该期间会通过「年度」过滤器并把
    # a_end 顶到最新季度末——此时 a_val 本身就是真实 TTM（原版在这里因找不到
    # 「财年后的 YTD」返回 None，导致 engine KeyError: cfo）
    direct_ttm = bool(fy_end) and a_end_s > fy_end
    ytd = pick(facts, tag_names, "ytd", units=units, fx=fx)
    cur = [(s, e, v) for (s, e), v in ytd.items()
           if 0 <= (date.fromisoformat(s) - a_end).days <= 8 and e > a_end_s]
    # 刚发完年报、尚无本财年 YTD 时，TTM 恰好等于最新年度
    if not cur:
        note = (f"公司直接申报的 TTM（twelve months ended {a_end_s}）" if direct_ttm
                else f"= FY({a_end_s})（尚无本财年 YTD）")
        return {"value": a_val, "end": a_end_s, "note": note}
    s, e, v_cur = max(cur, key=lambda x: x[1])
    span = (date.fromisoformat(e) - date.fromisoformat(s)).days
    prior = [v for (ps, pe), v in ytd.items()
             if abs((date.fromisoformat(e) - date.fromisoformat(pe)).days - 364) <= 10
             and abs((date.fromisoformat(pe) - date.fromisoformat(ps)).days - span) <= 10]
    if not prior:
        note = (f"公司直接申报的 TTM（twelve months ended {a_end_s}）" if direct_ttm
                else f"= FY({a_end_s})（缺上年同期 YTD，退回最新年度口径）")
        return {"value": a_val, "end": a_end_s, "note": note}
    return {"value": a_val + v_cur - prior[0], "end": e,
            "note": f"FY({a_end_s}) + YTD至{e} - 上年同期YTD"}


def assemble_series(facts: dict, name: str, spec=None, units=USD_UNITS,
                    fx=1.0) -> tuple[dict, dict]:
    """单科目的 年度/季度 序列组装：候选合并（同期取 filed 最新）→
    现金流 YTD 差分 → 只补缺回退 → 主线口径覆盖 → 营收子集守卫 →
    Q4 推导。纯函数，护栏规则全部在此，测试直接喂手写 facts。"""
    item = (SPEC if spec is None else spec)[name]
    tags, prefer_max = item["tags"], name == "revenue"
    annual = pick(facts, tags, "annual", prefer_max, units, fx)
    quarterly = pick(facts, tags, "quarterly", prefer_max, units, fx)
    if item["ytd_flow"]:
        for k, v in quarterly_from_ytd(facts, tags, units, fx).items():
            quarterly.setdefault(k, v)
    for fill_tag in item["fill"]:
        for k, v in pick(facts, [fill_tag], "annual", units=units, fx=fx).items():
            annual.setdefault(k, v)
        for k, v in pick(facts, [fill_tag], "quarterly", units=units, fx=fx).items():
            quarterly.setdefault(k, v)
        # 现金流科目的 10-Q 是 YTD 累计口径：回退标签若只标 YTD 帧，
        # 不差分就一期也补不进来（PFE 的 PaymentsOfOrdinaryDividends 即如此，
        # 离散季帧只有 18 期、差分后补到 72 期含最新季）。
        # **只在该标签自己的 YTD 序列内部差分，绝不与主列表跨标签相减**：
        # 主列表与回退标签口径可能不同（普通股 vs 含优先股），跨标签做减法
        # 得到的是两个口径的差额而不是当季金额。GOOGL 2026-06-30 正属此情形
        # （H1 只在 OrdinaryDividends 下、Q1 只在 PaymentsOfDividends 下），
        # 故意不补 —— 宁可缺一期，不要一个看起来合理的错数。
        if item["ytd_flow"]:
            for k, v in quarterly_from_ytd(facts, [fill_tag], units, fx).items():
                quarterly.setdefault(k, v)
    for ov_tag in item["override"]:
        annual.update(pick(facts, [ov_tag], "annual", units=units, fx=fx))
        quarterly.update(pick(facts, [ov_tag], "quarterly", units=units, fx=fx))
    if name == "revenue" and "Revenues" in facts:
        # 子集守卫：保险等行业把 ASC 606 附注子集标在 RFCWC 下
        # （MET 曾因此少报 31.6 倍），同期 Revenues 显著更大时以其为准
        for kind_dict, kind in ((annual, "annual"), (quarterly, "quarterly")):
            for k, v_full in pick(facts, ["Revenues"], kind,
                                  units=units, fx=fx).items():
                cur = kind_dict.get(k)
                if cur is not None and cur < 0.8 * v_full:
                    kind_dict[k] = v_full
    if not item["no_q4"]:
        for a_end, a_val in annual.items():
            ae = date.fromisoformat(a_end)
            in_year = {k: v for k, v in quarterly.items()
                       if timedelta(days=0) < ae - date.fromisoformat(k)
                       < timedelta(days=340)}
            if len(in_year) == 3 and a_end not in quarterly:
                quarterly[a_end] = a_val - sum(in_year.values())
    return annual, dict(sorted(quarterly.items()))


def _guard_derived_q4_eps(out: dict) -> None:
    """财年末季 EPS 的拆股口径守卫（NVDA 2024-06 10:1 拆股实测）。

    companyfacts 每个季度帧来自「最后一份报告该期的申报」：拆股当年，
    早季度帧可能停在拆前口径而年度/晚季度帧已是拆后口径，
    Q4 = 年度 − 前三季 会混用两种口径相减——NVDA Q4 FY24 推导出
    −0.25（真实 +0.49：1.19 − (0.82拆前 + 0.25 + 0.37)）。

    EPS 本就不满足「年度=四季相加」（加权股本逐季变化），推导值只是
    近似——必须经 净利 ÷ 年度稀释股本 的隐含 EPS 交叉核对：符号翻转
    或相对偏差超 35%（绝对差 5 美分以内豁免，容纳银行优先股股利等
    口径差）即置空。宁缺勿错：图表缺一根柱诚实，画一根错柱有毒。"""
    eps_q = out.get("eps_diluted_quarterly") or {}
    eps_a = out.get("eps_diluted_annual") or {}
    ni_q = out.get("net_income_quarterly") or {}
    sh_a = out.get("shares_diluted_annual") or {}
    for end in [e for e in eps_q if e in eps_a]:  # 推导只发生在财年末季
        ni, sh, eps = ni_q.get(end), sh_a.get(end), eps_q[end]
        if ni is None or not sh:
            continue
        implied = ni / sh
        if abs(eps - implied) <= 0.05:
            continue
        if eps * implied < 0 or \
                abs(eps - implied) > 0.35 * max(abs(implied), abs(eps)):
            del eps_q[end]


def _newest_end(facts: dict, tag: str) -> str:
    if tag not in facts:
        return ""
    return max((f.get("end", "") for u in facts[tag]["units"].values() for f in u),
               default="")


def _companyfacts(ticker: str, cik: int, headers: dict) -> dict:
    """缺可用税则时重试一次排除偶发坏响应，再由调用方区分真实原因报错。"""
    resp: dict = {}
    for attempt in range(2):
        r = httpx.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                      headers=headers, timeout=90)
        if r.status_code == 404:
            raise FactsError(f"SEC 没有 {ticker} 的 XBRL companyfacts 数据")
        if r.status_code != 200:
            raise FactsError(f"SEC companyfacts 接口返回 {r.status_code}（稍后重试）",
                             transient=True)
        resp = r.json()
        if "us-gaap" in resp["facts"] or "ifrs-full" in resp["facts"]:
            break
        if attempt == 0:
            time.sleep(2)
    return resp


# 信托/基金类实体名 token：GLD（SPDR GOLD TRUST）实测——商品信托无损益表 XBRL，
# 旧报错只有一句「重组旧 CIK」假设，把真实原因整个盖掉。名字就在 companyfacts
# payload 里，不为 SIC 多打一次 submissions 接口
_FUND_NAME_TOKENS = ("TRUST", "FUND", "ETF")


def _no_revenue_reason(resp: dict, ticker: str, cik: int) -> str:
    """companyfacts 里找不到任何营收概念时的分类报错（0020）。纯函数可单测。

    三种形态分开说，原因按可能性排序——单一的「重组旧 CIK」假设在实测里
    4/6 个失败被带偏：①实体名含 TRUST/FUND/ETF 直接定性；②taxonomy 整体
    缺席；③taxonomy 在但营收概念探测全空（探测过的 tag 列出来，可核对）。"""
    name = str(resp.get("entityName") or "?")
    head = f"{ticker}（CIK {cik}，{name}）"
    words = set(name.upper().replace(",", " ").replace(".", " ").split())
    if words & set(_FUND_NAME_TOKENS):
        return (head + " 实体名含 TRUST/FUND/ETF——商品信托/基金类发行人无经营营收"
                "（申报不含损益表 XBRL），不适配本估值框架；其价格跟踪标的资产，"
                "请按资产本身分析")
    present = [t for t in ("us-gaap", "ifrs-full") if t in (resp.get("facts") or {})]
    if not present:
        return (head + " 无 us-gaap/ifrs-full taxonomy。按可能性排序：①基金/信托类"
                "载体不申报财务报表 XBRL；②ticker 映射指向重组后的新实体，历史财务"
                "留在旧 CIK 下——可在 https://www.sec.gov/cgi-bin/browse-edgar "
                "按公司名搜旧实体确认")
    probed = (SPEC["revenue"]["tags"] + SPEC["revenue"]["override"]
              + SPEC_IFRS["revenue"]["tags"])
    return (head + f" 有 {'/'.join(present)} taxonomy 但无营收概念"
            f"（探测了这些 tag：{', '.join(probed)}）。按可能性排序：①信托/基金/"
            "持牌载体无经营营收；②重组后的新实体，历史财务留在旧 CIK 下"
            "（EDGAR 按公司名搜旧实体确认）")


def _pick_taxonomy(resp: dict, ticker: str, cik: int) -> tuple[dict, str]:
    """税则按「谁的营收数据更新」选择：SONY/TM 等公司中途从 US GAAP 切到
    IFRS，companyfacts 里两个税则并存，无脑取 us-gaap 会拿到停更多年的旧数据。"""
    all_facts = resp["facts"]

    def newest(tax, tags):
        if tax not in all_facts:
            return ""
        g = all_facts[tax]
        return max((f.get("end", "") for t in tags if t in g
                    for u in g[t]["units"].values() for f in u), default="")

    gaap = newest("us-gaap", SPEC["revenue"]["tags"] + SPEC["revenue"]["override"])
    ifrs = newest("ifrs-full", SPEC_IFRS["revenue"]["tags"])
    if gaap and gaap >= ifrs:
        return all_facts["us-gaap"], "us-gaap"
    if ifrs:
        return all_facts["ifrs-full"], "ifrs-full"
    raise FactsError(_no_revenue_reason(resp, ticker, cik))


# 券商类经营收入标签——broker 签名的形态证据（0021）。companyfacts 里没有 SIC，
# 不为分类多打一次 submissions 接口：这些标签在经营性公司几乎不出现，且签名还
# 叠加「营业利润缺席/停报」硬条件，误路由面被压到极窄
_BROKER_TAGS = ("InterestAndDividendIncomeOperating",
                "BrokerageCommissionsRevenue",
                "InterestIncomeExpenseNet")


def _detect_mode(facts: dict, taxonomy: str) -> tuple[str, dict]:
    """发行人模式检测：银行/券商申报 RevenuesNetOfInterestExpense、不报
    （或早已停报）营业利润。us-gaap 两种模式共用同一份 SPEC——银行口径由
    revenue 的 override 与 equity/goodwill/intangibles 时点科目覆盖，mode
    只决定核心损益科目、Rule of 40 与现金流 TTM 是否适用。"""
    if taxonomy != "us-gaap":
        return "standard", SPEC_IFRS
    rn_end = _newest_end(facts, "RevenuesNetOfInterestExpense")
    op_end = _newest_end(facts, "OperatingIncomeLoss")
    if rn_end and (not op_end or
                   (date.fromisoformat(rn_end) - date.fromisoformat(op_end)).days > 400):
        return "financials", SPEC
    # 券商签名（0021，HOOD 实测漏检）：利润表是「总收入 − 总运营费用 → 税前」，
    # 既无 OperatingIncomeLoss 也无银行的 RevenuesNetOfInterestExpense——两个既有
    # 签名都不认，误落 standard 后因缺营业利润被判「不适配」。Revenues（券商的
    # Total net revenues，语义即净收入）与税前利润同步新鲜 + 券商标签在场即路由
    # financials：fin 模式的营收主列表本就含 Revenues，P/E+P/TBV 对券商成立；
    # 券商 CFO 含客户资金摆动，standard 的 DCF 腿本来就不该碰（fin 不出 CFO TTM）。
    # OperatingIncomeLoss 新鲜的经营性公司不受影响；停报营业利润的经营性公司
    # （COHR 型）无券商标签，走 standard 的推导回退（_derive_op_income_ttm）
    rev_end = max((_newest_end(facts, t) for t in
                   ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")),
                  default="")
    pre_end = max((_newest_end(facts, t) for t in SPEC["pretax_income"]["tags"]),
                  default="")

    def _fresh(end_s: str) -> bool:
        """与本分支其余条件同一时效窗：距营收锚 <=400 天算新鲜。"""
        return bool(end_s and rev_end) and (date.fromisoformat(rev_end)
                                            - date.fromisoformat(end_s)).days <= 400

    # 券商标签也过时效闸（0022 C17）：此前 presence-anywhere（历史上出现过即算），
    # 与旁边逐条 date-gated 的 rev/pretax/op 条件不对称——带着一枚陈年 broker tag
    # 的经营性公司（UHT 实测今天就持 InterestIncomeExpenseNet）一旦改列报停掉
    # OperatingIncomeLoss，会被静默按银行估值（P/TBV 框架、fin prompt、CFO/capex
    # 全压掉，零警告）。
    broker_end = max((_newest_end(facts, t) for t in _BROKER_TAGS), default="")
    # 经营性公司的推导救援优先（0022 C17）：_detect_mode 跑在 _derive_op_income_ttm
    # 之前，路由 financials 会**结构性**关掉 standard 分支里的 op = rev−cogs−rnd−sga
    # 救援。rev/cogs/rnd/sga 组件全新鲜说明利润表仍是经营性列报（券商不报 cogs），
    # COHR 型停报票必须留在 standard 吃救援，即便它恰好带着新鲜的 broker tag。
    _deriv_fresh = all(
        _fresh(max((_newest_end(facts, t) for t in SPEC[n]["tags"]), default=""))
        for n in ("cogs", "rnd", "sga"))
    if (_fresh(broker_end) and rev_end and pre_end and not _deriv_fresh
            and (not op_end or (date.fromisoformat(rev_end)
                                - date.fromisoformat(op_end)).days > 400)
            and (date.fromisoformat(rev_end)
                 - date.fromisoformat(pre_end)).days <= 400):
        return "financials", SPEC
    return "standard", SPEC


def _detect_currency(facts: dict, revenue_tags) -> str:
    """按营收 tag 里年度期数最多的货币为申报货币，非美元按现汇折算。"""
    counts: dict[str, int] = {}
    for tag in revenue_tags:
        if tag not in facts:
            continue
        for unit, rows in facts[tag]["units"].items():
            if unit == "shares" or "/" in unit:
                continue
            n = sum(1 for f in rows if f.get("start") and f.get("end") and
                    350 <= (date.fromisoformat(f["end"])
                            - date.fromisoformat(f["start"])).days <= 380)
            counts[unit] = counts.get(unit, 0) + n
    if not counts:
        return "USD"
    # 期数最多者胜；打平优先 USD（部分 20-F 附带便利折算的 USD 列）
    return max(counts, key=lambda u: (counts[u], u == "USD"))


def income_items(mode: str) -> tuple:
    """核心损益科目：缺任何一项都说明该发行人不适配当前估值框架。"""
    return ("revenue", "pretax_income", "net_income") if mode == "financials" \
        else ("revenue", "op_income", "net_income")


def _derive_op_income_ttm(out: dict, ttm: dict) -> None:
    """营业利润 TTM 的自下而上推导回退（0021，COHR 救援）。原地改写 ttm。

    发行人停报 OperatingIncomeLoss（改列报）时，TTM 锚定检查把 op_income 置
    None、整票被判不适配——但 rev/cogs/rnd/sga 若在**同一 TTM 窗口**（营收锚的
    4 个季度）全部在场，营业利润可推导：op = rev − cogs − rnd − sga
    （COHR 验证：7,118.2 − 4,449.1 − 723.0 − 1,044.6 ≈ 901.5M）。推导值打
    derived 标记 + note——服务层据此在 prompt 注入口径提示（可能含未单列的
    摊销/重组项，判断层须与利润表原文核对）。任一组件缺任一窗口季度即放弃、
    维持既有 hard fail：宁可拒绝，不造一个看起来合理的错数。"""
    op = ttm.get("op_income") or {}
    rev = ttm.get("revenue") or {}
    if op.get("value") is not None or "quarters" not in rev:
        return
    window = rev["quarters"]
    comps = {}
    for name in ("cogs", "rnd", "sga"):
        q = out.get(name + "_quarterly") or {}
        if any(k not in q for k in window):
            return
        comps[name] = sum(q[k] for k in window)
    ttm["op_income"] = {
        "value": rev["value"] - comps["cogs"] - comps["rnd"] - comps["sga"],
        "quarters": list(window), "derived": True,
        "note": "推导值 = rev − cogs − rnd − sga（OperatingIncomeLoss 停报/滞后），"
                "可能含未单列的摊销/重组项"}


def classify_core_gaps(out: dict) -> list[str]:
    """核心科目 TTM 缺数的适配性分类（0020）-> problems 行。纯函数可单测。

    此前一句「该发行人未申报对应科目」盖住所有形态，实测 4/6 个失败诊断被带偏：
    COHR 是 op_income 停在 2024-06-30 之后**停报**（营收照常申报 143 期，发行人
    改了列报口径）；新上市票是**历史不足**（XBRL 只有 2 季，等 10-Q 即可）——
    都不是「未申报」。逐核心概念给出最后申报期，报错本身就是诊断。"""
    ttm = out["ttm"]
    missing = [k for k in income_items(out["mode"])
               if (ttm.get(k) or {}).get("value") is None]
    if not missing:
        return []
    # 新上市发行人：年度 0 期且季度 <4——不是没申报，是历史还不够构成 TTM
    ann = out.get("revenue_annual") or {}
    q = out.get("revenue_quarterly") or {}
    if not ann and len(q) < 4:
        ends = "、".join(sorted(q)) or "—"
        return [f"新上市发行人：XBRL 仅 {len(q)} 季（期末：{ends}），"
                "TTM 需 4 个连续季度——待后续 10-Q 申报后再试"]
    per = []
    # 「停报」的对照锚 = 全票最新数据期末（UV1）：概念最新期与锚同步却凑不齐 TTM 的，
    # 是**新开始申报、期数不足**（或中间有缺口），不是停报——刚开始报某概念的发行人
    # 被告知"发行人改了列报口径"会把诊断带向完全相反的方向
    anchor = out.get("data_latest") or max(
        max(out.get("revenue_annual") or {}, default=""),
        max(out.get("revenue_quarterly") or {}, default=""))
    for k in missing:
        a = out.get(k + "_annual") or {}
        qq = out.get(k + "_quarterly") or {}
        n = len(a) + len(qq)
        if not n:
            per.append(f"  · {k}: 从未申报该概念"
                       "（保险/部分能源与REIT/外国银行的科目体系暂不支持）")
            continue
        last = max(list(a) + list(qq))
        if anchor and last >= anchor:
            per.append(f"  · {k}: 历史有 {n} 期、最新一期 {last} 与全票数据锚同步"
                       "（仍在申报，非停报）——期数不足或中间有缺口，凑不齐连续"
                       " 4 季 TTM，待后续申报累积后再试")
        else:
            per.append(f"  · {k}: 历史有 {n} 期、最后一期 {last} 之后停报"
                       "（发行人改了列报口径，非从未申报）")
    return ["核心损益科目 TTM 无数据：" + ", ".join(missing) + "\n" + "\n".join(per)]


def _rule_of_40(out: dict, ttm: dict) -> dict:
    """Rule of 40：营收增速 + 利润率，「高倍数值不值得给」的标尺（软件/平台业
    惯例：>40 分算优秀——增速是在换未来利润还是单纯烧钱）。营收增速 = TTM vs
    上一个 TTM（最近 8 个离散季度，逐 gap 校验），比单季 YoY 稳；利润率给两个
    口径：营业利润率（主）与 FCF 利润率——FCF 把 SBC 加了回来（SBC 是真实
    成本），剔 SBC 变体一并给出。非核心项：任何一项算不出只留空，不阻断取数。"""
    ro40: dict = {}
    q8 = list(out["revenue_quarterly"].items())[-8:]
    if len(q8) == 8:
        ends = [date.fromisoformat(k) for k, _ in q8]
        if all(80 <= (ends[i + 1] - ends[i]).days <= 100 for i in range(7)):
            cur4 = sum(v for _, v in q8[4:])
            prev4 = sum(v for _, v in q8[:4])
            if prev4 > 0:
                ro40["rev_g_ttm"] = round(cur4 / prev4 - 1, 4)
    # 利润率的分子分母必须同窗：营收 TTM 必须来自四季加总（有 "quarters" 键），
    # 营业利润同理——任一侧退回 FY（"note" 路径）都会做出「FY 分子 ÷ TTM 分母」
    # 的混窗利润率还标着 TTM。现金流的 YTD 差额口径本身即 TTM，但「退回最新年度」
    # 变体是旧窗——留 caliber_note 一起展示，不静默
    rev_e = ttm.get("revenue") or {}
    rev = rev_e.get("value") if "quarters" in rev_e else None
    if rev:
        op_e = ttm.get("op_income") or {}
        cfo_e, cap_e = ttm.get("cfo") or {}, ttm.get("capex") or {}
        sbc_e = ttm.get("sbc") or {}
        if "quarters" in op_e and op_e.get("value") is not None:
            ro40["opm_ttm"] = round(op_e["value"] / rev, 4)
        if cfo_e.get("value") is not None and cap_e.get("value") is not None:
            ro40["fcf_margin_ttm"] = round((cfo_e["value"] - cap_e["value"]) / rev, 4)
            stale_cf = [n for n in (cfo_e.get("note"), cap_e.get("note"))
                        if n and "退回" in n]
            if stale_cf:
                ro40["caliber_note"] = ("FCF 口径含退回旧财年的现金流项，"
                                        "与 TTM 营收窗口不一致，FCF 分仅供参考")
        if "quarters" in sbc_e and sbc_e.get("value") is not None:
            ro40["sbc_margin_ttm"] = round(sbc_e["value"] / rev, 4)
    if "rev_g_ttm" in ro40:
        if "opm_ttm" in ro40:
            ro40["score_op"] = round(100 * (ro40["rev_g_ttm"] + ro40["opm_ttm"]), 1)
        if "fcf_margin_ttm" in ro40:
            ro40["score_fcf"] = round(100 * (ro40["rev_g_ttm"]
                                             + ro40["fcf_margin_ttm"]), 1)
            if "sbc_margin_ttm" in ro40:
                ro40["score_fcf_ex_sbc"] = round(
                    100 * (ro40["rev_g_ttm"] + ro40["fcf_margin_ttm"]
                           - ro40["sbc_margin_ttm"]), 1)
    return ro40


def build_facts(ticker: str, email: str, cik: int | None = None) -> dict:
    """cik 可选：服务端已有缓存的 ticker->CIK 映射时直接传入，省一次 700KB 下载。"""
    ticker = ticker.upper()
    headers = _headers(email)
    if cik is None:
        cik = resolve_cik(ticker, headers)
    resp = _companyfacts(ticker, cik, headers)
    facts, taxonomy = _pick_taxonomy(resp, ticker, cik)
    mode, spec = _detect_mode(facts, taxonomy)

    currency = _detect_currency(facts, spec["revenue"]["tags"])
    fx = 1.0
    if currency != "USD":
        import yfinance as yf
        try:
            fx = float(yf.Ticker(f"{currency}USD=X").fast_info["lastPrice"])
        except Exception as e:  # noqa: BLE001 —— Yahoo 抛什么都算取汇率失败
            # 非美元申报发行人的整份序列都按这个汇率折算，取不到就没有可用输出。
            # 归一成 FactsError(transient) 而不是让 TypeError/网络异常裸奔：
            # CLI 得到可读报错，服务端由既有的 transient 分支映射 502 而非 500
            raise FactsError(f"{currency}/USD 现汇取不到"
                             f"（{type(e).__name__}: {e}）——请稍后重试", transient=True)
        print(f"{ticker} 申报货币 {currency}，按现汇 {fx:.5f} 折算美元"
              "（历史序列为恒定汇率口径）", file=sys.stderr)
    units = (currency, f"{currency}/shares", "shares")

    out = {"ticker": ticker, "cik": cik, "mode": mode, "taxonomy": taxonomy,
           "currency": currency, "fx_to_usd": fx,
           # 银行报表格式（损益表第一行是 Total net revenue）：
           # 没有毛利概念，图表端据此不硬算毛利率
           "bank_format": "RevenuesNetOfInterestExpense" in facts}
    for name, item in spec.items():
        if item["instant"]:
            out[name + "_instant"] = pick(facts, item["tags"], "instant",
                                          units=units, fx=fx)
            continue
        annual, quarterly = assemble_series(facts, name, spec, units, fx)
        out[name + "_annual"] = annual
        out[name + "_quarterly"] = quarterly

    _guard_derived_q4_eps(out)

    # TTM 全部科目锚定到营收窗口末季：某科目标签断更时（COHR 的营业利润曾停在
    # 2024）不许拿旧窗口的 TTM 与新窗口的营收并排展示。
    # sbc 参与 TTM 但不进核心科目：缺失只应留空、不该让整只票判为不适配
    ttm: dict = {}
    core = income_items(mode)
    anchor = max(out["revenue_quarterly"], default=None)
    for name in core + (("sbc",) if "sbc" in spec else ()):
        q, a = out[name + "_quarterly"], out[name + "_annual"]
        done = False
        if len(q) >= 4:
            last4 = list(q.items())[-4:]
            if anchor and last4[-1][0] != anchor:
                ttm[name] = {"value": None, "error": f"口径滞后：最新期 {last4[-1][0]}"}
                continue
            ends = [date.fromisoformat(k) for k, _ in last4]
            gaps = [(ends[i + 1] - ends[i]).days for i in range(3)]
            if all(80 <= g <= 100 for g in gaps):
                ttm[name] = {"value": sum(v for _, v in last4),
                             "quarters": [k for k, _ in last4]}
                done = True
        if not done and a:
            # 外国发行人（20-F/6-K）常无季度 XBRL：退回最新财年并注明口径
            a_end, a_val = sorted(a.items())[-1]
            ttm[name] = {"value": a_val,
                         "note": f"= FY({a_end})（无季度 XBRL，TTM 退回最新财年）"}
        elif not done:
            ttm[name] = {"value": None, "error": "无季度亦无年度数据"}
    # 银行的 CFO 含贷款投放，不是自由现金流口径，financials 模式不出 TTM
    if mode == "standard":
        fy_end = max(out["revenue_annual"], default="")
        for name in ("cfo", "capex"):
            if name not in spec:
                continue
            r = ttm_via_ytd(facts, spec[name]["tags"], out[name + "_annual"],
                            fy_end, units, fx)
            if r:
                if anchor and abs((date.fromisoformat(anchor)
                                   - date.fromisoformat(r["end"])).days) > 100:
                    r = {"value": None, "error": f"口径滞后：最新期 {r['end']}"}
                ttm[name] = r
        # 营业利润停报的自下而上推导回退（0021）——组件不齐则保持 None，
        # 由 classify_core_gaps 按「停报」分类报错
        _derive_op_income_ttm(out, ttm)
    out["ttm"] = ttm
    out["data_latest"] = max(max(out["revenue_annual"], default=""),
                             max(out["revenue_quarterly"], default=""))
    if mode == "standard":
        ro40 = _rule_of_40(out, ttm)
        if ro40:
            out["rule_of_40"] = ro40
    return out


def _add_bands(out: dict, ticker: str, email: str) -> None:
    """历史「已实现前瞻 PE」分布：供 engine.py 的目标 PE 越界诊断（pe_band_check）。
    引擎是零联网的确定性计算层，带子必须在数据层备好。属非核心项：任何失败只记
    pe_band_error 不阻断取数——但要留痕，不静默。VALUATION_NO_PE_BAND=1 可关闭。
    只有 CLI（估值管道）走这里：图表端 build_facts 不消费带子，也不该付这份下载。"""
    from pe_band import compute_band, compute_ptbv_band, load_inputs
    try:
        # 一次下载（companyfacts + yfinance 历史价 + 拆股表），PE/PS 两份带子共用——
        # compute_band 各自下载会翻倍 SEC/yfinance 请求且容易撞限速
        inputs = load_inputs(ticker, email, years=5)
    except Exception as e:
        out["pe_band_error"] = f"{type(e).__name__}: {e}"
        print(f"警告: 带子数据未取到（{out['pe_band_error']}）——PE/PS 带本次不生成",
              file=sys.stderr)
        return
    try:
        # basis="ntm"：必须与 engine 的前瞻期同源。engine 算 rev1 = TTM×(1+g)，
        # 前瞻期恒为 NTM 而非财年（见 valuation_service.fwd_window）。用 forward
        # （按财年切）只在 report_end = 财年末时才对，AMZN/META 这类 12 月财年公司
        # 在 Q1/Q2/Q3 报告期会系统性错位，pe_vs_history 的分位数就不可信。
        band = compute_band(ticker, email, years=5, basis="ntm", inputs=inputs)
        band.pop("_sorted", None)
        out["pe_band"] = band
        # 口径/年数一律从 band 自身字段拼：写死字符串会在换 basis 时静默说谎
        # （曾切到 ntm 后仍打印 "forward"，排查与回归对比都会被带偏）
        print(f"PE带({band['basis']},{band['years']}y): 中位 {band['median']:.1f}x  区间 "
              f"{band['min']:.1f}~{band['max']:.1f}x  {band['days']} 个交易日"
              + ("  ⚠覆盖不足,锚停用" if band.get("thin_coverage") else ""))
    except Exception as e:
        out["pe_band_error"] = f"{type(e).__name__}: {e}"
        print(f"警告: PE 带未生成（{out['pe_band_error']}）——目标 PE 越界诊断本次不生效",
              file=sys.stderr)
    # P/TBV 带（financials 模式）：金融股的估值锚是 P/B 系（E 带杠杆带周期，
    # 一个坏账周期就能打没；净资产相对稳定）——repo 的 financials 模式本来就按
    # P/TBV 建，带子给它历史分位锚。trailing 口径（TBV 是存量，无「已实现 NTM」
    # 语义），消费方 = engine ptbv_band_check + 判断层 P/TBV 锚注入。
    if out["mode"] == "financials":
        try:
            tb = compute_ptbv_band(ticker, email, years=5, inputs=inputs)
            out["ptbv_band"] = tb
            print(f"P/TBV带(trailing,{tb['years']}y): 中位 {tb['median']:.2f}x  区间 "
                  f"{tb['min']:.2f}~{tb['max']:.2f}x  {tb['days']} 个交易日")
        except Exception as e:
            out["ptbv_band_error"] = f"{type(e).__name__}: {e}"
            print(f"警告: P/TBV 带未生成（{out['ptbv_band_error']}）——目标 P/TBV "
                  "越界诊断本次不生效", file=sys.stderr)
    # P/S 带（standard 模式）：同一套机械、分母换每股营收。近零利润票（COIN/
    # 微利期周期股）PE 带与 PE 腿一起失效，PS 是那个域的教科书参照——engine
    # 的近零利润守卫拿它给参考价。金融股不出（营收=总净收入口径，PS 无惯例）。
    if out["mode"] == "standard":
        try:
            psb = compute_band(ticker, email, years=5, basis="ntm",
                               metric="rps", inputs=inputs)
            psb.pop("_sorted", None)
            out["ps_band"] = psb
            print(f"P/S带({psb['basis']},{psb['years']}y): 中位 {psb['median']:.2f}x  区间 "
                  f"{psb['min']:.2f}~{psb['max']:.2f}x  {psb['days']} 个交易日")
        except Exception as e:
            out["ps_band_error"] = f"{type(e).__name__}: {e}"
            print(f"警告: P/S 带未生成（{out['ps_band_error']}）——近零利润参照本次不生效",
                  file=sys.stderr)


def main() -> None:
    ticker, out_path, email = sys.argv[1].upper(), sys.argv[2], sys.argv[3]
    try:
        out = build_facts(ticker, email)
    except FactsError as e:
        raise SystemExit(str(e))

    ro40 = out.get("rule_of_40") or {}
    if "score_op" in ro40:
        print(f"Rule of 40: 营收增速 {ro40.get('rev_g_ttm', 0):+.1%} + "
              f"OPM {ro40.get('opm_ttm', 0):.1%} = {ro40['score_op']:.0f} 分"
              + (f"（FCF 口径 {ro40['score_fcf']:.0f}"
                 + (f"，剔 SBC {ro40['score_fcf_ex_sbc']:.0f}"
                    if "score_fcf_ex_sbc" in ro40 else "") + "）"
                 if "score_fcf" in ro40 else ""))
    if os.environ.get("VALUATION_NO_PE_BAND") != "1":
        _add_bands(out, ticker, email)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    ttm, q = out["ttm"], out["revenue_quarterly"]
    print(f"{ticker} (CIK {out['cik']}): mode={out['mode']} taxonomy={out['taxonomy']} "
          f"currency={out['currency']} 年度 {len(out['revenue_annual'])} 期, "
          f"季度 {len(q)} 期, 最新 {out['data_latest'] or '-'}")
    print("TTM:", {k: v.get("value") for k, v in ttm.items()})

    # ---- 适配性诊断：能修的已自动路由（financials/IFRS/非美元），剩下的给可诊断的报错。
    # 分类（新上市/停报/从未申报）见 classify_core_gaps（0020）
    problems, warnings = [], []
    problems += classify_core_gaps(out)
    if out["mode"] == "financials" and not out.get("equity_instant"):
        problems.append("缺股东权益时点数据，P/TBV 法无法计算")
    if out["data_latest"]:
        staleness = (date.today() - date.fromisoformat(out["data_latest"])).days
        if staleness > 550:
            warnings.append(f"最新申报期为 {out['data_latest']}（{staleness} 天前）——"
                            "外国发行人 6-K 无 XBRL 时结构化数据只到上一份年报，"
                            "判断层请以财报原文章节里的最新季度数字为准")
    for w in warnings:
        print(f"警告: {w}", file=sys.stderr)
    if problems:
        print(f"\n{ticker} 不适配当前估值框架：", file=sys.stderr)
        for p in problems:
            print(f"- {p}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
