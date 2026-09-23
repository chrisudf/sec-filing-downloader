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


def test_shared_param_contract():
    # 两个端点的 freq/years 口径必须永远一致（同一常量）
    assert FREQ_PATTERN == "^(quarterly|annual)$"
    assert fs.FREQ_PATTERN is ss.FREQ_PATTERN
