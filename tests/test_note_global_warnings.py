# -*- coding: utf-8 -*-
"""other_income_note 贯通 + 情景无关警告的全局通道（0015）。

回归对象两个：
- other_income_note：校验层硬要求、prompt 花一整段索要的推导口径，engine 出 dict
  一直把它丢掉，build_report 的 E15 再拍一句「净利息及其他（正常化）」——全链唯一
  有出处的那句话进不了报告。
- 全局警告：时效/期后事件/带滞后是数据事实、三情景共有，此前全部挂 base 下，
  bear/bull 的 warnings 读起来"干净"（9 个评审 agent 共同点名）。engine 出
  warnings_global 顶层通道，报告红旗区最前渲染「[全局]」块。
引擎与 build_report 都是模块级脚本，走子进程；xlsx 用 openpyxl 读回断言。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "valuation" / "engine.py"
BUILD = ROOT / "valuation" / "build_report.py"

OI_NOTE = "取自 10-Q『Interest and other, net』，剔除 $80M 一次性重估，按 TTM 年化"


def _std_facts():
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
        # lag 320 天 > 270 → band_lag_warnings 必触发（全局通道的带滞后样本）
        "pe_band": {
            "basis": "ntm", "years": 5, "days": 1100, "min": 8.0, "max": 40.0,
            "pctiles": {str(p): 18.0 + p / 9 for p in
                        (1, 5, 10, 25, 50, 75, 90, 95, 99)},
            "span": {"start": "2021-01-04", "end": "2025-10-20", "lag_days": 320},
            "recent": {"years": 3, "days": 700,
                       "pctiles": {"10": 21.0, "25": 23.0, "50": 25.0,
                                   "75": 27.5, "90": 29.0},
                       "span": {"start": "2023-01-03", "end": "2025-10-20",
                                "lag_days": 320}}},
    }


def _std_cfg(**over):
    def sc(g, opm, pe, m1, wacc=0.10, tg=0.025):
        return dict(g=g, opm=opm, tax=0.1, pe=pe, m1=m1, m2=8, wacc=wacc, tg=tg,
                    g0=0.05, gN=0.03, margins=[0.10] * 10)
    d = dict(
        ticker="TGLB", name="Test Global", date="2026-09-06", mode="standard",
        price=24.0, mcap=2400, shares=100.0, fwd_shares=100.0,
        net_cash=0.0, net_cash_note="10-Q 流动性章节：现金 0 − 债务 0",
        adj_ni=120.0, adj_note="无重大一次性项目，用报告净利",
        other_income=0.0, other_income_note=OI_NOTE,
        fwd_label="NTM 2026-07~2027-06",
        seg1="A", seg2="B", seg1_share=0.9, notes=["x"],
        rationale={k: "x" for k in ("g", "opm", "pe", "m1", "rl", "wacc")},
        scenarios=dict(bear=sc(-0.05, 0.06, 18, 10, wacc=0.11),
                       base=sc(0.05, 0.10, 25, 13),
                       bull=sc(0.12, 0.13, 29, 15, wacc=0.09, tg=0.03)),
    )
    d.update(over)
    return d


PPCE = [{"date": "2026-08-18", "kind": "增发", "amount_musd": 19700.0,
         "note": "8-K 2026-08-10 定价"}]


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


def test_other_income_note_reaches_valuation_json(tmp_path):
    out = _run_engine(tmp_path, _std_cfg(), _std_facts())
    assert out["other_income_note"] == OI_NOTE


def test_ppce_and_band_lag_go_to_global_channel(tmp_path):
    """期后事件声明与带滞后：全局通道有、任何情景通道都不再有（移动不是复制）。"""
    out = _run_engine(tmp_path, _std_cfg(post_period_capital_events=PPCE),
                      _std_facts())
    gmsgs = [m for _, m in out["warnings_global"]]
    assert any("期后资本事件已声明" in m for m in gmsgs)
    assert any(m.startswith("带子止于") for m in gmsgs)
    for sc in ("bear", "base", "bull"):
        smsgs = [m for _, m in out["scenarios"][sc]["warnings"]]
        assert not any("期后资本事件" in m or m.startswith("带子止于") for m in smsgs)


def test_global_channel_empty_when_clean(tmp_path):
    """无期后事件、无 vintage（无 manifest）、带子新鲜：全局通道为空列表而非缺键。"""
    facts = _std_facts()
    facts["pe_band"]["span"]["lag_days"] = 100
    facts["pe_band"]["recent"]["span"]["lag_days"] = 100
    out = _run_engine(tmp_path, _std_cfg(), facts)
    assert out["warnings_global"] == []


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    """engine → build_report 全链跑一次，openpyxl 读回（module 级：xlsx 生成较贵）。"""
    openpyxl = pytest.importorskip("openpyxl")
    tmp = tmp_path_factory.mktemp("rep")
    out = _run_engine(tmp, _std_cfg(post_period_capital_events=PPCE), _std_facts())
    xlsx = tmp / "report.xlsx"
    r = subprocess.run([sys.executable, str(BUILD), str(tmp / "out.json"), str(xlsx)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       # 子进程 stdout 是中文：显式给 PYTHONIOENCODING（与 app.valuation_service._run
                       # 同法），否则 Windows 管道按 cp1252 编码，被测脚本 print 时就 UnicodeEncodeError
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stdout + r.stderr
    return openpyxl.load_workbook(xlsx), out


def test_report_e15_renders_other_income_note(report):
    wb, _ = report
    assert wb["情景假设"]["E15"].value == OI_NOTE


def test_report_sources_sheet_lists_other_income_note(report):
    wb, _ = report
    rows = {ws["A%d" % r].value: ws["B%d" % r].value
            for ws in [wb["出处"]] for r in range(1, ws.max_row + 1)}
    assert rows.get("年化其他收益") == OI_NOTE


def test_report_renders_global_block_first(report):
    """红旗区最前是「[全局]」行——全局块排在情景旗之前。"""
    wb, out = report
    ws = wb["摘要"]
    flag_rows = [(r, ws["A%d" % r].value) for r in range(1, ws.max_row + 1)
                 if isinstance(ws["A%d" % r].value, str)
                 and ws["A%d" % r].value.startswith(("⛔", "⚠"))]
    assert flag_rows, "红旗区未渲染"
    n_global = len(out["warnings_global"])
    assert n_global >= 2   # ppce + 带滞后
    head = [v for _, v in flag_rows[:n_global]]
    assert all("[全局]" in v for v in head)
    assert any("期后资本事件已声明" in v for v in head)


# =====================================================================
# 股数口径失配黄旗（0018）：服务层 ADR 标定回退 1.0 时把失配比例写进
# cfg.share_count_mismatch，引擎必须把它变成看得见的全局黄旗
# =====================================================================

def test_share_count_mismatch_yellow_in_global_channel(tmp_path):
    facts = _std_facts()
    facts["pe_band"]["span"]["lag_days"] = 100
    facts["pe_band"]["recent"]["span"]["lag_days"] = 100
    out = _run_engine(tmp_path, _std_cfg(share_count_mismatch=0.1037), facts)
    hits = [(lv, m) for lv, m in out["warnings_global"] if "市值隐含股数" in m]
    assert hits and hits[0][0] == "yellow"
    assert "10.4%" in hits[0][1] and "XBRL 股数口径" in hits[0][1]
    # 情景通道不重复（全局事实只进全局块）
    for sc in ("bear", "base", "bull"):
        assert not any("市值隐含股数" in m
                       for _, m in out["scenarios"][sc]["warnings"])


def test_no_mismatch_key_no_flag(tmp_path):
    facts = _std_facts()
    facts["pe_band"]["span"]["lag_days"] = 100
    facts["pe_band"]["recent"]["span"]["lag_days"] = 100
    out = _run_engine(tmp_path, _std_cfg(), facts)
    assert not any("市值隐含股数" in m for _, m in out["warnings_global"])
