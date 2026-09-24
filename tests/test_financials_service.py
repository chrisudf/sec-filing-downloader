# -*- coding: utf-8 -*-
"""financials 服务的口径组合规则与端到端重塑用例。"""
import pytest

from app import financials_service as fs
from app import segments_service as ss
from app.common import FREQ_PATTERN
from app.edgar import EdgarError


def make_inst(data: dict, n: int):
    """按 key 造 inst() 闭包，缺的 key 全 null。"""
    def inst(name):
        return data.get(name, [None] * n)
    return inst


def test_total_debt_classified():
    # AAPL 型：真非流动 + 当期到期 + 商业票据
    inst = make_inst({"lt_debt_noncurrent": [80e9],
                      "lt_debt_current": [10e9],
                      "commercial_paper": [5e9]}, 1)
    assert fs._total_debt(inst) == [95e9]


def test_total_debt_lt_total_no_double_count():
    # AT&T 型：LongTermDebt 总口径已含当期到期，只补 DebtCurrent 超出部分
    inst = make_inst({"lt_debt_total": [133_402e6],
                      "debt_current": [9_477e6],
                      "lt_debt_current": [7_386e6]}, 1)
    assert fs._total_debt(inst) == [133_402e6 + (9_477e6 - 7_386e6)]


def test_total_debt_null_over_wrong():
    # KO 型：没有任何长期腿时给 null，不许把商业票据当总债务
    inst = make_inst({"commercial_paper": [1_139e6]}, 1)
    assert fs._total_debt(inst) == [None]


def test_total_debt_combined_fallback():
    # SOFI/REIT 型：只有长短期合并口径
    inst = make_inst({"debt_combined": [3_301e6]}, 1)
    assert fs._total_debt(inst) == [3_301e6]


def test_total_debt_st_full_line_no_double_count():
    # IBM 型：ShortTermBorrowings 是整行短期债务（已含当期到期），附注又单标当期到期
    # 2026-06-30：56,212 + 5,775 = 61,987（公司口径）；旧规则得 67,759
    inst = make_inst({"lt_debt_noncurrent": [56_212e6],
                      "lt_debt_current": [5_772e6],
                      "st_borrowings": [5_775e6]}, 1)
    assert fs._total_debt(inst) == [(56_212 + 5_772 + 5_775) * 1e6]   # 旧行为保持
    assert fs._total_debt(inst, st_full_line=True) == [61_987e6]


def test_total_debt_st_full_line_absorbs_cp():
    # IBM 2019-12-31：整行 8,797 已含 CP 304 与当期到期 7,522 → 公司口径 62,899
    inst = make_inst({"lt_debt_noncurrent": [54_102e6],
                      "lt_debt_current": [7_522e6],
                      "commercial_paper": [304e6],
                      "st_borrowings": [8_797e6]}, 1)
    assert fs._total_debt(inst, st_full_line=True) == [62_899e6]


def test_total_debt_st_full_line_lt_total_branch():
    # 只有 LongTermDebt 总口径时：整行里的当期到期已在总口径中，只补超出部分
    inst = make_inst({"lt_debt_total": [60_000e6],
                      "lt_debt_current": [5_000e6],
                      "st_borrowings": [5_300e6]}, 1)
    assert fs._total_debt(inst, st_full_line=True) == [60_300e6]
    # 缺 lc 拆不开：不加（宁少勿双计）
    inst = make_inst({"lt_debt_total": [60_000e6], "st_borrowings": [5_300e6]}, 1)
    assert fs._total_debt(inst, st_full_line=True) == [60_000e6]


def test_st_full_line_detection():
    ibm = {  # 真实 IBM 形状（$B）：2018 两标签并存相等；其后 sb≈lc 多期
        "debt_current_instant": {"2018-12-31": 10.207, "2017-12-31": 6.987},
        "st_borrowings_instant": {"2018-12-31": 10.207, "2025-12-31": 6.424,
                                  "2026-03-31": 8.655, "2026-06-30": 5.775},
        "lt_debt_current_instant": {"2018-12-31": 7.051, "2025-12-31": 6.424,
                                    "2026-03-31": 7.554, "2026-06-30": 5.772},
    }
    assert fs._st_borrowings_is_full_line(ibm)
    # 只凭判据②也成立（没有 DebtCurrent 重叠期）
    only2 = {k: v for k, v in ibm.items() if k != "debt_current_instant"}
    assert fs._st_borrowings_is_full_line(only2)
    # 只凭判据①也成立（sb≈lc 的期不足两期）
    only1 = {"debt_current_instant": {"2018-12-31": 10.207},
             "st_borrowings_instant": {"2018-12-31": 10.207},
             "lt_debt_current_instant": {"2018-12-31": 7.051}}
    assert fs._st_borrowings_is_full_line(only1)
    # 按定义打标签：短借与当期到期是两笔独立的钱
    honest = {"st_borrowings_instant": {"2025-12-31": 3.0, "2026-06-30": 2.1},
              "lt_debt_current_instant": {"2025-12-31": 5.0, "2026-06-30": 4.4}}
    assert not fs._st_borrowings_is_full_line(honest)
    # 单期巧合相等不够
    once = {"st_borrowings_instant": {"2025-12-31": 5.0, "2026-06-30": 2.1},
            "lt_debt_current_instant": {"2025-12-31": 5.0, "2026-06-30": 4.4}}
    assert not fs._st_borrowings_is_full_line(once)
    # DebtCurrent == 短借 但当期到期为 0：相等是平凡的，不能当证据
    trivial = {"debt_current_instant": {"2025-12-31": 3.0},
               "st_borrowings_instant": {"2025-12-31": 3.0},
               "lt_debt_current_instant": {"2025-12-31": 0.0}}
    assert not fs._st_borrowings_is_full_line(trivial)
    assert not fs._st_borrowings_is_full_line({})


def test_nearest_instant():
    inst = {"2026-06-27": 1.0, "2026-03-28": 2.0}
    assert fs._nearest_instant(inst, ["2026-06-27"]) == [1.0]
    assert fs._nearest_instant(inst, ["2026-06-30"]) == [1.0]   # 3 天内
    assert fs._nearest_instant(inst, ["2026-09-30"]) == [None]  # 超 10 天窗口


def test_helpers():
    assert fs._add([1, None], [2, None]) == [3, None]
    assert fs._sub([5, None], [2, 1]) == [3, None]
    assert fs._ratio([1, None], [4, 4]) == [0.25, None]
    assert fs._ratio([1], [0]) == [None]


def _facts(n_quarters=5, bank=False):
    """最小合成 facts：单调递增营收，净利=营收 20%。"""
    ends = [f"202{5 + (3 + i) // 4}-{(3 + i) % 4 * 3 + 3:02d}-30"
            for i in range(n_quarters)]
    ends = ["2025-06-30", "2025-09-30", "2025-12-30", "2026-03-30",
            "2026-06-30"][:n_quarters]
    def series(mult):
        return {e: (i + 1) * mult for i, e in enumerate(ends)}
    return {
        "ticker": "TEST", "cik": 1, "bank_format": bank,
        "revenue_quarterly": series(100e6), "revenue_annual": {},
        "cogs_quarterly": series(40e6), "gross_profit_quarterly": {},
        "net_income_quarterly": series(20e6),
        "op_income_quarterly": series(30e6),
        "cfo_quarterly": series(25e6), "capex_quarterly": series(5e6),
        "ttm": {},
    }


def test_reshape_identities():
    r = fs._reshape(_facts(), {"name": "T", "fiscalYearEnd": ""}, "quarterly", 3)
    inc, cf = r["income"], r["cashflow"]
    for i in range(len(r["periods"])):
        assert inc["margins"]["net"][i] == round(
            inc["net_income"][i] / inc["revenue"][i], 4)
        assert cf["fcf"][i] == cf["ocf"][i] - cf["capex"][i]
        # 毛利回退 = 营收 - 营业成本
        assert inc["gross_profit"][i] == inc["revenue"][i] - inc["cogs"][i]


def test_reshape_wires_st_full_line_detection():
    """接线：_reshape 必须把发行人级判据传进 _total_debt（纯函数测不到调用点）。"""
    facts = _facts()
    ends = sorted(facts["revenue_quarterly"])
    facts["lt_debt_noncurrent_instant"] = {e: 56_000e6 for e in ends}
    facts["lt_debt_current_instant"] = {e: 5_772e6 for e in ends}
    facts["st_borrowings_instant"] = {e: 5_775e6 for e in ends}   # IBM 型整行
    r = fs._reshape(facts, {"name": "T"}, "quarterly", 3)
    assert r["balance"]["total_debt"] == [61_775e6] * len(ends)


def test_reshape_op_income_derived_flags():
    facts = _facts()
    facts["op_income_derived"] = {"quarterly": ["2026-03-30", "2026-06-30"]}
    r = fs._reshape(facts, {"name": "T"}, "quarterly", 3)
    assert r["income"]["op_income_derived"] == [False, False, False, True, True]
    r = fs._reshape(_facts(), {"name": "T"}, "quarterly", 3)
    assert not any(r["income"]["op_income_derived"])


def test_reshape_bank_format_suppresses_gross():
    # SOFI 教训：银行报表没有毛利概念，恰好有成本标签也不许硬算 82%
    r = fs._reshape(_facts(bank=True), {"name": "T"}, "quarterly", 3)
    assert all(v is None for v in r["income"]["cogs"])
    assert all(v is None for v in r["income"]["gross_profit"])
    assert all(v is None for v in r["income"]["margins"]["gross"])


def test_reshape_staleness_guard():
    # JPM/DUK 教训：营收标签断更时报错，不许把十年前的数据当最新画
    facts = _facts()
    facts["revenue_quarterly"] = {"2014-12-31": 1e9}
    facts["net_income_quarterly"] = {"2026-06-30": 1e9}
    with pytest.raises(EdgarError) as e:
        fs._reshape(facts, {"name": "T"}, "quarterly", 3)
    assert e.value.status == 422


def test_reshape_gap_warning():
    # XOM 教训：新申报主体断档时给 warning，不让相隔一年的柱贴着画
    facts = _facts()
    facts["revenue_quarterly"] = {"2025-06-30": 1e9, "2026-06-30": 2e9}
    facts["net_income_quarterly"] = dict(facts["revenue_quarterly"])
    facts["cfo_quarterly"] = {}
    facts["capex_quarterly"] = {}
    r = fs._reshape(facts, {"name": "T"}, "quarterly", 3)
    assert r["warning"] is not None


B = 1e9


def test_pick_securities_sparse_afs_loses_to_full_classified():
    # AMZN 季度 3 年窗实测：有价证券行 12 期齐全；AFS 只有 4 期，而且是 Anthropic
    # 可转债（中位 45.8B）。旧规则「中位数最大」选了后者：8 期空白 + 最新一期多 53B
    cls = [v * B for v in (14.6, 13.4, 12.2, 17.9, 13.0, 22.4, 28.4, 35.4,
                           27.3, 36.2, 41.3, 44.8)]
    afs = [None] * 8 + [23.7 * B, 45.8 * B, 42.2 * B, 97.9 * B]
    assert fs._pick_securities(cls, [None] * 12, afs) is cls


def test_pick_securities_afs_not_superset_is_dropped_even_when_full():
    # 定时炸弹：AMZN 再过几个季度 AFS 覆盖就过半、中位数又更大——光靠覆盖度门槛会
    # 重新选中 Anthropic 可转债。任何一期 AFS 明显小于证券行 ⇒ 不是证券合计
    cls = [27.3 * B, 36.2 * B, 41.3 * B, 44.8 * B]
    afs = [23.7 * B, 45.8 * B, 42.2 * B, 97.9 * B]
    assert not fs._afs_is_superset(afs, cls)
    assert fs._pick_securities(cls, [None] * 4, afs) is cls
    # NVDA 2026 拆行后 AFS 只剩债券腿（39.5 vs 债券+股票 52.0）：同理
    assert fs._pick_securities([38.5 * B, 52.0 * B], [None] * 2,
                               [52.1 * B, 39.5 * B]) == [38.5 * B, 52.0 * B]


def test_pick_securities_insurer_afs_total_still_wins():
    # MET：分类行零星（~5B），AFS 是整个债券组合（~300B）且逐期都大 ⇒ 仍选 AFS
    cls = [5.3 * B] * 12
    afs = [293.8 * B] * 12
    assert fs._pick_securities(cls, [18.1 * B] * 12, afs) is afs


def test_pick_securities_half_coverage_is_eligible():
    # AIG 年度：分类 4/10、无分类整行 10/10（18.8B 的其他投资）、AFS 6/10（251B 组合）。
    # 纯「覆盖度优先」会换成 18.8B 的子项；门槛是最佳覆盖的一半，AFS 6 期合格
    cls = [12.3 * B] * 4 + [None] * 6
    unc = [18.8 * B] * 10
    afs = [None] * 4 + [251.1 * B] * 6
    assert fs._pick_securities(cls, unc, afs) is afs


def test_pick_securities_unclassified_bank_and_ties():
    # SOFI：无分类整行与 AFS 同覆盖，中位数整行更大 ⇒ 整行（行为不变）
    unc, afs = [2.2 * B] * 12, [2.1 * B] * 12
    assert fs._pick_securities([None] * 12, unc, afs) is unc
    # 并列时按候选顺序取分类行
    cls = [5.0 * B] * 12
    assert fs._pick_securities(cls, [None] * 12, list(cls)) is cls
    # 三源全空：退回分类行（全 null）
    empty = [None] * 3
    assert fs._pick_securities(empty, [None] * 3, [None] * 3) is empty


def test_private_equity_gain_amzn_other_nonop():
    # AMZN 实测四季：Q3'25 / Q4'25 / Q1'26 / Q2'26
    up = [7.2 * B, 0.42 * B, 12.33 * B, 50.49 * B]
    eq = [0.15 * B, 0.86 * B, -0.88 * B, 1.3 * B]
    oth = [10.19 * B, 1.18 * B, 15.65 * B, 53.41 * B]
    # Q4'25 的 0.42B 小于同期股权投资损益 0.86B，按"可能在 eq 里"保守不单列
    assert fs._private_equity_gain(up, eq, oth) == [7.2 * B, None, 12.33 * B, 50.49 * B]


def test_private_equity_gain_inside_equity_gain_not_double_counted():
    # GOOGL 2021-03：eq 4.84 ⊇ 上调 4.68 ⇒ 不单列（否则与 equity_inv_gain 算两遍）
    assert fs._private_equity_gain([4.68 * B], [4.84 * B], [0.37 * B]) == [None]
    # NVDA 2026-04：eq 与 other_nonop 同为 ~15.9B（同一笔两次标注），上调 2.6B 在里面
    assert fs._private_equity_gain([2.6 * B], [15.94 * B], [15.93 * B]) == [None]
    # GOOGL 2022-06：eq 被有价股票浮亏拖成负数，但 other_nonop 0.26B 装不下 0.91B
    # ⇒ 只能在 eq 里；单比 eq 与上调的大小会误判
    assert fs._private_equity_gain([0.91 * B], [-0.25 * B], [0.26 * B]) == [None]
    # NVDA 2026-01：无 eq 标签、other_nonop 7.85B 装得下 1.28B ⇒ 单列
    assert fs._private_equity_gain([1.28 * B], [None], [7.85 * B]) == [1.28 * B]
    # 缺值/非正：一律不单列
    assert fs._private_equity_gain([None, 0.0, 1 * B], [None] * 3,
                                   [5 * B, 5 * B, None]) == [None, None, None]


def test_reshape_wires_securities_and_private_equity():
    """接线：_reshape 必须走新的证券选源与私募重估单列（纯函数测不到调用点）。"""
    facts = _facts()
    ends = sorted(facts["revenue_quarterly"])
    facts["st_securities_instant"] = {e: 30 * B for e in ends}
    facts["afs_securities_total_instant"] = {ends[-1]: 97.9 * B}
    # 末期 AMZN 型（单列）；倒数第二期 GOOGL 型（在股权投资损益里，不许单列）——
    # 后者保证接线走的是判定函数而不是直接透传 pe_upward_adj
    facts["pe_upward_adj_quarterly"] = {ends[-2]: 4.68 * B, ends[-1]: 50.49 * B}
    facts["equity_inv_gain_quarterly"] = {ends[-2]: 4.84 * B, ends[-1]: 1.3 * B}
    facts["other_nonop_quarterly"] = {ends[-2]: 0.37 * B, ends[-1]: 53.41 * B}
    r = fs._reshape(facts, {"name": "T"}, "quarterly", 3)
    assert r["balance"]["securities"] == [30 * B] * len(ends)
    assert r["oneoff"]["private_equity_gain"] == [None] * (len(ends) - 1) + [50.49 * B]


def test_shared_param_contract():
    # 两个端点的 freq/years 口径必须永远一致（同一常量）
    assert FREQ_PATTERN == "^(quarterly|annual)$"
    assert fs.FREQ_PATTERN is ss.FREQ_PATTERN
