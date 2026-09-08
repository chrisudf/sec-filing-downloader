# -*- coding: utf-8 -*-
"""prompt/TUNING ↔ 校验层/引擎 契约（0013）。

prompt 向判断层公开的执法规则若与校验器真实执行的不一致，模型会按文档自检
通过、然后被代码拒绝——每一处漂移都直接兑换成浪费的 retry（单次运行总调用
<=3，烧不起）。本文件把三类漂移钉死：
- 数字契约：prompt 里写的执法值（wacc−tg 间距等）必须等于校验器的行为边界；
- 行为契约：margins 谷底锚的 TTM→历史中位回退、pe/m1 的税前/营业利润双键——
  文档描述的分叉行为用校验器实测钉住；
- 封顶死锁：锚窗 P50 > 上界 60 时 band_meta 预告封顶、pe_band_check 豁免偏离旗，
  两端必须同门槛（TSLA 锚窗 P50 ~230x、ISRG 61.6x 实测死锁）。
"""
import ast
import re
from pathlib import Path

import pytest

from app.valuation_service import _band_meta, _validate_judgment

ROOT = Path(__file__).resolve().parent.parent
STD_PROMPT = (ROOT / "valuation" / "judgment_prompt.md").read_text(encoding="utf-8")
FIN_PROMPT = (ROOT / "valuation" / "judgment_prompt_financials.md").read_text(encoding="utf-8")
TUNING = (ROOT / "valuation" / "TUNING.md").read_text(encoding="utf-8")

# pe_band_check 逐字抽自生产源码（engine.py 是模块级脚本，import 即执行）
_ENG_SRC = (ROOT / "valuation" / "engine.py").read_text(encoding="utf-8")
_SEGS = [ast.get_source_segment(_ENG_SRC, n) for n in ast.parse(_ENG_SRC).body
         if isinstance(n, ast.FunctionDef) and n.name in ("_pctile_rank", "pe_band_check")]
assert len(_SEGS) == 2
_NS = {}
exec("\n\n".join(_SEGS), _NS)
pe_band_check = _NS["pe_band_check"]


def _mk(**over):
    """最小合法 standard judgment；子情景可用 over={'bear': {...}} 覆盖。"""
    def sc(g, opm, pe, m1, margins, wacc=0.10, tg=0.025, g0=0.04, gN=0.03, tax=0.1):
        return dict(g=g, opm=opm, tax=tax, pe=pe, m1=m1, m2=0, wacc=wacc, tg=tg,
                    g0=g0, gN=gN, margins=list(margins))
    d = dict(
        fwd_shares=1000.0, net_cash=0.0, net_cash_note="x",
        adj_ni=100.0, adj_note="x", other_income=0.0, other_income_note="x",
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


def _mkfin(**over):
    """最小合法 financials judgment。"""
    def sc(g, nm, pe, ptbv, wacc=0.12, tg=0.03):
        return dict(g=g, nm=nm, pe=pe, ptbv=ptbv, wacc=wacc, tg=tg)
    d = dict(
        fwd_shares=1050.0, adj_ni=200.0, adj_note="x",
        rationale={k: "x" for k in ("g", "nm", "pe", "ptbv", "wacc")},
        notes=["x"],
        scenarios=dict(bear=sc(-0.05, 0.04, 10, 0.9),
                       base=sc(0.10, 0.10, 15, 1.5),
                       bull=sc(0.20, 0.14, 20, 2.0)),
    )
    for k, v in over.items():
        if k in ("bear", "base", "bull"):
            d["scenarios"][k].update(v)
        else:
            d[k] = v
    return d


# ---- 数字契约：wacc−tg 间距（prompt 写的数 = 校验器的行为边界，两模式各一）----

@pytest.mark.parametrize("mode,prompt,mk", [("standard", STD_PROMPT, _mk),
                                            ("financials", FIN_PROMPT, _mkfin)])
def test_tg_gap_prompt_value_is_enforced_boundary(mode, prompt, mk):
    m = re.search(r"须比wacc小至少([0-9.]+)", prompt)
    assert m, "prompt 未写明 wacc−tg 执法间距"
    gap = float(m.group(1))
    # 恰好等于间距：放行；差 0.001：拒绝——prompt 的数就是行为边界
    _validate_judgment(mk(base={"wacc": 0.10, "tg": round(0.10 - gap, 4)}), mode)
    with pytest.raises(ValueError, match="wacc-tg"):
        _validate_judgment(mk(base={"wacc": 0.10, "tg": round(0.10 - gap + 0.001, 4)}),
                           mode)


# ---- 数字契约：standard 的 wacc 硬边界 / margins 硬顶 / 四类引擎红旗 ----

def test_std_prompt_documents_wacc_hard_bound():
    """schema 注的 0.08-0.12 是参考量级，硬边界 [0.05, 0.2] 必须同时公开。"""
    assert "[0.05, 0.2]" in STD_PROMPT
    _validate_judgment(_mk(bear={"wacc": 0.05, "tg": 0.005}), "standard")
    with pytest.raises(ValueError, match="wacc"):
        _validate_judgment(_mk(bear={"wacc": 0.045, "tg": 0.0}), "standard")


def test_std_prompt_documents_margins_hard_clamp():
    """上限 1.2×FCF 率但硬顶 0.9——此前文档只写 1.2 倍，没写 0.9。"""
    assert "硬顶 0.9" in STD_PROMPT
    # fcf_margin=0.85 → 1.2×0.85=1.02 被钳到 0.9：0.9 放行、0.91 拒绝。
    # 三档谷底都抬过 0.4×0.85=0.34，让谷底护栏不抢在钳位之前触发
    hi = dict(bear={"margins": [0.35] * 10}, base={"margins": [0.40] * 10})
    _validate_judgment(_mk(bull={"margins": [0.5] * 9 + [0.9]}, **hi),
                       "standard", fcf_margin=0.85)
    with pytest.raises(ValueError, match=r"margins 必须是"):
        _validate_judgment(_mk(bull={"margins": [0.5] * 9 + [0.91]}, **hi),
                           "standard", fcf_margin=0.85)


def test_std_prompt_lists_all_four_engine_reds():
    """引擎有四类 red（>8×TTM / TV>75% / P-FCF 界外 / DCF 每股 <=0），文档只写过三类。"""
    for needle in (">8×TTM", "终值占 EV>75%", "[5,90]", "DCF 每股价值 <=0"):
        assert needle in STD_PROMPT, f"prompt 缺引擎红旗：{needle}"


def test_std_prompt_documents_margins_yearly_ordering():
    assert "margins 逐年" in STD_PROMPT


# ---- 行为契约：margins 谷底锚的回退链（TTM → 历史年度中位 → 停用）----

def test_trough_fallback_documented_in_prompt_and_tuning():
    """2026-08-31 起校验器在 TTM FCF 率 <=2%/负时退到历史年度中位——文档此前
    仍写"自动跳过"，模型照文档给深谷底、然后被一条文档里不存在的规则拒绝。"""
    assert "历史年度 FCF 利润率中位" in STD_PROMPT
    assert "自动跳过该下限" not in STD_PROMPT
    assert "历史年度 FCF 利润率中位" in TUNING


def test_prompt_negative_fcf_note_matches_trough_fallback():
    """同一份 prompt 不许自相矛盾（PR #16 评审）。

    上面第 5 条已写「谷底锚会退到历史年度中位」，下面的 ⚠️ 段却还说负 FCF 时
    「两道护栏都退化、DCF 基本是裸奔」。判断层照后者写深谷底 → 自检通过 →
    被校验器拒 → 白烧一轮 retry（单次运行总调用 <=3）。"""
    assert "两道护栏都退化" not in STD_PROMPT
    assert "裸奔" not in STD_PROMPT
    # 行为侧同时钉住：当期 FCF 为负 + 历史中位健康 → 下限照样拦得住
    # （既有用例只覆盖了当期为正但 <=2% 的 0.01，负值正是 ⚠️ 段说的场景）
    with pytest.raises(ValueError, match="历史年度 FCF 利润率中位"):
        _validate_judgment(_mk(), "standard", fcf_margin=-0.015,
                           hist_fcf_margin=0.054)


def test_trough_rejection_names_ttm_anchor():
    d = _mk()   # bear 谷底 0.02 < 0.4×0.10
    with pytest.raises(ValueError, match="当前 TTM FCF 利润率"):
        _validate_judgment(d, "standard", fcf_margin=0.10)


def test_trough_rejection_names_hist_median_anchor():
    """TTM <=2% 时退到历史中位执法，拒绝文案必须点名换过锚。"""
    d = _mk()
    with pytest.raises(ValueError, match="历史年度 FCF 利润率中位"):
        _validate_judgment(d, "standard", fcf_margin=0.01, hist_fcf_margin=0.10)


def test_trough_skipped_only_when_both_anchors_unusable():
    _validate_judgment(_mk(), "standard", fcf_margin=0.01, hist_fcf_margin=0.015)
    _validate_judgment(_mk(), "standard", fcf_margin=-0.3, hist_fcf_margin=None)


# ---- 行为契约：pe/tax 键在税前、m1/m2 键在营业利润（两键会分家）----

def test_op_loss_pretax_profit_requires_m_zero_but_normal_pe():
    """营业亏损 + 大额利息收入 → 税前为正：m1/m2 必须 0，pe 照常规边界。"""
    ok = _mk(other_income=1000.0,
             bear={"opm": -0.05, "pe": 8, "m1": 0, "m2": 0})
    _validate_judgment(ok, "standard", rev0=1000.0)
    with pytest.raises(ValueError, match="营业利润为负，m1/m2 必须写 0"):
        _validate_judgment(_mk(other_income=1000.0,
                               bear={"opm": -0.05, "pe": 8, "m1": 5, "m2": 0}),
                           "standard", rev0=1000.0)
    # pe=0 是亏损协议的专用声明值——税前为正时不许用（证明 pe 键在税前而非营业利润）
    with pytest.raises(ValueError, match="pe 需在"):
        _validate_judgment(_mk(other_income=1000.0,
                               bear={"opm": -0.05, "pe": 0, "m1": 0, "m2": 0}),
                           "standard", rev0=1000.0)


def test_op_profit_pretax_loss_requires_pe_tax_zero_but_normal_m():
    """营业微利 + 大额净利息支出 → 税前为负：pe/tax 必须 0，m1 照常规边界。"""
    ok = _mk(other_income=-200.0,
             bear={"opm": 0.01, "tax": 0, "pe": 0, "m1": 10},
             base={"opm": 0.25}, bull={"opm": 0.30})
    _validate_judgment(ok, "standard", rev0=1000.0)
    with pytest.raises(ValueError, match="目标 PE 必须写 0"):
        _validate_judgment(_mk(other_income=-200.0,
                               bear={"opm": 0.01, "tax": 0, "pe": 10, "m1": 10},
                               base={"opm": 0.25}, bull={"opm": 0.30}),
                           "standard", rev0=1000.0)
    with pytest.raises(ValueError, match="tax 必须为 0"):
        _validate_judgment(_mk(other_income=-200.0,
                               bear={"opm": 0.01, "tax": 0.1, "pe": 0, "m1": 10},
                               base={"opm": 0.25}, bull={"opm": 0.30}),
                           "standard", rev0=1000.0)


# ---- 封顶死锁：band_meta 预告 + pe_band_check 豁免（同门槛 60）----

def _band(p50_recent, p10=100.0, p90=400.0, mn=70.0, mx=1000.0):
    return {"basis": "ntm", "years": 5, "days": 1100, "min": mn, "max": mx,
            "pctiles": {"10": p10, "25": p10 + 20, "50": p50_recent,
                        "75": p90 - 50, "90": p90},
            "recent": {"years": 3, "days": 700,
                       "pctiles": {"10": p10, "25": p10 + 20, "50": p50_recent,
                                   "75": p90 - 50, "90": p90}}}


def test_band_meta_warns_when_anchor_p50_exceeds_pe_cap():
    txt = _band_meta("standard", {"pe_band": _band(230.0)})
    assert "超出校验上界 pe<=60" in txt and "封顶" in txt


def test_band_meta_no_cap_notice_when_anchor_below_cap():
    txt = _band_meta("standard", {"pe_band": _band(25.0, p10=21.0, p90=29.0,
                                                   mn=18.0, mx=32.0)})
    assert "超出校验上界" not in txt


def test_pe_band_check_capped_base_gets_explainer_not_deviation_flag():
    """TSLA 形态：锚窗 P50 230x，base 只能给上界 60——60 低于带 min=70，
    此前必吃「从未出现过」+「界外」旗，把上界强加的偏离算到判断层头上。"""
    dd = {}
    w = pe_band_check("base", 60.0, _band(230.0), dd)
    assert len(w) == 1 and w[0][0] == "yellow"
    assert "封顶" in w[0][1] and "豁免" in w[0][1]
    assert dd["pe_vs_history"]["capped_at_validation_limit"] == 60


def test_pe_band_check_capped_nonbase_silent_with_ddiag():
    dd = {}
    assert pe_band_check("bull", 60.0, _band(230.0), dd) == []
    assert dd["pe_vs_history"]["capped_at_validation_limit"] == 60


def test_pe_band_check_below_cap_pe_not_exempted():
    """判断层自己给了 59（≠上界）——不是封顶，偏离旗照打。"""
    dd = {}
    w = pe_band_check("base", 59.0, _band(230.0), dd)
    assert w and "封顶" not in w[0][1]
    assert "capped_at_validation_limit" not in dd["pe_vs_history"]


def test_pe_band_check_cap_value_with_low_anchor_normal_path():
    """pe=60 但锚窗 P50 只有 25——不是死锁，走正常检查（60 > max 32 → 从未出现过）。"""
    dd = {}
    w = pe_band_check("base", 60.0, _band(25.0, p10=21.0, p90=29.0, mn=18.0, mx=32.0), dd)
    assert w and "从未出现过" in w[0][1]
    assert "capped_at_validation_limit" not in dd["pe_vs_history"]


# ---- 反双重计数键集契约（0022 C14）：prompt 必须与校验器同为 pe/m1/m2 ----

def test_double_count_prompt_names_m2():
    """校验器自始按 (pe, m1, m2)（bear m2>0 时）执法，prompt 却只写 pe/m1——
    真双分部票按文档自检通过、被一条文档里不存在的 m2 规则拒绝，烧一次 retry。"""
    assert "pe/m1/m2 不得低于 0.6×base" in STD_PROMPT
    assert "pe/m1/m2 不得高于 1.4×base" in STD_PROMPT
    assert "m2 仅在真双分部" in STD_PROMPT


def test_double_count_enforces_m2_when_two_segment():
    """行为钉死：bear m2>0（真双分部）时 bull m2 > 1.4×base m2 被拒且点名 m2。"""
    d = _mk(seg1_share=0.7,
            bear={"m2": 6}, base={"m2": 8}, bull={"m2": 12})   # 12 > 1.4×8=11.2
    with pytest.raises(ValueError, match=r"bull 双重计数.*m2"):
        _validate_judgment(d, "standard", rev0=1000.0)


def test_double_count_m2_within_linkage_passes():
    d = _mk(seg1_share=0.7, bear={"m2": 6}, base={"m2": 8}, bull={"m2": 11})
    _validate_judgment(d, "standard", rev0=1000.0)


# ---- ppce 时效触发的日期来源（0022 C11）：不许引用不存在的注入字段 ----

def test_ppce_age_clause_uses_injected_dates_not_phantom_field():
    """检查清单 #3 曾说触发值在「元数据里的『距报告期天数』」——prompt 组装
    （valuation_service:~1408）从未注入过这个字段；照字面找不到的模型可能整条
    跳过 ppce 核对。改为指示按注入的 date= 与 filing 索引标题里的报告期自行推算。"""
    assert "元数据里的「距报告期天数」" not in STD_PROMPT
    assert "『距报告期天数』" not in STD_PROMPT
    assert "自行推算" in STD_PROMPT and "date= 减去" in STD_PROMPT


# ---- margins 上界锚随口径闸换锚（0022 C2/C12）：文档与行为一起钉 ----

def test_margins_cap_gate_documented_and_reanchors():
    assert "偏离 FACTS 的 TTM 营收 >10%" in STD_PROMPT
    assert "上限（1.2×）与谷底锚都改锚历史年度中位" in STD_PROMPT
    hi = dict(bear={"margins": [0.60] * 10}, base={"margins": [0.65] * 10},
              bull={"margins": [0.70] * 10})
    # 口径闸（fcf_margin=None）+ 历史中位 0.70：上界 0.84，「维持现状」放行
    _validate_judgment(_mk(**hi), "standard", fcf_margin=None, hist_fcf_margin=0.70)
    # 历史锚也缺：静态 0.65，拒绝文案点名锚（retry 的唯一信息来源）
    with pytest.raises(ValueError, match="静态 0.65"):
        _validate_judgment(_mk(**hi), "standard", fcf_margin=None,
                           hist_fcf_margin=None)
