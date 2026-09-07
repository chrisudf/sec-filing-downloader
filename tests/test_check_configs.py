# -*- coding: utf-8 -*-
"""check_configs 回归工具与产线校验同源（0022 C4）。

不变量（工具自己的注释写的）：回归工具比产线严 = 假警报。0019 给产线加了
override >10% 偏差的口径闸（fcf_margin→None、margins 谷底/上界退历史中位锚）
并以 config 的 ttm_revenue_override 为 rev0 基准，工具一直没接——
PENDING_10Q/陈旧 XBRL 运行留档的、产线 PASS 的 config 被工具用过期 TTM 锚
BLOCK。工具必须调 _fcfm_for_validation 与 _hist_fcfm_median（产线同一实现）。
"""
import json

from valuation.check_configs import main as check_main


def _cfg(**over):
    def sc(g, opm, pe, m1, margins, wacc=0.10, tg=0.025):
        return dict(g=g, opm=opm, tax=0.1, pe=pe, m1=m1, m2=0, wacc=wacc, tg=tg,
                    g0=0.04, gN=0.03, margins=list(margins))
    d = dict(
        ticker="TSMX", mode="standard",
        fwd_shares=1000.0, net_cash=0.0, net_cash_note="x",
        adj_ni=100.0, adj_note="x", other_income=0.0, other_income_note="x",
        seg1="A", seg2="B", seg1_share=0.9, notes=["x"],
        rationale={k: "x" for k in ("g", "opm", "pe", "m1", "rl", "wacc")},
        # 偏离 +25% > 10%：产线口径闸弃用陈旧 TTM FCF 锚（TSM 型 6-K 前滚留档）
        ttm_revenue_override=125_000.0,
        ttm_revenue_note="6-K 原文前滚",
        scenarios=dict(
            bear=sc(-0.05, 0.05, 10, 10, [0.07] * 10, wacc=0.11),
            base=sc(0.05, 0.10, 13, 13, [0.08] * 10),
            bull=sc(0.13, 0.15, 15, 15, [0.09] * 10, wacc=0.09, tg=0.03)))
    d.update(over)
    return d


def _facts():
    a = [f"20{y}-12-31" for y in range(17, 26)]
    return {"mode": "standard",
            # 陈旧 TTM FCF 率 20%（谷底锚 0.4×20%=8% 会拦 margins 7%）
            "ttm": {"revenue": {"value": 100_000e6},
                    "cfo": {"value": 25_000e6}, "capex": {"value": 5_000e6}},
            # 历史年度中位 6%：产线谷底锚 2.4%、上界 max(0.65, 1.2×6%)=0.65
            "revenue_annual": {k: 100_000e6 for k in a},
            "cfo_annual": {k: 11_000e6 for k in a},
            "capex_annual": {k: 5_000e6 for k in a}}


def _run(tmp_path, cfg):
    cfg_dir = tmp_path / "cfgs"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "TSMX_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    fdir = tmp_path / "facts" / "TSMX"
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "facts.json").write_text(json.dumps(_facts()), encoding="utf-8")
    return check_main(["check_configs.py", str(cfg_dir), str(tmp_path / "facts")])


def test_override_deviation_config_passes_like_production(tmp_path, capsys):
    """产线判定：闸门触发 → 谷底锚=历史中位 6%（floor 2.4%）→ margins 7% PASS。
    修前工具用陈旧 TTM 锚（floor 8%）BLOCK 同一份 config——假警报。"""
    rc = _run(tmp_path, _cfg())
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "1 PASS / 0 BLOCK" in out and "完整" in out


def test_small_deviation_still_uses_ttm_anchor(tmp_path, capsys):
    """偏差 +5% <= 10%：闸门不触发，当前 TTM 锚照用（floor 8% 拦 7%）——
    与产线同判 BLOCK，且拒绝文案点名 TTM 锚。工具不许比产线松。"""
    rc = _run(tmp_path, _cfg(ttm_revenue_override=105_000.0))
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCK" in out and "当前 TTM FCF" in out
