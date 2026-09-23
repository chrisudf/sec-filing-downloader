# -*- coding: utf-8 -*-
"""口径冲突表态闸（0029）：NTM 分位与 trailing 无滞后分位方向相反时，要求判断层对
PE 锚口径表态（pe_regime），并把两个 trailing 倍数（GAAP / 调整后）算好摆出。

回归对象是 IBM 2026-09-23 那次真实运行：NTM 带内 P68、trailing 26.8x 低于 P25、
带子滞后 576 天；判断层写"锚未过时"，依据是 现价÷$11.25=20.5x——$11.25 含 2025Q4
约 $2B 税收利得，按它自己的 adj_ni 是 25.3x，结论相反。
"""
import ast
import copy
from pathlib import Path

import pytest

from app.valuation_service import PE_REGIMES, _band_meta, _validate_judgment

ROOT = Path(__file__).resolve().parent.parent
_SRC = (ROOT / "valuation" / "engine.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)
_SEGS = [ast.get_source_segment(_SRC, n) for n in _TREE.body
         if isinstance(n, ast.FunctionDef)
         and n.name in ("_isnum", "regime_conflict_warnings")]
assert len(_SEGS) == 2
_ENG_REGIMES = next(ast.literal_eval(n.value) for n in _TREE.body
                    if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", None) == "PE_REGIMES")
_NS = {"PE_REGIMES": _ENG_REGIMES}
exec("\n\n".join(_SEGS), _NS)
regime_conflict_warnings = _NS["regime_conflict_warnings"]


def test_regimes_pinned_across_modules():
    """校验层放行的值引擎却不认 → red 永远消不掉；两处必须逐字一致。"""
    assert PE_REGIMES == _ENG_REGIMES == ("band", "recent", "blend")


# ---- 引擎闸 ----

IBM_TRW = {"span": {"end": "2025-02-24", "lag_days": 576},
           "fwd_pe_now": 24.3, "fwd_pe_now_pctile": 67.5, "fwd_pe_now_vs_band": "in_band",
           "fwd_pe_now_position": "带内第 68 百分位",
           "pe": {"10": 15.17, "25": 15.8, "50": 19.59, "75": 26.31, "90": 28.68},
           "target_pe": 19.6, "mult_reversion_to_target": -0.1934}
IBM_TRP = {"current": 26.83, "current_date": "2026-02-23", "lag_days": 212,
           "vs_band": "below_p25", "position": "低于 P25（30.9x）", "pctile": None}
IBM_PX = dict(price=231.38, adj_eps=9.13, gaap_ttm_eps=11.25)


def _run(trw=IBM_TRW, trp=IBM_TRP, regime=None, note=None, **px):
    kw = dict(IBM_PX, **px)
    return regime_conflict_warnings(trw, trp, regime, note, kw["price"],
                                    kw["adj_eps"], kw["gaap_ttm_eps"])


def test_ibm_shape_without_regime_is_red_with_both_trailing_numbers():
    w = _run()
    assert len(w) == 1 and w[0][0] == "red"
    msg = w[0][1]
    assert "GAAP 20.6x / 调整后(adj_ni) 25.3x" in msg      # 引擎算好，不让判断层自己除
    assert "两者差 >10%" in msg
    assert "-19%" in msg                                   # 选择的后果写明
    assert "只需补这两个字段" in msg and "不要为消旗去改 g/opm" in msg
    assert "滞后 212 天" in msg                             # trailing 读数自己的滞后也要说


def test_declared_regime_downgrades_to_yellow():
    w = _run(regime="band", note="旧 regime 的软件增速假设已被 Q2 有机零增长证伪")
    assert w[0][0] == "yellow" and "pe_regime=band" in w[0][1]
    assert "等于假设重定价全部回吐" in w[0][1]
    assert "声明与数字不一致" not in w[0][1]               # band + 目标贴中枢，一致


def test_invalid_regime_value_stays_red():
    assert _run(regime="old", note="x" * 20)[0][0] == "red"


def test_declared_blend_but_pe_at_band_center_flags_mismatch():
    w = _run(regime="blend", note="折中两种口径，理由见 rationale.pe 的定价证据")
    assert w[0][0] == "yellow" and "声明与数字不一致" in w[0][1]


def test_declared_band_but_pe_far_from_center_flags_mismatch():
    trw = dict(IBM_TRW, target_pe=24.0)                    # 偏离 P50 19.59 +23%
    w = _run(trw=trw, regime="band", note="沿用历史带中枢，带子分位代表长期均值回归")
    assert "声明与数字不一致" in w[0][1]


def test_reverse_conflict_fires():
    trw = dict(IBM_TRW, fwd_pe_now_vs_band="below_p10", fwd_pe_now_pctile=None)
    trp = dict(IBM_TRP, vs_band="above_p75", position="高于 P75")
    assert _run(trw=trw, trp=trp)[0][0] == "red"


@pytest.mark.parametrize("trw,trp", [
    # 同侧：NVDA 型（NTM ≈P23、trailing 低于 P25）——两个口径都说便宜，不冲突
    (dict(IBM_TRW, fwd_pe_now_pctile=23.0), IBM_TRP),
    # NTM 在中间（AMZN 型 ≈P46）——不够构成方向冲突
    (dict(IBM_TRW, fwd_pe_now_pctile=46.0), IBM_TRP),
    # trailing 在带内
    (IBM_TRW, dict(IBM_TRP, vs_band="in_band", pctile=50.0)),
    # 带子不滞后：NTM 分布看得见最近一年，没有盲区可谈
    (dict(IBM_TRW, span={"end": "2026-01-01", "lag_days": 200}), IBM_TRP),
    (IBM_TRW, None),
    (None, IBM_TRP),
])
def test_no_conflict_no_warning(trw, trp):
    assert _run(trw=trw, trp=trp) == []


def test_ntm_above_p90_counts_as_high():
    trw = dict(IBM_TRW, fwd_pe_now_vs_band="above_p90", fwd_pe_now_pctile=None)
    assert _run(trw=trw)[0][0] == "red"


def test_missing_eps_renders_na_not_crash():
    w = _run(adj_eps=None, gaap_ttm_eps=-1.0)
    assert w[0][0] == "red" and "GAAP n/a / 调整后(adj_ni) n/a" in w[0][1]
    assert "两者差" not in w[0][1]


def test_engine_wiring():
    """接线：纯函数测不到模块级调用点（test_pure 的已知限制）——钉住调用与字段落盘。"""
    assert "out[\"warnings_global\"] += regime_conflict_warnings(" in _SRC
    assert "cfg.get(\"pe_regime\"), cfg.get(\"pe_regime_note\")" in _SRC
    assert "_trw[\"trailing_pe_adj\"]" in _SRC and "_trw[\"trailing_pe_gaap\"]" in _SRC
    # 必须在 trailing_basis_position 之后（它要吃 _trp）
    assert _SRC.index("_trp = trailing_basis_position(") < _SRC.index(
        "+= regime_conflict_warnings(")


# ---- 校验层（形状）----

def _mk(**over):
    def sc(g, opm, pe, m1, margins, wacc=0.10, tg=0.025, g0=0.04, gN=0.03, tax=0.1):
        return dict(g=g, opm=opm, tax=tax, pe=pe, m1=m1, m2=0, wacc=wacc, tg=tg,
                    g0=g0, gN=gN, margins=list(margins))
    d = dict(
        fwd_shares=1000.0, net_cash=0.0, net_cash_note="x",
        adj_ni=100.0, adj_note="x", other_income=0.0, other_income_note="x",
        accounting_estimate_changes=[], accounting_estimate_note="x",
        seg1="A", seg2="B", seg1_share=0.9,
        rationale={k: "x" for k in ("g", "opm", "pe", "m1", "rl", "wacc", "dcf_margin")},
        notes=["x"],
        scenarios=dict(
            bear=sc(-0.05, 0.05, 10, 10, [0.02, 0.03, 0.04, 0.05, 0.06,
                                          0.07, 0.08, 0.08, 0.09, 0.09], wacc=0.11),
            base=sc(0.05, 0.10, 13, 13, [0.03, 0.04, 0.06, 0.07, 0.09,
                                         0.10, 0.11, 0.12, 0.13, 0.14]),
            bull=sc(0.13, 0.15, 15, 15, [0.05, 0.08, 0.11, 0.13, 0.15,
                                         0.16, 0.17, 0.18, 0.19, 0.20], wacc=0.09, tg=0.03)))
    d.update(over)
    return d


def test_validation_regime_optional():
    _validate_judgment(_mk())                                   # 不给：放行（引擎闸兜底）


def test_validation_regime_accepts_valid():
    _validate_judgment(_mk(pe_regime="recent",
                           pe_regime_note="近一年 trailing 中位 36.7x，重定价有订单证据"))


@pytest.mark.parametrize("over", [
    {"pe_regime": "old", "pe_regime_note": "x" * 20},
    {"pe_regime": "band"},                                      # 缺 note
    {"pe_regime": "band", "pe_regime_note": "短"},
    {"pe_regime_note": "只给 note 不给 regime 也不行"},
])
def test_validation_regime_rejects_bad_shape(over):
    with pytest.raises(ValueError, match="pe_regime"):
        _validate_judgment(_mk(**over))


# ---- prompt ----

def _band(lag=576):
    return {"basis": "ntm", "years": 5, "days": 488,
            "pctiles": {str(p): 20.0 for p in (10, 25, 50, 75, 90)},
            "span": {"start": "2021-09-23", "end": "2025-02-24", "lag_days": lag},
            "trailing_nolag": {"pctiles": {"25": 30.9, "50": 34.9, "75": 37.1},
                               "current": 26.8,
                               "span": {"start": "2024-08-20", "end": "2026-02-23"}}}


def test_band_meta_announces_gate_and_adjusted_eps():
    txt = _band_meta("standard", {"pe_band": _band()})
    assert "口径冲突闸" in txt and "pe_regime" in txt and "pe_regime_note" in txt
    assert "adj_ni ÷ 稀释股数" in txt                    # TTM EPS 用调整后口径
    assert "当前 GAAP TTM EPS 见 FACTS" not in txt       # 旧指令（IBM 事故的直接来源）已删


def test_band_meta_no_gate_without_lag():
    band = _band()
    band.pop("span")
    assert "口径冲突闸" not in _band_meta("standard", {"pe_band": band})


def test_prompt_schema_lists_regime_fields():
    txt = (ROOT / "valuation" / "judgment_prompt.md").read_text(encoding="utf-8")
    assert "\"pe_regime\"" in txt and "\"pe_regime_note\"" in txt
    assert "band|recent|blend" in txt
