# -*- coding: utf-8 -*-
"""战略持股进 DCF 与 SOTP（0033）。

回归对象：AMZN 的 Anthropic/OpenAI 持股（TODO v0.4.4 估约 $18.7/股税后）此前不进任何
一条腿——FCFF 不含重估收益、EBIT 不含营业外、net_cash 只收现金与有价证券。
口径（2026-09-25 与本人确认）：按资产负债表账面值、只进 DCF 与 SOTP、非上市打 20% 折价、
上市不打折、未实现收益按 21% 计税。

本文件钉住的是「不变坏」的几条承诺：
- 没申报（缺键或 []）的运行与改动前逐位相同；
- PE 腿、DCF 护栏（n.m. 闸 / P/FCF / 终值占比）一律按不含持股的经营价值判；
- 拿不准一律不计入：布尔没显式写 false、XBRL 无从核对、超出 XBRL 上限；
- Excel 公式与引擎同源（verify_report 独立重算一致）。
"""
import ast
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app.valuation_service import _compact_facts, _prev_core, _validate_judgment
from tests.test_note_global_warnings import _std_cfg, _std_facts
from tests.test_prompt_contract import _mk
from valuation.fetch_facts import SPEC

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "valuation" / "engine.py"
BUILD = ROOT / "valuation" / "build_report.py"
VERIFY = ROOT / "valuation" / "verify_report.py"
COMPARE = ROOT / "valuation" / "compare.py"
PROMPT = (ROOT / "valuation" / "judgment_prompt.md").read_text(encoding="utf-8")

# ---- 从生产源码逐字抽出函数与 HOLD_* 常量（engine.py 是模块级脚本，不能 import）----
_SRC = ENGINE.read_text(encoding="utf-8")
_NODES = [n for n in ast.parse(_SRC).body
          if (isinstance(n, ast.FunctionDef)
              and n.name in ("_isnum", "holdings_xbrl_cap", "strategic_holdings_value"))
          or (isinstance(n, ast.Assign)
              and any(getattr(t, "id", "").startswith("HOLD_") for t in n.targets))]
assert len(_NODES) == 6, [getattr(n, "name", None) for n in _NODES]
_NS = {"date": date}
exec("\n".join(ast.get_source_segment(_SRC, n) for n in _NODES), _NS)
cap_fn = _NS["holdings_xbrl_cap"]
value_fn = _NS["strategic_holdings_value"]

ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def _item(**over):
    d = dict(name="X Labs 优先股", kind="private", carrying_value_musd=300.0,
             cost_basis_musd=50.0, in_net_cash=False, income_in_operating_income=False,
             source="10-Q Note 4 Investments")
    d.update(over)
    return d


# ======================= 纯函数：strategic_holdings_value =======================

def test_no_declaration_is_exact_zero():
    for items in (None, []):
        assert value_fn(items, 1000.0, 100.0) == (0.0, None, [])


def test_private_discount_then_tax_on_gain_over_basis():
    v, d, w = value_fn([_item()], 400.0, 100.0)
    # 300 × 0.8 = 240；税 = 21% × (240 − 50) = 39.9；税后 200.1
    assert v == pytest.approx(200.1)
    assert d["status"] == "counted" and d["per_share"] == pytest.approx(2.0)
    it = d["items"][0]
    assert (it["after_discount_musd"], it["tax_musd"], it["net_musd"]) == (240.0, 39.9, 200.1)
    assert [lv for lv, _ in w] == ["info"]


def test_public_no_discount_and_missing_basis_taxes_full_value():
    v, d, _ = value_fn([_item(kind="public", cost_basis_musd=None)], 400.0, 100.0)
    assert v == pytest.approx(300 * 0.79)
    assert d["items"][0]["cost_basis_musd"] is None


def test_basis_above_value_means_no_tax():
    v, _, _ = value_fn([_item(cost_basis_musd=500.0)], 400.0, 100.0)
    assert v == pytest.approx(240.0)


@pytest.mark.parametrize("flag", ["in_net_cash", "income_in_operating_income"])
def test_flag_true_is_recorded_but_not_counted(flag):
    v, d, w = value_fn([_item(**{flag: True})], 400.0, 100.0)
    assert v == 0.0 and d["status"] == "none_eligible"
    assert d["items"][0]["counted"] is False
    assert w == []            # 已如实声明的跳过是正常情形，不打旗


@pytest.mark.parametrize("flag", ["in_net_cash", "income_in_operating_income"])
@pytest.mark.parametrize("bad", [None, "false", 0])
def test_flag_not_explicit_false_is_not_counted(flag, bad):
    """只认显式 False：缺键 / 字符串 "false" / 0 都不计入并打黄旗。"""
    it = _item()
    if bad is None:
        del it[flag]
    else:
        it[flag] = bad
    v, d, w = value_fn([it], 400.0, 100.0)
    assert v == 0.0 and not d["items"][0]["counted"]
    assert any(lv == "yellow" and "未计入" in m for lv, m in w)


def test_over_cap_is_red_and_nothing_counted():
    v, d, w = value_fn([_item(carrying_value_musd=300.0), _item(name="Y", carrying_value_musd=200.0)],
                       400.0, 100.0, {"inv_nonmarketable_equity_instant": ["2026-06-30", 400.0]})
    assert v == 0.0 and d["status"] == "over_cap"
    assert all(not it["counted"] for it in d["items"])
    reds = [m for lv, m in w if lv == "red"]
    assert len(reds) == 1 and "500M" in reds[0] and "400M" in reds[0]


def test_cap_tolerance_two_percent():
    assert value_fn([_item(carrying_value_musd=408.0)], 400.0, 100.0)[1]["status"] == "counted"
    assert value_fn([_item(carrying_value_musd=408.1)], 400.0, 100.0)[1]["status"] == "over_cap"


def test_no_xbrl_cap_is_yellow_and_nothing_counted():
    v, d, w = value_fn([_item()], None, 100.0)
    assert v == 0.0 and d["status"] == "unverified"
    assert [lv for lv, _ in w] == ["yellow"]


def test_malformed_items_are_skipped_not_crash():
    items = ["junk", _item(carrying_value_musd=0), _item(kind="fund"),
             _item(carrying_value_musd="300"), _item(name="好的", carrying_value_musd=100.0)]
    v, d, w = value_fn(items, 400.0, 100.0)
    assert [it["counted"] for it in d["items"]] == [False, False, False, False, True]
    assert v == pytest.approx(100 * 0.8 - 0.21 * (80 - 50))
    assert any(lv == "yellow" and "字段不全" in m for lv, m in w)


def test_cap_only_checks_counted_items():
    """已在 net_cash 里的项不参与上限核对（它们本来就不加）。"""
    v, d, _ = value_fn([_item(carrying_value_musd=300.0),
                        _item(name="国债", carrying_value_musd=5000.0, in_net_cash=True)],
                       400.0, 100.0)
    assert d["status"] == "counted" and v > 0


# ======================= 纯函数：holdings_xbrl_cap =======================

def test_cap_sums_latest_per_bucket_and_drops_stale():
    facts = {
        "inv_nonmarketable_equity_instant": {"2025-12-31": 50e9, "2026-06-30": 90e9},
        "afs_securities_total_instant": {"2026-06-30": 97.9e9},
        # 停标的旧科目：比最新期早 400 天以上 → 剔除，不撑大上限
        "inv_equity_method_instant": {"2024-12-31": 30e9},
        # 只在 10-K 里标的科目：半年前 → 保留
        "inv_long_term_instant": {"2025-12-31": 10e9},
        "inv_other_long_term_instant": {"2026-06-30": 0.0},     # 非正值不计
    }
    cap, parts = cap_fn(facts)
    assert cap == pytest.approx(90_000 + 97_900 + 10_000)
    assert set(parts) == {"inv_nonmarketable_equity_instant",
                          "afs_securities_total_instant", "inv_long_term_instant"}


def test_cap_none_when_no_bucket():
    assert cap_fn({}) == (None, {})
    assert cap_fn({"inv_equity_method_instant": {"2026-06-30": 0}}) == (None, {})


def test_fetch_facts_declares_cap_buckets_as_instants():
    for k in ("inv_nonmarketable_equity", "inv_equity_method", "inv_long_term",
              "inv_other_long_term"):
        assert SPEC[k]["instant"] is True
    keys = set(_NS["HOLD_CAP_KEYS"])
    assert {k[:-len("_instant")] for k in keys} <= set(SPEC)


# ======================= 引擎全链（子进程）=======================

def _run(tmp_path, cfg, facts, tag):
    c, f, o = tmp_path / f"c_{tag}.json", tmp_path / f"f_{tag}.json", tmp_path / f"o_{tag}.json"
    c.write_text(json.dumps(cfg), encoding="utf-8")
    f.write_text(json.dumps(facts), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ENGINE), str(c), str(f), str(o)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(o.read_text(encoding="utf-8"))


def _hold_facts():
    f = _std_facts()
    f["inv_nonmarketable_equity_instant"] = {"2026-06-30": 400e6}
    return f


HOLD = [_item()]          # 税后 200.1M，每股 2.001（shares=100）
HPS = 200.1 / 100


def test_empty_list_is_byte_identical_to_missing_key(tmp_path):
    a = _run(tmp_path, _std_cfg(), _hold_facts(), "a")
    b = _run(tmp_path, _std_cfg(strategic_holdings=[]), _hold_facts(), "b")
    assert a == b
    assert "strategic_holdings" not in a["meta"]


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sh")
    return (_run(tmp, _std_cfg(), _hold_facts(), "none"),
            _run(tmp, _std_cfg(strategic_holdings=HOLD), _hold_facts(), "hold"))


def test_only_dcf_and_sotp_legs_move(pair):
    none, hold = pair
    for sc in ("bear", "base", "bull"):
        a, b = none["scenarios"][sc], hold["scenarios"][sc]
        assert b["pe_target"] == a["pe_target"]
        assert b["eps1"] == a["eps1"]
        assert b["dcf_ps"] == pytest.approx(a["dcf_ps"] + HPS, abs=0.06)
        assert b["dcf_ps_operating"] == a["dcf_ps"]
        assert b["sotp_ps"] == pytest.approx(a["sotp_ps"] + HPS, abs=0.06)
        assert b["diagnostics"]["pe_plus_holdings"] == pytest.approx(a["pe_target"] + HPS, abs=0.06)
        assert "dcf_ps_operating" not in a


def test_guard_diagnostics_unchanged_by_holdings(pair):
    """DCF 护栏看的是经营价值：诊断量除 PE 参考数外逐项不变，情景警告不增不减。"""
    none, hold = pair
    for sc in ("bear", "base", "bull"):
        a, b = none["scenarios"][sc], hold["scenarios"][sc]
        # blend_p_adjni 是综合目标价的读数（综合含持股，本该变）；其余全是护栏输入
        skip = ("pe_plus_holdings", "blend_p_adjni")
        assert ({k: v for k, v in b["diagnostics"].items() if k not in skip}
                == {k: v for k, v in a["diagnostics"].items() if k not in skip})
        assert b["blend_methods"] == a["blend_methods"]
        # 方法离散度与终值敏感性是「进综合的腿」的读数（腿含持股，本该变）；其余警告
        # 全部来自护栏输入，一条不增不减
        readouts = ("终值折现占 EV", "方法离散度")
        assert ([m for _, m in b["warnings"] if not m.startswith(readouts)]
                == [m for _, m in a["warnings"] if not m.startswith(readouts)])


def test_meta_warning_and_reverse_dcf(pair):
    none, hold = pair
    sh = hold["meta"]["strategic_holdings"]
    assert sh["status"] == "counted" and sh["value_musd"] == pytest.approx(200.1)
    assert sh["legs"] == ["dcf", "sotp"]
    assert any(lv == "info" and "战略持股计入 DCF 与 SOTP" in m
               for lv, m in hold["warnings_global"])
    # 市值里含持股：经营部分要与 市值−持股 比，隐含增速更低
    assert hold["reverse_dcf"] < none["reverse_dcf"]


def test_sensitivity_table_shifts_by_holdings_per_share(pair):
    none, hold = pair
    for w, row in none["sensitivity"].items():
        for g, v in row.items():
            assert abs(hold["sensitivity"][w][g] - (v + HPS)) <= 1


def test_negative_operating_dcf_stays_out_of_blend(tmp_path):
    """经营 DCF 为负、持股把 DCF 腿抬成正数：n.m. 闸仍按经营价值判，这条腿不许投票回来。"""
    cfg = _std_cfg(strategic_holdings=[_item(carrying_value_musd=5000.0, cost_basis_musd=5000.0)])
    for s in cfg["scenarios"].values():
        s["margins"] = [-0.02] * 10
    facts = _std_facts()
    facts["inv_nonmarketable_equity_instant"] = {"2026-06-30": 6000e6}
    out = _run(tmp_path, cfg, facts, "neg")
    for sc in ("bear", "base", "bull"):
        v = out["scenarios"][sc]
        assert v["dcf_ps_operating"] < 0 < v["dcf_ps"]
        assert "dcf" not in v["blend_methods"]


def test_over_cap_leaves_every_value_unchanged(tmp_path, pair):
    none, _ = pair
    facts = _std_facts()
    facts["inv_nonmarketable_equity_instant"] = {"2026-06-30": 100e6}
    out = _run(tmp_path, _std_cfg(strategic_holdings=HOLD), facts, "over")
    assert out["meta"]["strategic_holdings"]["status"] == "over_cap"
    assert any(lv == "red" for lv, _ in out["warnings_global"])
    for sc in ("bear", "base", "bull"):
        for k in ("pe_target", "dcf_ps", "sotp_ps", "blend"):
            assert out["scenarios"][sc][k] == none["scenarios"][sc][k]
    assert out["reverse_dcf"] == none["reverse_dcf"]
    assert out["sensitivity"] == none["sensitivity"]


def test_financials_mode_ignores_holdings(tmp_path):
    """fin 分支没有 DCF/SOTP 桥：申报了也不进任何数，引擎不崩。"""
    from tests.test_fin_valuation import _fin_cfg, _fin_facts
    a = _run(tmp_path, _fin_cfg(), _fin_facts(), "fa")
    b = _run(tmp_path, _fin_cfg(strategic_holdings=HOLD), _fin_facts(), "fb")
    assert a == b


# ======================= Excel：公式与引擎同源 =======================

def test_report_formulas_match_engine_with_holdings(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    pytest.importorskip("formulas")
    _run(tmp_path, _std_cfg(strategic_holdings=HOLD), _hold_facts(), "rep")
    xlsx = tmp_path / "rep.xlsx"
    r = subprocess.run([sys.executable, str(BUILD), str(tmp_path / "o_rep.json"), str(xlsx)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)
    assert r.returncode == 0, r.stdout + r.stderr
    wb = openpyxl.load_workbook(xlsx)
    assert wb["情景假设"]["B29"].value == pytest.approx(200.1)
    assert wb["DCF"]["A14"].value == "加：净现金 + 战略持股 ($M)"
    assert wb["DCF"]["B14"].value == "='情景假设'!$B$26+'情景假设'!$B$29"
    assert wb["SOTP"]["C10"].value == "='情景假设'!$B$26+'情景假设'!$B$29"
    r = subprocess.run([sys.executable, str(VERIFY), str(tmp_path / "o_rep.json"), str(xlsx)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "战略持股" in r.stdout


# ======================= 校验层 =======================

def _judg(**over):
    return _mk(strategic_holdings=[_item()], strategic_holdings_note="10-Q Note 4", **over)


def test_validator_accepts_well_formed_and_empty():
    _validate_judgment(_judg(), "standard")
    _validate_judgment(_mk(), "standard")          # [] + note


def test_validator_requires_field_and_note():
    d = _mk()
    del d["strategic_holdings"]
    with pytest.raises(ValueError, match="strategic_holdings 必填"):
        _validate_judgment(d, "standard")
    with pytest.raises(ValueError, match="strategic_holdings_note 必填"):
        _validate_judgment(_mk(strategic_holdings_note=" "), "standard")


@pytest.mark.parametrize("over, pat", [
    ({"kind": "fund"}, "kind"),
    ({"carrying_value_musd": 0}, "carrying_value_musd"),
    ({"carrying_value_musd": "300"}, "carrying_value_musd"),
    ({"in_net_cash": "false"}, "in_net_cash"),
    ({"income_in_operating_income": 0}, "income_in_operating_income"),
    ({"cost_basis_musd": -1}, "cost_basis_musd"),
    ({"source": ""}, "source"),
])
def test_validator_rejects_bad_items(over, pat):
    with pytest.raises(ValueError, match=pat):
        _validate_judgment(_mk(strategic_holdings=[_item(**over)],
                               strategic_holdings_note="x"), "standard")


def test_validator_rejects_missing_item_key():
    it = _item()
    del it["in_net_cash"]
    with pytest.raises(ValueError, match="须含"):
        _validate_judgment(_mk(strategic_holdings=[it], strategic_holdings_note="x"), "standard")


def test_continuity_carries_holdings():
    assert _prev_core({"strategic_holdings": HOLD, "net_cash": 1})["strategic_holdings"] == HOLD


# ======================= prompt / 注入 / compare 契约 =======================

def test_prompt_states_engine_constants():
    assert f"{_NS['HOLD_DISCOUNT']['private']:.0%} 折价" in PROMPT
    assert f"{_NS['HOLD_GAIN_TAX']:.0%}" in PROMPT
    assert _NS["HOLD_DISCOUNT"]["public"] == 0.0 and "上市不打折" in PROMPT
    assert "只加进 DCF 与\n     SOTP" in PROMPT or "只加进 DCF 与 SOTP" in PROMPT
    assert '"strategic_holdings": [' in PROMPT and '"strategic_holdings_note"' in PROMPT


def test_compact_facts_lists_investment_buckets():
    from tests.test_prompt_injection import _facts as inj_facts
    f = inj_facts()
    assert "投资类科目时点" in _compact_facts(f) and "申报的战略持股无从核对" in _compact_facts(f)
    f["inv_nonmarketable_equity_instant"] = {"2026-06-30": 90e9}
    assert "非上市股权(计量替代法) 2026-06-30 90,000M" in _compact_facts(f)


def test_compare_flags_holdings_caliber_change(tmp_path, pair):
    none, hold = pair
    po, pn = tmp_path / "o.json", tmp_path / "n.json"
    po.write_text(json.dumps(none), encoding="utf-8")
    pn.write_text(json.dumps(hold), encoding="utf-8")
    r = subprocess.run([sys.executable, str(COMPARE), str(po), str(pn)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "战略持股每股值不同（0.00 → 2.00）" in r.stdout
    r = subprocess.run([sys.executable, str(COMPARE), str(po), str(po)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)
    assert "战略持股" not in r.stdout


def test_vintage_record_carries_holdings_ps(tmp_path, pair):
    sys.path.insert(0, str(ROOT / "valuation"))
    import vintages
    none, hold = pair
    for v in (none, hold):
        v = dict(v, meta=dict(v["meta"], vintage={"report_end": "2026-06-30"}))
        vintages.record(v, gate_clean=True, root=tmp_path)
    samples = vintages.load("TGLB", root=tmp_path)[0]["samples"]
    assert [s.get("strategic_holdings_ps") for s in samples] == [None, 2.0]
