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
