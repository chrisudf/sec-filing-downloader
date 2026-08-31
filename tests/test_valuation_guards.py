# -*- coding: utf-8 -*-
"""判断层护栏：margins 情景排序 + 期后资本事件声明（2026-08-31）。

回归对象是 INTC 2026-08-30 那次真实运行：net_cash 停在旧报告期 → bear 的 P/FCF
红旗是假的 → gate 打回后判断层上修 bear.margins 越过 base → 自相矛盾的假设发货。
"""
import copy

import pytest

from app.valuation_service import _validate_judgment


def _mk(**over):
    """最小合法 standard config；子情景可用 over={'bear': {...}} 覆盖。"""
    def sc(g, opm, pe, m1, margins, wacc=0.10, tg=0.025, g0=0.04, gN=0.03, tax=0.1):
        return dict(g=g, opm=opm, tax=tax, pe=pe, m1=m1, m2=0, wacc=wacc, tg=tg,
                    g0=g0, gN=gN, margins=list(margins))
    d = dict(
        fwd_shares=1000.0, net_cash=0.0, net_cash_note="x",
        adj_ni=100.0, adj_note="x", other_income=0.0,
        seg1="A", seg2="B", seg1_share=0.9,
        rationale={k: "x" for k in ("g", "opm", "pe", "m1", "rl", "wacc", "dcf_margin")},
        notes=["x"],
        scenarios=dict(
            bear=sc(-0.05, 0.05, 10, 10, [0.02, 0.03, 0.04, 0.05, 0.06,
                                          0.07, 0.08, 0.08, 0.09, 0.09], wacc=0.11),
            base=sc(0.05, 0.10, 13, 13, [0.03, 0.04, 0.06, 0.07, 0.09,
                                         0.10, 0.11, 0.12, 0.13, 0.14]),
            bull=sc(0.13, 0.15, 15, 15, [0.05, 0.08, 0.11, 0.13, 0.15,
                                         0.16, 0.17, 0.18, 0.19, 0.20], wacc=0.09, tg=0.03),
        ),
    )
    for k, v in over.items():
        if k in ("bear", "base", "bull"):
            d["scenarios"][k].update(v)
        else:
            d[k] = v
    return d


# ---- fix#1：margins 跨情景排序 ----

def test_margins_ordering_ok():
    _validate_judgment(_mk(), "standard")


def test_margins_bear_above_base_blocked():
    """INTC 实测形态：bear 首年 4% > base 首年 3%。v2 只排序标量，这条曾静默通过。"""
    d = _mk(bear={"margins": [0.04, 0.05, 0.06, 0.07, 0.08,
                              0.09, 0.10, 0.10, 0.11, 0.11]})
    with pytest.raises(ValueError, match=r"margins 第 1 年"):
        _validate_judgment(d, "standard")


def test_margins_base_above_bull_blocked():
    d = _mk(base={"margins": [0.05, 0.09, 0.12, 0.14, 0.16,
                              0.17, 0.18, 0.19, 0.20, 0.21]})
    with pytest.raises(ValueError, match=r"margins 第 2 年"):
        _validate_judgment(d, "standard")


def test_margins_equal_allowed():
    """持平不算倒挂——与标量排序用同一个 <= 判据。"""
    same = [0.03, 0.04, 0.06, 0.07, 0.09, 0.10, 0.11, 0.12, 0.13, 0.14]
    _validate_judgment(_mk(bear={"margins": same}), "standard")


def test_margins_violation_reported_at_first_bad_year():
    d = _mk(bear={"margins": [0.02, 0.03, 0.04, 0.08, 0.06,
                              0.07, 0.08, 0.08, 0.09, 0.09]})
    with pytest.raises(ValueError, match=r"margins 第 4 年"):
        _validate_judgment(d, "standard")


# ---- fix#3'：期后资本事件声明 ----

EV = {"date": "2026-08-18", "kind": "增发", "amount_musd": 19700.0,
      "note": "8-K 2026-08-10 定价"}


def test_ppce_absent_ok():
    """字段可选——8 份既有 prev_config 都没有它，不能因此全部失效。"""
    d = _mk()
    assert "post_period_capital_events" not in d
    _validate_judgment(d, "standard")


@pytest.mark.parametrize("val", [[EV], [], [dict(EV, amount_musd=-14200.0, kind="回购")]])
def test_ppce_valid_shapes(val):
    _validate_judgment(_mk(post_period_capital_events=val), "standard")


@pytest.mark.parametrize("bad,msg", [
    ("增发 200 亿", "必须是数组"),
    ([{k: v for k, v in EV.items() if k != "note"}], "须含 date/kind/amount_musd/note"),
    ([dict(EV, note="   ")], r"note 必填"),
    ([dict(EV, amount_musd="19.7B")], r"amount_musd 必须是数字"),
])
def test_ppce_invalid_shapes(bad, msg):
    with pytest.raises(ValueError, match=msg):
        _validate_judgment(_mk(post_period_capital_events=bad), "standard")
