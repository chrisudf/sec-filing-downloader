# -*- coding: utf-8 -*-
"""SOTP 腿「加回无形资产摊销」诊断（0030）——只诊断，不改任何腿的数。

动机：vintages 43 个 gate-clean base 样本里 37 个 SOTP < PE，11 只票全中。
第一嫌疑是口径错配：分部可比倍数多按摊销前利润报，却乘在摊销后的合并 op1 上。
IBM 2026-09-23 实跑：base 偏离 +29% → 加回摊销 $2,944M 后 +3%。
"""
import ast
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SRC = (ROOT / "valuation" / "engine.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)
_NAMES = ("_isnum", "amort_ttm_musd", "sotp_addback_diag", "leg_multiple_crosscheck")
_SEGS = [ast.get_source_segment(_SRC, n) for n in _TREE.body
         if isinstance(n, ast.FunctionDef) and n.name in _NAMES]
assert len(_SEGS) == len(_NAMES)
_NS = {"date": date}
exec("\n\n".join(_SEGS), _NS)
amort_ttm_musd = _NS["amort_ttm_musd"]
sotp_addback_diag = _NS["sotp_addback_diag"]
leg_multiple_crosscheck = _NS["leg_multiple_crosscheck"]

# IBM 真实季度摊销（$，现金流量表 YTD 差分后）
IBM_Q = {"2025-06-30": 687e6, "2025-09-30": 699e6, "2025-12-31": 710e6,
         "2026-03-31": 719e6, "2026-06-30": 816e6}
# IBM base 情景（bundle config）：pe_target 186.4、fwd_shares 956、net_cash −56,000、
# op1 11,971、seg1_share 0.6、m1 18 / m2 11
IBM_BASE = dict(pe_target=186.4, fwd_shares=956, net_cash=-56000, op1=11971,
                seg1_share=0.6, m1=18, m2=11)


def test_amort_ttm_four_consecutive_quarters():
    v, note = amort_ttm_musd({"amortization_quarterly": IBM_Q})
    assert v == pytest.approx(699 + 710 + 719 + 816)
    assert note.startswith("TTM")


def test_amort_ttm_falls_back_to_fy_on_gap():
    q = {k: v for k, v in IBM_Q.items() if k != "2025-12-31"}
    v, note = amort_ttm_musd({"amortization_quarterly": q,
                              "amortization_annual": {"2025-12-31": 2737e6}})
    assert v == pytest.approx(2737) and "FY(2025-12-31)" in note


def test_amort_ttm_stale_annual_rejected():
    # AAPL 实况：最后一次标该概念是 FY2017——不许拿九年前的数冒充当前摊销
    f = {"data_latest": "2026-06-27",
         "amortization_annual": {"2017-09-30": 1200e6}}
    assert amort_ttm_musd(f) == (None, None)
    f["amortization_annual"] = {"2025-12-31": 817e6}     # AMZN 型：半年前的 FY 可用
    f["data_latest"] = "2026-06-30"
    assert amort_ttm_musd(f)[0] == pytest.approx(817)


def test_amort_ttm_stale_quarters_rejected():
    old = {"2019-03-31": 1e8, "2019-06-30": 1e8, "2019-09-30": 1e8, "2019-12-31": 1e8}
    assert amort_ttm_musd({"data_latest": "2026-06-30",
                           "amortization_quarterly": old}) == (None, None)
    fresh = dict(IBM_Q)
    assert amort_ttm_musd({"data_latest": "2026-06-30",
                           "amortization_quarterly": fresh})[0] == pytest.approx(2944)


def test_amort_ttm_none_when_absent():
    # 老 facts.json 没有该序列：返回 None，调用方不出诊断（不拿 0 冒充"没有摊销"）
    assert amort_ttm_musd({}) == (None, None)
    assert amort_ttm_musd(None) == (None, None)


def test_addback_ibm_base_gap_mostly_closes():
    d = sotp_addback_diag(amort=2944, **IBM_BASE)
    assert d["ev_ebit_sotp"] == 15.2
    assert d["sotp_vs_pe"] == pytest.approx(0.287, abs=0.001)
    assert d["sotp_vs_pe_after_addback"] == pytest.approx(0.033, abs=0.001)
    assert d["amort_share_of_op1"] == pytest.approx(0.246, abs=0.001)
    # 加回后的 SOTP 每股 = (15.2 × (11,971+2,944) − 56,000) / 956
    assert d["sotp_ps_addback"] == pytest.approx((15.2 * 14915 - 56000) / 956, abs=0.1)


@pytest.mark.parametrize("over", [
    {"amort": None}, {"amort": 0}, {"amort": -5}, {"op1": 0}, {"pe_target": 0},
    {"fwd_shares": 0}, {"m1": 0, "m2": 0},
])
def test_addback_none_on_degenerate_inputs(over):
    kw = dict(IBM_BASE, amort=2944)
    kw.update(over)
    assert sotp_addback_diag(**kw) is None


def test_crosscheck_message_carries_addback_verdict():
    ab = sotp_addback_diag(amort=2944, **IBM_BASE)
    w = leg_multiple_crosscheck(IBM_BASE["pe_target"], IBM_BASE["fwd_shares"],
                                IBM_BASE["net_cash"], IBM_BASE["op1"],
                                IBM_BASE["seg1_share"], IBM_BASE["m1"], IBM_BASE["m2"],
                                addback=ab)
    assert w and "+29%" in w[0][1]
    assert "加回 EBIT 后偏离 +3%" in w[0][1] and "口径错配可解释大部分差距" in w[0][1]


def test_crosscheck_addback_not_the_cause():
    # 摊销很小：加回后仍超阈值 → 指向 seg1_share/倍数本身
    ab = sotp_addback_diag(amort=100, **IBM_BASE)
    w = leg_multiple_crosscheck(*[IBM_BASE[k] for k in (
        "pe_target", "fwd_shares", "net_cash", "op1", "seg1_share", "m1", "m2")],
        addback=ab)
    assert "摊销口径不是主因" in w[0][1]


def test_crosscheck_unchanged_without_addback():
    """老 facts 无摊销序列：文案与 0023 逐字一致（不多不少）。"""
    w = leg_multiple_crosscheck(*[IBM_BASE[k] for k in (
        "pe_target", "fwd_shares", "net_cash", "op1", "seg1_share", "m1", "m2")])
    assert w[0][1].endswith("请对齐 pe 与 m1/m2 的口径")


def test_crosscheck_within_tol_stays_silent_even_with_addback():
    ab = sotp_addback_diag(amort=2944, **dict(IBM_BASE, m1=22, m2=14))
    w = leg_multiple_crosscheck(*[dict(IBM_BASE, m1=22, m2=14)[k] for k in (
        "pe_target", "fwd_shares", "net_cash", "op1", "seg1_share", "m1", "m2")],
        addback=ab)
    assert w == []


def test_engine_wiring():
    assert "_AMORT, _AMORT_NOTE = amort_ttm_musd(facts)" in _SRC
    assert "ddiag[\"sotp_addback\"] = _ab" in _SRC
    assert "addback=_ab)" in _SRC


def test_fetch_facts_amortization_item():
    from valuation.fetch_facts import SPEC
    assert SPEC["amortization"]["tags"] == ["AmortizationOfIntangibleAssets"]
    assert SPEC["amortization"]["ytd_flow"] is True     # IBM 在现金流量表按 YTD 申报
