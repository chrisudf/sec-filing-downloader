# -*- coding: utf-8 -*-
"""seg1_share 与发行人 XBRL 分部申报的对照（0023）。

回归对象是 MSFT 2026-09-08 那次真实运行：判断层给
seg1="Microsoft 整体（软件/云一体化）"、seg1_share=1.0、rationale 里零理由，
于是引擎按 seg1_share >= 0.85 把 SOTP 腿降级为参考项、整条腿退出综合——
而发行人自己按三个分部申报（Intelligent Cloud 43.7% / Productivity 42.0% /
More Personal Computing 14.3%）。校验层当时只查 0<=x<=1，一个字的证据都不要。

管道此前根本没取过分部数据（facts.json 里一条分部字段都没有），所以这一组
测试同时覆盖：注入用的纯函数、校验闸、引擎呈现层黄旗、两处常量的一致性。
"""
import ast
from pathlib import Path

import pytest

from app.valuation_service import (SOTP_SEG1_CAP, _check_seg1_share,
                                   _seg_label, _segment_facts, _segment_lines,
                                   _validate_judgment)

# ---- 按 test_valuation_guards 的手法从 engine.py 逐字抽纯函数（engine 是模块级
# 脚本，import 即执行）。seg_share_crosscheck 用到模块级常量 SOTP_SEG1_CAP，
# 必须连常量一起注入——顺便把它抠出来做两处一致性的钉子。
_ENG = Path(__file__).resolve().parent.parent / "valuation" / "engine.py"
_SRC = _ENG.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)
_SEG = [ast.get_source_segment(_SRC, n) for n in _TREE.body
        if isinstance(n, ast.FunctionDef)
        and n.name in ("_isnum", "seg_share_crosscheck")]
assert len(_SEG) == 2, _SEG
_ENG_CAP = next(n.value.value for n in _TREE.body
                if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "SOTP_SEG1_CAP")
_NS = {"SOTP_SEG1_CAP": _ENG_CAP}
exec(chr(10).join(_SEG), _NS)
seg_share_crosscheck = _NS["seg_share_crosscheck"]


def _seg(members, period="2026-06-30", freq="quarterly"):
    """segments payload 的最小形状（fetch_segments.build_segments 的输出子集）。"""
    return {"axes": {"segment": {freq: {period: {
        "members": dict(members), "total": sum(members.values()),
        "reconciled": True, "derived": True}}}}}


# MSFT 实测三分部（2026-06-30 季度营收，$）
_MSFT = _seg({"IntelligentCloudMember": 39306e6,
              "ProductivityAndBusinessProcessesMember": 37847e6,
              "MorePersonalComputingMember": 12854e6})


# ---- 常量一致性：校验层与引擎必须同一条降级线 ----

def test_sotp_cap_pinned_across_modules():
    """两处各写一份 0.85 会让「校验层放行的 config 在引擎里静默关腿」。"""
    assert SOTP_SEG1_CAP == _ENG_CAP == 0.85


# ---- _segment_facts ----

def test_segment_facts_msft_shape():
    sf = _segment_facts(_MSFT)
    assert sf["n"] == 3 and sf["period"] == "2026-06-30" and sf["freq"] == "quarterly"
    assert sf["top_share"] == pytest.approx(39306 / 90007, abs=1e-4)
    # 按额从大到小，判断层看第一行就是主分部
    assert sf["members"][0][0] == "IntelligentCloudMember"


def test_segment_facts_prefers_quarterly_over_annual():
    d = _seg({"A": 6.0, "B": 4.0})
    d["axes"]["segment"]["annual"] = {"2025-12-31": {
        "members": {"A": 90.0, "B": 10.0}, "total": 100.0}}
    assert _segment_facts(d)["freq"] == "quarterly"


def test_segment_facts_falls_back_to_annual():
    d = {"axes": {"segment": {"annual": {"2025-12-31": {
        "members": {"A": 6.0, "B": 4.0}, "total": 10.0}}}}}
    sf = _segment_facts(d)
    assert sf["freq"] == "annual" and sf["top_share"] == pytest.approx(0.6)


def test_segment_facts_takes_latest_period():
    d = _seg({"A": 6.0, "B": 4.0})
    d["axes"]["segment"]["quarterly"]["2026-09-30"] = {
        "members": {"A": 1.0, "B": 9.0}, "total": 10.0}
    assert _segment_facts(d)["period"] == "2026-09-30"


@pytest.mark.parametrize("payload", [
    None, {}, {"axes": {}}, {"axes": {"segment": {}}},
    _seg({"OnlyOneMember": 100.0}),          # 单成员不算分部结构
    _seg({"A": 0.0, "B": 0.0}),              # 全零：没有可用占比
    _seg({"A": float("nan"), "B": 1.0}),     # NaN 被 _isnum 之外的 >0 判据剔掉
])
def test_segment_facts_none_when_no_usable_structure(payload):
    assert _segment_facts(payload) is None


def test_segment_facts_drops_nonpositive_members():
    """负额/零额成员（对账残差、抵消列）不进占比分母。"""
    sf = _segment_facts(_seg({"A": 6.0, "B": 4.0, "Elim": -1.0, "Z": 0.0}))
    assert sf["n"] == 2 and sf["top_share"] == pytest.approx(0.6)


# ---- _seg_label ----

@pytest.mark.parametrize("raw,want", [
    ("IntelligentCloudMember", "Intelligent Cloud"),
    ("AmazonWebServicesSegmentMember", "Amazon Web Services"),
    ("MorePersonalComputingMember", "More Personal Computing"),
    ("US", "US"),
])
def test_seg_label(raw, want):
    assert _seg_label(raw) == want


# ---- _segment_lines（注入文案）----

def test_segment_lines_warns_when_multi_segment():
    """发行人多分部且集中度低于降级线时，必须预告「关腿要给理由」。"""
    out = _segment_lines(_segment_facts(_MSFT))
    assert "3 个分部" in out and "43.7%" in out
    assert "rationale.sotp" in out and "拒收" in out
    # 原 token 一并给出——判断层要能拿它回财报核对
    assert "[IntelligentCloudMember]" in out
    # 口径提醒：这是营收不是利润
    assert "营业利润" in out and "参照不是答案" in out


def test_segment_lines_no_warning_when_issuer_concentrated():
    """发行人自己就集中（>=85%）时不预告——判断层给高 seg1_share 本就合理。"""
    out = _segment_lines(_segment_facts(_seg({"A": 95.0, "B": 5.0})))
    assert "rationale.sotp" not in out


def test_segment_lines_absent_is_explicit():
    """缺对照物要说出口，不能留白——沉默会让判断层以为有背书。"""
    out = _segment_lines(None)
    assert "无可用的分部营收申报" in out and "SECTIONS" in out


# ---- _check_seg1_share（校验闸）----

def _rat(**over):
    r = {k: "x" for k in ("g", "opm", "pe", "m1", "rl", "wacc", "dcf_margin")}
    r.update(over)
    return r


def test_check_blocks_msft_shape():
    """MSFT 实测形态：多分部 + seg1_share=1.0 + 无 rationale.sotp -> 拒收。"""
    with pytest.raises(ValueError, match="rationale.sotp"):
        _check_seg1_share({"seg1_share": 1.0, "rationale": _rat()},
                          _segment_facts(_MSFT))


def test_check_error_names_the_actual_segments():
    """拒绝文案要点名发行人的真实分部，否则判断层无从下手。"""
    with pytest.raises(ValueError) as e:
        _check_seg1_share({"seg1_share": 1.0, "rationale": _rat()},
                          _segment_facts(_MSFT))
    assert "Intelligent Cloud 44%" in str(e.value)
    assert "3 个分部" in str(e.value)


def test_check_passes_with_rationale_sotp():
    """给了理由就放行——利润集中度高于营收集中度是合法的。"""
    _check_seg1_share(
        {"seg1_share": 1.0,
         "rationale": _rat(sotp="MPC 分部营业利润率仅 4%，见 10-K 分部表")},
        _segment_facts(_MSFT))


def test_check_passes_below_cap():
    """seg1_share < 0.85：SOTP 腿照常入综合，没有可关的腿，不判罚。"""
    _check_seg1_share({"seg1_share": 0.6, "rationale": _rat()},
                      _segment_facts(_MSFT))


def test_check_passes_at_issuer_concentration():
    """发行人自己就 >=85%：判断层给 1.0 与申报一致，不该拦。"""
    _check_seg1_share({"seg1_share": 1.0, "rationale": _rat()},
                      _segment_facts(_seg({"A": 95.0, "B": 5.0})))


def test_check_passes_without_segment_facts():
    """没有对照物就不判罚——不能拿缺数当证据反过来指控判断层。"""
    _check_seg1_share({"seg1_share": 1.0, "rationale": _rat()}, None)


def test_check_blank_rationale_sotp_is_not_a_reason():
    """空串/空白不算理由。"""
    with pytest.raises(ValueError):
        _check_seg1_share({"seg1_share": 1.0, "rationale": _rat(sotp="   ")},
                          _segment_facts(_MSFT))


def test_check_tolerates_missing_rationale_key():
    """rationale 缺失时不崩在这条闸上（更根本的错由 rationale 结构校验先报）。"""
    with pytest.raises(ValueError, match="rationale.sotp"):
        _check_seg1_share({"seg1_share": 1.0}, _segment_facts(_MSFT))


# ---- 穿过 _validate_judgment 的集成 ----

def _mk(**over):
    def sc(g, opm, pe, m1, margins, wacc=0.10, tg=0.025, g0=0.04, gN=0.03, tax=0.1):
        return dict(g=g, opm=opm, tax=tax, pe=pe, m1=m1, m2=0, wacc=wacc, tg=tg,
                    g0=g0, gN=gN, margins=list(margins))
    d = dict(
        fwd_shares=1000.0, net_cash=0.0, net_cash_note="x",
        adj_ni=100.0, adj_note="x", other_income=0.0, other_income_note="x",
        seg1="A", seg2="B", seg1_share=0.9, rationale=_rat(), notes=["x"],
        scenarios=dict(
            bear=sc(-0.05, 0.05, 10, 10, [0.02, 0.03, 0.04, 0.05, 0.06,
                                          0.07, 0.08, 0.08, 0.09, 0.09], wacc=0.11),
            base=sc(0.05, 0.10, 13, 13, [0.03, 0.04, 0.06, 0.07, 0.09,
                                         0.10, 0.11, 0.12, 0.13, 0.14]),
            bull=sc(0.13, 0.15, 15, 15, [0.05, 0.08, 0.11, 0.13, 0.15,
                                         0.16, 0.17, 0.18, 0.19, 0.20], wacc=0.09, tg=0.03),
        ),
    )
    d.update(over)
    return d


def test_validate_blocks_unjustified_sotp_optout():
    with pytest.raises(ValueError, match="rationale.sotp"):
        _validate_judgment(_mk(seg1_share=0.9), "standard",
                           seg_facts=_segment_facts(_MSFT))


def test_validate_passes_when_justified():
    _validate_judgment(_mk(seg1_share=0.9, rationale=_rat(sotp="见 10-K 分部表")),
                       "standard", seg_facts=_segment_facts(_MSFT))


def test_validate_default_no_seg_facts_is_backward_compatible():
    """不传 seg_facts（老调用点 / 取数失败）时行为与 0023 之前逐字相同。"""
    _validate_judgment(_mk(seg1_share=0.9), "standard")


# ---- 引擎呈现层黄旗 ----

def test_engine_flags_gap_when_sotp_dropped():
    w = seg_share_crosscheck(1.0, 0.437, 3, sotp_in_blend=False)
    assert len(w) == 1 and w[0][0] == "yellow"
    assert "整条腿退出综合" in w[0][1] and "+56%" in w[0][1]


def test_engine_flags_gap_but_notes_sotp_still_in():
    w = seg_share_crosscheck(0.20, 0.60, 3, sotp_in_blend=True)
    assert len(w) == 1 and "照常入综合" in w[0][1] and "只作口径提示" in w[0][1]


def test_engine_silent_within_gate():
    assert seg_share_crosscheck(0.50, 0.44, 3, sotp_in_blend=True) == []


@pytest.mark.parametrize("s1,rev,n", [
    (None, 0.44, 3), (1.0, None, 3), (1.0, 0.44, None), (1.0, 0.44, 0),
    (True, 0.44, 3),   # bool 不是数字（_isnum 谓词）
])
def test_engine_silent_without_comparator(s1, rev, n):
    """缺对照物不出旗——没有对照就没有对照结论。"""
    assert seg_share_crosscheck(s1, rev, n, sotp_in_blend=False) == []
