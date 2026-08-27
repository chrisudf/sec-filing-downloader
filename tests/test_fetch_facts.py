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
