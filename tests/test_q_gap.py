"""滚动四季窗的季度间隔（2026-11-27：COST 第四季 16 周，期末间隔 112 天）。"""
from datetime import date, timedelta

from tests.test_pe_band_guards import _hist, _inputs
from valuation import pe_band as pb
from valuation.pe_band import _consecutive, _valid_windows, build_ttm_eps


def _d(*xs):
    return [date.fromisoformat(x) for x in xs]


def test_costco_16_week_q4_is_consecutive():
    # FY2026: Q1 11-23 / Q2 02-15 / Q3 05-10 / Q4 08-30（12/12/12/16 周）
    assert _consecutive(_d("2025-11-23", "2026-02-15", "2026-05-10", "2026-08-30"))


def test_missing_quarter_still_breaks_window():
    # 缺一季 = 间隔 ~180 天，照旧不连续
    assert not _consecutive(_d("2025-11-23", "2026-05-10", "2026-08-30", "2026-11-22"))
    assert not _consecutive(_d("2026-01-01", "2026-02-15"))     # 太近（< 80 天）


def _q(ends, val=1.0, filed="2026-10-08"):
    return {e: {"val": val, "filed": filed, "first_filed": filed} for e in ends}


def test_ttm_reaches_fiscal_year_end_for_16_week_q4():
    ends = ["2024-11-24", "2025-02-16", "2025-05-11", "2025-08-31",
            "2025-11-23", "2026-02-15", "2026-05-10", "2026-08-30"]
    ni = _q(ends)
    ni["2025-08-31"]["val"] = ni["2026-08-30"]["val"] = 16 / 12   # 16 周季度利润多三成
    sh = _q(ends, val=444e6)
    assert [w[-1][0] for w in _valid_windows(ni)][-1] == "2026-08-30"
    pts = build_ttm_eps(ni, sh)
    assert pts and max(p["period_end"] for p in pts) == "2026-08-30"


# ---------- #45 评审：从原始 companyfacts 行走 compute_band 真实路径 ----------
# 上面几条直接喂构造好的 ni_q。评审担心 pick() 的季度时长过滤（80-100 天）会在
# _consecutive 之前就把 112 天的第四季丢掉——COST 没有第四季 10-Q，第四季是
# derive_q4 用「年报 − 前三季」补的，不经那道过滤。这条从原始行走一遍钉住。

def _cost_facts(n_fy=7):
    """COST 型财历的原始 facts：10-Q 只有前三季（各 12 周），10-K 给 52 周全年，
    没有任何第四季（16 周）的行。最近一个 10-K 已申报、下一季 10-Q 还没出。"""
    last_fy = date.today() - timedelta(days=100)
    ni, sh, fy_ends, q3_ends = [], [], [], []
    for k in reversed(range(n_fy)):
        fy_end = last_fy - timedelta(days=364 * k)
        prev = fy_end - timedelta(days=364)
        q_ends = [prev + timedelta(days=84 * j) for j in (1, 2, 3)]
        for s, e in zip([prev] + q_ends[:2], q_ends):
            row = {"start": (s + timedelta(days=1)).isoformat(), "end": e.isoformat(),
                   "filed": (e + timedelta(days=35)).isoformat(), "form": "10-Q"}
            ni.append(dict(row, val=100.0))
            sh.append(dict(row, val=100.0))
        row = {"start": (prev + timedelta(days=1)).isoformat(), "end": fy_end.isoformat(),
               "filed": (fy_end + timedelta(days=40)).isoformat(), "form": "10-K",
               "fp": "FY"}
        ni.append(dict(row, val=300.0 + 100.0 * 16 / 12))   # 第四季 16 周，利润多三成
        sh.append(dict(row, val=100.0))
        fy_ends.append(fy_end)
        q3_ends.append(q_ends[-1])
    facts = {"NetIncomeLoss": {"units": {"USD": ni}},
             "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": sh}}}
    return facts, fy_ends, q3_ends


def test_compute_band_ttm_reaches_fy_end_from_raw_facts(monkeypatch):
    facts, fy_ends, q3_ends = _cost_facts()
    assert (fy_ends[-1] - q3_ends[-1]).days == 112

    def _last_ttm_period():
        band = pb.compute_band("COSTX", "x@example.com", years=5, basis="trailing",
                               include_series=True, inputs=_inputs(facts, _hist()))
        return band["series"][-1]["ttm_period"]

    assert _last_ttm_period() == fy_ends[-1].isoformat()
    # 对照：同一份原始数据在旧上限 100 天下 TTM 停在第三季（修前 COST 的症状）
    monkeypatch.setattr(pb, "Q_GAP_MAX", 100)
    assert _last_ttm_period() == q3_ends[-1].isoformat()
