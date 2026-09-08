# -*- coding: utf-8 -*-
"""compare.py 的腿集合口径警告按情景判（0022 C15）。

fin v3 起 PE 腿逐情景 n.m. 退出综合（scenarios[sc].blend_methods），顶层
blend_weights 恒为全集——compare 只看顶层键集时，两次 v3 运行间 bear.nm 跨过
0/1% 门槛、bear 综合从两腿均值变 P/TBV 单腿（合成 +63.6% 型跳变）不触发任何
口径警告，底部噪声警报还把它归因成假设漂移。trend.py:120 早已按情景消费
blend_methods，compare 必须对齐。compare.py 是模块级脚本，走子进程。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

COMPARE = Path(__file__).resolve().parent.parent / "valuation" / "compare.py"


def _fin_val(date, bear):
    """最小 fin v3 valuation.json；bear=(nm, pe, blend, legs)。"""
    nm, pe, blend, legs = bear

    def sc(nm, pe, ptbv, blend, legs):
        return {"assumptions": {"g": 0.05, "nm": nm, "pe": pe, "ptbv": ptbv,
                                "wacc": 0.12, "tg": 0.03},
                "blend": blend, "upside": round(blend / 10.0 - 1, 4),
                "pe_target": 3.1, "ptbv_ps": 3.6, "blend_methods": legs}
    return {"ticker": "TFIN", "date": date, "mode": "financials",
            "semantics_version": 3,
            "blend_weights": {"pe": 1, "ptbv": 1},   # 顶层恒为全集——正是盲区
            "meta": {"price": 10.0, "tbv": 4000, "fwd_label": "NTM 2026-07~2027-06",
                     "vintage": {"report_end": "2026-06-30"}},
            "ttm": {"revenue": 2000, "pretax_income": 250, "net_income": 200},
            "adj_ni": 200.0, "adj_note": "x",
            "scenarios": {"bear": sc(nm, pe, 0.9, blend, legs),
                          "base": sc(0.10, 15, 1.5, 4.6, ["pe", "ptbv"]),
                          "bull": sc(0.14, 20, 2.0, 6.0, ["pe", "ptbv"])}}


def _run_compare(tmp_path, old, new):
    po, pn = tmp_path / "old.json", tmp_path / "new.json"
    po.write_text(json.dumps(old), encoding="utf-8")
    pn.write_text(json.dumps(new), encoding="utf-8")
    r = subprocess.run([sys.executable, str(COMPARE), str(po), str(pn)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       # 子进程 stdout 是中文：显式给 PYTHONIOENCODING（与 app.valuation_service._run
                       # 同法），否则 Windows 管道按 cp1252 编码，被测脚本 print 时就 UnicodeEncodeError
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_per_scenario_leg_change_warns(tmp_path):
    """bear nm 0.04 → −0.10（pe 强制 0，PE 腿退出）：综合 2.2 → 3.6（+64%），
    顶层 blend_weights 两边一致——修前零口径警告。"""
    old = _fin_val("2026-08-01", (0.04, 10, 2.2, ["pe", "ptbv"]))
    new = _fin_val("2026-09-06", (-0.10, 0, 3.6, ["ptbv"]))
    out = _run_compare(tmp_path, old, new)
    assert "bear 参与综合的方法不同" in out
    assert "PE/PTBV → PTBV" in out


def test_same_legs_no_leg_warning(tmp_path):
    old = _fin_val("2026-08-01", (0.04, 10, 2.2, ["pe", "ptbv"]))
    new = _fin_val("2026-09-06", (0.05, 11, 2.4, ["pe", "ptbv"]))
    out = _run_compare(tmp_path, old, new)
    assert "参与综合的方法不同" not in out


def test_semantics_explainer_covers_v4_and_fin_v3(tmp_path):
    """次要项：语义版本解释文案此前止步 standard v3 / fin v2——v3→v4 对比会打出
    一段描述不到位的警告。"""
    old = dict(_fin_val("2026-08-01", (0.04, 10, 2.2, ["pe", "ptbv"])),
               semantics_version=2)
    new = _fin_val("2026-09-06", (0.04, 10, 2.2, ["pe", "ptbv"]))
    out = _run_compare(tmp_path, old, new)
    assert "估值语义版本不同" in out
    assert "v4（2026-09-06）" in out and "fin v3（2026-09-06）" in out
