"""滚动四季窗的季度间隔（2026-11-27：COST 第四季 16 周，期末间隔 112 天）。"""
from datetime import date

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
