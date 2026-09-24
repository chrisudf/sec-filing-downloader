# -*- coding: utf-8 -*-
"""watchlist 批量 PE 分位表（valuation/pe_rank.py）的纯函数：分位/陈旧判定、前瞻 PE、渲染。

联网部分（load_inputs / fetch_consensus / yfinance info）不在这里测——它们都是
既有模块的函数，本模块只负责拼装与口径。
"""
from datetime import date, timedelta

from valuation import pe_rank as pr

KEY = "pe_trailing"
TODAY = date.today()


def _series(vals, end=TODAY):
    """逐日序列（末值=最新），日期倒推，间隔 1 天。"""
    n = len(vals)
    return [{"date": (end - timedelta(days=n - 1 - i)).isoformat(), KEY: v}
            for i, v in enumerate(vals)]


def _band(vals, end=TODAY, thin=False, with_series=False, recent=None):
    s = _series(vals, end)
    b = {"current": dict(s[-1], ttm_period="2026-06-30"), "_sorted": sorted(vals),
         "thin_coverage": thin, "days": len(vals), "recent": recent}
    if with_series:
        b["series"] = s
    return b


def test_rank_and_fresh():
    vals = list(range(10, 310))            # 300 天，末值 309 = 最高
    vals[-1] = 12                          # 当前压到很低
    b10 = _band(vals)
    b5 = _band(vals, with_series=True, recent={"pctiles": {10: 1, 50: 2, 90: 3}})
    m = pr.summarize(b10, b5, TODAY, KEY)
    assert m["fresh"] and m["pe"] == 12
    # 严格低于 12 的只有 10、11 两天
    assert round(m["r10"], 2) == round(100 * 2 / 300, 2)
    assert m["r5"] == m["r10"]
    assert m["p3"] == {10: 1, 50: 2, 90: 3}


def test_stale_current_is_flagged():
    b10 = _band(list(range(300)), end=TODAY - timedelta(days=40))
    m = pr.summarize(b10, None, TODAY, KEY)
    assert not m["fresh"]
    assert m["r5"] is None and m["r3"] is None


def test_thin_coverage_withholds_rank():
    m = pr.summarize(_band([20.0] * 100, thin=True), None, TODAY, KEY)
    assert m["r10"] is None and m["thin"]
    # 10y 够厚、5y 薄（近 5 年才转盈的型）：只扣 5y，不连坐 10y
    vals = [float(v) for v in range(300)]
    m = pr.summarize(_band(vals), _band(vals, thin=True, with_series=True), TODAY, KEY)
    assert m["r10"] is not None and m["r5"] is None


def test_three_year_rank_uses_only_recent_days():
    # 前 800 天 PE=500（都在 3 年前，全高于当前），近 3 年 300 天 PE 从 1 涨到 300
    old = [{"date": (TODAY - timedelta(days=2000 - i)).isoformat(), KEY: 500.0}
           for i in range(800)]
    new = _series([float(v) for v in range(1, 301)])
    series = old + new
    b5 = {"current": new[-1], "_sorted": sorted(s[KEY] for s in series),
          "thin_coverage": False, "days": len(series), "series": series, "recent": None}
    b10 = dict(b5)
    m = pr.summarize(b10, b5, TODAY, KEY)
    assert round(m["r3"], 2) == round(100 * 299 / 300, 2)   # 近 3 年里最高
    assert round(m["r10"], 2) == round(100 * 299 / 1100, 2)  # 全窗里被 500 那段压低


def test_three_year_rank_needs_min_days():
    b5 = _band([float(v) for v in range(100)], with_series=True)
    m = pr.summarize(b5, b5, TODAY, KEY)
    assert m["r3"] is None


def test_forward_pe_and_range_direction():
    cons = {"fy1": {"avg": 9.31, "low": 9.01, "high": 10.1, "n": 51},
            "fy2": {"avg": 15.68, "low": 9.80, "high": 18.75, "n": 53}}
    fw = pr.forward(225.51, cons)
    assert round(fw["fy1_pe"], 1) == 24.2
    assert round(fw["fy2_pe"], 1) == 14.4
    # 最乐观预期 -> 最低 PE
    assert fw["fy2_pe_lo"] < fw["fy2_pe"] < fw["fy2_pe_hi"]
    assert not fw["fy2_low_nonpos"]


def test_forward_loss_and_nan():
    nan = float("nan")
    fw = pr.forward(70.0, {"fy1": {"avg": -0.4, "low": -0.6, "high": -0.2, "n": 12},
                           "fy2": {"avg": 0.05, "low": -0.3, "high": 0.4, "n": 12}})
    assert fw["fy1_pe"] is None
    assert fw["fy2_pe_hi"] is None and fw["fy2_low_nonpos"]
    assert "含亏损" in pr._fwd_cells(fw)[2]
    fw = pr.forward(70.0, {"fy2": {"avg": nan, "low": nan, "high": nan, "n": 0}})
    assert fw["fy2_pe"] is None and not fw["fy2_low_nonpos"]
    assert pr._fwd_cells(fw)[2] == "—–—"
    # 分母趋零：RKLB 实测 70.31 ÷ 0.02 = 3515x
    fw = pr.forward(70.31, {"fy2": {"avg": 0.02, "low": -0.3, "high": 0.02, "n": 9}})
    assert pr._fwd_cells(fw)[1:] == [">999x", ">999–含亏损"]


def _row(**over):
    m = {"pe": 28.5, "date": TODAY.isoformat(), "fresh": True, "ttm_period": "2026-07-26",
         "days10": 2000, "thin": False, "r10": 7.0, "r5": 2.0, "r3": 2.0,
         "p3": {10: 33, 50: 51, 90: 73}}
    r = {"ticker": "NVDA", "close": 225.51, "px_date": TODAY, "gaap": dict(m),
         "op": dict(m), "fwd": pr.forward(225.51, {"fy2": {"avg": 15.68, "low": 9.8,
                                                           "high": 18.75, "n": 53}}),
         "fy_labels": ("FY2027(至2027-01)", "FY2028(至2028-01)"),
         "yh": {"tpe": 28.47, "peg": 0.49}, "notes": []}
    r.update(over)
    return r


def test_render_columns_align():
    stale = dict(_row()["gaap"], fresh=False, date="2026-07-30")
    rows = [_row(), _row(ticker="AMZN", gaap=stale),
            {"ticker": "QQQ", "skip": "index：SEC 无 EPS"},
            _row(ticker="RKLB", gaap={"err": "样本不足"}, op={"err": "样本不足"})]
    text, notes = pr.render(rows, "2026-09-24")
    table = [ln for ln in text.splitlines() if ln.startswith("| ")]
    assert len(table) == 1 + len(rows)
    assert all(ln.count("|") == len(pr.HEADER) + 1 for ln in table)
    assert "28.5x†07-30" in text
    assert any("AMZN TTM: 当前 TTM 窗口被剔" in n for n in notes)
    assert any(n.startswith("QQQ: 跳过") for n in notes)
    assert any("RKLB 营业线: 样本不足" in n for n in notes)


def test_csv_rows_flatten_all_kinds():
    recs = pr.csv_rows([_row(), {"ticker": "QQQ", "skip": "index"}])
    assert list(recs[0]) == list(recs[1])       # 同一组列，DictWriter 不会缺列
    assert recs[0]["gaap_3y_p50"] == 51 and recs[1]["skip"] == "index"
    assert recs[0]["fy2_label"] == "FY2028(至2028-01)"


def test_load_watchlist(tmp_path):
    f = tmp_path / "w.toml"
    f.write_text('[tickers.QQQ]\nkind = "index"\n[tickers.MSFT]\n', encoding="utf-8")
    assert pr.load_watchlist(f) == [("QQQ", "index"), ("MSFT", "stock")]
    assert pr.load_watchlist(f, "msft, nvda") == [("MSFT", "stock"), ("NVDA", "stock")]
    assert pr.load_watchlist(tmp_path / "missing.toml", "aapl") == [("AAPL", "stock")]
