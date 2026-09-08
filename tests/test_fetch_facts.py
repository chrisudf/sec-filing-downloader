# -*- coding: utf-8 -*-
"""fetch_facts 护栏纯函数用例：候选合并、YTD 差分、fill/override/子集守卫、
Q4 推导。场景全部来自真实公司踩过的坑（见各用例注释）。"""
from valuation.fetch_facts import (FILL_TAGS, OVERRIDE_TAGS, SPEC, TAGS,
                                   assemble_series, pick, quarterly_from_ytd,
                                   ttm_via_ytd)


def F(*rows):
    """rows: (start, end, val, filed)；start=None 为时点。"""
    return {"units": {"USD": [
        {"start": s, "end": e, "val": v, "filed": f}
        for s, e, v, f in rows]}}


def test_pick_latest_filed_wins():
    facts = {"A": F(("2025-01-01", "2025-03-31", 100, "2025-05-01"),
                    ("2025-01-01", "2025-03-31", 105, "2026-05-01"))}
    assert pick(facts, ["A"], "quarterly") == {"2025-03-31": 105}


def test_pick_same_filed_first_tag_wins():
    # 同一份申报同时标两个候选：列表顺序即优先级
    facts = {"A": F(("2025-01-01", "2025-03-31", 1, "2025-05-01")),
             "B": F(("2025-01-01", "2025-03-31", 2, "2025-05-01"))}
    assert pick(facts, ["A", "B"], "quarterly") == {"2025-03-31": 1}


def test_quarterly_from_ytd_differencing():
    # 10-Q 现金流是财年累计：Q2=H1-Q1、Q3=9M-H1、Q4=FY-9M
    facts = {"C": F(("2025-01-01", "2025-03-31", 10, "f"),
                    ("2025-01-01", "2025-06-30", 25, "f"),
                    ("2025-01-01", "2025-09-30", 45, "f"),
                    ("2025-01-01", "2025-12-31", 70, "f"))}
    assert quarterly_from_ytd(facts, ["C"]) == {
        "2025-03-31": 10, "2025-06-30": 15, "2025-09-30": 20, "2025-12-31": 25}


def test_quarterly_from_ytd_gap_guard():
    # 相邻两期间隔不是一个季度（80-100 天）时不差分
    facts = {"C": F(("2025-01-01", "2025-03-31", 10, "f"),
                    ("2025-01-01", "2025-09-30", 45, "f"))}
    assert quarterly_from_ytd(facts, ["C"]) == {"2025-03-31": 10}


def test_fill_only_fills_missing():
    # AVGO：净利润主标签缺季时用 ProfitLoss 补缺，已有值一律不覆盖
    facts = {"NetIncomeLoss": F(("2025-01-01", "2025-03-31", 100, "f")),
             "ProfitLoss": F(("2025-01-01", "2025-03-31", 999, "f"),
                             ("2025-04-01", "2025-06-30", 200, "f"))}
    _, q = assemble_series(facts, "net_income")
    assert q == {"2025-03-31": 100, "2025-06-30": 200}


def test_override_replaces():
    # SOFI：RevenuesNetOfInterestExpense 是损益表第一行，必须压过附注子集
    facts = {"RevenueFromContractWithCustomerExcludingAssessedTax":
             F(("2026-04-01", "2026-06-30", 153_577_000, "f")),
             "RevenuesNetOfInterestExpense":
             F(("2026-04-01", "2026-06-30", 1_218_676_000, "f"))}
    _, q = assemble_series(facts, "revenue")
    assert q["2026-06-30"] == 1_218_676_000


def test_revenue_subset_guard():
    # MET：RFCWC 是 ASC606 附注子集（2.4B），Revenues 才是总营收（77B）
    facts = {"RevenueFromContractWithCustomerExcludingAssessedTax":
             F((("2025-01-01"), "2025-12-31", 2_436_000_000, "f")),
             "Revenues": F(("2025-01-01", "2025-12-31", 77_084_000_000, "f"))}
    a, _ = assemble_series(facts, "revenue")
    assert a["2025-12-31"] == 77_084_000_000


def test_revenue_equal_tags_unchanged():
    # COST：两标签同值，守卫零影响
    facts = {"RevenueFromContractWithCustomerExcludingAssessedTax":
             F(("2025-01-01", "2025-12-31", 275_235, "f")),
             "Revenues": F(("2025-01-01", "2025-12-31", 275_235, "f"))}
    a, _ = assemble_series(facts, "revenue")
    assert a["2025-12-31"] == 275_235


def test_q4_derivation():
    facts = {"NetIncomeLoss": F(
        ("2025-01-01", "2025-03-31", 10, "f"),
        ("2025-04-01", "2025-06-30", 12, "f"),
        ("2025-07-01", "2025-09-30", 14, "f"),
        ("2025-01-01", "2025-12-31", 50, "f"))}
    _, q = assemble_series(facts, "net_income")
    assert q["2025-12-31"] == 50 - 36


def test_ttm_via_ytd():
    facts = {"C": F(("2025-01-01", "2025-12-31", 100, "f"),
                    ("2025-01-01", "2025-06-30", 40, "f"),
                    ("2026-01-01", "2026-06-30", 55, "f"))}
    annual = {"2025-12-31": 100}
    r = ttm_via_ytd(facts, ["C"], annual)
    assert r["value"] == 100 + 55 - 40


def test_baseline_contract_frozen():
    """基线 key 的候选列表喂估值判断层，不许静默改动。改这里 = 有意
    变更契约，必须同步确认 valuation_service/_compact_facts 的消费方。"""
    frozen = {
        "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                    "Revenues", "SalesRevenueNet",
                    "RevenueFromContractWithCustomerIncludingAssessedTax",
                    "RegulatedAndUnregulatedOperatingRevenue"],
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
        "lt_securities": ["MarketableSecuritiesNoncurrent",
                          "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
                          "LongTermInvestments"],
        "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
        "current_debt": ["DebtCurrent", "LongTermDebtCurrent"],
        "commercial_paper": ["CommercialPaper"],
        "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    }
    for k, tags in frozen.items():
        assert TAGS[k] == tags, f"基线科目 {k} 的候选列表被改动"
    assert FILL_TAGS["net_income"] == [
        "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]
    assert OVERRIDE_TAGS == {"revenue": ["RevenuesNetOfInterestExpense"]}
    assert set(SPEC) == set(TAGS)


def test_shares_no_q4_derivation():
    # 加权平均股本是均值：年度-前三季会推出负股数（AAPL 曾出 -30B）
    facts = {"WeightedAverageNumberOfDilutedSharesOutstanding": F(
        ("2025-01-01", "2025-03-31", 14.9e9, "f"),
        ("2025-04-01", "2025-06-30", 14.9e9, "f"),
        ("2025-07-01", "2025-09-30", 14.9e9, "f"),
        ("2025-01-01", "2025-12-31", 14.8e9, "f"))}
    _, q = assemble_series(facts, "shares_diluted")
    assert "2025-12-31" not in q  # 不推导，宁缺勿错


# ---- 推导 Q4 EPS 的拆股口径守卫 ----
from valuation.fetch_facts import _guard_derived_q4_eps


def _eps_out(eps_q4, ni_q4, sh_fy):
    return {
        "eps_diluted_quarterly": {"2024-01-28": eps_q4},
        "eps_diluted_annual": {"2024-01-28": 1.19},
        "net_income_quarterly": {"2024-01-28": ni_q4},
        "shares_diluted_annual": {"2024-01-28": sh_fy},
    }


def test_q4_eps_guard_drops_split_mix():
    # NVDA FY24 实况：推导 -0.25 vs 隐含 12.286B/24.89B=+0.49，符号翻转
    out = _eps_out(-0.25, 12.286e9, 24.89e9)
    _guard_derived_q4_eps(out)
    assert "2024-01-28" not in out["eps_diluted_quarterly"]


def test_q4_eps_guard_keeps_consistent():
    # NVDA FY26 实况：推导 1.76 vs 隐含 42.96B/24.5B=1.754，偏差 0.3%
    out = _eps_out(1.76, 42.96e9, 24.5e9)
    _guard_derived_q4_eps(out)
    assert out["eps_diluted_quarterly"]["2024-01-28"] == 1.76


def test_q4_eps_guard_small_abs_diff_exempt():
    # 银行优先股股利类口径差：隐含 0.05 vs 报 0.02，绝对差 3 美分豁免
    out = _eps_out(0.02, 0.05e9, 1e9)
    _guard_derived_q4_eps(out)
    assert out["eps_diluted_quarterly"]["2024-01-28"] == 0.02


def test_q4_eps_guard_non_fye_untouched():
    # 非财年末季不在守卫范围（推导只发生在财年末）
    out = {"eps_diluted_quarterly": {"2023-10-29": -9.99},
           "eps_diluted_annual": {"2024-01-28": 1.19},
           "net_income_quarterly": {"2023-10-29": 9.243e9},
           "shares_diluted_annual": {"2024-01-28": 24.89e9}}
    _guard_derived_q4_eps(out)
    assert out["eps_diluted_quarterly"]["2023-10-29"] == -9.99


def test_q4_eps_guard_missing_inputs_noop():
    out = _eps_out(-0.25, None, 24.89e9)
    _guard_derived_q4_eps(out)
    assert out["eps_diluted_quarterly"]["2024-01-28"] == -0.25


def test_q4_eps_guard_same_sign_large_deviation():
    # 同号但偏差 68%（2.0 vs 隐含 1.19）：35% 相对偏差闸必须抓住——
    # 只靠符号翻转闸抓不到（阈值哨兵：把 0.35 改大此用例必挂）
    out = _eps_out(2.0, 29.6e9, 24.89e9)  # 隐含 1.19
    _guard_derived_q4_eps(out)
    assert "2024-01-28" not in out["eps_diluted_quarterly"]


def test_q4_eps_guard_below_threshold_kept():
    # 同号偏差 ~26%（1.50 vs 隐含 1.19）< 35%：正常口径差保留
    out = _eps_out(1.50, 29.6e9, 24.89e9)
    _guard_derived_q4_eps(out)
    assert out["eps_diluted_quarterly"]["2024-01-28"] == 1.50


def test_q4_eps_guard_wired_into_build_facts():
    # 接线哨兵：守卫必须在 build_facts 主流程里被调用——
    # 单元用例只测纯函数，删掉调用行整套仍绿（评审 mutation 实测）
    import inspect
    from valuation.fetch_facts import build_facts
    assert "_guard_derived_q4_eps(out)" in inspect.getsource(build_facts)


# =====================================================================
# dividends 的 PaymentsOfOrdinaryDividends 回退（2026-08-31）
# 实测：PFE **只**标这个标签，主列表两个全空 -> 分红整列 None（0/12 季）。
# =====================================================================

def test_dividends_fallback_tag_declared():
    """契约：回退标签走 fill（只补缺），不进主列表——它与主列表口径可能不同
    （普通股 vs 含优先股，MO 2007 两者差 2 倍），平级合并会让『同期取 filed
    最新』在两个口径间随机跳。"""
    assert SPEC["dividends"]["tags"] == ["PaymentsOfDividends",
                                         "PaymentsOfDividendsCommonStock"]
    assert SPEC["dividends"]["fill"] == ["PaymentsOfOrdinaryDividends"]
    assert SPEC["dividends"]["ytd_flow"] is True


def test_dividends_fill_covers_empty_main_tags():
    """PFE 形态：主列表两个标签一条数据都没有，全靠回退标签。"""
    facts = {"PaymentsOfOrdinaryDividends": F(
        ("2026-01-01", "2026-03-29", 2445e6, "2026-05-01"))}
    _, q = assemble_series(facts, "dividends")
    assert q == {"2026-03-29": 2445e6}


def test_dividends_fill_ytd_differencing():
    """回退标签只有 YTD 帧时也要差分——现金流科目的 10-Q 本来就是财年累计。
    PFE 的离散季帧只有 18 期，靠差分才补到 72 期、拿到最新季。"""
    facts = {"PaymentsOfOrdinaryDividends": F(
        ("2026-01-01", "2026-03-29", 2445e6, "2026-05-01"),
        ("2026-01-01", "2026-06-28", 4896e6, "2026-08-01"))}
    _, q = assemble_series(facts, "dividends")
    assert q["2026-03-29"] == 2445e6
    assert round(q["2026-06-28"]) == round(4896e6 - 2445e6)   # = 2451e6


def test_dividends_no_cross_tag_ytd_differencing():
    """GOOGL 形态：Q1 只在主标签下、H1 只在回退标签下。跨标签相减得到的是
    两个口径的差额而不是当季金额 —— 宁可缺一期，不要一个看起来合理的错数。"""
    facts = {"PaymentsOfDividends": F(
                 ("2026-01-01", "2026-03-31", 2542e6, "2026-04-30")),
             "PaymentsOfOrdinaryDividends": F(
                 ("2026-01-01", "2026-06-30", 5231e6, "2026-07-23"))}
    _, q = assemble_series(facts, "dividends")
    assert q == {"2026-03-31": 2542e6}, "不得跨标签差分出 2026-06-30"


def test_dividends_fill_never_overwrites():
    """主列表已有值的期一律不动，回退标签给出不同数也不许覆盖。"""
    facts = {"PaymentsOfDividends": F(
                 ("2026-01-01", "2026-03-31", 100e6, "2026-04-30")),
             "PaymentsOfOrdinaryDividends": F(
                 ("2026-01-01", "2026-03-31", 999e6, "2026-07-23"))}
    _, q = assemble_series(facts, "dividends")
    assert q == {"2026-03-31": 100e6}


def test_fill_ytd_differencing_only_for_ytd_flow_items():
    """非现金流科目（ytd_flow=False）的 fill 不做差分：net_income 的
    ProfitLoss 回退是离散期口径，差分会算出垃圾。"""
    assert SPEC["net_income"]["ytd_flow"] is False
    facts = {"ProfitLoss": F(("2026-01-01", "2026-03-31", 10e6, "2026-04-30"),
                             ("2026-01-01", "2026-06-30", 25e6, "2026-07-30"))}
    _, q = assemble_series(facts, "net_income")
    assert "2026-06-30" not in q or q["2026-06-30"] == 25e6


# =====================================================================
# 营业利润推导回退 + 券商路由（0021）
# COHR：op_income 停报（0020 只把报错分类修对），rev/cogs/rnd/sga 同窗齐全时
# 自下而上推导救回整票；HOOD：券商利润表「总收入−总运营费用→税前」，两个既有
# 签名（RNIE / 新鲜 OperatingIncomeLoss）都不认，误落 standard 判不适配。
# =====================================================================

import pytest

_W = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]


def _cohr_out(**over):
    """COHR 形态：营收 TTM 7,118.2M，组件四季合计 cogs 4,449.1 / rnd 723.0 /
    sga 1,044.6（$M，按原始美元喂）。"""
    def q(total):
        return {k: total * 1e6 / 4 for k in _W}
    d = {"cogs_quarterly": q(4449.1), "rnd_quarterly": q(723.0),
         "sga_quarterly": q(1044.6)}
    d.update(over)
    return d


def test_derive_op_income_cohr_shape():
    from valuation.fetch_facts import _derive_op_income_ttm
    ttm = {"revenue": {"value": 7118.2e6, "quarters": list(_W)},
           "op_income": {"value": None, "error": "口径滞后：最新期 2024-06-30"}}
    _derive_op_income_ttm(_cohr_out(), ttm)
    op = ttm["op_income"]
    assert op["value"] == pytest.approx(901.5e6)
    assert op["derived"] is True and op["quarters"] == list(_W)
    assert "推导值" in op["note"] and "摊销/重组" in op["note"]


def test_derive_op_income_missing_component_hard_fails():
    """sga 缺窗口内一季：不推导、原 error 原样保留——宁缺勿错。"""
    from valuation.fetch_facts import _derive_op_income_ttm
    out = _cohr_out()
    del out["sga_quarterly"]["2026-06-30"]
    ttm = {"revenue": {"value": 7118.2e6, "quarters": list(_W)},
           "op_income": {"value": None, "error": "口径滞后：最新期 2024-06-30"}}
    _derive_op_income_ttm(out, ttm)
    assert ttm["op_income"] == {"value": None, "error": "口径滞后：最新期 2024-06-30"}


def test_derive_op_income_untouched_when_reported():
    from valuation.fetch_facts import _derive_op_income_ttm
    ttm = {"revenue": {"value": 7118.2e6, "quarters": list(_W)},
           "op_income": {"value": 900e6, "quarters": list(_W)}}
    _derive_op_income_ttm(_cohr_out(), ttm)
    assert ttm["op_income"]["value"] == 900e6
    assert "derived" not in ttm["op_income"]   # 申报值不许被盖


def test_derive_op_income_wired_into_build_facts():
    import inspect
    from valuation.fetch_facts import build_facts
    assert "_derive_op_income_ttm(out, ttm)" in inspect.getsource(build_facts)


def _tag(end):
    return {"units": {"USD": [{"end": end}]}}


def test_detect_mode_broker_routes_financials():
    """HOOD 形态：Revenues+税前新鲜、无 OperatingIncomeLoss、券商标签在场。"""
    from valuation.fetch_facts import _detect_mode
    facts = {"Revenues": _tag("2026-06-30"),
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
                 _tag("2026-06-30"),
             "InterestAndDividendIncomeOperating": _tag("2026-06-30")}
    mode, _spec = _detect_mode(facts, "us-gaap")
    assert mode == "financials"


def test_detect_mode_stopped_op_income_without_broker_tags_stays_standard():
    """COHR 形态：营业利润停报但不是券商——留在 standard 吃推导回退。"""
    from valuation.fetch_facts import _detect_mode
    facts = {"Revenues": _tag("2026-06-30"),
             "OperatingIncomeLoss": _tag("2024-06-30"),
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
                 _tag("2026-06-30")}
    assert _detect_mode(facts, "us-gaap")[0] == "standard"


def test_detect_mode_fresh_op_income_with_broker_tag_stays_standard():
    """经营性公司带上券商类标签（罕见）也不误路由：营业利润新鲜即 standard。"""
    from valuation.fetch_facts import _detect_mode
    facts = {"Revenues": _tag("2026-06-30"),
             "OperatingIncomeLoss": _tag("2026-06-30"),
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
                 _tag("2026-06-30"),
             "InterestIncomeExpenseNet": _tag("2026-06-30")}
    assert _detect_mode(facts, "us-gaap")[0] == "standard"


def test_detect_mode_bank_signature_unchanged():
    from valuation.fetch_facts import _detect_mode
    facts = {"RevenuesNetOfInterestExpense": _tag("2026-06-30")}
    assert _detect_mode(facts, "us-gaap")[0] == "financials"


def test_detect_mode_broker_stale_pretax_stays_standard():
    """税前利润也停报的票不路由：fin 模式核心科目照样缺，换模式救不了。"""
    from valuation.fetch_facts import _detect_mode
    facts = {"Revenues": _tag("2026-06-30"),
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
                 _tag("2024-06-30"),
             "InterestAndDividendIncomeOperating": _tag("2026-06-30")}
    assert _detect_mode(facts, "us-gaap")[0] == "standard"


def test_detect_mode_stale_broker_tag_stays_standard():
    """0022 C17：UHT 型经营性公司带一枚陈年 broker tag（历史上出现过、早已停报）。
    presence-anywhere 判据下，它一旦改列报停掉 OperatingIncomeLoss 就会被静默
    按银行估值（P/TBV 框架、fin prompt、CFO/capex 全压掉）——broker tag 必须与
    旁边的 rev/pretax/op 条件同一时效闸（距营收锚 <=400 天）。"""
    from valuation.fetch_facts import _detect_mode
    facts = {"Revenues": _tag("2026-06-30"),
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
                 _tag("2026-06-30"),
             "InterestIncomeExpenseNet": _tag("2018-09-30")}
    assert _detect_mode(facts, "us-gaap")[0] == "standard"


def test_detect_mode_fresh_components_rescue_takes_precedence():
    """0022 C17：COHR 型（停报营业利润）+ 恰好带一枚新鲜 broker tag：
    rev/cogs/rnd/sga 全新鲜说明利润表仍是经营性列报（券商不报 cogs），必须留在
    standard 吃 _derive_op_income_ttm 救援——_detect_mode 跑在救援之前，
    路由 financials 会结构性关掉它。"""
    from valuation.fetch_facts import _detect_mode
    facts = {"Revenues": _tag("2026-06-30"),
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
                 _tag("2026-06-30"),
             "InterestIncomeExpenseNet": _tag("2026-06-30"),
             "OperatingIncomeLoss": _tag("2024-06-30"),
             "CostOfGoodsAndServicesSold": _tag("2026-06-30"),
             "ResearchAndDevelopmentExpense": _tag("2026-06-30"),
             "SellingGeneralAndAdministrativeExpense": _tag("2026-06-30")}
    assert _detect_mode(facts, "us-gaap")[0] == "standard"


def test_detect_mode_broker_with_stale_components_still_financials():
    """真券商改列报（HOOD 型）不受救援优先影响：cogs/rnd/sga 缺或陈旧时
    推导救援本就无从谈起，broker 路由照走。"""
    from valuation.fetch_facts import _detect_mode
    facts = {"Revenues": _tag("2026-06-30"),
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
                 _tag("2026-06-30"),
             "InterestAndDividendIncomeOperating": _tag("2026-06-30"),
             "CostOfGoodsAndServicesSold": _tag("2019-12-31")}
    assert _detect_mode(facts, "us-gaap")[0] == "financials"
