# -*- coding: utf-8 -*-
"""营业线口径 NTM 带（0031）：分母 = 营业利润 ×(1−21%) ÷ 稀释股数。参考读数，不是锚。

动机：GAAP 净利分母被营业线以下的一次性项目打断时，畸变过滤器整窗剔除——IBM
2022/2024 两次养老金结算 + 2025Q4 税务结案让 5 年窗口只剩 488 天、滞后 576 天，
2025 年的重定价整段不在分布里。实测换分母后 IBM 576→336 天、AMZN 419→328、
GOOGL 427→329；MSFT/META 本就在结构下限（~330），不变。
"""
import ast
from datetime import date, timedelta
from pathlib import Path

import pytest

from valuation import pe_band as pb
from valuation.pe_band import compute_band, op_income_rows

ROOT = Path(__file__).resolve().parent.parent

# ---- engine.py 纯函数逐字抽取（模块级脚本，import 即执行）----
_SRC = (ROOT / "valuation" / "engine.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)
_SEGS = [ast.get_source_segment(_SRC, n) for n in _TREE.body
         if isinstance(n, ast.FunctionDef)
         and n.name in ("_isnum", "_pctile_rank", "op_band_reading")]
assert len(_SEGS) == 3
_ENG_TAX = next(n.value.value for n in _TREE.body
                if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "OP_TAX")


def _reader(tax):
    ns = {"OP_TAX": tax}
    exec("\n\n".join(_SEGS), ns)
    return ns["op_band_reading"]


op_band_reading = _reader(_ENG_TAX)


# ---------------------------------------------------------------- 常量/标签一致性

def test_tax_pinned_across_modules():
    """同一常数用于带子分母与引擎前瞻分母时才在价格里约掉——两处必须一致。"""
    assert pb.OP_TAX == _ENG_TAX == 0.21


def test_component_tags_match_fetch_facts():
    """带子的营业利润与引擎 op1 必须是同一个营业利润（同一推导、同一候选标签）。"""
    from valuation.fetch_facts import SPEC
    assert pb.OP_TAGS == SPEC["op_income"]["tags"]
    assert pb.COGS_TAGS == SPEC["cogs"]["tags"]
    assert pb.RND_TAGS == SPEC["rnd"]["tags"]
    assert pb.SGA_TAGS == SPEC["sga"]["tags"]


# ---------------------------------------------------------------- op_income_rows

def _qs(e):
    m, y = e.month - 2, e.year
    if m <= 0:
        m, y = m + 12, y - 1
    return date(y, m, 1)


def _flow(q_ends, vals, q4_discrete=True, annual=True):
    """companyfacts 形状的一条流量标签。q4_discrete=False 时不报 Q4 单季（IBM 型：
    10-Q 只有 Q1-Q3，Q4 要靠 FY−前三季推）。"""
    rows = []
    for e, v in zip(q_ends, vals):
        if not q4_discrete and e.month == 12:
            continue
        rows.append({"start": _qs(e).isoformat(), "end": e.isoformat(), "val": v,
                     "filed": (e + timedelta(days=40)).isoformat()})
    if annual:
        by = {}
        for e, v in zip(q_ends, vals):
            by.setdefault(e.year, []).append(v)
        for y, vs in by.items():
            if len(vs) == 4:
                rows.append({"start": f"{y}-01-01", "end": f"{y}-12-31", "val": sum(vs),
                             "filed": f"{y + 1}-02-24", "fp": "FY"})
    return {"units": {"USD": rows}}


Q8 = [date(y, m, d) for y in (2024, 2025) for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))]


def _ibm_like(q4_discrete=False, op=None):
    f = {"Revenues": _flow(Q8, [1000.0] * 8, q4_discrete),
         "CostOfRevenue": _flow(Q8, [400.0] * 8, q4_discrete),
         "ResearchAndDevelopmentExpense": _flow(Q8, [100.0] * 8, q4_discrete),
         "SellingGeneralAndAdministrativeExpense": _flow(Q8, [200.0] * 8, q4_discrete)}
    if op is not None:
        f["OperatingIncomeLoss"] = op
    return f


def test_op_rows_never_reported_derives_every_period_incl_q4():
    a, q = op_income_rows(_ibm_like())
    # 300/季 × (1−21%)；Q4 由 FY−前三季推出（IBM 10-Q 不报 Q4 单季）
    assert set(q) == {e.isoformat() for e in Q8}
    assert all(v["val"] == pytest.approx(300 * 0.79) for v in q.values())
    assert a["2025-12-31"]["val"] == pytest.approx(1200 * 0.79)


def test_op_rows_known_date_is_latest_component():
    f = _ibm_like(q4_discrete=True)
    # SG&A 那季晚报：推导值要等最后一个组件公布才算得出
    for r in f["SellingGeneralAndAdministrativeExpense"]["units"]["USD"]:
        if r["end"] == "2025-06-30":
            r["filed"] = "2025-09-30"
    _, q = op_income_rows(f)
    assert q["2025-06-30"]["first_filed"] == "2025-09-30"
    assert q["2025-03-31"]["first_filed"] == "2025-05-10"


def test_op_rows_reported_then_stopped_fills_only_after():
    """申报值优先且不覆盖；只补最后申报期之后（COHR 停报型）——中间空洞不填。"""
    # 申报 2024Q1、Q3（Q2 缺——申报序列中间的空洞），2024Q3 之后停报
    reported = [date(2024, 3, 31), date(2024, 9, 30)]
    op = _flow(reported, [250.0] * len(reported), annual=False)
    _, q = op_income_rows(_ibm_like(q4_discrete=True, op=op))
    assert q["2024-09-30"]["val"] == pytest.approx(250 * 0.79)    # 申报值（也按展示税率）
    assert q["2025-06-30"]["val"] == pytest.approx(300 * 0.79)    # 停报后推导
    assert "2024-06-30" not in q      # 空洞不填：公式外的营业项会让推导值与申报值错位


# ---------------------------------------------------------------- compute_band 端到端

class _Hist:
    """yfinance DataFrame 的最小替身（iterrows + empty）。"""
    def __init__(self, rows):
        self._rows = rows
        self.empty = not rows

    def iterrows(self):
        import datetime as _dt
        for d, c in self._rows:
            yield _dt.datetime(d.year, d.month, d.day), {"Close": c}


def _hist(years=6, close=100.0):
    t = date.today()
    d, rows = t - timedelta(days=round(365.25 * years)), []
    while d <= t:
        if d.weekday() < 5:
            rows.append((d, close))
        d += timedelta(days=1)
    return _Hist(rows)


def _last_qend():
    t = date.today()
    c = [date(y, m, dd) for y in (t.year - 1, t.year)
         for m, dd in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return max(d for d in c if (t - d).days >= 75)


def _quarter_ends(n):
    d, out = _last_qend(), []
    while len(out) < n:
        out.append(d)
        m, y = d.month - 3, d.year
        if m <= 0:
            m, y = m + 12, y - 1
        d = date(y, m, {3: 31, 6: 30, 9: 30, 12: 31}[m])
    return list(reversed(out))


def _full_facts(one_off_idx):
    """32 季：营业利润 300/季恒定；净利 200/季，但 one_off_idx 那季有一笔营业线以下
    的一次性收益（税务结案型）把净利打到 900。"""
    q = _quarter_ends(32)
    ni = [200.0] * 32
    ni[one_off_idx] = 900.0

    def flow(vals):
        # 带子要求有财年行（新上市守卫）；Q4 单季也报，年度行只作财年边界
        return _flow(q, vals, q4_discrete=True, annual=True)
    return {"NetIncomeLoss": flow(ni),
            "Revenues": flow([1000.0] * 32), "CostOfRevenue": flow([400.0] * 32),
            "ResearchAndDevelopmentExpense": flow([100.0] * 32),
            "SellingGeneralAndAdministrativeExpense": flow([200.0] * 32),
            "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
                {"start": _qs(e).isoformat(), "end": e.isoformat(), "val": 100.0,
                 "filed": (e + timedelta(days=40)).isoformat()} for e in q]}}}, q


def _inputs(facts):
    return {"cik": 1, "facts": facts, "dei": {}, "hist": _hist(), "splits": {},
            "years": 6}


def test_below_the_line_one_off_culls_gaap_band_but_not_op_band():
    """IBM 型：一次性收益在营业线以下。GAAP 带整段剔除含它的 NTM 窗口、滞后变长；
    营业线带不受影响，覆盖更近的日子。"""
    # 倒数第二季（IBM 2025Q4 型）：含它的 TTM 窗口正是最近那几个 NTM 分母，
    # GAAP 带的末端因此被推回去——落在更早的季度只会在分布中间挖洞、末端不动
    facts, q = _full_facts(one_off_idx=30)
    eps = compute_band("IBMX", "x@e.com", years=5, basis="ntm", inputs=_inputs(facts))
    op = compute_band("IBMX", "x@e.com", years=5, basis="ntm", metric="opeps",
                      inputs=_inputs(facts))
    assert eps["anom_days"]["ntm"] > 0
    assert op["anom_days"] == {"trailing": 0, "ntm": 0}
    assert op["span"]["end"] > eps["span"]["end"]    # 滞后更短
    assert op["days"] > eps["days"]
    assert op["metric"] == "opeps"
    # 价 100 ÷ (300×4×0.79/100) = 10.55，恒定
    assert op["median"] == pytest.approx(100 / (1200 * 0.79 / 100), rel=1e-6)
    assert op["trailing_nolag"] is not None          # 前缀 peop_ 的对照块同样生成


def test_in_operating_line_one_off_still_culled_in_op_band():
    """营业线**以内**的一次性（重组/和解/减值）照旧由畸变过滤器剔除——换分母只免疫
    营业线以下那一类，不是关掉过滤器。"""
    facts, q = _full_facts(one_off_idx=26)
    sga = facts["SellingGeneralAndAdministrativeExpense"]["units"]["USD"]
    sga[20]["val"] = 1100.0                          # 那一季营业利润 300 → −600
    op = compute_band("RESTR", "x@e.com", years=5, basis="trailing", metric="opeps",
                      inputs=_inputs(facts))
    assert op["anom_days"]["trailing"] > 0


# ---------------------------------------------------------------- engine.op_band_reading

def _band(pp, recent=True, lag=336, thin=False):
    b = {"basis": "ntm", "years": 5, "days": 745, "thin_coverage": thin,
         "pctiles": {"10": 18.3, "25": 21.0, "50": 24.0, "75": 27.0, "90": 29.7},
         "span": {"start": "2021-09-23", "end": "2025-10-22", "lag_days": lag}}
    if recent:
        b["recent"] = {"years": 3, "days": 443, "pctiles": dict(pp),
                       "span": {"start": "2023-09-25", "end": "2025-10-22",
                                "lag_days": lag}}
    return b


IBM_RECENT = {"10": 23.36, "25": 25.18, "50": 26.67, "75": 28.15, "90": 30.17}
# IBM base（bundle config）：op1 11,971M、fwd_shares 956M、eps1 9.51、价 231.38
IBM_ARGS = dict(op1=11971, fwd_shares=956, eps1=9.51, price=231.38, gaap_lag=576)


def test_reading_ibm_numbers():
    r = op_band_reading(_band(IBM_RECENT), **IBM_ARGS)
    assert r["fwd_opeps"] == pytest.approx(11971 * 0.79 / 956, abs=0.01)
    assert r["px"]["50"] == pytest.approx(26.67 * 11971 * 0.79 / 956, abs=0.1)   # 263.8
    assert r["gaap_pe_equiv"]["50"] == pytest.approx(r["px"]["50"] / 9.51, abs=0.05)
    assert r["now_pe"] == 23.4 and r["now_vs_band"] == "in_band"
    assert r["window"] == "近3年" and r["days"] == 443
    assert r["lag_gain_days"] == 576 - 336


def test_display_tax_cancels_out_of_prices():
    """21% 只是展示常数：换成 0% 且带子倍数同比缩放时，每股价格与等价 GAAP PE 逐字不变。"""
    r21 = op_band_reading(_band(IBM_RECENT), **IBM_ARGS)
    scaled = {k: v * (1 - 0.21) for k, v in IBM_RECENT.items()}
    r0 = _reader(0.0)(_band(scaled), **IBM_ARGS)
    for q in ("10", "25", "50", "75", "90"):
        assert r0["px"][q] == pytest.approx(r21["px"][q], abs=0.2)
        assert r0["gaap_pe_equiv"][q] == pytest.approx(r21["gaap_pe_equiv"][q], abs=0.05)


def test_reading_falls_back_to_full_window():
    r = op_band_reading(_band(IBM_RECENT, recent=False), **IBM_ARGS)
    assert r["window"] == "近5年" and r["pe"]["50"] == 24.0


@pytest.mark.parametrize("price,rel", [(150.0, "below_p10"), (400.0, "above_p90")])
def test_reading_out_of_band_gives_relation_not_rank(price, rel):
    r = op_band_reading(_band(IBM_RECENT), **dict(IBM_ARGS, price=price))
    assert r["now_vs_band"] == rel and r["now_pctile"] is None


def test_reading_no_gaap_equiv_without_positive_eps():
    r = op_band_reading(_band(IBM_RECENT), **dict(IBM_ARGS, eps1=-0.5))
    assert r["gaap_pe_equiv"] is None and r["px"]["50"] > 0


@pytest.mark.parametrize("band,over", [
    (None, {}), (_band(IBM_RECENT, thin=True), {}),
    ({"pctiles": {"50": 20.0}}, {}),                      # 分位不全
    (_band(IBM_RECENT), {"op1": 0}), (_band(IBM_RECENT), {"fwd_shares": 0}),
])
def test_reading_none_cases(band, over):
    assert op_band_reading(band, **dict(IBM_ARGS, **over)) is None


def test_engine_wiring():
    assert "_opr = op_band_reading(facts.get(\"op_band\")" in _SRC
    assert "_trw[\"op_band\"] = _opr" in _SRC


def test_fetch_facts_builds_op_band():
    src = (ROOT / "valuation" / "fetch_facts.py").read_text(encoding="utf-8")
    assert "metric=\"opeps\"" in src and "out[\"op_band\"] = opb" in src


def test_report_renders_op_band_row():
    src = (ROOT / "valuation" / "build_report.py").read_text(encoding="utf-8")
    assert "_tr.get(\"op_band\")" in src and "营业线口径带（参考，不是锚）" in src


# ---------------------------------------------------------------- prompt

def test_prompt_op_band_line():
    from app.valuation_service import _band_meta, _op_band_meta
    txt = _op_band_meta(_band(IBM_RECENT), 576)
    assert "参考（不是锚）" in txt and "P50 26.7x" in txt
    assert "少滞后 240 天" in txt and "不能**与 GAAP PE 直接比" in txt
    assert _op_band_meta(None, 576) == ""
    assert _op_band_meta(_band(IBM_RECENT, thin=True), 576) == ""
    gaap = {"basis": "ntm", "years": 5, "days": 488,
            "pctiles": {str(p): 20.0 for p in (10, 25, 50, 75, 90)},
            "span": {"start": "2021-09-23", "end": "2025-02-24", "lag_days": 576}}
    full = _band_meta("standard", {"pe_band": gaap, "op_band": _band(IBM_RECENT)})
    assert "营业线口径 NTM 带" in full
    assert "营业线口径 NTM 带" not in _band_meta("standard", {"pe_band": gaap})
