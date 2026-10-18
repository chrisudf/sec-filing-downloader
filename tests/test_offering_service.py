# -*- coding: utf-8 -*-
"""最近定价增发服务（app/offering_service.py）：选取、落盘缓存、逐份容错与告警上浮。

网络全部走 httpx.MockTransport；封面内容用 tests/fixtures/offering_covers 的真实样本。
"""
import asyncio
import html
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from app import edgar
from app import offering_service as svc

FIX = Path(__file__).parent / "fixtures" / "offering_covers"
CIK = 50863


def page(name: str) -> bytes:
    text = (FIX / f"{name}.txt").read_text(encoding="utf-8")
    return f"<html><body><p>{html.escape(text)}</p></body></html>".encode()


def row(acc, form, filed, doc):
    return {"accessionNumber": acc, "form": form, "filingDate": filed,
            "reportDate": "", "primaryDocument": doc, "size": 1}


def recent(rows):
    keys = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument", "size")
    out = {k: [r[k] for r in rows] for k in keys}
    out["items"] = [r.get("items", "") for r in rows]
    return out


BASE_ROWS = [
    row("0000000000-26-000004", "424B5", "2026-09-01", "atm.htm"),
    row("0000000000-26-000003", "424B5", "2026-08-12", "final.htm"),
    row("0000000000-26-000002", "424B5", "2026-08-10", "prelim.htm"),
    row("0000000000-24-000001", "424B5", "2024-02-16", "debt.htm"),
    row("0000000000-26-000009", "10-Q", "2026-07-25", "q.htm"),
]
DOCS = {"atm.htm": "aep_2020_atm", "final.htm": "intc_2026_priced",
        "prelim.htm": "intc_2026_prelim", "debt.htm": "intc_2024_debt",
        "rk26.htm": "rklb_2026_atm", "rk25a.htm": "rklb_2025_atm_amend", "nnn.htm": "nnn_2018_atm_shares"}
SALES_FIX = Path(__file__).parent / "fixtures" / "atm_sales"


# 假行情：申报日当天或之前最近一个交易日的收盘（2025-03-11 → 03-10，2025-05-30 → 05-29）
CLOSES = {"2025-03-10": 17.12, "2025-05-29": 25.4, "2026-05-20": 134.28, "2026-08-12": 79.5,
          "2026-08-13": 81.2, "2026-09-01": 100.0}


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "_yahoo_closes", lambda t, start, end: dict(CLOSES))
    monkeypatch.setattr(edgar, "REQUEST_GAP", 0)
    monkeypatch.setattr(edgar, "_ticker_cache", {"map": None, "ts": 0.0})
    monkeypatch.setattr(svc, "CACHE_DIR", tmp_path / "offering_cache")
    svc._offer_cache.clear()


def make_handler(rows, docs=DOCS, fail=(), hits=None):
    hits = hits if hits is not None else []

    def handler(req: httpx.Request):
        url = str(req.url)
        if url == edgar.TICKERS_URL:
            return httpx.Response(200, json={"0": {"cik_str": CIK, "ticker": "INTC", "title": "INTEL CORP"}})
        if url.endswith(f"CIK{CIK:010d}.json"):
            return httpx.Response(200, json={"name": "INTEL CORP",
                                             "filings": {"recent": recent(rows), "files": []}})
        doc = url.rsplit("/", 1)[-1]
        if doc in fail:
            hits.append(doc)
            return httpx.Response(500)
        if doc in docs:
            hits.append(doc)
            return httpx.Response(200, content=page(docs[doc]))
        if doc == "index.json":
            hits.append(url)
            return httpx.Response(200, json={"directory": {"item": [
                {"name": "pr_8k.htm"}, {"name": "pr_ex99-1.htm"}]}})
        if doc == "pr_ex99-1.htm":
            hits.append(doc)
            body = (SALES_FIX / "rklb_2026_8k_ex99.txt").read_text(encoding="utf-8")
            return httpx.Response(200, content=f"<html><body><p>{html.escape(body)}</p></body></html>".encode())
        if doc == "q.htm":
            hits.append(doc)
            body = (SALES_FIX / "rklb_2026q2.txt").read_text(encoding="utf-8")
            return httpx.Response(200, content=f"<html><body><p>{html.escape(body)}</p></body></html>".encode())
        return httpx.Response(404)

    return handler, hits


def run(handler, today=date(2026, 10, 17)):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await svc.find_offering(c, "INTC", today=today)
    return asyncio.run(go())


def test_picks_newest_priced_and_stops():
    handler, hits = make_handler(BASE_ROWS)
    r = run(handler)
    o = r["offering"]
    assert o["price"] == 95.0 and o["date"] == "2026-08-10" and o["date_kind"] == "定价日"
    assert o["discount_pct"] == -2.58 and o["who_label"] == "一级发行"
    assert o["stabilizing"] is False
    assert o["url"].endswith("000000000026000003/0000000000-26-000003-index.htm")
    # ATM 被拒后读到终版即停：初步版、发债、10-Q 都不下载
    assert hits == ["atm.htm", "final.htm"]
    assert r["candidates"] == 4 and r["scanned"] == 2 and r["warning"] is None
    # 比定价增发更新的 ATM 一并带回（前端显示「之后又开过」）
    assert [(a["date"], a["usd"], a["amendment"]) for a in r["atm_programs"]] == \
        [("2026-09-01", 1_000_000_000.0, False)]


def test_disk_cache_avoids_refetch():
    handler, hits = make_handler(BASE_ROWS)
    run(handler)
    hits.clear()
    svc._offer_cache.clear()
    assert run(handler)["offering"]["price"] == 95.0
    assert hits == []


def test_stabilizing_window():
    handler, _ = make_handler(BASE_ROWS[1:])
    o = run(handler, today=date(2026, 8, 14))["offering"]
    assert o["trading_days_since"] == 4 and o["stabilizing"] is True and o["drift_window"] is True


def _add_weekdays(d, n):
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


@pytest.mark.parametrize("days, stabilizing, drift", [
    (30, True, True), (31, False, True), (120, False, True), (121, False, False)])
def test_tag_window_boundaries(days, stabilizing, drift):
    # 前端按这两个开关分三档文案：≤30 个交易日提示不托底、≤120 提示跑输窗口、之后只说
    # 不是压力/支撑位。阈值来自 10-19 全样本研究的 30 日稳定期与 120 日跑输窗口
    r = row("0000000000-26-000003", "424B5", "2026-08-12", "final.htm")
    c = {"price": 95.0, "who": "primary", "pricing_date": "2026-08-10"}
    o = svc._shape(r, CIK, c, _add_weekdays(date(2026, 8, 10), days))
    assert (o["trading_days_since"], o["stabilizing"], o["drift_window"]) == (days, stabilizing, drift)


def test_note_heavy_forms_skipped_with_warning():
    notes = [row(f"0000000000-26-1{i:05d}", "424B3", "2026-09-15", f"n{i}.htm") for i in range(151)]
    handler, hits = make_handler(notes + BASE_ROWS)
    r = run(handler)
    assert r["offering"]["price"] == 95.0
    assert "424B3" in r["warning"] and not any(h.startswith("n") for h in hits)


def test_scan_cap_is_surfaced(monkeypatch):
    monkeypatch.setattr(svc, "MAX_SCAN", 1)
    handler, _ = make_handler(BASE_ROWS)
    r = run(handler)
    assert r["offering"] is None
    assert "只扫描了最新 1 份" in r["warning"]


def test_single_download_failure_does_not_kill_line():
    handler, hits = make_handler(BASE_ROWS, fail=("atm.htm",))
    r = run(handler)
    assert r["offering"]["price"] == 95.0
    assert "1 份招股书下载失败" in r["warning"]


def test_endpoint_uppercases_and_caches(monkeypatch):
    handler, hits = make_handler(BASE_ROWS)
    monkeypatch.setattr(edgar, "contact_email", lambda: "test@example.com")
    monkeypatch.setattr(edgar, "_client",
                        lambda email: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = asyncio.run(svc.offering("intc"))
    assert r["ticker"] == "INTC" and r["offering"]["price"] == 95.0
    hits.clear()
    asyncio.run(svc.offering("INTC"))   # 内存缓存命中，不再请求
    assert hits == []


def test_atm_only_issuer_lists_programs():
    # RKLB 型：近 3 年只开 ATM，没有定价增发——不能显示成「没有增发」
    rows = [row("0000000000-26-000020", "424B5", "2026-08-13", "rk26.htm"),
            row("0000000000-25-000019", "424B5", "2025-05-30", "rk25a.htm"),
            row("0000000000-25-000018", "424B5", "2025-03-11", "nnn.htm")]
    handler, hits = make_handler(rows)
    r = run(handler)
    assert r["offering"] is None
    assert [(a["date"], a["usd"], a["shares"], a["amendment"]) for a in r["atm_programs"]] == [
        ("2026-08-13", 1_944_369_826.0, None, False),
        ("2025-05-30", 500_000_000.0, None, True),
        ("2025-03-11", None, 12_000_000, False),
    ]
    # 申报日收盘：当天是交易日取当天，否则取之前最近一个交易日
    assert [(a["close"], a["close_date"], a["close_src"]) for a in r["atm_programs"]] == [
        (81.2, "2026-08-13", "行情"), (25.4, "2025-05-29", "行情"), (17.12, "2025-03-10", "行情")]
    assert r["scanned"] == 3 and r["warning"] is None


def test_atm_price_falls_back_to_cover(monkeypatch):
    def boom(*a):
        raise RuntimeError("Yahoo 卡住")
    monkeypatch.setattr(svc, "_yahoo_closes", boom)
    rows = [row("0000000000-26-000020", "424B5", "2026-08-13", "rk26.htm")]
    handler, _ = make_handler(rows)
    r = run(handler)
    a = r["atm_programs"][0]
    # RKLB 封面那句「On August 10, 2026, the last reported sale price … was $80.04」在第 ~17,000 字
    assert (a["close"], a["close_date"], a["close_src"]) == (80.04, "2026-08-10", "封面")
    assert r["warning"] == "行情获取失败，ATM 参考价改用封面写的前收盘价"


def test_atm_sales_from_latest_periodic_report():
    # 有 ATM 时读最近一份（且不早于最早 ATM）的 10-Q，抽实际成交；申报日收盘只是参考价
    rows = [row("0000000000-26-000020", "424B5", "2026-08-13", "rk26.htm"),
            {**row("0000000000-26-000030", "10-Q", "2026-08-10", "q.htm"), "reportDate": "2026-06-30"},
            row("0000000000-26-000010", "424B5", "2026-05-20", "atm.htm")]
    handler, hits = make_handler(rows)
    r = run(handler)
    a = r["atm_programs"][0]
    assert a["rollover"] is True and a["prior_sold_usd"] == 1_055_630_173.0
    s = r["atm_sales"]
    assert (s["report"]["form"], s["report"]["report_date"]) == ("10-Q", "2026-06-30")
    assert sorted((e["kind"], e["shares"], e["avg_price"]) for e in s["entries"]) == sorted([
        ("sale", 6_726_862, 70.47), ("sale", 7_783_458, 135.62), ("collar", 7_451_200, 63.61)])
    hits.clear()
    svc._offer_cache.clear()
    run(handler)                         # 10-Q 解析结果按 accession 落盘
    assert "q.htm" not in hits


def test_no_periodic_report_after_atm():
    # 只有一份 ATM、之后还没出定期报告：不去读更早的报告（里面不可能有这份额度的成交）
    handler, _ = make_handler([row("0000000000-26-000020", "424B5", "2026-08-13", "rk26.htm")])
    assert run(handler)["atm_sales"] is None


def test_atm_sales_timeline_includes_8k_after_report():
    # RKLB 2026 全年：3 月协议（含领口远期）→ 5 月协议 → 8-13 续展余额在 9-15 卖完（只在 8-K）
    rows = [row("0000000000-26-000040", "8-K", "2026-09-15", "pr_8k.htm"),
            row("0000000000-26-000020", "424B5", "2026-08-13", "rk26.htm"),
            {**row("0000000000-26-000030", "10-Q", "2026-08-10", "q.htm"), "reportDate": "2026-06-30"},
            row("0000000000-26-000012", "424B5", "2026-05-20", "atm.htm"),
            row("0000000000-26-000011", "424B5", "2026-03-17", "nnn.htm")]
    rows[0]["items"] = "7.01,8.01,9.01"
    handler, _ = make_handler(rows)
    s = run(handler)["atm_sales"]
    got = [(e["window_start"], e["window_end"], e["kind"], e["shares"], e["avg_price"], e["source_form"],
            e["program_rollover"]) for e in s["entries"]]
    assert got == [
        ("2026-03-17", "2026-05-20", "sale", 6_726_862, 70.47, "10-Q", False),
        ("2026-03-17", "2026-05-20", "collar", 7_451_200, 63.61, "10-Q", False),
        ("2026-05-20", "2026-06-30", "sale", 7_783_458, 135.62, "10-Q", False),
        ("2026-08-13", "2026-09-15", "sale", 29_300_000, 66.35, "8-K", True),
    ]
    assert s["eightk_scanned"] == 1
