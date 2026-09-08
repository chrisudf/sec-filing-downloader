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
        and n.name in ("_isnum", "vintage_warnings", "band_lag_warnings",
                       "other_income_crosscheck", "hist_fcf_margins",
                       "terminal_margin_warnings", "terminal_sensitivity",
                       "dcf", "dcf_diag_warnings", "ps_reference")]
# _isnum 是这些函数共用的模块级谓词（排除 bool），必须一起抽——
# 否则 exec 出来的命名空间里没有它，全部 NameError
assert len(_SEG) == 10, _SEG
_NS = {}
exec(chr(10).join(_SEG), _NS)
_isnum = _NS["_isnum"]
vintage_warnings = _NS["vintage_warnings"]
band_lag_warnings = _NS["band_lag_warnings"]
other_income_crosscheck = _NS["other_income_crosscheck"]
hist_fcf_margins = _NS["hist_fcf_margins"]
terminal_margin_warnings = _NS["terminal_margin_warnings"]
terminal_sensitivity = _NS["terminal_sensitivity"]
dcf = _NS["dcf"]
dcf_diag_warnings = _NS["dcf_diag_warnings"]
ps_reference = _NS["ps_reference"]


def _mk(**over):
    """最小合法 standard config；子情景可用 over={'bear': {...}} 覆盖。"""
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
    """给了 net_cash_impact_musd 才敢报"对净现金影响"与市值占比——
    增发的现金流向与净现金影响相同（$19.7B 进来、不增负债）。"""
    ev = [dict(EVENT, net_cash_impact_musd=19700.0)]
    (lv, msg), = vintage_warnings(
        _cfg(net_cash=-1100.0, mcap=475500.0, post_period_capital_events=ev), _vt(age=64))
    assert lv == "yellow"
    assert "期后资本事件已声明" in msg and "+19,700M" in msg
    assert "对净现金影响" in msg
    assert "4.1%" in msg or "4.2%" in msg          # 19700/475500
    assert "2026-08-18 增发" in msg


def test_declared_event_without_impact_has_no_mcap_share():
    """缺字段时连市值占比都不给——占市值多少本来就是净现金口径的问题。"""
    (_, msg), = vintage_warnings(
        _cfg(net_cash=-1100.0, mcap=475500.0, post_period_capital_events=[EVENT]),
        _vt(age=64))
    assert "现金流向合计" in msg and "占市值" not in msg


def test_declared_event_wins_over_fresh_age():
    """事件优先于龄：即使报告很新，声明了就该列出来。"""
    out = vintage_warnings(_cfg(post_period_capital_events=[EVENT]), _vt(age=3))
    assert len(out) == 1 and "期后资本事件已声明" in out[0][1]


def test_declared_event_zero_mcap_no_zerodiv():
    out = vintage_warnings(_cfg(mcap=0, post_period_capital_events=[EVENT]), _vt(age=64))
    assert len(out) == 1 and "占市值" not in out[0][1]


def test_declared_events_netted():
    """多笔取净额：增发 +200 与回购 −50，两者现金流向=净现金影响，合计 +150。"""
    evs = [dict(EVENT, amount_musd=200.0, net_cash_impact_musd=200.0),
           dict(EVENT, kind="回购", amount_musd=-50.0, net_cash_impact_musd=-50.0)]
    (_, msg), = vintage_warnings(_cfg(post_period_capital_events=evs), _vt(age=64))
    assert "对净现金影响 +150M" in msg


# ---- 股数/现金自相矛盾：可机械证明的那条 ----

def test_buyback_modeled_but_cash_not_flags_overvaluation():
    """股数侧建模了重手回购（3.1%，回购趋势/SBC 外推到不了的量级），net_cash
    停在报告期没扣。v4 前用 AAPL 的 1.05%（155M）触发——那个量级现归入常规
    前瞻建模、由分红盲区条覆盖（见下 test_routine_buyback_drift_*）。"""
    (lv, msg), = vintage_warnings(
        _cfg(shares=14715.0, fwd_shares=14260.0, net_cash=62173.0,
             post_period_capital_events=[]), _vt(age=65))
    assert lv == "yellow"
    assert "时点不一致" in msg and "已建模净回购" in msg
    assert "系统性**高估**" in msg
    assert "1.5%" in msg          # 触发时文案要写明阈值
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
    c = _cfg(shares=14715.0, fwd_shares=14260.0, post_period_capital_events=[])
    assert vintage_warnings(c, _vt(age=65)) != []


@pytest.mark.parametrize("fwd,fires", [
    (1000.0, False),   # 无差
    (995.0, False),    # 差 0.5%
    (986.0, False),    # 差 1.4%——SBC 摊薄/回购趋势的常规前瞻建模量级（TSLA +0.8% 实测噪声）
    (985.0, False),    # 恰 1.5%，阈值是**严格大于**，不触发
    (984.9, True),     # 刚过阈值
    (1015.1, True),    # 增发方向同样过阈值
    (970.0, True),     # 3%——离散期后事件的量级
])
def test_share_delta_threshold(fwd, fires):
    (_, msg), = vintage_warnings(_cfg(fwd_shares=fwd), _vt(age=60))
    assert ("时点不一致" in msg) is fires


def test_routine_buyback_drift_covered_by_dividend_blindspot():
    """AAPL 实测形态（v4 后）：1.05% 回购漂移不再触发机械矛盾条，但 AAPL 分红
    ——分红盲区条照常提示，不是彻底静默。"""
    (lv, msg), = vintage_warnings(
        _cfg(shares=14715.0, fwd_shares=14560.0, net_cash=62173.0,
             post_period_capital_events=[]), _vt(age=65), DIVQ)
    assert lv == "yellow"
    assert "时点不一致" not in msg and "分红不改股数" in msg


def test_sbc_creep_not_flagged_as_issuance():
    """TSLA 实测形态：fwd_shares +1.4% 是 SBC 摊薄的常规外推，不是期后现金增发。"""
    (_, msg), = vintage_warnings(
        _cfg(shares=1000.0, fwd_shares=1014.0, post_period_capital_events=[]),
        _vt(age=60), {})
    assert "时点不一致" not in msg and "现金流入未计入" not in msg
    assert "未见分红记录" in msg          # 落到剩余项提示，不是静默


def test_zero_shares_no_zerodiv():
    out = vintage_warnings(_cfg(shares=0.0, fwd_shares=100.0), _vt(age=60))
    assert len(out) == 1 and "时点不一致" not in out[0][1]


# ---- 分红盲区：股数不动也要提示（KO 抓到） ----

def test_no_dividend_payer_gets_accurate_wording():
    """AMZN 实测：不分红、股数差 +0.43%（低于阈值）。两条可自动排除的路径都
    排除了，再提"请确认分红流出"是噪音——改为说清楚还剩什么需要人看。"""
    (_, msg), = vintage_warnings(
        _cfg(shares=10903.0, fwd_shares=10950.0, net_cash=-9236.0,
             post_period_capital_events=[]), _vt(age=62), {})
    assert "未见分红记录" in msg
    assert "仅剩并购/分拆/发债偿债需人工确认" in msg
    assert "请自行确认分红流出" not in msg


DIVQ = {"2025-09-30": 2.2e9, "2025-12-31": 2.3e9,
        "2026-03-31": 2.3e9, "2026-06-30": 2.4e9}


def test_dividend_payer_gets_blindspot_reminder():
    (_, msg), = vintage_warnings(_cfg(post_period_capital_events=[]), _vt(age=60), DIVQ)
    assert "分红不改股数" in msg


def test_only_last_four_quarters_count():
    """INTC 形态：有 70 期历史分红，但 2024-08 起停发 —— 看近四季，不看有没有过。"""
    stopped = {"2023-12-31": 5e8, "2024-03-31": 5e8,
               "2025-09-30": 0.0, "2025-12-31": 0.0,
               "2026-03-31": 0.0, "2026-06-30": 0.0}
    (_, msg), = vintage_warnings(_cfg(post_period_capital_events=[]), _vt(age=60), stopped)
    assert "未见分红记录" in msg


def test_missing_dividend_series_is_not_a_payer_claim():
    """序列缺失时措辞是"未见记录"而非"无分红"——证据不是事实断言。"""
    for d in (None, {}):
        (_, msg), = vintage_warnings(_cfg(post_period_capital_events=[]), _vt(age=60), d)
        assert "未见分红记录" in msg and "无分红" not in msg


def test_dividends_irrelevant_when_undeclared():
    """未声明时无论分不分红都要求去核对——那条路径还没走过。"""
    for d in (DIVQ, {}, None):
        (_, msg), = vintage_warnings(_cfg(), _vt(age=60), d)
        assert "期后资本事件未声明" in msg


def test_dividend_blindspot_declared_empty_still_warns():
    """KO 实测形态：股数只降 0.21%（不触发机械检查），但分红持续流出。"""
    (lv, msg), = vintage_warnings(
        _cfg(shares=4314.0, fwd_shares=4305.0, net_cash=-29500.0,
             post_period_capital_events=[]), _vt(age=59, end="2026-07-03"), DIVQ)
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

_TN = {"pctiles": {"50": 32.59}, "current": 28.13, "gap_since_main_band": {
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
    assert "非数据缺失" in msg            # 明说这不是 bug，别去"修"它
    assert "最近一年不在这个分布里" in msg


def test_band_lag_quotes_blind_window():
    (_, msg), = band_lag_warnings(
        {"trailing_nolag": _TN}, {"end": "2025-07-31", "lag_days": 395}, 32.3)
    assert "2025-08~2026-07" in msg                    # 盲区窗口（截到月）
    assert "32.6x" in msg and "28.1x" in msg           # 盲区中位 + 现价 trailing
    assert "不可直接比" in msg                          # 口径差必须写明


def test_band_lag_without_nolag_reference():
    """没有无滞后对照时只报滞后本身，不硬凑一个数。"""
    (_, msg), = band_lag_warnings({}, {"end": "2025-07-31", "lag_days": 395}, 32.3)
    assert "395 天" in msg and "盲区" not in msg


def test_band_lag_handles_missing_now_pe():
    (_, msg), = band_lag_warnings({}, {"end": "2025-07-31", "lag_days": 395}, None)
    assert "带内分位是" in msg          # 不渲染 "（现价 Nonex）"


def test_band_lag_nan_current_not_rendered():
    """KO/AAPL 实测 trailing_nolag.current 为 NaN——直接格式化会渲染出 "现价 nanx"。"""
    nan = float("nan")
    (_, msg), = band_lag_warnings(
        {"trailing_nolag": dict(_TN, current=nan)},
        {"end": "2025-07-23", "lag_days": 404}, 28.0)
    assert "nan" not in msg
    assert "32.6x" in msg and "现价 28.0x" in msg      # 盲区中位仍在，带内分位仍在


def test_band_lag_nan_now_pe_not_rendered():
    (_, msg), = band_lag_warnings({}, {"end": "x", "lag_days": 400}, float("nan"))
    assert "nan" not in msg and "带内分位是" in msg


def test_band_lag_nan_gap_p50_skips_block():
    (_, msg), = band_lag_warnings(
        {"trailing_nolag": {"gap_since_main_band": {"p50": float("nan")}}},
        {"end": "x", "lag_days": 400}, 30.0)
    assert "nan" not in msg and "盲区" not in msg


@pytest.mark.parametrize("band,span,now", [
    ({"trailing_nolag": _TN}, {"end": "2025-07-31", "lag_days": 395}, 32.3),
    ({}, {"end": "2025-07-31", "lag_days": 999}, None),
])
def test_band_lag_never_red(band, span, now):
    assert all(lv == "yellow" for lv, _ in band_lag_warnings(band, span, now))


# =====================================================================
# other_income_crosscheck —— 判断层给的数 vs 财报原始行
# 起因：AMZN 同日、同财报、同输入的两次运行，其他假设全部靠连续性机制逐字
# 沿用，唯独 other_income 从 1500 漂到 1000（-33%）—— 它是 need 里唯一
# 不用交推导的事实类字段。
# =====================================================================

def _q(**kw):
    """按 $ 原始单位造季度序列（facts 存原始美元，函数内 /1e6）。"""
    return {n + "_quarterly": {f"2026-{m:02d}-30": v * 1e6 / 4 for m in (3, 6, 9, 12)}
            for n, v in kw.items()}


def test_oi_silent_when_close():
    """判断层 1,000 vs 参考 1,000 —— 无差额不出声。"""
    f = _q(interest_income=1500, interest_expense_nonop=500, other_nonop=0)
    assert other_income_crosscheck(f, 1000, 8.0, 1000.0) == []


def test_oi_flags_amzn_shape():
    """AMZN 实测：other_nonop TTM +80,425M（私募股权重估）。
    自动采纳会把 EPS 抬 $7.4/股 —— 这正是"只对照不覆盖"的理由。"""
    f = _q(interest_income=4660, interest_expense_nonop=3331, other_nonop=80425)
    (lv, msg), = other_income_crosscheck(f, 1000, 8.22, 10950.0)
    assert lv == "yellow"
    assert "-80,754M" in msg and "EPS -7.37" in msg
    assert "其他非经营" in msg and "80,425" in msg      # 指出差额落在哪一项
    assert "引擎不自动采纳原始行" in msg


def test_oi_flags_ko_shape_and_names_expense_leg():
    """KO：利息支出 1,642 是权重最大项（绝对值），要被点名。"""
    f = _q(interest_income=828, interest_expense_nonop=1642, other_nonop=840)
    (_, msg), = other_income_crosscheck(f, 1500, 3.20, 4305.0)
    assert "+1,474M" in msg and "利息支出" in msg


def test_oi_unavailable_says_so_not_silent():
    """AAPL 实测：不标 InvestmentIncomeInterest，TTM 凑不齐四季。
    静默跳过正是 PR #9 Lesson 3 点名的陷阱 —— 必须显式说对照没跑成。"""
    f = _q(interest_expense_nonop=3933, other_nonop=-382)      # 缺利息收入
    (lv, msg), = other_income_crosscheck(f, 1000, 9.69, 14560.0)
    assert lv == "yellow"
    assert "无法与财报对照" in msg and "利息收入" in msg
    assert "全靠判断层给数" in msg


def test_oi_three_quarters_is_not_ttm():
    """**三个季度也算凑不齐** —— TTM 必须四季齐，否则 sum 出来的是 9 个月，
    与 $M 年化口径不可比。三条序列都给三季，才卡得住 `== 4` 这个边界
    （只让其中一条缺，另两条的 None 会先短路，测不到边界）。"""
    q3 = {f"2026-{m:02d}-30": 250e6 for m in (3, 6, 9)}
    f = {n + "_quarterly": dict(q3) for n in
         ("interest_income", "interest_expense_nonop", "other_nonop")}
    (_, msg), = other_income_crosscheck(f, 1000, 8.0, 1000.0)
    assert "无法与财报对照" in msg


@pytest.mark.parametrize("gap_musd,fires", [
    (40, False),     # EPS 0.04 < 绝对门槛 0.05
    (60, False),     # EPS 0.06 > 绝对门槛，但 < 10% x eps1(8.0)=0.80
    (900, True),     # EPS 0.90 > 两道门槛
])
def test_oi_dual_gate(gap_musd, fires):
    """两道门槛都要过：绝对 EPS 0.05 + 相对前瞻 EPS 10%。
    低 EPS 标的不被绝对值刷屏，高 EPS 标的不被小差额刷屏。"""
    f = _q(interest_income=1000, interest_expense_nonop=0, other_nonop=0)
    out = other_income_crosscheck(f, 1000 + gap_musd, 8.0, 1000.0)
    assert bool(out) is fires


def test_oi_absolute_gate_protects_low_eps_names():
    """绝对门槛单独可证：eps1 只有 0.20 时，相对门槛是 0.02——EPS 差 0.04
    已经越过相对门槛，全靠绝对门槛 0.05 拦住。去掉绝对门槛这条就会响。"""
    f = _q(interest_income=1000, interest_expense_nonop=0, other_nonop=0)
    assert other_income_crosscheck(f, 1040, 0.20, 1000.0) == []


def test_oi_relative_gate_protects_high_eps_names():
    """相对门槛单独可证：EPS 差 0.30 远超绝对门槛 0.05，但对 eps1=8.0
    只占 3.75%，不值得出旗。"""
    f = _q(interest_income=1000, interest_expense_nonop=0, other_nonop=0)
    assert other_income_crosscheck(f, 1300, 8.0, 1000.0) == []


def test_oi_no_shares_silent_but_no_eps1_still_warns():
    """fwd_shares 缺失时差额换算不成 EPS 口径，只能沉默；eps1=None（PE 腿
    n.m.）**不再沉默**——差额仍是真实信息，只是不给占比（与 eps1=0.0 的
    ZeroDivisionError 修复同一批行为变化，见下）。"""
    f = _q(interest_income=1000, interest_expense_nonop=0, other_nonop=0)
    assert other_income_crosscheck(f, 90000, 8.0, 0) == []
    (lv, msg), = other_income_crosscheck(f, 90000, None, 1000.0)
    assert lv == "yellow"
    assert "EPS +89.00" in msg and "占前瞻 EPS" not in msg


def test_oi_eps1_zero_warns_without_ratio_not_crash():
    """运行时复现：近盈亏平衡标的（正是 pe_nm 人群）base eps1=round(ni1/
    fwd_shares, 2) 把 |eps1|<0.005 抹成 0.0 —— 相对门槛 rel_gate×0=0 放行，
    材料级差额一路走到占比除法 ZeroDivisionError，在全部 LLM 花费之后炸掉
    整个 engine 子进程。修法=去掉占比子句而非跳过对照。"""
    f = _q(interest_income=1000, interest_expense_nonop=0, other_nonop=0)
    for z in (0.0, -0.0):
        (lv, msg), = other_income_crosscheck(f, 1900, z, 1000.0)   # EPS 差 0.90
        assert lv == "yellow"
        assert "+900M" in msg and "EPS +0.90" in msg
        assert "占前瞻 EPS" not in msg and "占比无意义" in msg
    # 绝对门槛对零 EPS 标的照常生效：0.04 < 0.05 不出旗
    assert other_income_crosscheck(f, 1040, 0.0, 1000.0) == []


def test_oi_eps1_truthy_keeps_ratio_wording():
    """eps1 正常时占比子句必须保留 —— 修复只针对假值分支。"""
    f = _q(interest_income=1000, interest_expense_nonop=0, other_nonop=0)
    (_, msg), = other_income_crosscheck(f, 1900, 1.50, 1000.0)
    assert "占前瞻 EPS 60%" in msg


def test_oi_never_red():
    f = _q(interest_income=4660, interest_expense_nonop=3331, other_nonop=80425)
    for args in ((f, 1000, 8.22, 10950.0), ({}, 1000, 8.0, 1000.0)):
        assert all(lv == "yellow" for lv, _ in other_income_crosscheck(*args))


def test_other_income_note_required():
    """other_income 此前是 need 里唯一不用交推导的事实类字段 —— 也就是唯一
    没有锚的数。AMZN 同日两次运行它从 1500 漂到 1000（-33%），其他假设
    全部靠连续性机制逐字沿用。"""
    for bad in (None, "", "   "):
        d = _mk(other_income_note=bad)
        if bad is None:
            del d["other_income_note"]
        with pytest.raises(ValueError, match=r"other_income_note"):
            _validate_judgment(d, "standard")


def test_other_income_note_accepted():
    _validate_judgment(_mk(other_income_note="10-Q Interest and other, net，剔除 X"),
                       "standard")


# =====================================================================
# DCF 终值护栏 —— 起因见 AMZN：TTM FCF 为负时，四道 DCF 护栏里两道自动失效
#   dcf_equity_over_ttm_fcf  -> 算不出，整条跳过
#   margins 谷底 >= 0.4xTTM   -> fcf_margin > 0.02 不成立，整条跳过
# 而 FCF 为负恰恰是 DCF 最不可靠的时候。
# =====================================================================

AMZN_HIST = [("2016-12-31", .069), ("2017-12-31", .036), ("2018-12-31", .074),
             ("2019-12-31", .077), ("2020-12-31", .067), ("2021-12-31", -.031),
             ("2022-12-31", -.033), ("2023-12-31", .056), ("2024-12-31", .052),
             ("2025-12-31", .011)]


def _scen(bear=.06, base=.09, bull=.12):
    return {n: {"margins": [0.0] * 9 + [m]}
            for n, m in (("bear", bear), ("base", base), ("bull", bull))}


def test_hist_fcf_margins_intersects_three_series():
    f = {"revenue_annual": {"2024-12-31": 100.0, "2025-12-31": 200.0},
         "cfo_annual": {"2024-12-31": 30.0, "2025-12-31": 50.0},
         "capex_annual": {"2024-12-31": 10.0}}          # 2025 缺 capex
    assert hist_fcf_margins(f) == [("2024-12-31", 0.2)]


def test_hist_fcf_margins_empty_when_no_data():
    assert hist_fcf_margins({}) == []
    assert hist_fcf_margins(None) == []


def test_terminal_margin_flags_above_recent_peak():
    """AMZN 实测：base 终值 9% > 近十年峰值 7.7%。"""
    out = terminal_margin_warnings(AMZN_HIST, _scen())
    names = [m for _, m in out]
    assert len(out) == 2                                   # base 与 bull，bear 6% 不响
    assert "base 终值 FCF 利润率 9%" in names[0] and "8%" in names[0]
    assert "bull 终值 FCF 利润率 12%" in names[1] and "1.56 倍" in names[1]
    assert all(lv == "yellow" for lv, _ in out)


def test_terminal_margin_window_excludes_old_regime():
    """全 19 年含 2009 的 11.9%（营收 $24B、AWS 未成型、capex 极轻的另一家公司），
    拿它当 2036 年的锚会让 base 9% 静默通过 —— 必须只看近十年。"""
    old = [("2009-12-31", .119)] + AMZN_HIST
    assert len(terminal_margin_warnings(old, _scen(), window=10)) == 2
    assert terminal_margin_warnings(old, _scen(), window=99) == []   # 用全历史就漏报


def test_terminal_margin_tolerance_kills_rounding_false_positive():
    """AAPL 实测：bear 0.2800 vs 峰值 0.2799 会渲染成 "28% 高于 28%"。
    四舍五入造出的假阳性比漏报更伤信任。"""
    hist = [("20%02d-12-31" % i, .2799) for i in range(10)]
    assert terminal_margin_warnings(hist, _scen(bear=.28, base=.28, bull=.28)) == []
    out = terminal_margin_warnings(hist, _scen(bear=.28, base=.31, bull=.33))
    assert len(out) == 2


def test_terminal_margin_silent_without_history():
    assert terminal_margin_warnings([], _scen()) == []


def test_terminal_sensitivity_fires_above_gate():
    """AMZN 实测：终值占 EV 74%，终值 9%±2pp -> 每股 133/161/189。"""
    m = [0.0] * 9 + [0.09]

    def f(mm):
        # 敏感性必须**只动第 10 年**：整条路径一起缩放，测的就不是"终值那一个
        # 数字值多少钱"了。假 dcf_fn 若只看 mm[-1] 分辨不出这个区别（变异
        # 「只动第10年改成整条缩放」曾因此逃掉），所以在这里把前九年钉死。
        assert list(mm[:-1]) == m[:-1], "前九年不得改动"
        return {0.07: 133.0, 0.09: 161.0, 0.11: 189.0}[round(mm[-1], 2)]

    (lv, msg), = terminal_sensitivity(f, 0.74, m, 161.0)
    assert lv == "yellow"
    assert "74%" in msg and "133 / 161 / 189" in msg and "±17%" in msg


@pytest.mark.parametrize("share", [None, 0.5, 0.65])
def test_terminal_sensitivity_silent_below_gate(share):
    """阈值 65%，等于不触发（严格大于）。不动 tv_pv_share 原有的 75% 红旗线——
    三档 72-74% 不是漏网，是 wacc-tg=6% 下终值倍数 16.7x 的数学必然。"""
    m = [0.0] * 9 + [0.09]
    assert terminal_sensitivity(lambda mm: 100.0, share, m, 161.0) == []


def test_terminal_sensitivity_silent_without_base_ps():
    m = [0.0] * 9 + [0.09]
    assert terminal_sensitivity(lambda mm: 100.0, 0.9, m, None) == []
    assert terminal_sensitivity(lambda mm: None, 0.9, m, 161.0) == []


def test_terminal_sensitivity_lambda_shares_base_matches_dcf_ps():
    """运行时复现：调用点的敏感性 lambda 曾传 cfg["fwd_shares"]，而它包夹的
    base dcf_ps（与其余全部 dcf() 调用）用 cfg["shares"] —— lo/base/hi 三元组
    混两个股本基，+4.9% 增发把 "36/43/50（±16%）" 印成 "34/43/48（±20%）"，
    敏感性被增发/回购幅度污染。
    从生产源码抽出调用点 lambda 本体，在 shares≠fwd_shares 的 cfg 下执行：
    margins 不动时它必须与 cfg["shares"] 基的 base dcf_ps 一致 ——
    谁把 fwd_shares 塞回去谁挂。"""
    calls = [n for n in ast.walk(ast.parse(_SRC))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "terminal_sensitivity"]
    assert len(calls) == 1, "terminal_sensitivity 调用点应唯一"
    lam_src = ast.get_source_segment(_SRC, calls[0].args[0])
    margins = [0.05] * 9 + [0.09]
    ns = dict(
        dcf=dcf, rev0=1000.0,
        _bcfg=dict(g0=0.06, gN=0.03, wacc=0.10, tg=0.025),
        cfg=dict(net_cash=500.0, shares=1000.0, fwd_shares=1049.0),  # +4.9% 增发
    )
    lam = eval(lam_src, ns)      # 调用点换了变量名会 NameError —— 响亮失败，别兜
    base_ps = dcf(1000.0, 0.06, 0.03, margins, 0.10, 0.025, 500.0,
                  ns["cfg"]["shares"])[0]
    assert lam(margins) == pytest.approx(base_ps, rel=1e-12)


# ---- margins 谷底护栏：负 FCF 时退到历史中位，而不是整条跳过 ----

def test_margins_floor_falls_back_to_history_when_fcf_negative():
    """AMZN 形态：TTM FCF 率 -1.5% -> 原写法 `fcf_margin > 0.02` 不成立就整条
    跳过。退到历史中位 5.4% 后，0.4x = 2.2% 的谷底门槛重新生效。"""
    # bear 路径必须仍然逐年 <= base（否则先被情景排序拦下，测不到谷底护栏）
    d = _mk(bear={"margins": [-0.10] + [0.02] * 9})
    _validate_judgment(d, "standard", fcf_margin=-0.015)          # 无历史 -> 放行
    with pytest.raises(ValueError, match=r"0.4×历史年度 FCF 利润率中位"):
        _validate_judgment(d, "standard", fcf_margin=-0.015, hist_fcf_margin=0.054)


def test_margins_floor_prefers_current_over_history():
    """当期 FCF 率可用时以它为准，历史只是备用锚。"""
    d = _mk(bear={"margins": [0.01] + [0.02] * 9})
    with pytest.raises(ValueError, match=r"0.4×当前 TTM FCF 利润率"):
        _validate_judgment(d, "standard", fcf_margin=0.10, hist_fcf_margin=0.02)


# =====================================================================
# ppce 净额口径：amount_musd 是现金流向，net_cash_impact_musd 才是对
# net_cash(现金−负债) 的影响。AMZN 2026-08-31 实测：发债现金 +25,000、债务同增、
# 净现金 0；首版裸求和印出"净 +3,700M"，而真实变化是 -21,300。
# =====================================================================

DEBT = {"date": "2026-07-01", "kind": "发债", "amount_musd": 25000.0, "note": "10-Q"}
BUY = {"date": "2026-07-01", "kind": "并购", "amount_musd": -21300.0, "note": "10-Q"}


def test_ppce_without_impact_field_says_cashflow_not_netcash():
    """缺字段时不装能算：把合计标成现金流向，并点名发债这个反例。"""
    (_, msg), = vintage_warnings(
        _cfg(post_period_capital_events=[DEBT, BUY]), _vt(age=62))
    assert "现金流向合计 +3,700M" in msg
    assert "2/2 笔未给 net_cash_impact_musd" in msg
    assert "不等于净现金影响" in msg
    assert "对净现金影响" not in msg          # 不许冒充净现金


def test_ppce_with_impact_field_reports_real_netcash_delta():
    """AMZN 真实形态：发债净现金 0、并购 -21,300 -> 合计 -21,300，
    与 net_cash 从 -9,236 调到 -30,536 完全吻合。"""
    ev = [dict(DEBT, net_cash_impact_musd=0.0),
          dict(BUY, net_cash_impact_musd=-21300.0)]
    (_, msg), = vintage_warnings(
        _cfg(net_cash=-30536.0, mcap=2900000.0,
             post_period_capital_events=ev), _vt(age=62))
    assert "对净现金影响 -21,300M" in msg
    assert "现金流向合计" not in msg


def test_ppce_shows_both_only_when_they_differ():
    """一致的那条不拖重复数字，避免每行都挂个括号。"""
    ev = [dict(DEBT, net_cash_impact_musd=0.0),
          dict(BUY, net_cash_impact_musd=-21300.0)]
    (_, msg), = vintage_warnings(_cfg(post_period_capital_events=ev), _vt(age=62))
    assert "+25,000M（净现金 +0M）" in msg      # 发债：两者不同 -> 并列
    assert "-21,300M（净现金" not in msg        # 并购：两者相同 -> 不并列


def test_ppce_partial_impact_field_is_not_summed():
    """只给了一半就不能当净现金合计用——半可信比不可信更危险。"""
    ev = [dict(DEBT, net_cash_impact_musd=0.0), BUY]
    (_, msg), = vintage_warnings(_cfg(post_period_capital_events=ev), _vt(age=62))
    assert "1/2 笔未给" in msg and "现金流向合计" in msg


def test_ppce_impact_zero_is_not_treated_as_missing():
    """0 是合法的净现金影响（发债的正确答案），不能被当成"没给"。"""
    ev = [dict(DEBT, net_cash_impact_musd=0.0)]
    (_, msg), = vintage_warnings(_cfg(post_period_capital_events=ev), _vt(age=62))
    assert "对净现金影响 +0M" in msg and "未给" not in msg


def test_ppce_impact_must_be_numeric():
    ev = [dict(DEBT, net_cash_impact_musd="中性")]
    with pytest.raises(ValueError, match=r"net_cash_impact_musd 必须是数字"):
        _validate_judgment(_mk(post_period_capital_events=ev), "standard")


def test_ppce_impact_is_optional():
    """既有 config 没有这个字段，不能因此全挂。"""
    _validate_judgment(_mk(post_period_capital_events=[DEBT, BUY]), "standard")


# =====================================================================
# bool 不是数字（Copilot 在 PR #14 上点出，实际波及 17 处，数处早于本轮）
# Python 里 bool 是 int 的子类 -> isinstance(True, (int, float)) 为真，
# JSON 写 true 会悄悄通过校验、float(True)=1.0、渲染成 "+1M"。
# =====================================================================

@pytest.mark.parametrize("v", [True, False])
def test_isnum_rejects_bool(v):
    assert _isnum(v) is False


@pytest.mark.parametrize("v", [0, 1, -1, 0.0, 1.5, -21300.0])
def test_isnum_accepts_real_numbers(v):
    assert _isnum(v) is True


@pytest.mark.parametrize("v", [None, "1", "true", [], {}])
def test_isnum_rejects_non_numbers(v):
    assert _isnum(v) is False


def test_isnum_docstring_matches_implementation():
    """把文档和实现钉在一起。

    这段 docstring 是全仓唯一解释 bool-is-int 这个坑的地方，而它**已经被悄悄
    改坏过一次**：批量替换 isinstance -> _isnum 的正则扫全文件，把文档里作为
    反例引用的 isinstance 也改了，于是"所以 isinstance(True,...) 为真"变成
    "所以 _isnum(True) 为真"——字面相反，且不影响任何测试（Copilot 在 PR #15
    上点出）。注释被改错不会让代码红，只能靠这种断言。"""
    doc = _isnum.__doc__ or ""
    assert "`_isnum(True)` 为真" not in doc, "文档声称 _isnum(True) 为真，与实现相反"
    assert "_isnum(True)" in doc and "False" in doc, "文档应显式写出 _isnum(True) -> False"
    assert _isnum(True) is False


@pytest.mark.parametrize("field", ["amount_musd", "net_cash_impact_musd"])
def test_ppce_bool_amount_rejected(field):
    """JSON 里的 true 不能当成 1.0M 混进金额。"""
    ev = [dict(EVENT, **{field: True})]
    with pytest.raises(ValueError, match=field):
        _validate_judgment(_mk(post_period_capital_events=ev), "standard")


def test_other_income_bool_rejected():
    with pytest.raises(ValueError, match="other_income 必须是数字"):
        _validate_judgment(_mk(other_income=True), "standard")


def test_fwd_shares_bool_rejected():
    with pytest.raises(ValueError, match="fwd_shares"):
        _validate_judgment(_mk(fwd_shares=True), "standard")


@pytest.mark.parametrize("v", [True, False])
def test_margins_bool_rejected(v):
    """必须用 False 才隔离得出类型检查：True=1 会被区间上限（1 <= m_cap 不成立）
    顺手拦下，而 **False=0 落在合法区间内**，没有类型检查就会变成 0% 的 FCF
    利润率溜进 DCF 路径。变异「margins 不查类型」首轮就是被 True 掩盖而逃掉的。"""
    d = _mk(bear={"margins": [v] + [0.02] * 9})
    with pytest.raises(ValueError, match="margins"):
        _validate_judgment(d, "standard")


def test_ppce_bool_impact_not_summed_as_one():
    """即使绕过校验（旧 config 直接喂引擎），引擎侧也不能把 True 当 +1M。"""
    ev = [dict(EVENT, net_cash_impact_musd=True)]
    (_, msg), = vintage_warnings(_cfg(post_period_capital_events=ev), _vt(age=62))
    assert "现金流向合计" in msg          # True 被视为"没给"，退回现金流向口径
    assert "+1M" not in msg


def test_band_lag_bool_current_not_rendered():
    (_, msg), = band_lag_warnings(
        {"trailing_nolag": dict(_TN, current=True)},
        {"end": "2025-07-31", "lag_days": 400}, 32.3)
    assert "现价 +1" not in msg and "现价 1.0x" not in msg


def test_crosscheck_bool_series_treated_as_missing():
    """facts 里某季被写成 true 时，宁可报"凑不齐四季"也不能算进 TTM。"""
    f = _q(interest_income=1000, interest_expense_nonop=0, other_nonop=0)
    f["interest_income_quarterly"]["2026-06-30"] = True
    (_, msg), = other_income_crosscheck(f, 90000, 8.0, 1000.0)
    assert "无法与财报对照" in msg


def test_hist_fcf_margins_skips_bool_years():
    f = {"revenue_annual": {"2024-12-31": 100.0, "2025-12-31": 200.0},
         "cfo_annual": {"2024-12-31": 30.0, "2025-12-31": True},
         "capex_annual": {"2024-12-31": 10.0, "2025-12-31": 50.0}}
    assert hist_fcf_margins(f) == [("2024-12-31", 0.2)]


def test_terminal_margin_bool_terminal_ignored():
    hist = [("20%02d-12-31" % i, 0.05) for i in range(10)]
    assert terminal_margin_warnings(hist, {"base": {"margins": [0.0] * 9 + [True]}}) == []


# =====================================================================
# v4（2026-09-06）：ppce 泄压阀——reflected_in_net_cash 逐笔确认后降级 info
# =====================================================================

def test_ppce_all_reflected_discharges_to_info():
    """AAPL/MSFT 实测：net_cash_note 已对完账，"请确认已计入"黄旗照样响、
    无法解除。逐笔 reflected_in_net_cash=true 后降级为 info 留痕行。"""
    evs = [dict(EVENT, net_cash_impact_musd=19700.0, reflected_in_net_cash=True)]
    (lv, msg), = vintage_warnings(_cfg(post_period_capital_events=evs), _vt(age=64))
    assert lv == "info"
    assert "已声明并逐笔确认计入" in msg and "2026-08-18 增发 +19,700M" in msg
    assert "请确认" not in msg


def test_ppce_partial_reflected_keeps_yellow():
    """任何一笔有金额的事件缺确认 → 黄旗照旧，不许打折。"""
    evs = [dict(EVENT, net_cash_impact_musd=19700.0, reflected_in_net_cash=True),
           dict(EVENT, kind="回购", amount_musd=-5000.0, net_cash_impact_musd=-5000.0)]
    (lv, msg), = vintage_warnings(_cfg(post_period_capital_events=evs), _vt(age=64))
    assert lv == "yellow" and "请确认" in msg


def test_ppce_string_true_does_not_discharge():
    """引擎只认布尔 True——字符串 "true" 不算（校验层另行拒绝，这里是引擎侧兜底）。"""
    evs = [dict(EVENT, net_cash_impact_musd=19700.0, reflected_in_net_cash="true")]
    (lv, _), = vintage_warnings(_cfg(post_period_capital_events=evs), _vt(age=64))
    assert lv == "yellow"


def test_ppce_pure_contingent_needs_no_confirmation():
    """NVDA 形态：$105B 担保承诺无现金流（amount=0）——不该被"请确认计入"缠住；
    与已确认的有金额事件同列时整体仍可降级。"""
    evs = [{"date": "2026-08-01", "kind": "担保或有", "amount_musd": 0.0,
            "note": "10-Q 承诺与或有：$105B 供应承诺担保"},
           dict(EVENT, net_cash_impact_musd=19700.0, reflected_in_net_cash=True)]
    (lv, msg), = vintage_warnings(_cfg(post_period_capital_events=evs), _vt(age=64))
    assert lv == "info" and "担保或有" in msg


def test_ppce_note_appended_to_info_line():
    evs = [dict(EVENT, net_cash_impact_musd=19700.0, reflected_in_net_cash=True)]
    (_, msg), = vintage_warnings(
        _cfg(post_period_capital_events=evs,
             ppce_note="增发净额已并入 net_cash（见 net_cash_note 第 2 条）"),
        _vt(age=64))
    assert "增发净额已并入" in msg


# ---- 校验层：新字段/新 kind ----

def test_validator_rejects_non_bool_reflected():
    bad = [dict(EV, reflected_in_net_cash="true")]
    with pytest.raises(ValueError, match="reflected_in_net_cash"):
        _validate_judgment(_mk(post_period_capital_events=bad), "standard")


def test_validator_accepts_new_kinds_and_ppce_note():
    """担保或有/股息宣告 是合法 kind（kind 无白名单，pin 住这一点防未来误加）；
    ppce_note 字符串合法。"""
    evs = [{"date": "2026-09-10", "kind": "股息宣告", "amount_musd": 0.0,
            "net_cash_impact_musd": -6800.0, "reflected_in_net_cash": False,
            "note": "8-K：宣告 9/10 季度股息 $6.8B，10 月支付"},
           {"date": "2026-08-01", "kind": "担保或有", "amount_musd": 0.0,
            "note": "10-Q：$105B 供应承诺担保"}]
    _validate_judgment(_mk(post_period_capital_events=evs,
                           ppce_note="股息宣告未付，net_cash 未扣"), "standard")


def test_validator_rejects_non_str_ppce_note():
    with pytest.raises(ValueError, match="ppce_note"):
        _validate_judgment(_mk(ppce_note=123), "standard")


# =====================================================================
# 0009 诊断覆盖面：RKLB 型 pre-FCF 发行人把三道 DCF 护栏逼进静默/自相矛盾区
#   ① 近窗无正 FCF 年 -> terminal_margin_warnings 逐情景 continue = 整条静默
#   ② TV 占比 >75% red 对 TTM FCF<=0 结构必然，econ-review 打回逼判断层扭曲 margins
#   ③ pv_explicit<=0 -> tv_pv_share=None，truthiness 闸门放走最坏形态
#   ④ OCF 兜底措辞「非正」vs 闸门 <=2% 营收，0<FCF<=2% 时自相矛盾
# =====================================================================

def test_terminal_margin_no_positive_fcf_year_speaks():
    """①：RKLB 型近十年年年 FCF 为负——修前 peak<=0 逐情景 continue，零输出。"""
    hist = [(f"20{y}-12-31", -0.10 - y * 0.001) for y in range(16, 26)]
    out = terminal_margin_warnings(hist, _scen())
    assert len(out) == 1 and out[0][0] == "yellow"
    assert "无正 FCF 年份" in out[0][1]
    assert "rationale.dcf_margin" in out[0][1]


def test_terminal_margin_positive_peak_path_unchanged():
    """有正 FCF 年份时行为与措辞不变（AMZN 校准用例仍由上方老用例钉住）。"""
    out = terminal_margin_warnings(AMZN_HIST, _scen())
    assert len(out) == 2 and all("无正 FCF 年份" not in m for _, m in out)


def _dd(**over):
    d = {"yrN_rev_multiple": 2.0, "tv_pv_share": 0.70,
         "pv_explicit": 300, "tv_pv": 700}
    d.update(over)
    return d


def test_tv_share_red_for_fcf_positive_issuer():
    dd = _dd(tv_pv_share=0.80)
    out = dcf_diag_warnings(dd, 1000.0, 100.0, 1000.0, 200.0)   # FCF 10% 营收
    assert ["red", *[m for lv, m in out if lv == "red"]][1].startswith("终值折现占 EV 80%")
    assert dd["dcf_equity_over_ttm_fcf"] == 10.0


def test_tv_share_downgraded_to_yellow_for_pre_fcf():
    """②：TTM FCF<=0 时 >75% 是 wacc−tg 的数学必然，red 只会喂给 econ-review
    去逼判断层扭曲 margins（RKLB 实测级联）——降级 yellow 并说明结构性。"""
    dd = _dd(tv_pv_share=0.80)
    out = dcf_diag_warnings(dd, 1000.0, -50.0, 1000.0, 200.0)
    assert all(lv != "red" for lv, _ in out)
    tv = [m for lv, m in out if "终值折现占 EV 80%" in m]
    assert len(tv) == 1 and "结构性" in tv[0]
    # OCF 兜底黄旗同时在场
    assert any("OCF 锚 = 5.0x" in m for _, m in out)


def test_tv_share_none_escape_now_reported():
    """③：pv_explicit<=0 -> share=None 的最坏形态此前整条免检（RKLB bear）。"""
    dd = _dd(tv_pv_share=None, pv_explicit=-120, tv_pv=900)
    out = dcf_diag_warnings(dd, 1000.0, 100.0, 1000.0, 200.0)
    hits = [(lv, m) for lv, m in out if "显式期 PV 非正" in m]
    assert len(hits) == 1 and hits[0][0] == "red"
    assert "估值全押终值" in hits[0][1]
    # pre-FCF 时同样按 ② 降级
    out2 = dcf_diag_warnings(_dd(tv_pv_share=None, pv_explicit=-120, tv_pv=900),
                             1000.0, -50.0, 1000.0, None)
    hit2 = [(lv, m) for lv, m in out2 if "显式期 PV 非正" in m]
    assert len(hit2) == 1 and hit2[0][0] == "yellow"


def test_tv_share_none_with_nonpositive_tv_stays_silent():
    """tv_pv 也非正 = 整条 DCF <=0，由 dcf_ps<=0 的 red 护栏负责，这里不重复。"""
    dd = _dd(tv_pv_share=None, pv_explicit=-500, tv_pv=-100)
    out = dcf_diag_warnings(dd, 1000.0, 100.0, 1000.0, 200.0)
    assert all("终值" not in m and "显式期" not in m for _, m in out)


def test_ocf_fallback_wording_matches_gate():
    """④：0 < FCF <= 2% 营收踩闸门但不「非正」——措辞必须如实反映闸门并给数。"""
    dd = _dd()
    out = dcf_diag_warnings(dd, 1000.0, 15.0, 1000.0, 200.0)    # 1.5% 营收
    (msg,) = [m for lv, m in out if "护栏未生效" in m]
    assert "非正或占营收 <=2%" in msg and "1.5%" in msg and "15M" in msg
    assert "OCF 锚 = 5.0x" in msg
    assert dd["dcf_equity_over_ttm_ocf"] == 5.0
    # OCF 亦不可用时明说无备用锚
    out2 = dcf_diag_warnings(_dd(), 1000.0, -30.0, 1000.0, None)
    assert any("无备用锚" in m for _, m in out2)


def test_p_fcf_red_path_unchanged():
    dd = _dd()
    out = dcf_diag_warnings(dd, 10000.0, 100.0, 1000.0, 200.0)  # P/FCF=100 界外
    assert any(lv == "red" and "界外 [5,90]" in m for lv, m in out)


def test_ps_reference_thin_band_gives_no_price():
    """④'：thin_coverage 薄带（60 天）没有锚话语权——参考价不出、ddiag 不写。"""
    dd = {}
    txt = ps_reference({"thin_coverage": True, "days": 61, "basis": "ntm",
                        "pctiles": {"50": 2.0}}, 1000.0, 100.0, "bear", dd)
    assert "覆盖不足" in txt and "61" in txt
    assert "ps_ref" not in dd and "≈" not in txt


def test_ps_reference_normal_band_unchanged():
    dd = {}
    psb = {"basis": "ntm",
           "recent": {"pctiles": {"25": 1.0, "50": 2.0, "75": 3.0}}}
    txt = ps_reference(psb, 1000.0, 100.0, "bear", dd)
    assert "P/S 参考（不入综合）" in txt and "2.00x" in txt and "≈ 20.0" in txt
    assert dd["ps_ref"]["px"] == {"25": 10.0, "50": 20.0, "75": 30.0}
    assert dd["ps_ref"]["window"] == "recent"


def test_ps_reference_missing_p50_empty():
    assert ps_reference({}, 1000.0, 100.0, "bear", {}) == ""
    assert ps_reference(None, 1000.0, 100.0, "bear", {}) == ""


# ---- 0014：standard 顶层字段类型墙（此前只查存在，字符串/null 会烧完 LLM 才崩）----

@pytest.mark.parametrize("field,bad", [
    ("net_cash", "约 5,000"), ("net_cash", None), ("net_cash", True),
    ("adj_ni", "100"), ("adj_ni", False),
])
def test_std_numeric_toplevel_type_guard(field, bad):
    """engine 拿 net_cash 进 dcf() 加法、adj_ni 做除法——字符串/bool/null 必须在
    校验层拒绝（拒绝文案带字段名，retry 才修得动），不是在引擎阶段 TypeError。"""
    with pytest.raises(ValueError, match=f"{field} 必须是数字"):
        _validate_judgment(_mk(**{field: bad}), "standard")


@pytest.mark.parametrize("field", ["net_cash_note", "adj_note"])
@pytest.mark.parametrize("bad", [None, "", "  "])
def test_std_note_fields_must_be_nonempty_str(field, bad):
    """build_report 直接切片 adj_note[:40]、渲染 net_cash_note——null/空串提前拒
    （镜像 other_income_note 的既有检查）。"""
    with pytest.raises(ValueError, match=f"{field} 必填"):
        _validate_judgment(_mk(**{field: bad}), "standard")


def test_std_seg1_share_type_guard():
    with pytest.raises(ValueError, match="seg1_share"):
        _validate_judgment(_mk(seg1_share="0.9"), "standard")
    with pytest.raises(ValueError, match="seg1_share"):
        _validate_judgment(_mk(seg1_share=True), "standard")


# =====================================================================
# 连续性基准字段集（0017）—— other_income/seg* 曾缺席 prev_core：
# 8/31 补 other_income_note 的动机正是它的 -33% 跨运行漂移（AMZN），
# 连续性注入却不带这个字段，漂移从连续性通道原样漏回来。
# =====================================================================

def test_prev_core_includes_other_income_and_segments():
    from app.valuation_service import _prev_core
    prev = dict(_mk(), date="2026-09-01", ticker="AMZN", price=200.0,
                semantics_version=4, manifest_latest="2026-06-30")
    core = _prev_core(prev)
    for k in ("other_income", "other_income_note", "seg1", "seg2", "seg1_share",
              "date", "adj_ni", "net_cash", "fwd_shares", "scenarios", "rationale"):
        assert k in core, k
    # 非基准字段（price/ticker/manifest_latest）不进注入——prompt 里已单独注入现价
    assert "price" not in core and "ticker" not in core


def test_prev_core_filters_absent_keys():
    """financials 配置没有 other_income/seg*——null 不许冒充『上次的假设』。"""
    from app.valuation_service import _prev_core
    prev = {"date": "2026-09-01", "adj_ni": 100.0, "fwd_shares": 1000.0,
            "scenarios": {}, "rationale": {}}
    core = _prev_core(prev)
    assert "other_income" not in core and "seg1" not in core
    assert "net_cash" not in core          # fin 无该字段，旧写法会注入 null
    assert None not in core.values()


# =====================================================================
# ADR 比例标定（0018）—— TSLA 实测 adr_multiple=0.8963 原样发货：
# yfinance 市值隐含股数与 XBRL TTM 加权稀释股数差 10.4%，落在 1±8% snap 外、
# 整数 snap（>=2）下，于是美股普通票挂上「1 ADR=0.896 普通股」的假口径，
# shares 被 rebase、带子与每股值口径混掉。真实 ADR 比例只有整数或简单半数。
# =====================================================================

def _adr(raw):
    """构造 price/mcap/shares 使 price÷(mcap÷股数) = raw。"""
    from app.valuation_service import _adr_calibration
    return _adr_calibration(price=1000.0 * raw, mcap=1e12, shares_ord_m=1000.0)


def test_adr_noise_falls_back_to_one_with_mismatch():
    m, mismatch = _adr(0.8963)
    assert m == 1.0
    assert mismatch == pytest.approx(0.1037, abs=1e-4)


def test_adr_half_ratio_snaps():
    assert _adr(0.502) == (0.5, None)
    assert _adr(0.48) == (0.5, None)


def test_adr_integer_snap_preserved():
    assert _adr(3.03) == (3.0, None)
    assert _adr(5.02) == (5.0, None)   # TSM 1:5


def test_adr_near_one_snap_preserved():
    assert _adr(1.05) == (1.0, None)


def test_adr_noise_upper_side():
    m, mismatch = _adr(1.30)
    assert m == 1.0 and mismatch == pytest.approx(0.30)


def test_adr_outside_tightened_range_unchanged():
    # (0, 0.5] 外圈维持原行为：既不 snap 也不回退（收紧范围只有 (0.5, 2)）
    m, mismatch = _adr(0.30)
    assert m == pytest.approx(0.30) and mismatch is None


def test_adr_zero_mcap_defaults_to_one():
    from app.valuation_service import _adr_calibration
    assert _adr_calibration(100.0, 0.0, 1000.0) == (1.0, None)


# =====================================================================
# override 的口径闸（0019）—— TSM 实测：ttm_revenue_override 只换营收基准，
# TTM cfo/capex 还停在旧 XBRL 窗口（FCF 率 19.6% 旧 vs 25.8% 真），margins
# 谷底下限（0.4×当前）与上界（1.2×当前）都锚在过期分母上。偏离 >10% 时
# 校验层改锚历史年度中位（fcf_margin=None 的既有回退路径）。
# =====================================================================

def test_fcfm_gate_large_deviation_returns_none():
    from app.valuation_service import _fcfm_for_validation
    # override 120,000 vs 旧 TTM 100,000 = +20% —— 现金流锚过期，弃用
    assert _fcfm_for_validation(0.196, 120_000.0, 100_000.0) is None


def test_fcfm_gate_small_deviation_passthrough():
    from app.valuation_service import _fcfm_for_validation
    # +8% 属正常季度滚动，当前 TTM 照用
    assert _fcfm_for_validation(0.196, 108_000.0, 100_000.0) == 0.196


def test_fcfm_gate_no_override_passthrough():
    from app.valuation_service import _fcfm_for_validation
    assert _fcfm_for_validation(0.196, None, 100_000.0) == 0.196


def test_fcfm_gate_garbage_override_passthrough():
    """垃圾 override 不在这里拒——_check_rev_override 负责，闸门不许崩。"""
    from app.valuation_service import _fcfm_for_validation
    assert _fcfm_for_validation(0.196, "约 120,000", 100_000.0) == 0.196
    assert _fcfm_for_validation(0.196, True, 100_000.0) == 0.196


def test_fcfm_gate_none_fcfm_stays_none():
    from app.valuation_service import _fcfm_for_validation
    assert _fcfm_for_validation(None, 120_000.0, 100_000.0) is None


def test_fcfm_gate_wired_into_both_call_sites():
    """接线哨兵：首轮校验与经济复审两处都必须过这道闸——只改一处，复审通道
    会拿陈旧锚拒掉合法输出（与 0016 之前的注入/拒收不同源死循环同型）。"""
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent / "app"
           / "valuation_service.py").read_text(encoding="utf-8")
    assert src.count("fcf_margin=_fcfm_for_validation(") == 2


# =====================================================================
# 0022 评审修复批
# =====================================================================

# ---- C2/C12：margins 上界与谷底同步换锚（fcf_margin=None 时退历史中位）----

_HI_MARGINS = dict(bear={"margins": [0.60] * 10}, base={"margins": [0.65] * 10},
                   bull={"margins": [0.70] * 10})


def test_margins_cap_reanchors_to_hist_median_when_gate_fires():
    """高 FCF 率票（真实中位 0.70）在 override >10% 偏差闸下（fcf_margin=None）：
    上界改锚 1.2×历史中位=0.84——「维持现状」[0.70]*10 必须放行。修前上界
    静默塌回 0.65，谷底下限却换了锚，两次 retry 撞同一堵墙后硬失败。"""
    _validate_judgment(_mk(**_HI_MARGINS), "standard",
                       fcf_margin=None, hist_fcf_margin=0.70)


def test_margins_cap_static_when_hist_absent():
    """两个锚都不可用：维持既有静态 0.65（不放宽也不收紧）。"""
    with pytest.raises(ValueError, match="静态 0.65"):
        _validate_judgment(_mk(**_HI_MARGINS), "standard",
                           fcf_margin=None, hist_fcf_margin=None)


def test_margins_cap_rejection_names_hist_anchor():
    """拒绝文案点名上界锚（C12）：自愿 override 的换锚在 prompt 期不可知，
    retry 只能从拒绝文案得知本次上界是怎么来的。"""
    d = _mk(bear={"margins": [0.60] * 10}, base={"margins": [0.65] * 10},
            bull={"margins": [0.90] * 10})   # > 1.2×0.70=0.84
    with pytest.raises(ValueError, match="上界锚=历史年度 FCF 利润率中位"):
        _validate_judgment(d, "standard", fcf_margin=None, hist_fcf_margin=0.70)


def test_margins_cap_negative_fcfm_stays_static():
    """烧钱标的（TTM FCF<0）不换锚：prompt 契约明写「<=0 固定 0.65」——
    换锚只对 None（口径闸/缺 facts）生效。"""
    with pytest.raises(ValueError, match="静态 0.65"):
        _validate_judgment(_mk(**_HI_MARGINS), "standard",
                           fcf_margin=-0.48, hist_fcf_margin=0.70)


def test_margins_cap_positive_fcfm_unchanged():
    """TTM 锚可用时行为不变：1.2×0.85 钳到 0.9。"""
    hi = dict(bear={"margins": [0.35] * 10}, base={"margins": [0.40] * 10},
              bull={"margins": [0.5] * 9 + [0.9]})
    _validate_judgment(_mk(**hi), "standard", fcf_margin=0.85)


# ---- C3：亏损协议强制 0 的 m1/m2 不进反双重计数 ----

def test_split_shape_forced_zero_m_not_double_count():
    """0013 的 split 形态（营业亏损+大额利息收入→税前为正）：m1/m2 被亏损协议
    钉死为 0、pe 照常规边界给。情景盈利收缩到 50% 时，反双重计数不得再命令
    「请上调 bear.m1」——上调立刻被「营业利润为负，m1/m2 必须写 0」拒绝，
    诚实配置无解、两次 retry 烧光后硬失败（0013 自己的契约测试恰好用了
    other_income=1000 让收缩停在 20% 以内，漏掉了这个死锁）。"""
    d = _mk(other_income=200.0,
            bear={"opm": -0.05, "pe": 8, "m1": 0, "m2": 0})
    # bear eps=(950×−0.05+200)×0.9=137.25 vs base=(1050×0.10+200)×0.9=274.5 → 50%
    _validate_judgment(d, "standard", rev0=1000.0)


def test_bull_side_skips_base_forced_zero_m1():
    """base 被强制 m1=0 时，bull 的正 m1 > 1.4×0 恒真——比例检查对被协议钉死
    的基准没有意义，跳过；pe 未被强制，照常执法。"""
    d = _mk(other_income=300.0,
            bear={"opm": -0.10, "pe": 8, "m1": 0, "m2": 0},
            base={"opm": -0.05, "pe": 9, "m1": 0, "m2": 0},
            bull={"pe": 12, "m1": 15})
    _validate_judgment(d, "standard", rev0=1000.0)


def test_true_multiple_collapse_still_rejected():
    """守恒检查：bear 营业利润为正（m1 不被协议强制）时，盈利收缩叠加倍数塌方
    照旧是双重计数。"""
    with pytest.raises(ValueError, match="bear 双重计数"):
        _validate_judgment(_mk(bear={"m1": 5}), "standard", rev0=1000.0)


# ---- C5：纯或有 ppce 的 info 行不得谎称「已确认计入」----

_CONTINGENT = {"date": "2026-08-15", "kind": "担保或有", "amount_musd": 0.0,
               "note": "10-Q 承诺与或有"}


def test_ppce_contingent_only_no_false_confirmation():
    """material 全空时 all([]) 空真——旧 info 行谎称「已声明并逐笔确认计入
    net_cash」，而事件根本没有确认字段、也无现金可对账。"""
    (lv, msg), = vintage_warnings(
        _cfg(post_period_capital_events=[_CONTINGENT]), _vt(age=64))
    assert lv == "info"
    assert "无现金可对账" in msg and "担保或有" in msg
    assert "已声明并逐笔确认计入" not in msg


def test_ppce_contingent_only_explicit_false_still_honest():
    """更强形态：显式 reflected_in_net_cash=false 也曾被空真说成「已确认」。"""
    ev = dict(_CONTINGENT, reflected_in_net_cash=False)
    (lv, msg), = vintage_warnings(_cfg(post_period_capital_events=[ev]), _vt(age=64))
    assert lv == "info" and "逐笔确认" not in msg


def test_ppce_contingent_only_note_appended():
    (_, msg), = vintage_warnings(
        _cfg(post_period_capital_events=[_CONTINGENT],
             ppce_note="担保上限 $105B，无现金流"), _vt(age=64))
    assert "担保上限" in msg


def test_ppce_mixed_contingent_and_confirmed_unchanged():
    """混合清单（或有 + 已确认的有金额事件）仍走既有 info 措辞——C5 只改纯或有。"""
    evs = [_CONTINGENT,
           dict(EVENT, net_cash_impact_musd=19700.0, reflected_in_net_cash=True)]
    (lv, msg), = vintage_warnings(_cfg(post_period_capital_events=evs), _vt(age=64))
    assert lv == "info" and "已声明并逐笔确认计入" in msg


# ---- C6：fin（无 net_cash 的 cfg）的补救指令不得指向 fin schema 没有的字段 ----

def _fin_cfg_min(fwd_shares):
    # fin cfg 没有 net_cash 键——vintage_warnings 以此判定 bs_name=TBV
    return {"shares": 1000.0, "fwd_shares": fwd_shares, "mcap": 10000.0}


def test_fin_mismatch_tail_points_to_notes_and_tbv():
    """时点不一致条（增发形态）：fin 的补救不指 net_cash、不叫填 fin schema 里
    不存在的 post_period_capital_events——改指 notes 与 TBV 口径。"""
    (_, msg), = vintage_warnings(_fin_cfg_min(1050.0), _vt(age=60))
    assert "时点不一致" in msg and "未计入 TBV" in msg
    assert "net_cash" not in msg
    assert "notes 里" in msg and "TBV 是否已反映最终值" in msg


def test_std_mismatch_tail_verbatim_unchanged():
    (_, msg), = vintage_warnings(_cfg(fwd_shares=1050.0), _vt(age=60))
    assert msg.endswith("请在 post_period_capital_events 里声明并让 net_cash 反映最终值")


def test_fin_age_tail_does_not_ask_to_fill_ppce():
    """纯时效条（股数差 0.5% 不触发机械检查）：fin 不叫「填写」ppce，改指 notes。"""
    (_, msg), = vintage_warnings(_fin_cfg_min(1005.0), _vt(age=60))
    assert "报告期末已 60 天" in msg
    assert "net_cash" not in msg and "notes 里说明" in msg


def test_std_age_tail_verbatim_unchanged():
    (_, msg), = vintage_warnings(_cfg(), _vt(age=60))
    assert "期后资本事件未声明，请核对增发/回购/并购/分拆/分红后填写" in msg
