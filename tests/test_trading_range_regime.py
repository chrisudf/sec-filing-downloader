# -*- coding: utf-8 -*-
"""交易区间 regime 失效红线（0010）。

回归对象是 ISRG 实测那份最误导的输出：trading_range = 陈旧带分位 × base eps1
打出 546~675 vs 现价 367（fwd_pe_now 36.5 已低于 P10、带滞后 320 天、
mult_reversion 0.687）而无任何健康警示。两个条件叠加（带外 + 滞后 >250 天）时
「按历史分位均值回归」的前提本身可能已失效——修法保守：数字不改，只在
trading_range 挂 regime_note，随报告红线行与 RESULT 载荷走。
引擎走子进程（engine.py 是模块级脚本），合成 standard facts/config。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from app.valuation_service import _trading_range_payload

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "valuation" / "engine.py"


def _std_facts(lag_days=320):
    q = ["2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30",
         "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    a = [f"20{y}-12-31" for y in range(19, 26)]
    return {
        "mode": "standard",
        "ttm": {"revenue": {"value": 1000e6}, "op_income": {"value": 150e6},
                "net_income": {"value": 120e6}, "cfo": {"value": 200e6},
                "capex": {"value": 50e6}},
        "revenue_annual": {k: 900e6 for k in a},
        "op_income_annual": {k: 140e6 for k in a},
        "net_income_annual": {k: 110e6 for k in a},
        "cfo_annual": {k: 180e6 for k in a},
        "capex_annual": {k: 45e6 for k in a},
        "eps_diluted_annual": {k: 1.1 for k in a},
        "revenue_quarterly": {k: 250e6 for k in q},
        "op_income_quarterly": {k: 38e6 for k in q},
        "net_income_quarterly": {k: 30e6 for k in q},
        # 近3年子窗 P10~P90 = 21~29：现价决定 fwd_pe_now 落带内还是带外
        "pe_band": {
            "basis": "ntm", "years": 5, "days": 1100, "min": 8.0, "max": 40.0,
            "pctiles": {str(p): 18.0 + p / 9 for p in
                        (1, 5, 10, 25, 50, 75, 90, 95, 99)},
            "span": {"start": "2021-01-04", "end": "2025-10-20",
                     "lag_days": lag_days},
            "recent": {"years": 3, "days": 700,
                       "pctiles": {"10": 21.0, "25": 23.0, "50": 25.0,
                                   "75": 27.5, "90": 29.0},
                       "span": {"start": "2023-01-03", "end": "2025-10-20",
                                "lag_days": lag_days}}},
    }


def _std_cfg(price):
    def sc(g, opm, pe, m1, wacc=0.10, tg=0.025):
        return dict(g=g, opm=opm, tax=0.1, pe=pe, m1=m1, m2=8, wacc=wacc, tg=tg,
                    g0=0.05, gN=0.03, margins=[0.10] * 10)
    return dict(
        ticker="TREG", name="Test Regime", date="2026-09-06", mode="standard",
        price=price, mcap=round(price * 100), shares=100.0, fwd_shares=100.0,
        net_cash=0.0, net_cash_note="x", adj_ni=120.0, adj_note="x",
        other_income=0.0, other_income_note="x", fwd_label="NTM 2026-07~2027-06",
        seg1="A", seg2="B", seg1_share=0.9, notes=["x"],
        rationale={k: "x" for k in ("g", "opm", "pe", "m1", "rl", "wacc")},
        scenarios=dict(bear=sc(-0.05, 0.06, 18, 10, wacc=0.11),
                       base=sc(0.05, 0.10, 25, 13),
                       bull=sc(0.12, 0.13, 29, 15, wacc=0.09, tg=0.03)),
    )


def _run_engine(tmp_path, cfg, facts):
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    (tmp_path / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ENGINE), str(tmp_path / "config.json"),
                        str(tmp_path / "facts.json"), str(tmp_path / "out.json")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       # 子进程 stdout 是中文：显式给 PYTHONIOENCODING（与 app.valuation_service._run
                       # 同法），否则 Windows 管道按 cp1252 编码，被测脚本 print 时就 UnicodeEncodeError
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))


def test_below_p10_and_stale_band_gets_regime_note(tmp_path):
    """ISRG 形态：fwd_pe_now = 10/0.945 ≈ 10.6 << P10=21，滞后 320 天。"""
    out = _run_engine(tmp_path, _std_cfg(price=10.0), _std_facts(lag_days=320))
    tr = out["trading_range"]
    assert tr["fwd_pe_now_vs_band"] == "below_p10"
    note = tr.get("regime_note")
    assert note and "320 天" in note and "均值回归前提可能已失效" in note
    assert "target_pe" in note and "trailing" in note
    # 数字保守不改：分位×eps 的区间照出
    assert tr["px"]["50"] == round(25.0 * out["scenarios"]["base"]["eps1"], 1)
    # 载荷把 regime_note 一起带走
    payload, note2 = _trading_range_payload("standard", {}, out)
    assert note2 is None and payload["regime_note"] == note


def test_above_p90_and_stale_band_gets_regime_note(tmp_path):
    out = _run_engine(tmp_path, _std_cfg(price=33.0), _std_facts(lag_days=320))
    tr = out["trading_range"]
    assert tr["fwd_pe_now_vs_band"] == "above_p90"
    assert tr.get("regime_note") and "冲破上沿" in tr["regime_note"]


def test_in_band_no_regime_note(tmp_path):
    out = _run_engine(tmp_path, _std_cfg(price=24.0), _std_facts(lag_days=320))
    tr = out["trading_range"]
    assert tr["fwd_pe_now_vs_band"] == "in_band"
    assert "regime_note" not in tr


def test_out_of_band_but_fresh_band_no_regime_note(tmp_path):
    """带外但滞后 <=250 天：既有的带外黄旗照发，regime 红线不加——
    新鲜带子的带外是估值信息，不是前提失效。"""
    out = _run_engine(tmp_path, _std_cfg(price=10.0), _std_facts(lag_days=200))
    tr = out["trading_range"]
    assert tr["fwd_pe_now_vs_band"] == "below_p10"
    assert "regime_note" not in tr
    assert any("跌出下沿 P10" in m for _, m in out["scenarios"]["base"]["warnings"])


# ---- 网页端消费接线（0022 C10/C16）----

def test_web_ui_wires_regime_note_and_absent_reason():
    """RESULT 载荷的 regime_note（0010）与 trading_range_note（0008）此前只有
    xlsx/裸 API 消费——bundled 网页是另一个 shipped 消费者：区间缺席要给原因，
    regime 失效红线不给就是把已被引擎标记失效的区间当健康区间展示（ISRG 型）。
    grep 级接线哨兵（前端无 JS 测试设施）。"""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "trading_range_note" in html, "网页未渲染区间缺席原因"
    assert "regime_note" in html, "网页未渲染 regime 失效红线"
    assert "regime-note" in html, "regime 红线缺样式挂钩（应为红色 var(--err)）"
