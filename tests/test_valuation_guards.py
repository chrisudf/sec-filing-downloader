# -*- coding: utf-8 -*-
"""判断层护栏：margins 情景排序 + 期后资本事件声明（2026-08-31）。

回归对象是 INTC 2026-08-30 那次真实运行：net_cash 停在旧报告期 → bear 的 P/FCF
红旗是假的 → gate 打回后判断层上修 bear.margins 越过 base → 自相矛盾的假设发货。
"""
import ast
import copy
from pathlib import Path

import pytest

from app.valuation_service import _validate_judgment

# ---- 按 test_pure.py 的手法，从生产源码逐字抽出 vintage_warnings 再 exec。
# engine.py 是模块级脚本（import 即执行、要 sys.argv），不能直接 import；
# 抽函数保证测的是生产代码本体，不是副本。
_ENG = Path(__file__).resolve().parent.parent / "valuation" / "engine.py"
_SRC = _ENG.read_text(encoding="utf-8")
_SEG = [ast.get_source_segment(_SRC, n) for n in ast.parse(_SRC).body
        if isinstance(n, ast.FunctionDef)
        and n.name in ("vintage_warnings", "band_lag_warnings")]
assert len(_SEG) == 2, _SEG
_NS = {}
exec(chr(10).join(_SEG), _NS)
vintage_warnings = _NS["vintage_warnings"]
band_lag_warnings = _NS["band_lag_warnings"]


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


# =====================================================================
# fix#2 / fix#3'：vintage_warnings —— 报告期口径 vs 当前值的时点一致性
# =====================================================================

def _cfg(shares=1000.0, fwd_shares=1000.0, net_cash=5000.0, mcap=100000.0, **kw):
    c = dict(shares=shares, fwd_shares=fwd_shares, net_cash=net_cash, mcap=mcap)
    c.update(kw)
    return c


def _vt(age=60, end="2026-06-27"):
    return {"report_end": end, "filed": "2026-07-24", "age_days": age}


EVENT = {"date": "2026-08-18", "kind": "增发", "amount_musd": 19700.0, "note": "8-K"}


# ---- 不该响的情形 ----

@pytest.mark.parametrize("age", [None, 0, 45])
def test_fresh_or_unknown_age_silent(age):
    """龄 <=45 或未知 -> 不出声。阈值是 >45，45 本身不触发。"""
    assert vintage_warnings(_cfg(), _vt(age=age)) == []


def test_missing_vintage_silent():
    assert vintage_warnings(_cfg(), {}) == []
    assert vintage_warnings(_cfg(), None) == []


# ---- 声明了事件：列出来，不再唠叨 ----

def test_declared_event_listed_with_mcap_share():
    (lv, msg), = vintage_warnings(
        _cfg(net_cash=-1100.0, mcap=475500.0, post_period_capital_events=[EVENT]), _vt(age=64))
    assert lv == "yellow"
    assert "期后资本事件已声明" in msg and "+19,700M" in msg
    assert "4.1%" in msg or "4.2%" in msg          # 19700/475500
    assert "2026-08-18 增发" in msg


def test_declared_event_wins_over_fresh_age():
    """事件优先于龄：即使报告很新，声明了就该列出来。"""
    out = vintage_warnings(_cfg(post_period_capital_events=[EVENT]), _vt(age=3))
    assert len(out) == 1 and "期后资本事件已声明" in out[0][1]


def test_declared_event_zero_mcap_no_zerodiv():
    out = vintage_warnings(_cfg(mcap=0, post_period_capital_events=[EVENT]), _vt(age=64))
    assert len(out) == 1 and "占市值" not in out[0][1]


def test_declared_events_netted():
    """多笔事件取净额：增发 +200 与回购 −50 净 +150。"""
    evs = [dict(EVENT, amount_musd=200.0),
           dict(EVENT, kind="回购", amount_musd=-50.0)]
    (_, msg), = vintage_warnings(_cfg(post_period_capital_events=evs), _vt(age=64))
    assert "净 +150M" in msg


# ---- 股数/现金自相矛盾：可机械证明的那条 ----

def test_buyback_modeled_but_cash_not_flags_overvaluation():
    """AAPL 实测形态：股数侧减了 155M（建模回购），net_cash 停在报告期没扣。"""
    (lv, msg), = vintage_warnings(
        _cfg(shares=14715.0, fwd_shares=14560.0, net_cash=62173.0,
             post_period_capital_events=[]), _vt(age=65))
    assert lv == "yellow"
    assert "时点不一致" in msg and "已建模净回购" in msg
    assert "系统性**高估**" in msg
    assert "声明为空，与股数侧的假设矛盾" in msg


def test_issuance_modeled_but_cash_not_flags_undervaluation():
    """INTC 实测形态：fwd_shares 含增发新股，net_cash 未含增发现金。"""
    (lv, msg), = vintage_warnings(
        _cfg(shares=5104.0, fwd_shares=5300.0, net_cash=-20000.0), _vt(age=64))
    assert lv == "yellow"
    assert "已建模净增发" in msg and "系统性**低估**" in msg
    assert "请在 post_period_capital_events 里声明" in msg


def test_empty_declaration_does_not_silence_inconsistency():
    """声明 [] 不是静默开关 —— 这正是第一版设计的洞（AAPL 抓到）。"""
    c = _cfg(shares=14715.0, fwd_shares=14560.0, post_period_capital_events=[])
    assert vintage_warnings(c, _vt(age=65)) != []


@pytest.mark.parametrize("fwd,fires", [
    (1000.0, False),   # 无差
    (995.0, False),    # 差 0.5%，阈值是**严格大于**，不触发
    (994.9, True),     # 刚过阈值
    (1005.1, True),    # 增发方向同样过阈值
])
def test_share_delta_threshold(fwd, fires):
    (_, msg), = vintage_warnings(_cfg(fwd_shares=fwd), _vt(age=60))
    assert ("时点不一致" in msg) is fires


def test_zero_shares_no_zerodiv():
    out = vintage_warnings(_cfg(shares=0.0, fwd_shares=100.0), _vt(age=60))
    assert len(out) == 1 and "时点不一致" not in out[0][1]


# ---- 分红盲区：股数不动也要提示（KO 抓到） ----

def test_dividend_blindspot_declared_empty_still_warns():
    """KO 实测形态：股数只降 0.21%（不触发机械检查），但分红持续流出。"""
    (lv, msg), = vintage_warnings(
        _cfg(shares=4314.0, fwd_shares=4305.0, net_cash=-29500.0,
             post_period_capital_events=[]), _vt(age=59, end="2026-07-03"))
    assert lv == "yellow"
    assert "报告期末已 59 天" in msg
    assert "分红不改股数" in msg and "机械检查看不见它" in msg


def test_undeclared_asks_to_declare():
    (_, msg), = vintage_warnings(_cfg(), _vt(age=60))
    assert "期后资本事件未声明" in msg and "分红" in msg


@pytest.mark.parametrize("age,severe", [(60, False), (100, False), (101, True)])
def test_severe_staleness_wording(age, severe):
    (_, msg), = vintage_warnings(_cfg(), _vt(age=age))
    assert ("严重滞后" in msg) is severe


# ---- 最关键的不变量 ----

@pytest.mark.parametrize("cfg,vt", [
    (_cfg(), _vt(age=60)),
    (_cfg(), _vt(age=400)),
    (_cfg(shares=14715.0, fwd_shares=14560.0), _vt(age=65)),
    (_cfg(shares=5104.0, fwd_shares=5300.0), _vt(age=64)),
    (_cfg(post_period_capital_events=[EVENT]), _vt(age=64)),
    (_cfg(post_period_capital_events=[]), _vt(age=59)),
])
def test_never_red(cfg, vt):
    """绝不能是 red：red 会把这条打回判断层，而它是数据事实不是假设——
    判断层能"修"它的唯一途径就是扭曲假设，那正是本护栏要防的那条级联的成因。"""
    assert all(lv == "yellow" for lv, _ in vintage_warnings(cfg, vt))


def test_at_most_one_warning():
    """每次最多出一条，避免红旗区被同一件事刷屏。"""
    for cfg, vt in [(_cfg(), _vt(age=60)),
                    (_cfg(shares=14715.0, fwd_shares=14560.0), _vt(age=65)),
                    (_cfg(post_period_capital_events=[EVENT]), _vt(age=64))]:
        assert len(vintage_warnings(cfg, vt)) <= 1


# =====================================================================
# band_lag_warnings —— PE 带子的滞后提示
# 已实现 NTM PE 的分母必须等未来 12 个月真的发生，滞后约一年是**口径下限**，
# 不是数据缺失（2026-08-31 实测 AMZN 395 天 / KO 404 / AAPL 305）。
# =====================================================================

_TN = {"pctiles": {"50": 32.59}, "gap_since_main_band": {
    "span": {"start": "2025-08-01", "end": "2026-07-30"}, "days": 250, "p50": 32.57}}


@pytest.mark.parametrize("lag", [None, 0, 200, 270])
def test_band_lag_silent_when_short(lag):
    """阈值 270，等于不触发（严格大于）。"""
    assert band_lag_warnings({"trailing_nolag": _TN}, {"end": "x", "lag_days": lag}, 32.3) == []


def test_band_lag_silent_without_span():
    assert band_lag_warnings({}, None, 32.3) == []


def test_band_lag_fires_and_is_yellow():
    """AMZN 实测形态：滞后 395 天、现价 32.3x 落在带内第 82 百分位——
    此前只有跌出 P10/P90 才提示，带内一声不吭。"""
    (lv, msg), = band_lag_warnings(
        {"trailing_nolag": _TN}, {"end": "2025-07-31", "lag_days": 395}, 32.3)
    assert lv == "yellow"
    assert "2025-07-31" in msg and "395 天" in msg
    assert "32.3x" in msg
    assert "不是数据缺失" in msg          # 明说这不是 bug，别去"修"它
    assert "最近一年的倍数完全不在这个分布里" in msg


def test_band_lag_quotes_blind_window():
    (_, msg), = band_lag_warnings(
        {"trailing_nolag": _TN}, {"end": "2025-07-31", "lag_days": 395}, 32.3)
    assert "2025-08-01~2026-07-30" in msg and "250 天" in msg
    assert "32.6x" in msg                              # 盲区 trailing P50
    assert "不可直接相减" in msg                        # 口径差必须写明


def test_band_lag_without_nolag_reference():
    """没有无滞后对照时只报滞后本身，不硬凑一个数。"""
    (_, msg), = band_lag_warnings({}, {"end": "2025-07-31", "lag_days": 395}, 32.3)
    assert "395 天" in msg and "盲区" not in msg


def test_band_lag_handles_missing_now_pe():
    (_, msg), = band_lag_warnings({}, {"end": "2025-07-31", "lag_days": 395}, None)
    assert "带内分位是" in msg          # 不渲染 "（现价 Nonex）"


@pytest.mark.parametrize("band,span,now", [
    ({"trailing_nolag": _TN}, {"end": "2025-07-31", "lag_days": 395}, 32.3),
    ({}, {"end": "2025-07-31", "lag_days": 999}, None),
])
def test_band_lag_never_red(band, span, now):
    assert all(lv == "yellow" for lv, _ in band_lag_warnings(band, span, now))
