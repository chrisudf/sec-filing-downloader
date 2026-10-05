# -*- coding: utf-8 -*-
"""内部人交易卡片服务: 汇总、图上的点、接口的降级与错误映射。"""
import asyncio

import pytest

from app import insider_service as svc
from app.edgar import EdgarError
from valuation.fetch_insider import InsiderError


def R(date, kind, value=1000.0, key="1", owner="Noto Anthony", filed=None, **kw):
    return {"date": date, "kind": kind, "value": value, "owner_key": key,
            "owner": owner, "role": "董事/CEO", "filed": filed or date,
            "acc": f"{date}-{key}-{kind}", **kw}


def test_summarize_buckets_people_and_last_buy():
    rows = [R("2026-06-16", "buy", 250_000), R("2026-05-08", "buy", 250_000),
            R("2026-02-05", "buy", 100_000, key="2", owner="B"),
            R("2026-02-06", "sell", 1_900_000, key="3"),
            R("2026-09-21", "sell_plan", 170_000, key="4"),
            R("2026-09-21", "tax", 50_000), R("2026-03-01", "exercise", None)]
    s = svc.summarize(rows)
    assert s["buy"]["n"] == 3 and s["buy"]["people"] == 2
    assert s["buy"]["value"] == 600_000
    assert s["buy"]["last"]["date"] == "2026-06-16"
    assert s["sell"] == {"n": 1, "people": 1, "value": 1_900_000}
    assert s["sell_plan"]["n"] == 1 and s["private"]["n"] == 0
    assert s["other_n"] == 2


def test_last_buy_is_by_trade_date_not_filing_date():
    # TSM: 7/2 的交易 9/4 才申报, 不能盖过 8/19 那笔
    rows = [R("2026-07-02", "buy", filed="2026-09-04"), R("2026-08-19", "buy", key="2")]
    assert svc.summarize(rows)["buy"]["last"]["date"] == "2026-08-19"


def test_cluster_needs_two_people_within_30_days():
    a = R("2026-06-01", "buy")
    assert svc.summarize([a, R("2026-07-01", "buy", key="2")])["buy"]["cluster"]
    assert not svc.summarize([a, R("2026-07-02", "buy", key="2")])["buy"]["cluster"]
    assert not svc.summarize([a, R("2026-06-02", "buy")])["buy"]["cluster"]
    assert svc.summarize([])["buy"]["last"] is None


def test_close_at_on_or_before():
    dates, closes = ["2026-06-12", "2026-06-15"], [10.0, 11.0]
    assert svc.close_at(dates, closes, "2026-06-14") == 10.0   # 周日的赠与 -> 周五收盘
    assert svc.close_at(dates, closes, "2026-06-15") == 11.0
    assert svc.close_at(dates, closes, "2026-06-01") is None


def test_markers_group_same_day_same_kind_on_close():
    prices = [["2026-06-12", 10.0], ["2026-06-15", 11.0]]
    rows = [R("2026-06-15", "sell", 100.0, key="1"), R("2026-06-15", "sell", 50.0, key="2"),
            R("2026-06-15", "buy", 70.0), R("2026-06-14", "gift", 999.0),
            R("2026-06-13", "sell_plan", 30.0), R("2026-06-01", "buy", 5.0)]
    m = svc.markers(rows, prices)
    assert [(x["date"], x["kind"], x["y"], x["value"]) for x in m] == [
        ("2026-06-13", "sell_plan", 10.0, 30.0),
        ("2026-06-15", "buy", 11.0, 70.0),
        ("2026-06-15", "sell", 11.0, 150.0)]
    assert m[2]["rows"] == [0, 1]                # 指回 rows 下标, 前端 tooltip 用
    assert svc.markers(rows, []) == []


# ---------------------------------------------------------------- 接口

@pytest.fixture
def api(monkeypatch):
    svc._cache.clear()
    monkeypatch.setattr(svc.edgar, "contact_email", lambda: "x@y.z")

    async def info(ticker, email):
        return {"cik": 1818874, "name": "SoFi Technologies, Inc."}
    monkeypatch.setattr(svc.edgar, "company_info", info)
    state = {"ins": {"ticker": "SOFI", "cik": 1818874, "since": "2025-10-05",
                     "rows": [R("2026-06-16", "buy", 250_000, acc="0001613438-26-000016")],
                     "missing": 0},
             "px": [["2026-06-15", 17.5], ["2026-06-16", 17.7]], "calls": 0}

    def build(ticker, email, cik, years):
        state["calls"] += 1
        if isinstance(state["ins"], Exception):
            raise state["ins"]
        return state["ins"]

    def prices(ticker, since):
        if isinstance(state["px"], Exception):
            raise state["px"]
        return state["px"]
    monkeypatch.setattr(svc, "build_insider", build)
    monkeypatch.setattr(svc, "_prices", prices)
    yield state
    svc._cache.clear()


def test_endpoint_shape_and_cache(api):
    d = asyncio.run(svc.insider("sofi", years=1))
    assert d["ticker"] == "SOFI" and d["name"].startswith("SoFi")
    assert d["rows"][0]["url"] == ("https://www.sec.gov/Archives/edgar/data/1818874/"
                                   "000161343826000016/0001613438-26-000016-index.htm")
    assert d["markers"][0]["y"] == 17.7 and d["summary"]["buy"]["n"] == 1
    assert d["warning"] is None
    asyncio.run(svc.insider("SOFI", years=1))
    assert api["calls"] == 1                     # 1 小时内存缓存
    asyncio.run(svc.insider("SOFI", years=2))
    assert api["calls"] == 2                     # 年数不同, 缓存键不同


def test_price_failure_degrades_not_fails(api):
    api["px"] = RuntimeError("yahoo down")
    d = asyncio.run(svc.insider("SOFI", years=1))
    assert d["markers"] == [] and len(d["rows"]) == 1
    assert "取不到股价" in d["warning"]


def test_missing_filings_are_reported(api):
    api["ins"]["missing"] = 3
    d = asyncio.run(svc.insider("SOFI", years=1))
    assert "3 份申报本次没拉到" in d["warning"]


@pytest.mark.parametrize("err,status", [(InsiderError("SEC 接口返回 503", transient=True), 502),
                                        (InsiderError("没有数据"), 404)])
def test_errors_map_to_status(api, err, status):
    # 瞬态失败不能报成"没有数据"
    api["ins"] = err
    with pytest.raises(EdgarError) as e:
        asyncio.run(svc.insider("SOFI", years=1))
    assert e.value.status == status


def test_bad_ticker_rejected(api):
    with pytest.raises(EdgarError) as e:
        asyncio.run(svc.insider("SO FI!", years=1))
    assert e.value.status == 400
