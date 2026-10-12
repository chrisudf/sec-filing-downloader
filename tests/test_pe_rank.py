# -*- coding: utf-8 -*-
"""watchlist 批量 PE 分位表（valuation/pe_rank.py）的纯函数：分位/陈旧判定、前瞻 PE、渲染。

联网部分（load_inputs / fetch_consensus / yfinance info）不在这里测——它们都是
既有模块的函数，本模块只负责拼装与口径。
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

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


def test_last_close_skips_unsettled_nan_row():
    import pandas as pd
    nan = float("nan")
    idx = pd.to_datetime(["2026-09-29", "2026-09-30", "2026-10-01"]).tz_localize("America/New_York")
    hist = pd.DataFrame({"Open": [230.97, 229.27, 229.95], "Close": [227.21, 228.38, nan]},
                        index=idx)
    assert pr.last_close(hist) == (228.38, date(2026, 9, 30))
    import pytest
    with pytest.raises(RuntimeError):
        pr.last_close(hist.assign(Close=nan))


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
            {"ticker": "QQQ", "kind": "index", "skip": pr.ETF_SKIP},
            _row(ticker="RKLB", gaap={"err": "样本不足"}, op={"err": "样本不足"}),
            {"ticker": "GLD", "kind": "etf", "skip": pr.ETF_SKIP},
            {"ticker": "XYZ", "skip": "yfinance 取不到 XYZ 价格"}]
    text, notes = pr.render(rows, "2026-09-24")
    table = [ln for ln in text.splitlines() if ln.startswith("| ")]
    # 跳过的票不占表格行：表头 + 3 只有数的票
    assert len(table) == 1 + 3
    assert not any(ln.startswith(("| QQQ", "| GLD", "| XYZ")) for ln in table)
    assert all(ln.count("|") == len(pr.HEADER) + 1 for ln in table)
    # 汇成表格下方一行，同因合并、不同因分开
    assert f"未纳入：QQQ、GLD（{pr.ETF_SKIP}）；XYZ（yfinance 取不到 XYZ 价格）" in text
    assert "28.5x†07-30" in text
    assert any("AMZN TTM: 当前 TTM 窗口被剔" in n for n in notes)
    assert not any("QQQ" in n or "GLD" in n for n in notes)   # 脚注也不再逐条列
    assert any("RKLB 营业线: 样本不足" in n for n in notes)
    assert pr.skipped_line([_row()]) is None
    assert "未纳入" not in pr.render([_row()], "2026-09-24")[0]


def test_csv_rows_flatten_all_kinds():
    recs = pr.csv_rows([_row(), {"ticker": "QQQ", "skip": "index"}])
    assert list(recs[0]) == list(recs[1])       # 同一组列，DictWriter 不会缺列
    assert recs[0]["gaap_3y_p50"] == 51 and recs[1]["skip"] == "index"
    assert recs[0]["fy2_label"] == "FY2028(至2028-01)"
    # PEG 已删（LESSONS.md 2026-10-12）：Yahoo 组只留 tPE 做对账
    assert pr.HEADER[-1] == "Yahoo tPE" and "PEG" not in pr.HEADER
    assert recs[0]["yahoo_tpe"] == 28.47 and "yahoo_peg" not in recs[0]


def test_load_watchlist(tmp_path):
    f = tmp_path / "w.toml"
    f.write_text('[tickers.QQQ]\nkind = "index"\n[tickers.MSFT]\n', encoding="utf-8")
    assert pr.load_watchlist(f) == [("QQQ", "index"), ("MSFT", "stock")]
    assert pr.load_watchlist(f, "msft, nvda") == [("MSFT", "stock"), ("NVDA", "stock")]
    assert pr.load_watchlist(tmp_path / "missing.toml", "aapl") == [("AAPL", "stock")]


# ---- ⚠ 标到「本财年 PE」格 / 盘中判定 / 网页 JSON ----

def _trend(a0, c0, a1, c1):
    return {"0y": {"90daysAgo": a0, "current": c0}, "+1y": {"90daysAgo": a1, "current": c1}}


def test_one_time_flag_marks_only_fy1_cell():
    # AMZN 2026-08 实测：0y 8.57→12.11（+41%），+1y 只 +5.5%
    flag = pr.one_time_flag(_trend(8.57, 12.11, 9.90, 10.45))
    assert round(flag["j0"], 2) == 0.41 and round(flag["j1"], 3) == 0.056
    assert "+41%" in pr.one_time_note(flag)
    fw = dict(pr.forward(249.27, {"fy1": {"avg": 12.87, "low": 9, "high": 15, "n": 50},
                                  "fy2": {"avg": 10.47, "low": 8.69, "high": 15.04, "n": 50}}),
              fy1_suspect=flag)
    cells = pr._fwd_cells(fw)
    assert cells[0].endswith("⚠") and "⚠" not in cells[1] + cells[2]
    # 基本面上修两年一起抬：不是一次性
    assert pr.one_time_flag(_trend(8.0, 9.6, 9.0, 10.8)) is None
    assert pr.one_time_flag(None) is None


def test_fy1_suspect_reaches_csv_and_table():
    flag = {"j0": 0.48, "j1": 0.05}
    r = _row()
    r["fwd"] = dict(r["fwd"], fy1_pe=19.4, fy1_suspect=flag)
    text, _ = pr.render([r], "2026-09-24")
    assert "19.4x⚠" in text
    assert pr.csv_rows([r])[0]["fy1_suspect"] is True
    assert pr.csv_rows([_row()])[0]["fy1_suspect"] is False


def test_us_market_open():
    from datetime import datetime
    ET = pr.ET
    assert pr.us_market_open(datetime(2026, 9, 25, 10, 0, tzinfo=ET))       # 周五盘中
    assert not pr.us_market_open(datetime(2026, 9, 25, 9, 29, tzinfo=ET))
    assert not pr.us_market_open(datetime(2026, 9, 25, 16, 0, tzinfo=ET))
    assert not pr.us_market_open(datetime(2026, 9, 26, 11, 0, tzinfo=ET))   # 周六
    # 定时任务的时点：布里斯班周六 08:00 = 美东周五 18:00（已收盘）
    from zoneinfo import ZoneInfo
    assert not pr.us_market_open(datetime(2026, 9, 26, 8, 0, tzinfo=ZoneInfo("Australia/Brisbane")))
    # 布里斯班周六 00:30 = 美东周五 10:30（盘中）
    assert pr.us_market_open(datetime(2026, 9, 26, 0, 30, tzinfo=ZoneInfo("Australia/Brisbane")))


def test_render_marks_intraday():
    assert pr.INTRADAY_NOTE in pr.render([_row()], "2026-09-25", intraday=True)[0]
    assert pr.INTRADAY_NOTE not in pr.render([_row()], "2026-09-25")[0]


def test_payload_is_strict_json():
    import json
    nan = float("nan")
    rows = [_row(yh={"tpe": nan, "peg": None}),
            _row(ticker="MSFT", px_date=TODAY - timedelta(days=3)),
            {"ticker": "QQQ", "skip": "index"}]
    p = pr.payload(rows, ["n1"], "2026-09-25", "2026-09-25T08:00:00+10:00", False, "w.toml")
    s = json.dumps(p, allow_nan=False)            # NaN 残留会在这里抛
    back = json.loads(s)
    assert back["px_date"] == TODAY.isoformat()   # 取各票最大价格日
    assert back["rows"][0]["yh"]["tpe"] is None
    assert back["rows"][0]["gaap"]["p3"]["50"] == 51           # int 键 -> str 键
    assert back["rows"][0]["fy_labels"] == ["FY2027(至2027-01)", "FY2028(至2028-01)"]
    assert back["header"] == pr.HEADER and back["notes"] == ["n1"]
    assert back["skipped"] == "未纳入：QQQ（index）"          # 网页与 md 同一行文字


def test_write_atomic_leaves_no_tmp(tmp_path):
    f = tmp_path / "pe_rank_2026-09-25.json"
    pr.write_atomic(f, '{"a": 1}')
    pr.write_atomic(f, '{"a": 2}')
    assert f.read_text(encoding="utf-8") == '{"a": 2}'
    assert [p.name for p in tmp_path.iterdir()] == [f.name]


# ---------------------------------------------------------------- gaap_onetime
# 夹具：AMZN / META / HD 的真实 companyfacts，只留 gaap_onetime 用到的 5 个 tag、
# 2022-06 起的 3 个月 / 12 个月期（2026-10-02 拉取）
_FIX = Path(__file__).parent / "fixtures" / "onetime_companyfacts_min.json"


def _cf(t):
    return json.loads(_FIX.read_text(encoding="utf-8"))[t]


def test_onetime_amzn_stale_point_still_has_anthropic_gains():
    """† 停住的「末个有效点」（2025Q2–2026Q1 窗口）仍含 Q3'25 $10.2B、Q1'26 $15.7B
    Anthropic 重估：各自低于 pe_band 1.25 倍剔窗门槛，畸变守卫放行。表上 28.1x，还原约 35x。"""
    ot = pr.gaap_onetime(_cf("AMZN"), "2026-03-31")
    assert ot["ratio"] == pytest.approx(1.256, abs=0.005)
    assert ot["excess"] / 1e9 == pytest.approx(25.75, abs=0.05)
    # 被剔的 2026Q2 窗口本身更离谱（$53.4B 那季），同一判据也认得出
    assert pr.gaap_onetime(_cf("AMZN"), "2026-06-30")["ratio"] > 1.6


def test_onetime_meta_tax_charge_pushes_pe_up():
    """META Q3'25 OBBBA 一次性税费：净利 −86% 仍在剔窗门槛内（往下最多 −100%）。
    方向与 AMZN 相反——GAAP PE 被抬高，还原后更便宜。"""
    ot = pr.gaap_onetime(_cf("META"), "2026-06-30")
    assert ot["ratio"] == pytest.approx(0.875, abs=0.005)
    assert ot["etr"] == pytest.approx(0.222, abs=0.001)
    assert ot["etr_norm"] == pytest.approx(0.117, abs=0.001)
    g = dict(_row()["gaap"], pe=27.4, r10=43.1, r5=57.5, r3=43.6)
    note = pr.onetime_note(dict(ot, pe=23.9, asof="2026-10-01"), g)
    assert "有效税率 22%（常态 12%）" in note
    assert "营业外超常" not in note                 # 营业外只差 0.8%，不列
    assert "扣除后约 23.9x（2026-10-01 收盘）" in note
    assert note.endswith("原值 27.4x · P43/P58/P44")   # 表里是还原口径，原值进脚注


def test_onetime_clean_company_not_flagged():
    f = _cf("HD")
    end = max(pr._quarterly(f, pr.PRETAX_TAGS))
    assert pr.gaap_onetime(f, end) is None
    assert pr.gaap_onetime(f, end, impact=0)["ratio"] == pytest.approx(1, abs=0.01)


def _syn(n=12, ni_over=None, drop=(), with_op=True):
    """合成 companyfacts：每季营业利润 10、营业外 +1、税率 15%（$B）；
    ni_over = {季序号: (税前, 税[, 营业利润])} 覆盖某季，drop = 去掉的季序号。"""
    tags = {t: [] for t in ("NetIncomeLoss", "IncomeTaxExpenseBenefit", "OperatingIncomeLoss",
                            pr.PRETAX_TAGS[0])}
    end0 = date(2023, 3, 31)
    for i in range(n):
        if i in drop:
            continue
        e = end0 + timedelta(days=91 * i)
        pre, tax, op = ((ni_over or {}).get(i, (11.0, 11.0 * 0.15)) + (10.0,))[:3]
        for tag, v in (("NetIncomeLoss", pre - tax), ("IncomeTaxExpenseBenefit", tax),
                       ("OperatingIncomeLoss", op), (pr.PRETAX_TAGS[0], pre)):
            tags[tag].append({"start": (e - timedelta(days=90)).isoformat(), "end": e.isoformat(),
                              "val": v * 1e9, "filed": (e + timedelta(days=30)).isoformat(),
                              "form": "10-Q", "fp": "Q"})
    if not with_op:
        del tags["OperatingIncomeLoss"]
    return {t: {"units": {"USD": rows}} for t, rows in tags.items()}, \
        (end0 + timedelta(days=91 * (n - 1))).isoformat()


def test_onetime_synthetic_guards():
    f, end = _syn()
    assert pr.gaap_onetime(f, end, impact=-1)["ratio"] == pytest.approx(1)   # -1 = 不过滤
    # 末季营业外 +21（常态 +1）：超常 20，税率不变 -> ratio = 税前 64 ÷ 干净税前 44
    f, end = _syn(ni_over={11: (31.0, 31.0 * 0.15)})
    ot = pr.gaap_onetime(f, end)
    assert ot["excess"] == pytest.approx(20e9) and ot["ratio"] == pytest.approx(64 / 44)
    # 取不齐就不标：没有营业利润（金融股）、历史不足 8 季、窗口中间缺季
    assert pr.gaap_onetime(_syn(with_op=False)[0], end) is None
    assert pr.gaap_onetime(*_syn(n=7)) is None
    assert pr.gaap_onetime(*_syn(ni_over={11: (31.0, 31.0 * 0.15)}, drop=(10,))) is None


def test_onetime_clean_loss_has_no_pe():
    """扣掉营业外收益后 TTM 转亏：ratio=None，仍要标（这正是最该提醒的情形）。"""
    over = {i: (1.0, 0.15) for i in range(8)}            # 前 8 季：营业 10、营业外 −9
    over.update({i: (-7.0, 0.0, 2.0) for i in (8, 9, 10)})  # 营业利润掉到 2，季季亏损
    over[11] = (60.0, 9.0, 2.0)                          # 末季一笔 +67 的营业外收益
    f, end = _syn(ni_over=over)
    ot = pr.gaap_onetime(f, end)
    assert ot["ratio"] is None
    assert "扣除后 TTM 亏损" in pr.onetime_note(dict(ot, pe=None), _row()["gaap"])


def test_onetime_marks_cells_and_csv():
    """标了的行整组换成还原口径：≈PE、还原分位、还原近 3 年带；原值不进格子。"""
    ot = {"ttm_end": "2026-03-31", "excess": 25.75e9, "base_q": 1.07e9, "pretax": 115.5e9,
          "etr": 0.209, "etr_norm": 0.188, "ratio": 1.256, "pe": 35.3,
          "clean": {"days": 1570, "r10": 2.4, "r5": 5.1, "r3": None,
                    "p3": None}}
    g = dict(_row()["gaap"], fresh=False, date="2026-07-30", onetime=ot)
    assert pr._band_cells(g) == ["≈35.3x⚠", "P2", "P5", "—", "—"]
    assert pr._band_cells(dict(g, onetime=dict(ot, pe=None, clean=None)))[0] == "亏损⚠"
    rec = pr.csv_rows([_row(gaap=g), _row()])
    assert rec[0]["gaap_onetime"] and rec[0]["gaap_clean_pe"] == 35.3
    assert rec[0]["gaap_clean_r5"] == 5.1 and rec[0]["gaap_r5"] == 2.0      # 原值仍留在 csv
    assert not rec[1]["gaap_onetime"] and rec[1]["gaap_clean_pe"] is None


def test_onetime_reading_dagger_row_uses_latest_window():
    """† 行：显示的是 7/30 旧点（截至 2026Q1、旧价格），还原要用最新那扇被剔的窗口 ×
    当前收盘——AMZN 35.3x（旧点还原）vs 33.8x（最新窗口还原）。"""
    f = _cf("AMZN")
    g = {"fresh": False, "ttm_period": "2026-03-31", "pe": 28.13, "date": "2026-07-30"}
    b10 = {"anom_windows": [{"period_end": "2026-06-30", "quarter": "2026-06-30",
                             "ttm_eps": 12.454, "known_from": "2026-07-31"}]}
    ot = pr.onetime_reading(f, g, b10, 248.23, date(2026, 10, 1))
    assert ot["ttm_end"] == "2026-06-30" and ot["asof"] == "2026-10-01"
    assert ot["pe"] == pytest.approx(248.23 / 12.454 * 1.694, abs=0.05)
    # 最新窗口还没公告（可知日在价格日之后）-> 退回旧点、旧价格
    ot = pr.onetime_reading(f, g, b10, 235.5, date(2026, 7, 30))
    assert ot["ttm_end"] == "2026-03-31" and ot["asof"] == "2026-07-30"
    assert ot["pe"] == pytest.approx(28.13 * 1.256, abs=0.05)
    # 新鲜行直接用显示的点
    fresh = dict(g, fresh=True, ttm_period="2026-06-30", pe=19.93, date="2026-10-01")
    assert pr.onetime_reading(f, fresh, {}, 248.23, date(2026, 10, 1))["ttm_end"] == "2026-06-30"


def test_onetime_reading_falls_back_when_latest_window_clean():
    """最新被剔窗口还原不出偏离（畸变在营业线以内）-> 退回旧点，旧点本身仍可能含一次性项。"""
    f, _ = _syn(ni_over={7: (31.0, 31.0 * 0.15)})       # q7 的一次性：q7~q10 四扇窗含它
    ends = sorted(pr._quarterly(f, pr.PRETAX_TAGS))
    g = {"fresh": False, "ttm_period": ends[10], "pe": 20.0, "date": ends[10]}
    b10 = {"anom_windows": [{"period_end": ends[11], "quarter": ends[11],
                             "ttm_eps": 5.0, "known_from": ends[11]}]}
    ot = pr.onetime_reading(f, g, b10, 100.0, date.fromisoformat(ends[11]) + timedelta(days=60))
    assert ot["ttm_end"] == ends[10] and ot["pe"] == pytest.approx(20.0 * 64 / 44)


def test_clean_band_ranks_against_restored_history():
    """历史逐窗乘各自的 ratio 后再排：只还原当前点、去比没还原的历史是两种口径相比
    （GOOG 那样会从 P47 假性跳到 P88+）。ratio 缺 / None 的窗口那几天不进分布。"""
    days = [(TODAY - timedelta(days=i)) for i in range(400, 0, -1)]
    series = [{"date": d.isoformat(), KEY: 20.0 + (i % 10), "ttm_period": "A" if i < 200 else "B"}
              for i, d in enumerate(days)]
    raw = pr.clean_band(series, {"A": 1.0, "B": 1.0}, KEY, 25.0)
    # B 窗口含一次性收益（PE 被压低）：还原 ×1.5 后同样的当前值 25x 落到更低的分位
    fixed = pr.clean_band(series, {"A": 1.0, "B": 1.5}, KEY, 25.0)
    assert fixed["r3"] < raw["r3"] and fixed["days"] == raw["days"] == 400
    assert fixed["p3"][50] > raw["p3"][50]
    gone = pr.clean_band(series, {"A": 1.0, "B": None}, KEY, 25.0)     # 还原后亏损的窗口
    assert gone["days"] == 200 and gone["r3"] is None                  # < MIN_DAYS 不给分位


def test_onetime_reading_attaches_clean_band():
    """b10 带 series 时，还原口径的分位一并算好（逐窗 ratio 走 _onetime_at，不偷看未来）。"""
    f, end = _syn(ni_over={11: (31.0, 31.0 * 0.15)})
    ends = sorted(pr._quarterly(f, pr.PRETAX_TAGS))
    series = [{"date": (TODAY - timedelta(days=300 - i)).isoformat(), KEY: 20.0,
               "ttm_period": ends[11] if i >= 150 else ends[10]} for i in range(300)]
    g = {"fresh": True, "ttm_period": end, "pe": 20.0, "date": TODAY.isoformat()}
    ot = pr.onetime_reading(f, g, {"series": series}, 100.0, TODAY)
    assert ot["pe"] == pytest.approx(20.0 * 64 / 44)
    assert ot["clean"]["days"] == 300 and ot["clean"]["r3"] is not None


# ---------------------------------------------------------------- 下财年修正 / ↓
# 表格形状照 2026-10-12 yfinance 实测：列名 upLast7days 小写 d、downLast7Days 大写 D

def _rev_df(up30, down30):
    import pandas as pd
    return pd.DataFrame({"upLast7days": [0, up30], "upLast30days": [0, up30],
                         "downLast30days": [0, down30], "downLast7Days": [0, down30],
                         "currency": ["USD", "USD"]}, index=pd.Index(["0y", "+1y"], name="period"))


def _trend_df(cur, ago30):
    import pandas as pd
    return pd.DataFrame({"current": [1.0, cur], "7daysAgo": [1.0, cur], "30daysAgo": [1.0, ago30],
                         "60daysAgo": [1.0, ago30], "90daysAgo": [1.0, ago30],
                         "currency": ["USD", "USD"]}, index=pd.Index(["0y", "+1y"], name="period"))


class _FakeTicker:
    def __init__(self, rev, trend):
        self.eps_revisions, self.eps_trend = rev, trend


def _rev(up30, down30, cur, ago30):
    return pr.parse_revisions(_rev_df(up30, down30), _trend_df(cur, ago30))


def test_revisions_parse_and_cell():
    # TSLA 2026-10-12 实测：↑1/↓4，一致预期 2.15858 -> 2.18245
    rv = pr.fetch_revisions("TSLA", _FakeTicker(_rev_df(1, 4), _trend_df(2.18245, 2.15858)))
    assert rv == {"up30": 1, "down30": 4, "eps": 2.18245, "eps_30d": 2.15858}
    assert pr.revision_cell(rv) == "↑1/↓4 · +1.1%"
    # 下修用真正的减号（与 onetime_note 的 −$1.0B 一致）
    assert pr.revision_cell(_rev(2, 15, 3.94, 4.00)) == "↑2/↓15 · −1.5%"
    # 亏损 / 穿零报绝对值：百分比方向会反（RKLB −0.089→−0.066 是减亏）
    assert pr.revision_cell(_rev(2, 6, -0.06557, -0.08929)) == "↑2/↓6 · −0.09→−0.07"
    assert pr.revision_cell(_rev(3, 0, 0.16, -0.05)) == "↑3/↓0 · −0.05→0.16"
    # 只缺一致预期：照给家数
    rv = pr.parse_revisions(_rev_df(3, 1), None)
    assert rv["eps"] is None and pr.revision_cell(rv) == "↑3/↓1"


def test_consensus_and_revisions_share_one_ticker(monkeypatch):
    """一致预期和修正读同一个 Ticker 实例：yfinance 的 earningsTrend 按实例缓存，各建各的
    就是每只票多一次 Yahoo 请求（PR #39 review）。新建 Ticker 直接炸，证明两边都没另起实例。"""
    import pandas as pd
    import yfinance as yf
    from valuation import ref_table

    def no_new_ticker(*a, **k):
        raise AssertionError("不该再建 yf.Ticker")
    monkeypatch.setattr(yf, "Ticker", no_new_ticker)
    tk = _FakeTicker(_rev_df(2, 15), _trend_df(3.94, 4.00))
    tk.earnings_estimate = pd.DataFrame(
        {"avg": [3.5, 3.94], "low": [3.2, 3.5], "high": [3.8, 4.4], "numberOfAnalysts": [20, 18]},
        index=["0y", "+1y"])
    tk.fast_info = {"lastPrice": 100.0}
    cons, trend, px = ref_table.fetch_consensus("X", tk)
    assert cons["fy2"]["avg"] == 3.94 and trend["+1y"]["30daysAgo"] == 4.00 and px == 100.0
    assert pr.revisions("X", tk) == {"up30": 2, "down30": 15, "eps": 3.94, "eps_30d": 4.00}


def test_revisions_empty_etf_and_failure_show_dash():
    import pandas as pd
    empty = pd.DataFrame()
    # 空表（ETF/指数，或个股取数失败被 yfinance 吞掉）= 取不到，不是「没有修正」
    with pytest.raises(pr.EmptyRevisions):
        pr.fetch_revisions("QQQ", _FakeTicker(empty, empty))
    # 有表但下财年家数是 NaN：同样当取不到
    nan = float("nan")
    with pytest.raises(pr.EmptyRevisions):
        pr.fetch_revisions("X", _FakeTicker(_rev_df(nan, nan), _trend_df(1.0, 1.0)))
    rv = pr.revisions("QQQ", _FakeTicker(empty, empty))
    assert rv == {"err": "Yahoo 返回空表（无覆盖或取数失败）"}

    def boom(t, tk):
        raise ConnectionError("HTTP 429\nToo Many Requests")
    assert pr.revisions("NVDA", fetch=boom) == {"err": "ConnectionError: HTTP 429"}
    # 取不到：格子「—」、进脚注、不打标记；一致预期也没取到（fwd={}）照样不抛
    r = _row(fwd={})
    pr.apply_revisions(r, {"err": "Yahoo 返回空表（无覆盖或取数失败）"})
    assert r["notes"] == ["下财年修正取不到：Yahoo 返回空表（无覆盖或取数失败）"]
    assert not r["fwd"].get("fy2_down")
    # ETF 不联网直接跳过（collect 在取任何数之前就返回），不进表格
    etf = pr.collect("QQQ", "index", "x@example.com")
    assert etf["skip"] == pr.ETF_SKIP and "rev" not in etf
    # 旧结果 / 取不到的行：这一列「—」，表格列数不乱
    text, _ = pr.render([_row(), r, etf], "2026-10-12")
    table = [ln for ln in text.splitlines() if ln.startswith("| ")]
    assert len(table) == 1 + 2 and all(ln.count("|") == len(pr.HEADER) + 1 for ln in table)
    i = pr.HEADER.index("下财年修正(30天)")
    assert [ln.split(" | ")[i] for ln in table[1:]] == ["—", "—"]
    rec = pr.csv_rows([r, etf])
    assert list(rec[0]) == list(rec[1]) and rec[0]["fy2_rev_up30"] is None


def test_revision_mark_triggers_on_fy2_cell():
    rv = _rev(2, 15, 3.94, 4.00)
    assert pr.revised_down(rv)
    r = _row()
    pr.apply_revisions(r, rv)
    assert r["fwd"]["fy2_down"] is True
    assert r["notes"] == [pr.revision_note(rv)]
    assert "分母在被下修，下财年 PE 偏低、便宜得不实" in r["notes"][0]
    assert "↑2/↓15 · −1.5%" in r["notes"][0]
    cells = pr._fwd_cells(r["fwd"])
    assert cells[1] == "14.4x↓" and "↓" not in cells[0] + cells[2]
    text, notes = pr.render([r], "2026-10-12")
    row = next(ln for ln in text.splitlines() if ln.startswith("| NVDA"))
    assert "14.4x↓" in row and "↑2/↓15 · −1.5%" in row
    assert any("NVDA: 下财年 30 天" in n for n in notes)
    rec = pr.csv_rows([r])[0]
    assert rec["fy2_down"] is True and rec["fy2_rev_down30"] == 15
    # 门槛边界：恰好 REV_MIN_DOWN 家、0 家上调、一致预期微降 -> 标
    assert pr.revised_down(_rev(0, pr.REV_MIN_DOWN, 3.999, 4.00))
    # 门槛看下调家数、不看净数（README 写明）：↑4/↓5 净下调只有 1 家，也标
    assert pr.revised_down(_rev(4, 5, 3.99, 4.00))


def test_revision_counts_vs_magnitude_disagree_no_mark():
    cases = [
        _rev(1, 4, 2.18245, 2.15858),      # TSLA 实测：家数朝下（且不够 5 家），一致预期 +1.1%
        _rev(2, 6, -0.06557, -0.08929),    # RKLB 实测：净下调 6 家，但亏损在收窄（减亏）
        _rev(0, 8, 4.02, 4.00),            # 家数一边倒向下，一致预期反而上调
        _rev(6, 6, 3.90, 4.00),            # 幅度朝下，但不是净下调
        _rev(0, pr.REV_MIN_DOWN - 1, 3.90, 4.00),   # 幅度朝下，但下调家数不到门槛
        _rev(1, 9, 4.00, 4.00),            # 一致预期没动：变动 = 0 不算 < 0
        pr.parse_revisions(_rev_df(1, 9), None),    # 缺一致预期：只有家数不标
    ]
    for rv in cases:
        assert not pr.revised_down(rv), rv
        r = _row()
        pr.apply_revisions(r, rv)
        assert not r["fwd"].get("fy2_down") and r["notes"] == []
        assert "↓" not in pr._fwd_cells(r["fwd"])[1]
    # 下财年亏损（PE 为空）：家数和幅度都朝下也不标——没有「便宜」可言
    rv = _rev(1, 7, -0.30, -0.20)
    assert pr.revised_down(rv)
    r = _row(fwd=pr.forward(70.0, {"fy2": {"avg": -0.3, "low": -0.6, "high": 0.1, "n": 9}}))
    pr.apply_revisions(r, rv)
    assert not r["fwd"].get("fy2_down") and pr._fwd_cells(r["fwd"])[1] == "—"


def test_revision_mark_and_one_time_flag_independent():
    """⚠ 标本财年格（一次性收益进了本财年预期）、↓ 标下财年格（分母在被下修）：同一行可同时出现，
    各自一条脚注，互不覆盖。"""
    r = _row()
    r["fwd"] = dict(r["fwd"], fy1_pe=19.4, fy1_suspect={"j0": 0.48, "j1": 0.05})
    r["notes"].append(pr.one_time_note(r["fwd"]["fy1_suspect"]))
    pr.apply_revisions(r, _rev(3, 12, 15.20, 15.68))
    cells = pr._fwd_cells(r["fwd"])
    assert cells[0] == "19.4x⚠" and cells[1] == "14.4x↓"
    assert len(r["notes"]) == 2
    assert "一次性" in r["notes"][0] and "分母在被下修" in r["notes"][1]
    rec = pr.csv_rows([r])[0]
    assert rec["fy1_suspect"] is True and rec["fy2_down"] is True
    text, _ = pr.render([r], "2026-10-12")
    assert "19.4x⚠ | 14.4x↓" in text
    # 网页 JSON 带上原始读数（前端按同口径格式化）
    back = json.loads(json.dumps(pr.payload([r], [], "2026-10-12", "t", False, "w"),
                                 allow_nan=False))
    assert back["rows"][0]["rev"]["down30"] == 12 and back["rows"][0]["fwd"]["fy2_down"] is True


def _run_main(monkeypatch, tmp_path, *argv):
    """main() 走真实写文件路径，只把联网的 collect 换成 ETF 跳过行。"""
    monkeypatch.setattr(pr, "collect", lambda t, kind, email: {
        "ticker": t, "kind": kind, "skip": pr.ETF_SKIP})
    monkeypatch.setattr(pr, "contact_email", lambda: "test@example.com")
    monkeypatch.setattr(pr, "us_market_open", lambda: False)
    monkeypatch.setattr("sys.argv", ["pe_rank.py", "--out-dir", str(tmp_path), *argv])
    pr.main()


def test_out_stem_manual_suffix():
    assert pr.out_stem("2026-10-12", manual=False) == "pe_rank_2026-10-12"
    assert pr.out_stem("2026-10-12", manual=True) == "pe_rank_2026-10-12-manual"


def test_tickers_run_does_not_clobber_full_table(monkeypatch, tmp_path):
    # 2026-10-12 之前：--tickers 写同名 pe_rank_YYYY-MM-DD.*，盖掉当天整表，网页随之只剩那几只
    from app import pe_rank_service as ps
    wl = tmp_path / "watchlist.toml"
    wl.write_text('[tickers]\nQQQ = { kind = "etf" }\nGLD = { kind = "etf" }\n', encoding="utf-8")
    out = tmp_path / "out"
    _run_main(monkeypatch, out, "--watchlist", str(wl))
    full = ps.latest_file(out)
    assert full is not None and not full.stem.endswith("-manual")
    full_text = full.read_text(encoding="utf-8")

    _run_main(monkeypatch, out, "--watchlist", str(wl), "--tickers", "QQQ")
    manual = sorted(p.name for p in out.glob("pe_rank_*-manual.*"))
    assert [n.rsplit(".", 1)[1] for n in manual] == ["csv", "json", "md"]
    assert ps.latest_file(out) == full                      # 网页仍读整表
    assert full.read_text(encoding="utf-8") == full_text    # 整表没被覆盖
    assert '"GLD"' in full_text
    assert '"GLD"' not in (out / manual[1]).read_text(encoding="utf-8")
