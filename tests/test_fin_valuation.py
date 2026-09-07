# -*- coding: utf-8 -*-
"""financials 判断层/引擎护栏（fin v3，2026-09-06）。

回归对象：SOFI 实测一次 financials 运行零警告发货——校验层无跨情景检查
（bear.pe > bull.pe 照过），nm 硬下界 0 又把「刚扭亏票的亏损 bear」挡在门外，
唯一出路是假微利 × 15-30x PE 静默进综合（standard 的 COIN 除法事故同型）。
引擎测试走子进程（engine.py 是模块级脚本，import 即执行），合成 facts/config。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.valuation_service import _validate_judgment

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "valuation" / "engine.py"


# =====================================================================
# 校验层：跨情景排序 + 亏损协议
# =====================================================================

def _mkfin(**over):
    """最小合法 financials judgment；子情景可用 over={'bear': {...}} 覆盖。"""
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


def test_fin_valid_passes():
    _validate_judgment(_mkfin(), "financials")


@pytest.mark.parametrize("k", ["g", "nm", "pe", "ptbv"])
def test_fin_inverted_scenarios_blocked(k):
    """SOFI 形态：bear 参数越过 bull——fin v3 之前静默发货。"""
    d = _mkfin()
    d["scenarios"]["bear"][k], d["scenarios"]["bull"][k] = \
        d["scenarios"]["bull"][k], d["scenarios"]["bear"][k]
    with pytest.raises(ValueError, match=f"情景排序：{k}"):
        _validate_judgment(d, "financials")


def test_fin_equal_scenarios_allowed():
    """持平不算倒挂——与 standard 排序用同一个 <= 判据。"""
    same = dict(g=0.05, nm=0.08, pe=12, ptbv=1.2, wacc=0.12, tg=0.03)
    _validate_judgment(_mkfin(bear=dict(same), base=dict(same), bull=dict(same)),
                       "financials")


def test_fin_loss_scenario_with_pe0_accepted():
    """亏损协议：刚扭亏票的 bear 可以照实亏损（nm<0 + pe=0），不必造假微利。"""
    _validate_judgment(_mkfin(bear=dict(nm=-0.02, pe=0)), "financials")


def test_fin_loss_scenario_with_positive_pe_blocked():
    """亏损 × 正倍数 = 负目标价，必须显式 pe=0 声明 PE 腿失效。"""
    with pytest.raises(ValueError, match=r"pe 必须写 0"):
        _validate_judgment(_mkfin(bear=dict(nm=-0.02)), "financials")   # pe 仍是 10


def test_fin_nm_zero_is_loss_scenario():
    """nm=0 同属亏损协议（eps1=0，PE 法同样失效）。"""
    _validate_judgment(_mkfin(bear=dict(nm=0.0, pe=0)), "financials")
    with pytest.raises(ValueError, match=r"pe 必须写 0"):
        _validate_judgment(_mkfin(bear=dict(nm=0.0, pe=5)), "financials")


def test_fin_nm_floor():
    """下界 -0.5：比『总净收入一半都亏掉』更深的 NTM 亏损属崩溃定价，不是情景假设。"""
    with pytest.raises(ValueError, match=r"nm"):
        _validate_judgment(_mkfin(bear=dict(nm=-0.5, pe=0)), "financials")


def test_fin_pe_bounds_when_profitable():
    """盈利情景 pe 走 [1,60]——0 是亏损协议的专用声明值，不许给盈利情景。"""
    with pytest.raises(ValueError, match=r"pe 需在"):
        _validate_judgment(_mkfin(bear=dict(pe=0.5)), "financials")
    with pytest.raises(ValueError, match=r"pe 需在"):
        _validate_judgment(_mkfin(bear=dict(pe=0)), "financials")   # nm>0 时 0 不合法


def test_fin_ptbv_bounds():
    with pytest.raises(ValueError, match=r"ptbv"):
        _validate_judgment(_mkfin(bear=dict(ptbv=0.1)), "financials")


# =====================================================================
# 引擎：PE 腿 n.m. 守卫（亏损/微利），blend 退化为 P/TBV 单腿
# =====================================================================

def _fin_facts(**over):
    """最小合成 financials facts：TBV=4,000M / 时点股 1,000M -> 每股 TBV 4.0。"""
    q = ["2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30",
         "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    a = [f"20{y}-12-31" for y in range(19, 26)]
    d = {
        "mode": "financials",
        "ttm": {"revenue": {"value": 2000e6}, "pretax_income": {"value": 250e6},
                "net_income": {"value": 200e6}},
        "equity_instant": {"2026-06-30": 5000e6},
        "goodwill_instant": {"2026-06-30": 500e6},
        "intangibles_instant": {"2026-06-30": 500e6},
        "shares_outstanding_instant": {"2026-06-30": 1000e6},
        "revenue_annual": {k: 1500e6 for k in a},
        "pretax_income_annual": {k: 200e6 for k in a},
        "net_income_annual": {k: 160e6 for k in a},
        "eps_diluted_annual": {k: 0.16 for k in a},
        "revenue_quarterly": {k: 500e6 for k in q},
        "pretax_income_quarterly": {k: 60e6 for k in q},
        "net_income_quarterly": {k: 50e6 for k in q},
    }
    d.update(over)
    return d


def _fin_cfg(**over):
    def sc(g, nm, pe, ptbv, wacc=0.12, tg=0.03):
        return dict(g=g, nm=nm, pe=pe, ptbv=ptbv, wacc=wacc, tg=tg)
    c = dict(
        ticker="TFIN", name="Test Fin", date="2026-09-06", mode="financials",
        price=10.0, mcap=10000, shares=1000.0, fwd_shares=1050.0,
        fwd_label="NTM 2026-07~2027-06", adj_ni=200.0, adj_note="x",
        notes=["x"], rationale={k: "x" for k in ("g", "nm", "pe", "ptbv", "wacc")},
        semantics_version=3,
        scenarios=dict(bear=sc(-0.05, 0.04, 10, 0.9),
                       base=sc(0.10, 0.10, 15, 1.5),
                       bull=sc(0.20, 0.14, 20, 2.0)),
    )
    for k, v in over.items():
        if k in ("bear", "base", "bull"):
            c["scenarios"][k].update(v)
        else:
            c[k] = v
    return c


def run_fin_engine(tmp_path, cfg, facts, manifest=None):
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    (tmp_path / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
    argv = [sys.executable, str(ENGINE), str(tmp_path / "config.json"),
            str(tmp_path / "facts.json"), str(tmp_path / "out.json")]
    if manifest is not None:
        (tmp_path / "manifest.csv").write_text(manifest, encoding="utf-8")
        argv.append(str(tmp_path / "manifest.csv"))
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       # 子进程 stdout 是中文：显式给 PYTHONIOENCODING（与 app.valuation_service._run
                       # 同法），否则 Windows 管道按 cp1252 编码，被测脚本 print 时就 UnicodeEncodeError
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))


def test_fin_engine_semantics_v3(tmp_path):
    out = run_fin_engine(tmp_path, _fin_cfg(), _fin_facts())
    assert out["semantics_version"] == 3


def test_fin_engine_normal_scenario_keeps_both_legs(tmp_path):
    out = run_fin_engine(tmp_path, _fin_cfg(), _fin_facts())
    b = out["scenarios"]["base"]
    assert b["blend_methods"] == ["pe", "ptbv"]
    # rev1=2200, ni1=220, eps1=0.2095, PE法=3.1429; P/TBV法=4.0×1.5=6.0
    # -> blend=(3.1429+6.0)/2=4.5714≈4.6（引擎按未舍入值综合后才 round）
    assert b["blend"] == 4.6
    assert b["method_spread"] is not None


def test_fin_engine_drops_pe_leg_on_loss(tmp_path):
    """亏损情景（nm<0, pe=0）：PE 腿 n.m.、综合=P/TBV 单腿、fwd_pe 不给假读数。"""
    out = run_fin_engine(tmp_path, _fin_cfg(bear=dict(nm=-0.02, pe=0)), _fin_facts())
    b = out["scenarios"]["bear"]
    assert b["blend_methods"] == ["ptbv"]
    assert b["blend"] == b["ptbv_ps"] == 3.6          # 4.0 × 0.9
    assert b["fwd_pe"] is None
    assert b["method_spread"] is None                  # 单腿谈不上方法分歧
    assert any(lv == "yellow" and "PE 腿 n.m." in msg for lv, msg in b["warnings"])
    # 其余情景不受连累
    assert out["scenarios"]["base"]["blend_methods"] == ["pe", "ptbv"]


def test_fin_engine_nm_zero_no_zerodiv(tmp_path):
    """nm=0 -> eps1=0：fwd_pe 除零此前必崩，现在置 None。"""
    out = run_fin_engine(tmp_path, _fin_cfg(bear=dict(nm=0.0, pe=0)), _fin_facts())
    b = out["scenarios"]["bear"]
    assert b["fwd_pe"] is None and b["blend_methods"] == ["ptbv"]


def test_fin_engine_microprofit_pe_leg_nm(tmp_path):
    """微利守卫：nm=0.5% 的假微利 × 10x 不许当估值腿（COIN 除法事故同型）。"""
    out = run_fin_engine(tmp_path, _fin_cfg(bear=dict(nm=0.005, pe=10)), _fin_facts())
    b = out["scenarios"]["bear"]
    assert b["blend_methods"] == ["ptbv"]
    assert b["blend"] == b["ptbv_ps"]
    assert any("PE 腿 n.m." in msg for _, msg in b["warnings"])


# =====================================================================
# 基线护栏（0005）：fin 分支此前在所有 base 级护栏之前 return——
# 无时效检查、无 base 锚检查、method_spread 算了不查、PE 腿无历史带比对
# =====================================================================

# 定期报告期末 2026-03-31，cfg date=2026-09-06 -> 龄 159 天（>45 触发时效护栏）
MANIFEST_STALE = ("form,filingDate,reportDate,accessionNumber,primaryDocument,size\n"
                  "10-Q,2026-05-05,2026-03-31,0001,x.htm,1\n")
MANIFEST_FRESH = ("form,filingDate,reportDate,accessionNumber,primaryDocument,size\n"
                  "10-Q,2026-09-01,2026-08-15,0002,x.htm,1\n")


def test_fin_engine_vintage_staleness_yellow(tmp_path):
    """银行/券商/fintech 是 10-Q 滞后重灾区，fin 分支此前却没有任何时效检查。
    fin 无 net_cash——锚退到 TBV 口径（vintage_warnings 的 fin 适配）。
    0015 起时效类警告挂 warnings_global（数据事实与情景无关），不再塞 base。"""
    cfg = _fin_cfg(fwd_shares=1005.0)   # 股数差 0.5% < 1.5%，走纯时效条
    out = run_fin_engine(tmp_path, cfg, _fin_facts(), manifest=MANIFEST_STALE)
    msgs = [m for lv, m in out["warnings_global"] if lv == "yellow"]
    assert any("报告期末已 159 天" in m and "TBV" in m for m in msgs)
    assert not any("net_cash" in m for m in msgs)      # fin 不许冒充 net_cash 口径
    # 移动不是复制：base 通道里不许再有同一条
    assert not any("报告期末已" in m for _, m in out["scenarios"]["base"]["warnings"])


def test_fin_engine_vintage_issuance_mismatch(tmp_path):
    """SOFI 型增发季：fwd_shares 含新股（+5%）、TBV 停在报告期 → P/TBV 腿系统性低估。
    0015 起在 warnings_global。"""
    out = run_fin_engine(tmp_path, _fin_cfg(), _fin_facts(), manifest=MANIFEST_STALE)
    msgs = [m for _, m in out["warnings_global"]]
    assert any("时点不一致" in m and "未计入 TBV" in m and "低估" in m for m in msgs)


def test_fin_engine_fresh_vintage_silent(tmp_path):
    """龄 <=45 不出声——时效护栏不该对刚出的报告刷屏（全局/情景两通道都查）。"""
    out = run_fin_engine(tmp_path, _fin_cfg(), _fin_facts(), manifest=MANIFEST_FRESH)
    msgs = ([m for _, m in out["warnings_global"]]
            + [m for _, m in out["scenarios"]["base"]["warnings"]])
    assert not any("时点不一致" in m or "报告期末已" in m for m in msgs)


def test_fin_engine_base_price_deviation_yellow(tmp_path):
    """base 综合 4.57 vs 现价 10 = -54%：三档整队静默平移正是要拦的形态。"""
    out = run_fin_engine(tmp_path, _fin_cfg(), _fin_facts())
    msgs = [m for lv, m in out["scenarios"]["base"]["warnings"] if lv == "yellow"]
    assert any("base 综合较现价偏离 -54%" in m for m in msgs)


def test_fin_engine_base_price_deviation_silent_within_band(tmp_path):
    out = run_fin_engine(tmp_path, _fin_cfg(price=5.0), _fin_facts())   # -8.6%
    msgs = [m for _, m in out["scenarios"]["base"]["warnings"]]
    assert not any("偏离" in m for m in msgs)


def test_fin_engine_method_spread_yellow(tmp_path):
    """bear：PE 法 0.72 vs P/TBV 法 3.6 -> 离散 4.97x（>2x）。method_spread
    此前算完就落盘，从未检查。base 1.91x 不响（阈值严格大于 2）。"""
    out = run_fin_engine(tmp_path, _fin_cfg(), _fin_facts())
    bear = [m for _, m in out["scenarios"]["bear"]["warnings"]]
    base = [m for _, m in out["scenarios"]["base"]["warnings"]]
    assert any("方法离散度 4.97x" in m for m in bear)
    assert not any("方法离散度" in m for m in base)


PE_BAND = {"basis": "ntm", "years": 5, "days": 1200, "min": 8.0, "max": 30.0,
           "median": 15.0,
           "pctiles": {"10": 10.0, "25": 12.0, "50": 15.0, "75": 20.0, "90": 25.0}}


def test_fin_engine_pe_band_check_on_pe_leg(tmp_path):
    """pe_band 对 financials facts 本就生成，此前只查了 P/TBV 腿。"""
    out = run_fin_engine(tmp_path, _fin_cfg(bull=dict(pe=40)),
                         _fin_facts(pe_band=PE_BAND))
    bull = out["scenarios"]["bull"]
    assert any("目标 PE 40.0x" in m and "从未出现过" in m for _, m in bull["warnings"])
    assert bull["diagnostics"]["pe_vs_history"]["max"] == 30.0
    # 带内的 base 不响，但诊断照记
    base = out["scenarios"]["base"]
    assert not any("目标 PE" in m for _, m in base["warnings"])
    assert base["diagnostics"]["pe_vs_history"]["pctile"] == 50.0


def test_fin_engine_pe_band_skipped_when_pe_nm(tmp_path):
    """亏损情景 pe=0：对已声明不适用的倍数比对只会产出必然的黄旗（standard 同规则）。"""
    out = run_fin_engine(tmp_path, _fin_cfg(bear=dict(nm=-0.02, pe=0)),
                         _fin_facts(pe_band=PE_BAND))
    bear = out["scenarios"]["bear"]
    assert "pe_vs_history" not in bear["diagnostics"]
    assert not any("目标 PE" in m for _, m in bear["warnings"])


# =====================================================================
# 连续性锚持久化（0005）：写路径去掉 mode 门禁——load 路径早就支持 fin 语义，
# 不写 = fin 自动连续性结构性 no-op
# =====================================================================

def test_fin_prev_persistence_writes_file(tmp_path, monkeypatch):
    import app.valuation_service as vs
    monkeypatch.setattr(vs, "PREV_DIR", tmp_path / "prev")
    monkeypatch.delenv("VALUATION_NO_CONTINUITY", raising=False)
    cfg = dict(_fin_cfg(), manifest_latest=None)
    vs._persist_prev_config("TFIN", cfg, [], "2026-06-30")
    saved = json.loads((tmp_path / "prev" / "TFIN.json").read_text(encoding="utf-8"))
    assert saved["semantics_version"] == 3
    assert saved["manifest_latest"] == "2026-06-30"    # 报告期指纹随锚落盘


def test_prev_persistence_gate_blocks_reds(tmp_path, monkeypatch):
    """带病假设冻结成锚会让偏差跨运行复利——gate-clean 判据不因 fin 放行而松动。"""
    import app.valuation_service as vs
    monkeypatch.setattr(vs, "PREV_DIR", tmp_path / "prev")
    monkeypatch.delenv("VALUATION_NO_CONTINUITY", raising=False)
    vs._persist_prev_config("TFIN", _fin_cfg(), ["[base] 假设红旗"], "2026-06-30")
    assert not (tmp_path / "prev" / "TFIN.json").exists()


def test_prev_persistence_respects_no_continuity(tmp_path, monkeypatch):
    import app.valuation_service as vs
    monkeypatch.setattr(vs, "PREV_DIR", tmp_path / "prev")
    monkeypatch.setenv("VALUATION_NO_CONTINUITY", "1")
    vs._persist_prev_config("TFIN", _fin_cfg(), [], "2026-06-30")
    assert not (tmp_path / "prev" / "TFIN.json").exists()


def test_persistence_gate_is_mode_agnostic():
    """源码级：调用点不许再挂 mode 门禁（防重构时顺手加回去）。"""
    src = (ROOT / "app" / "valuation_service.py").read_text(encoding="utf-8")
    assert "_persist_prev_config(ticker, cfg, reds, latest_report)" in src
    assert 'mode == "standard" and not reds' not in src


# =====================================================================
# prompt 与校验层执法值一致（grep 级）：文档必须写校验器真实执行的数
# =====================================================================

def test_fin_prompt_documents_enforced_bounds():
    p = (ROOT / "valuation" / "judgment_prompt_financials.md").read_text(encoding="utf-8")
    for needle in ("(-0.5, 1.5)", "(-0.5, 0.6)", "[1, 60]", "[0.2, 8]",
                   "[0.05, 0.25]", "0.045", "bear <= base <= bull", "pe = 0"):
        assert needle in p, f"fin prompt 未写明执法边界：{needle}"
    assert "压到微利" not in p, "旧文案仍在推模型造假微利"


def test_both_prompts_state_enforced_wacc_tg_gap():
    """两个校验器执法的都是 0.045——standard prompt 此前写 0.05，与报错文案对不上。"""
    std = (ROOT / "valuation" / "judgment_prompt.md").read_text(encoding="utf-8")
    fin = (ROOT / "valuation" / "judgment_prompt_financials.md").read_text(encoding="utf-8")
    assert "0.045" in std and "至少0.05>" not in std
    assert "0.045" in fin


def test_fin_share_count_mismatch_yellow(tmp_path):
    """0018：ADR 标定回退的失配黄旗在 financials 分支同样生效。"""
    out = run_fin_engine(tmp_path, _fin_cfg(share_count_mismatch=0.11), _fin_facts())
    hits = [m for lv, m in out["warnings_global"]
            if lv == "yellow" and "市值隐含股数" in m]
    assert hits and "11.0%" in hits[0]
