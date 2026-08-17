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
import os
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
    # 股权激励（现金流量表加回项）：卖方"non-GAAP EPS"多半是剔了 SBC 的口径，
    # 而 SBC 是真实成本（稀释已体现在稀释股数里，但费用被剔了两次都不算过分）。
    # 取来供判断层做"SBC 调整后 PE"对照——AMZN/META 这类占净利两位数百分比。
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense",
            "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost"],
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
    # 时点流通股 = 每股 TBV 的正确分母。TBV 是时点存量，除以当季**加权平均**稀释
    # 股数等于拿流量均值配存量——增发季（SOFI 型一次性摊薄）加权数≈期末股数的
    # 一半，每股 TBV 会被高估近一倍。P/TBV 历史带自 0058 起已改用时点股数
    # （pe_band.ptbv_band），引擎当前点必须同源，否则"当前倍数 vs 历史锚"是两个口径。
    "shares_outstanding": ["CommonStockSharesOutstanding"],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
}
INSTANT_FINANCIALS = {"equity", "goodwill", "intangibles", "cash", "shares_outstanding"}

# IFRS 外国发行人（20-F）：概念名映射到与 standard 相同的输出键，下游无感知
TAGS_IFRS = {
    "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
    "op_income": ["ProfitLossFromOperatingActivities"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "eps_diluted": ["DilutedEarningsLossPerShare"],
    "cfo": ["CashFlowsFromUsedInOperatingActivities"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
              "PurchaseOfPropertyPlantAndEquipment",
              "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets"],
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

# 税则按"谁的营收数据更新"选择：SONY/TM 等公司中途从 US GAAP 切换到 IFRS，
# companyfacts 里两个税则并存，无脑取 us-gaap 会拿到停更多年的旧数据
def _tax_newest(tax, tags):
    if tax not in _all:
        return ""
    g = _all[tax]
    return max((f.get("end", "") for t in tags if t in g
                for u in g[t]["units"].values() for f in u), default="")


_gaap_newest = _tax_newest("us-gaap", TAGS_STANDARD["revenue"] + TAGS_FINANCIALS["revenue"])
_ifrs_newest = _tax_newest("ifrs-full", TAGS_IFRS["revenue"])
if _gaap_newest and _gaap_newest >= _ifrs_newest:
    facts, TAXONOMY = _all["us-gaap"], "us-gaap"
elif _ifrs_newest:
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


def pick(tag_names, kind, prefer_max=False):
    """多候选 tag 合并：同期取 filed 最新值。

    prefer_max（仅营收使用）：同期同 filed 日打平时取较大者——总营收 tag 与其
    分项 tag（合同收入）可能同时申报（CRCL：Revenues $2,747M vs
    RevenueFromContractWithCustomer $110M），总营收 ⊇ 分项，取小值是静默错误。"""
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
            filed = f.get("filed", "")
            val = f["val"] * scale
            if (key not in rows or filed > rows[key]["filed"]
                    or (prefer_max and filed == rows[key]["filed"] and val > rows[key]["val"])):
                rows[key] = {"val": val, "filed": filed}
    return {k: v["val"] for k, v in sorted(rows.items())}


def ttm_via_ytd(tag_names, annual, fy_end=""):
    if not annual:
        return None
    a_end_s, a_val = sorted(annual.items())[-1]
    a_end = date.fromisoformat(a_end_s)
    # AMZN 类公司按季直接申报 twelve-months-ended：该期间会通过"年度"过滤器并把
    # a_end 顶到最新季度末——此时 a_val 本身就是真实 TTM（原版在这里因找不到
    # "财年后的 YTD"返回 None，导致 engine KeyError: cfo）
    direct_ttm = bool(fy_end) and a_end_s > fy_end
    ytd = pick(tag_names, "ytd")
    cur = [(s, e, v) for (s, e), v in ytd.items()
           if 0 <= (date.fromisoformat(s) - a_end).days <= 8 and e > a_end_s]
    # 刚发完年报、尚无本财年 YTD 时，TTM 恰好等于最新年度
    if not cur:
        note = (f"公司直接申报的 TTM（twelve months ended {a_end_s}）" if direct_ttm
                else f"= FY({a_end_s})（尚无本财年 YTD）")
        return {"value": a_val, "note": note}
    s, e, v_cur = max(cur, key=lambda x: x[1])
    span = (date.fromisoformat(e) - date.fromisoformat(s)).days
    prior = [v for (ps, pe), v in ytd.items()
             if abs((date.fromisoformat(e) - date.fromisoformat(pe)).days - 364) <= 10
             and abs((date.fromisoformat(pe) - date.fromisoformat(ps)).days - span) <= 10]
    if not prior:
        note = (f"公司直接申报的 TTM（twelve months ended {a_end_s}）" if direct_ttm
                else f"= FY({a_end_s})（缺上年同期 YTD，退回最新年度口径）")
        return {"value": a_val, "note": note}
    return {"value": a_val + v_cur - prior[0], "note": f"FY({a_end_s}) + YTD至{e} - 上年同期YTD"}


# Q4 = 年度 - 前三季 只对可加总的流量科目成立；加权平均股数/EPS 不可加总
# （FY 加权股数 ≈ 四季平均而非加总，硬推会得到大负数并被当成最新股数消费）
DERIVE_Q4 = {"revenue", "op_income", "net_income", "pretax_income", "sbc"}
INCOME_ITEMS = ("revenue", "pretax_income", "net_income") if MODE == "financials" \
    else ("revenue", "op_income", "net_income")
# sbc 参与 TTM 但不进 INCOME_ITEMS：它不是核心科目，缺失只应留空、不该让整只票不适配
TTM_ITEMS = INCOME_ITEMS + (("sbc",) if "sbc" in TAGS else ())

out = {"ticker": TICKER, "cik": CIK, "mode": MODE, "taxonomy": TAXONOMY,
       "currency": CURRENCY, "fx_to_usd": FX}
for name in TAGS:
    if name in INSTANT:
        out[name + "_instant"] = pick(TAGS[name], "instant")
        continue
    annual = pick(TAGS[name], "annual", prefer_max=(name == "revenue"))
    quarterly = pick(TAGS[name], "quarterly", prefer_max=(name == "revenue"))
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
for name in TTM_ITEMS:
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
            r = ttm_via_ytd(TAGS[name], out[name + "_annual"],
                            max(out["revenue_annual"], default=""))
            if r:
                ttm[name] = r
out["ttm"] = ttm
out["data_latest"] = max(max(out["revenue_annual"], default=""),
                         max(out["revenue_quarterly"], default=""))

# ---- Rule of 40（standard 模式）：营收增速 + 利润率，"高倍数值不值得给"的标尺
# （软件/平台业惯例：>40 分算优秀——增速是在换未来利润还是单纯烧钱）。
# 营收增速 = TTM vs 上一个 TTM（最近 8 个离散季度，逐 gap 校验），比单季 YoY 稳；
# 利润率给两个口径：营业利润率（主）与 FCF 利润率——FCF 把 SBC 加了回来
# （SBC 是真实成本），剔 SBC 变体一并给出，SBC 占营收比高的票两者差十几分。
# 非核心项：任何一项算不出只留空，不阻断取数。
if MODE == "standard":
    ro40 = {}
    _q8 = list(out["revenue_quarterly"].items())[-8:]
    if len(_q8) == 8:
        _ends = [date.fromisoformat(k) for k, _ in _q8]
        if all(80 <= (_ends[i + 1] - _ends[i]).days <= 100 for i in range(7)):
            _cur4 = sum(v for _, v in _q8[4:])
            _prev4 = sum(v for _, v in _q8[:4])
            if _prev4 > 0:
                ro40["rev_g_ttm"] = round(_cur4 / _prev4 - 1, 4)
    # 利润率的分子分母必须同窗：营收 TTM 必须来自四季加总（有 "quarters" 键），
    # 营业利润同理——任一侧退回 FY（"note" 路径）都会做出「FY 分子 ÷ TTM 分母」
    # 的混窗利润率还标着 TTM。现金流的 YTD 差额口径本身即 TTM，但"退回最新年度"
    # 变体是旧窗——留 caliber_note 一起展示，不静默
    _rev_e = ttm.get("revenue") or {}
    _rev = _rev_e.get("value") if "quarters" in _rev_e else None
    if _rev:
        _op_e = ttm.get("op_income") or {}
        _cfo_e, _cap_e = ttm.get("cfo") or {}, ttm.get("capex") or {}
        _sbc_e = ttm.get("sbc") or {}
        if "quarters" in _op_e and _op_e.get("value") is not None:
            ro40["opm_ttm"] = round(_op_e["value"] / _rev, 4)
        if _cfo_e.get("value") is not None and _cap_e.get("value") is not None:
            ro40["fcf_margin_ttm"] = round((_cfo_e["value"] - _cap_e["value"]) / _rev, 4)
            _stale_cf = [n for n in (_cfo_e.get("note"), _cap_e.get("note"))
                         if n and "退回" in n]
            if _stale_cf:
                ro40["caliber_note"] = ("FCF 口径含退回旧财年的现金流项，"
                                        "与 TTM 营收窗口不一致，FCF 分仅供参考")
        if "quarters" in _sbc_e and _sbc_e.get("value") is not None:
            ro40["sbc_margin_ttm"] = round(_sbc_e["value"] / _rev, 4)
    if "rev_g_ttm" in ro40:
        if "opm_ttm" in ro40:
            ro40["score_op"] = round(100 * (ro40["rev_g_ttm"] + ro40["opm_ttm"]), 1)
        if "fcf_margin_ttm" in ro40:
            ro40["score_fcf"] = round(100 * (ro40["rev_g_ttm"] + ro40["fcf_margin_ttm"]), 1)
            if "sbc_margin_ttm" in ro40:
                ro40["score_fcf_ex_sbc"] = round(
                    100 * (ro40["rev_g_ttm"] + ro40["fcf_margin_ttm"] - ro40["sbc_margin_ttm"]), 1)
    if ro40:
        out["rule_of_40"] = ro40
        if "score_op" in ro40:
            print(f"Rule of 40: 营收增速 {ro40.get('rev_g_ttm', 0):+.1%} + "
                  f"OPM {ro40.get('opm_ttm', 0):.1%} = {ro40['score_op']:.0f} 分"
                  + (f"（FCF 口径 {ro40['score_fcf']:.0f}"
                     + (f"，剔 SBC {ro40['score_fcf_ex_sbc']:.0f}"
                        if "score_fcf_ex_sbc" in ro40 else "") + "）"
                     if "score_fcf" in ro40 else ""))

# ---- 历史「已实现前瞻 PE」分布：供 engine.py 的目标 PE 越界诊断（pe_band_check）
# 引擎是零联网的确定性计算层，带子必须在数据层备好。属非核心项：任何失败只记
# pe_band_error 不阻断取数——但要留痕，不静默。VALUATION_NO_PE_BAND=1 可关闭。
if os.environ.get("VALUATION_NO_PE_BAND") != "1":
    from pe_band import compute_band, compute_ptbv_band, load_inputs
    _pb_inputs = None
    try:
        # 一次下载（companyfacts + yfinance 历史价 + 拆股表），PE/PS 两份带子共用——
        # compute_band 各自下载会翻倍 SEC/yfinance 请求且容易撞限速
        _pb_inputs = load_inputs(TICKER, EMAIL, years=5)
    except Exception as _e:
        out["pe_band_error"] = f"{type(_e).__name__}: {_e}"
        print(f"警告: 带子数据未取到（{out['pe_band_error']}）——PE/PS 带本次不生成",
              file=sys.stderr)
    if _pb_inputs:
        try:
            # basis="ntm"：必须与 engine 的前瞻期同源。engine 算 rev1 = TTM×(1+g)，
            # 前瞻期恒为 NTM 而非财年（见 valuation_service.fwd_window）。用 forward
            # （按财年切）只在 report_end = 财年末时才对，AMZN/META 这类 12 月财年公司
            # 在 Q1/Q2/Q3 报告期会系统性错位，pe_vs_history 的分位数就不可信。
            _band = compute_band(TICKER, EMAIL, years=5, basis="ntm", inputs=_pb_inputs)
            _band.pop("_sorted", None)
            out["pe_band"] = _band
            # 口径/年数一律从 _band 自身字段拼：写死字符串会在换 basis 时静默说谎
            # （曾切到 ntm 后仍打印 "forward"，排查与回归对比都会被带偏）
            print(f"PE带({_band['basis']},{_band['years']}y): 中位 {_band['median']:.1f}x  区间 "
                  f"{_band['min']:.1f}~{_band['max']:.1f}x  {_band['days']} 个交易日"
                  + ("  ⚠覆盖不足,锚停用" if _band.get("thin_coverage") else ""))
        except Exception as _e:
            out["pe_band_error"] = f"{type(_e).__name__}: {_e}"
            print(f"警告: PE 带未生成（{out['pe_band_error']}）——目标 PE 越界诊断本次不生效",
                  file=sys.stderr)
        # P/TBV 带（financials 模式）：金融股的估值锚是 P/B 系（E 带杠杆带周期，
        # 一个坏账周期就能打没；净资产相对稳定）——图3 的教科书结论，repo 的
        # financials 模式本来就按 P/TBV 建，带子给它历史分位锚。trailing 口径
        # （TBV 是存量，无 "已实现 NTM" 语义），消费方=engine ptbv_band_check +
        # 判断层 P/TBV 锚注入。
        if MODE == "financials":
            try:
                _tb = compute_ptbv_band(TICKER, EMAIL, years=5, inputs=_pb_inputs)
                out["ptbv_band"] = _tb
                print(f"P/TBV带(trailing,{_tb['years']}y): 中位 {_tb['median']:.2f}x  区间 "
                      f"{_tb['min']:.2f}~{_tb['max']:.2f}x  {_tb['days']} 个交易日")
            except Exception as _e:
                out["ptbv_band_error"] = f"{type(_e).__name__}: {_e}"
                print(f"警告: P/TBV 带未生成（{out['ptbv_band_error']}）——目标 P/TBV "
                      "越界诊断本次不生效", file=sys.stderr)
        # P/S 带（standard 模式）：同一套机械、分母换每股营收。近零利润票（COIN/
        # 微利期周期股）PE 带与 PE 腿一起失效，PS 是那个域的教科书参照——engine
        # 的近零利润守卫拿它给参考价。金融股不出（营收=总净收入口径，PS 无惯例）。
        if MODE == "standard":
            try:
                _psb = compute_band(TICKER, EMAIL, years=5, basis="ntm",
                                    metric="rps", inputs=_pb_inputs)
                _psb.pop("_sorted", None)
                out["ps_band"] = _psb
                print(f"P/S带({_psb['basis']},{_psb['years']}y): 中位 {_psb['median']:.2f}x  区间 "
                      f"{_psb['min']:.2f}~{_psb['max']:.2f}x  {_psb['days']} 个交易日")
            except Exception as _e:
                out["ps_band_error"] = f"{type(_e).__name__}: {_e}"
                print(f"警告: P/S 带未生成（{out['ps_band_error']}）——近零利润参照本次不生效",
                      file=sys.stderr)

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
    problems.append(f"核心损益科目无数据：{', '.join(missing)}"
                    "（该发行人未申报对应科目——保险/部分能源与REIT/外国银行的科目体系暂不支持）")
if MODE == "financials" and not out.get("equity_instant"):
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
    print(f"\n{TICKER} 不适配当前估值框架：", file=sys.stderr)
    for p in problems:
        print(f"- {p}", file=sys.stderr)
    sys.exit(1)
