# -*- coding: utf-8 -*-
"""外国发行人 TTM PE 兜底（valuation/foreign_band.py）的纯函数：形态判定、20-F 核对、
防前视的逐日 TTM、窗口分位。联网部分（Yahoo earnings dates / 汇率）不在这里测。"""
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from valuation import foreign_band as fb

# 4 个财年（日历年），每年 4 季，公告日 = 季末后约 16 天；每季 EPS 1.0（每 ADR 美元）
QDATES = ["2021-04-15", "2021-07-15", "2021-10-14", "2022-01-13",
          "2022-04-14", "2022-07-14", "2022-10-13", "2023-01-12",
          "2023-04-20", "2023-07-20", "2023-10-19", "2024-01-18",
          "2024-04-18", "2024-07-18", "2024-10-17", "2025-01-16",
          "2025-04-17"]                     # 最后一季：FY2025，20-F 未出
QS = [{"date": d, "eps": 1.0} for d in QDATES]
# 20-F：本币每普通股 EPS；ADR = 5 股、汇率 30 -> 每 ADR 美元 = val*5/30 = 4.0 时 val=24
ANNUAL = {f"{y}-12-31": {"val": 24.0, "filed": f"{y + 1}-04-15"} for y in (2021, 2022, 2023, 2024)}
FX = {e: 30.0 for e in ANNUAL}


def _facts(units_q=False):
    rows = [{"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 100.0, "fp": "FY",
             "form": "20-F", "filed": f"{y + 1}-04-15"} for y in (2022, 2023)]
    if units_q:
        rows.append({"start": "2023-01-01", "end": "2023-03-31", "val": 25.0, "fp": "Q1",
                     "form": "10-Q", "filed": "2023-05-01"})
    return {"NetIncomeLoss": {"units": {"USD": rows}}}


def test_foreign_shape():
    assert fb.foreign_shape({"taxonomy": "ifrs-full", "facts": {}})
    # us-gaap 20-F 发行人：有年度、无季度
    assert fb.foreign_shape({"taxonomy": "us-gaap", "facts": _facts()})
    # 有季度 = 普通美股，不走兜底（取数失败不能悄悄换源）
    assert not fb.foreign_shape({"taxonomy": "us-gaap", "facts": _facts(units_q=True)})
    assert not fb.foreign_shape({"taxonomy": "us-gaap", "facts": {}})


def test_parse_yahoo_quarters_drops_unreported_and_uses_et_date():
    idx = pd.DatetimeIndex(["2026-01-15 01:00", "2026-04-15 20:00", "2026-07-16 02:00"],
                           tz="America/New_York")
    df = pd.DataFrame({"Reported EPS": [3.14, 3.49, float("nan")]}, index=idx[::-1][::-1])
    q = fb.parse_yahoo_quarters(df)
    assert q == [{"date": "2026-01-15", "eps": 3.14}, {"date": "2026-04-15", "eps": 3.49}]
    assert fb.parse_yahoo_quarters(None) == []
    assert fb.parse_yahoo_quarters(pd.DataFrame()) == []


def test_annual_eps_prefers_local_currency_over_convenience_usd():
    def rows(v):
        return [{"start": "2024-01-01", "end": "2024-12-31", "val": v, "fp": "FY",
                 "form": "20-F", "filed": "2025-04-17"}]
    facts = {"DilutedEarningsLossPerShare": {"units": {"TWD/shares": rows(44.67),
                                                       "USD/shares": rows(1.36)}}}
    ccy, a = fb.annual_eps(facts)
    assert ccy == "TWD" and a["2024-12-31"]["val"] == 44.67
    ccy, _ = fb.annual_eps({"DilutedEarningsLossPerShare": {"units": {"USD/shares": rows(1.36)}}})
    assert ccy == "USD"
    assert fb.annual_eps({}) == (None, {})


def test_quarters_of_year_assigns_q4_announced_after_year_end():
    qs = fb.quarters_of_year(QS, "2022-12-31")
    assert [q["date"] for q in qs] == ["2022-04-14", "2022-07-14", "2022-10-13", "2023-01-12"]


def test_infer_ratio():
    assert fb.infer_ratio(QS, ANNUAL, FX, "TWD") == 5
    # 换算对不上任何整比例 -> 不推断
    assert fb.infer_ratio(QS, {k: dict(v, val=v["val"] * 0.89) for k, v in ANNUAL.items()},
                          FX, "TWD") is None


def test_crosscheck_and_accept():
    annual = dict(ANNUAL)
    annual["2022-12-31"] = {"val": 30.0, "filed": "2023-04-15"}   # 该年 Yahoo 偏差 −20%
    checks = fb.crosscheck(QS, annual, FX, 5, "TWD")
    assert [c["ok"] for c in checks] == [True, False, True, True]
    assert checks[1]["why"] == "偏差超限" and checks[1]["dev"] == pytest.approx(-0.2)
    ok, tail = fb.accepted_quarters(QS, checks)
    assert tail == 1 and ok["2025-04-17"] == "USD"          # 20-F 之后的季度放行，沿用币种
    assert {c["unit"] for c in checks if c["ok"]} == {"USD"}
    assert not ok.keys() & {"2022-04-14", "2022-07-14", "2022-10-13", "2023-01-12"}  # 整年剔


def test_latest_checked_year_failing_refuses_whole_band():
    annual = dict(ANNUAL)
    annual["2024-12-31"] = {"val": 30.0, "filed": "2025-04-15"}
    ok, why = fb.accepted_quarters(QS, fb.crosscheck(QS, annual, FX, 5, "TWD"))
    assert ok is None and "2024-12-31" in why


def test_crosscheck_incomplete_year():
    checks = fb.crosscheck(QS[:-6], ANNUAL, FX, 5, "TWD")   # FY2024 只剩 1 季
    assert checks[-1]["why"].startswith("Yahoo 该财年") and not checks[-1]["ok"]
    ok, why = fb.accepted_quarters([], [dict(c, yahoo=None) for c in checks])
    assert ok is None


def test_build_series_no_lookahead_and_gap():
    ok = dict.fromkeys((q["date"] for q in QS), "USD")
    days = [date(2022, 1, 12), date(2022, 1, 13), date(2022, 1, 14)]
    s = fb.build_series(QS, ok, [(d, 40.0) for d in days])
    # 2022-01-13 公告的第 4 季：当天不算已知，次日起才有 4 季 TTM
    assert [x["date"] for x in s] == ["2022-01-14"]
    assert s[0]["pe_trailing"] == 10.0 and s[0]["ttm_period"] == "2022-01-13"
    # 中间缺季（相邻公告间隔超 Q_GAP 上限）的窗口不算
    gappy = [q for q in QS if q["date"] != "2021-10-14"]
    assert fb.build_series(gappy, ok, [(date(2022, 1, 14), 40.0)]) == []
    # 未放行的季度在窗口里 -> 不算
    del ok["2021-07-15"]
    assert fb.build_series(QS, ok, [(date(2022, 1, 14), 40.0)]) == []


def test_summarize_series_windows():
    today = date.today()
    n = 400
    series = [{"date": (today - timedelta(days=n - 1 - i)).isoformat(),
               "pe_trailing": float(10 + i), "ttm_eps": 1.0, "ttm_period": "x"}
              for i in range(n)]
    series[-1]["pe_trailing"] = 12.0
    m = fb.summarize_series(series, today)
    assert m["fresh"] and m["pe"] == 12.0 and m["days10"] == n and not m["thin"]
    assert m["r10"] == pytest.approx(100 * 2 / n)
    assert m["r3"] == m["r10"] and set(m["p3"]) == {10, 25, 50, 75, 90}
    # 样本不足 250 天：不给分位
    m = fb.summarize_series(series[-100:], today)
    assert m["thin"] and m["r10"] is None and m["p3"] is None
    # 近零剔除：EPS < 中位 25% 的日子不进分布
    s2 = [dict(x, ttm_eps=0.1) if i < 50 else x for i, x in enumerate(series)]
    assert fb.summarize_series(s2, today)["days10"] == n - 50
    with pytest.raises(RuntimeError):
        fb.summarize_series([], today)


def test_source_note():
    checks = fb.crosscheck(QS, ANNUAL, FX, 5, "TWD")
    m = {"xcheck": {"ccy": "TWD", "ratio": 5, "ratio_inferred": False, "tail": 1,
                    "years": checks, "n_ok": 4, "n": 4}}
    s = fb.source_note(m)
    assert "4/4" in s and "1 ADR = 5 股" in s and "1 季年报未出" in s


# ASML 型：Yahoo 近年给本币（欧元）EPS、早年给美元；20-F 欧元、1 ADR = 1 股
EUR_FX = 0.9
EQ = ([{"date": d, "eps": 1.0 / EUR_FX} for d in QDATES[:4]]          # FY2021：Yahoo 美元
      + [{"date": d, "eps": 1.0} for d in QDATES[4:]])                # FY2022 起：Yahoo 欧元
EANN = {f"{y}-12-31": {"val": 4.0, "filed": f"{y + 1}-04-15"} for y in (2021, 2022, 2023, 2024)}
EFX = {e: EUR_FX for e in EANN}


def test_local_currency_years_detected_per_year():
    assert fb.infer_ratio(EQ, EANN, EFX, "EUR") == 1
    checks = fb.crosscheck(EQ, EANN, EFX, 1, "EUR")
    assert [c["unit"] for c in checks] == ["USD", "EUR", "EUR", "EUR"]
    assert all(c["ok"] for c in checks)
    ok, tail = fb.accepted_quarters(EQ, checks)
    assert ok["2021-04-15"] == "USD" and ok["2023-04-20"] == "EUR" and ok["2025-04-17"] == "EUR"


def test_local_quarters_converted_at_daily_fx():
    ok = dict.fromkeys((q["date"] for q in EQ), "EUR")
    fx_at = fb.fx_lookup([(date(2024, 1, 1), 0.8)])
    d = date(2024, 2, 1)
    s = fb.build_series(EQ, ok, [(d, 50.0)], fx_at)
    # 最近 4 季 = 4.0 欧元 ÷ 当日 0.8 = 5.0 美元 -> PE 10
    assert s[0]["ttm_eps"] == pytest.approx(5.0) and s[0]["pe_trailing"] == pytest.approx(10.0)
    # 当日之前没有汇率 -> 这天跳过，不拿缺失当 1
    assert fb.build_series(EQ, ok, [(d, 50.0)], fb.fx_lookup([(date(2024, 3, 1), 0.8)])) == []
    assert fb.build_series(EQ, ok, [(d, 50.0)]) == []


def test_fx_annual():
    daily = [(date(2024, 1, 1) + timedelta(days=i), 0.9) for i in range(366)]
    assert fb.fx_annual({"2024-12-31": {}}, daily, "EUR") == {"2024-12-31": pytest.approx(0.9)}
    assert fb.fx_annual({"2024-12-31": {}}, daily[:100], "EUR") == {}
    assert fb.fx_annual({"2024-12-31": {}}, [], "USD") == {"2024-12-31": 1.0}


def test_sanity_fx_catches_inverted_quote():
    fb.sanity_fx(0.9114, 1.09721, "EUR")          # 实测：互为倒数，放行
    fb.sanity_fx(32.943, 0.03036, "TWD")
    fb.sanity_fx(32.943, None, "TWD")             # 对照取不到不拦
    with pytest.raises(RuntimeError):
        fb.sanity_fx(1.09721, 1.09721, "EUR")     # 拿反了：本该 0.91 却是 1.10


def _stmt_df(vals):
    cols = pd.to_datetime(list(vals))
    return pd.DataFrame([list(vals.values())], index=["Diluted EPS"], columns=cols)


def test_parse_statement_eps():
    df = _stmt_df({"2026-09-30": 12.93, "2026-06-30": float("nan")})
    assert fb.parse_statement_eps(df) == {"2026-09-30": 12.93}
    assert fb.parse_statement_eps(None) == {}
    assert fb.parse_statement_eps(pd.DataFrame(index=["Net Income"])) == {}


# 2026-11 实测 ASML：公告值 Q3'26 11.82，财报与 6-K 都是 12.93；其余四季两路一致
ASML_Q = [{"date": "2025-10-15", "eps": 5.49}, {"date": "2026-01-27", "eps": 7.34},
          {"date": "2026-04-14", "eps": 7.15}, {"date": "2026-07-13", "eps": 7.58},
          {"date": "2026-10-14", "eps": 11.82}]
ASML_STMT = {"2025-09-30": 5.48, "2025-12-31": 7.34, "2026-03-31": 7.15,
             "2026-06-30": 7.58, "2026-09-30": 12.93}


def test_reconcile_fixes_bad_quarter_in_local_currency():
    ok = dict.fromkeys((q["date"] for q in ASML_Q), "EUR")
    q2, info = fb.reconcile_statement(ASML_Q, ok, ASML_STMT, "EUR")
    assert info["n"] == 5 and info["skipped"] is None
    assert [f["date"] for f in info["fixed"]] == ["2026-10-14"]   # 5.49 vs 5.48 是四舍五入，不改
    assert q2[-1]["eps"] == 12.93 and ASML_Q[-1]["eps"] == 11.82   # 不改原列表


def test_reconcile_usd_quarters_via_fx():
    # TSM 型：公告值每 ADR 美元，财报每 ADR 台币
    q = [{"date": "2026-07-16", "eps": 4.31}, {"date": "2026-10-15", "eps": 4.60}]
    ok = dict.fromkeys((x["date"] for x in q), "USD")
    stmt = {"2026-06-30": 4.31 * 32, "2026-09-30": 4.60 * 32}
    q2, info = fb.reconcile_statement(q, ok, stmt, "TWD", fb.fx_lookup([(date(2026, 1, 1), 32.0)]))
    assert info["n"] == 2 and not info["fixed"] and q2 == q
    # 没汇率 -> 美元季度不对照
    _, info = fb.reconcile_statement(q, ok, stmt, "TWD")
    assert info["n"] == 0


def test_reconcile_skips_when_scale_disagrees():
    # 财报给的是每普通股（差一个换股比 5）：多数季对不上 = 口径不同，整路不用，不能全改
    ok = dict.fromkeys((q["date"] for q in ASML_Q), "EUR")
    stmt = {k: v / 5 for k, v in ASML_STMT.items()}
    q2, info = fb.reconcile_statement(ASML_Q, ok, stmt, "EUR")
    assert q2 is ASML_Q and not info["fixed"] and "口径对不上" in info["skipped"]
    # 没放行的季度不对照
    _, info = fb.reconcile_statement(ASML_Q, {}, ASML_STMT, "EUR")
    assert info == {"n": 0, "fixed": [], "skipped": None}


def test_original_vals_vs_restated():
    def row(v, filed):
        return {"start": "2016-01-01", "end": "2016-12-31", "val": v, "fp": "FY",
                "form": "20-F", "filed": filed}
    facts = {"EarningsPerShareDiluted": {"units": {"EUR/shares": [
        row(3.64, "2019-02-06"), row(3.44, "2017-02-08"), row(3.44, "2018-02-07")]}}}
    ccy, a = fb.annual_eps(facts)
    assert a["2016-12-31"]["val"] == 3.64 and a["2016-12-31"]["orig"] == 3.44


# ---- ASML 真实数据回归（tests/fixtures/asml_foreign_band.json，2026-11-18 快照）----
# 独立验算见 docs/asml_pe_verification.md（scripts/verify_foreign_6k.py，6-K 原文对照）

@pytest.fixture(scope="module")
def asml():
    p = Path(__file__).parent / "fixtures" / "asml_foreign_band.json"
    return json.loads(p.read_text(encoding="utf-8"))


def test_asml_crosscheck_regression(asml):
    q, a, fx = asml["quarters"], asml["annual"], asml["fx"]
    assert fb.infer_ratio(q, a, fx, "EUR") == 1
    checks = {c["fy_end"][:4]: c for c in fb.crosscheck(q, a, fx, 1, "EUR")}
    # 2011：Yahoo 美元数整体偏低（6-K 对照 −7%~−10%）-> 剔除；不能靠放宽容差放进来
    assert not checks["2011"]["ok"] and checks["2011"]["unit"] == "USD"
    # 2013 年起 Yahoo 改给欧元；之前美元
    assert checks["2012"]["unit"] == "USD"
    assert all(checks[str(y)]["unit"] == "EUR" for y in range(2013, 2026))
    # ASC 606 追溯重述：2016/2017 对的是 20-F 原始申报值（3.44/4.91），不是重述后的 3.64/4.79
    for y, v in (("2016", 3.44), ("2017", 4.91)):
        assert checks[y]["ok"] and checks[y]["basis"] == "原始"
        assert checks[y]["sec"] == pytest.approx(v)
    # 2014 年起每年都过、且（2021 除外）与 20-F 差在四舍五入以内
    for y in range(2014, 2026):
        c = checks[str(y)]
        assert c["ok"], y
        if y != 2021:
            assert abs(c["dev"]) < 0.006, (y, c["dev"])
    # 2021：Yahoo 把 Q4'21 记成 3.93（6-K 4.38），年度合计 −3.0% 仍在容差内——已知残留
    assert checks["2021"]["dev"] == pytest.approx(-0.030, abs=0.001)


def test_asml_tail_and_statement_fix(asml):
    q, a, fx = asml["quarters"], asml["annual"], asml["fx"]
    ok, tail = fb.accepted_quarters(q, fb.crosscheck(q, a, fx, 1, "EUR"))
    assert tail == 3 and all(ok[d] == "EUR" for d in ("2026-04-14", "2026-07-13", "2026-10-14"))
    q2, info = fb.reconcile_statement(q, ok, ASML_STMT, "EUR")
    assert [f["date"] for f in info["fixed"]] == ["2026-10-14"]
    # 当前 TTM（欧元）= 7.34 + 7.15 + 7.58 + 12.93；不修正是 11.82，PE 高估约 3%
    last4 = [x["eps"] for x in q2 if x["date"] >= "2026-01-01"]
    assert sum(last4) == pytest.approx(35.00)
