# -*- coding: utf-8 -*-
"""内部人交易取数与分类。合成 XML 的结构照抄真实申报 (SOFI Noto 买入、TSM
员工购股计划、谷歌风投卖 Ethos、真实 4/A 的整份重报), 措辞取自 2026-06~09
EDGAR 全文检索的真实脚注。"""
import json
from datetime import date

import httpx
import pytest

from valuation import fetch_insider as fi

SOFI = 1818874


def form4_xml(*, issuer="0001818874", owners=None, aff="0", rows=(),
              footnotes=None, orig=None) -> bytes:
    owners = owners or [("0001613438", "Noto Anthony",
                         {"isDirector": "1", "isOfficer": "1",
                          "officerTitle": "Chief Executive Officer"})]
    own = "".join(
        f"<reportingOwner><reportingOwnerId><rptOwnerCik>{cik}</rptOwnerCik>"
        f"<rptOwnerName>{name}</rptOwnerName></reportingOwnerId>"
        "<reportingOwnerRelationship>"
        + "".join(f"<{k}>{v}</{k}>" for k, v in rel.items())
        + "</reportingOwnerRelationship></reportingOwner>"
        for cik, name, rel in owners)

    def fn(ids):
        return "".join(f'<footnoteId id="{i}"/>' for i in ids or ())

    txns = "".join(
        "<nonDerivativeTransaction>"
        f"<securityTitle><value>Common Stock</value></securityTitle>"
        f"<transactionDate><value>{r['date']}</value>{fn(r.get('date_fn'))}</transactionDate>"
        f"<transactionCoding><transactionFormType>4</transactionFormType>"
        f"<transactionCode>{r.get('code', 'P')}</transactionCode></transactionCoding>"
        f"<transactionAmounts><transactionShares><value>{r['shares']}</value></transactionShares>"
        f"<transactionPricePerShare><value>{r.get('price', '')}</value></transactionPricePerShare>"
        f"<transactionAcquiredDisposedCode><value>{r.get('ad', 'A')}</value>"
        "</transactionAcquiredDisposedCode></transactionAmounts>"
        "<postTransactionAmounts><sharesOwnedFollowingTransaction>"
        f"<value>{r.get('after', 1000)}</value>{fn(r.get('post_fn'))}"
        "</sharesOwnedFollowingTransaction></postTransactionAmounts>"
        f"<ownershipNature><directOrIndirectOwnership><value>{r.get('direct', 'D')}</value>"
        f"</directOrIndirectOwnership><natureOfOwnership><value>{r.get('nature', '')}</value>"
        "</natureOfOwnership></ownershipNature></nonDerivativeTransaction>"
        for r in rows)
    foot = "".join(f'<footnote id="{k}">{v}</footnote>' for k, v in (footnotes or {}).items())
    return (f"<ownershipDocument><issuer><issuerCik>{issuer}</issuerCik></issuer>{own}"
            + (f"<dateOfOriginalSubmission>{orig}</dateOfOriginalSubmission>" if orig else "")
            + f"<aff10b5One>{aff}</aff10b5One>"
            f"<nonDerivativeTable>{txns}</nonDerivativeTable>"
            f"<footnotes>{foot}</footnotes></ownershipDocument>").encode()


def txn(shares, price, date="2026-06-16", **kw):
    return {"date": date, "shares": shares, "price": price, **kw}


def owner(cik, name="X", **rel):
    return (cik, name, {"isDirector": "1", **rel})


def filing(acc, filed, xml, form="4"):
    return {"acc": acc, "filed": filed, "form": form, "doc": fi.parse_form4(xml)}


def rows_of(filings, since="2025-01-01"):
    return fi.transactions(filings, SOFI, since)


# ---------------------------------------------------------------- 解析

def test_parse_keeps_every_code():
    d = fi.parse_form4(form4_xml(rows=[
        txn(13888, 18.0578),
        txn(500, 18.0, code="S", ad="D"),
        txn(900, 0, code="F", ad="D"),
        txn(1000, "", code="A"),
    ]))
    assert d["issuer_cik"] == SOFI
    assert [r["code"] for r in d["rows"]] == ["P", "S", "F", "A"]
    assert d["rows"][3]["price"] is None
    assert d["owners"][0]["cik"] == "1613438"


@pytest.mark.parametrize("v,want", [("1", True), ("true", True), ("True", True),
                                    ("0", False), ("false", False), ("", False)])
def test_booleans_accept_both_spellings(v, want):
    # GOOG 的 aff10b5One 写 "true" —— 只认 "1" 会把 Pichai 的计划卖出整批判反
    d = fi.parse_form4(form4_xml(aff=v, owners=[("1", "A", {"isDirector": v})],
                                 rows=[txn(1, 1)]))
    assert d["plan"] is want and d["owners"][0]["director"] is want


def test_only_transaction_footnotes_attached_and_nature_normalized():
    d = fi.parse_form4(form4_xml(
        rows=[txn(100, 20, date_fn=["F1"], post_fn=["F2"],
                   nature="Held  through  Silver Lake  Partners IV")],
        footnotes={"F1": "weighted average", "F2": "includes shares acquired under the ESPP"},
        orig="2026-07-29"))
    r = d["rows"][0]
    assert r["notes"] == ["weighted average"]
    assert r["nature"] == "Held through Silver Lake Partners IV"
    assert d["orig_date"] == "2026-07-29"


# ---------------------------------------------------------------- 分类

def _kind(code="P", ad="A", notes=(), nature="", plan=False):
    return fi.classify({"code": code, "ad": ad, "notes": list(notes), "nature": nature}, plan)


PRIVATE_NOTES = [
    "the Reporting Person also purchased 361,905 shares of common stock of the "
    "Issuer in the Private Placement for an aggregate cash purchase price of $380,000",
    "the Reporting Person purchased 115,965 shares of the Issuer's Class B common "
    "stock directly from the Issuer for aggregate cash consideration of $81,176",
    "The securities were acquired from the Issuer pursuant to a Securities Purchase "
    "Agreement dated June 23, 2026",
    "Represents shares purchased directly from the Issuer in connection with the PIPE transaction",
    "Reflects ordinary shares acquired through a directed share program conducted in "
    "connection with the Issuer's initial public offering",
    "included in the 181,750 private placement units of the Issuer purchased by the Sponsor",
    "purchased in the Issuer's underwritten public offering at the public offering price",
]
OPEN_NOTES = [
    "The purchase price of $18.0578 reported in Column 4 is the weighted average purchase "
    "price for the 13,888 shares acquired by the Reporting Person within a range of $18.025 "
    "to $18.070 per share. The Reporting Person hereby undertakes to provide to the Staff "
    "of the SEC, the Issuer or any security holder of the Issuer, upon request",
    "These shares were purchased in multiple transactions at prices ranging from $80.07 "
    "to $81.00, inclusive. The Reporting Person undertakes to provide to the Issuer",
    "The shares were purchased in open market transactions following the initial public offering",
]


@pytest.mark.parametrize("note", PRIVATE_NOTES)
def test_private_purchases(note):
    # 代码 P = 公开市场**或私下**购买
    assert _kind(notes=[note]) == "private"


@pytest.mark.parametrize("note", OPEN_NOTES)
def test_open_market_purchases(note):
    assert _kind(notes=[note]) == "buy"


def test_espp_by_footnote_or_nature():
    assert _kind(notes=["purchased by the administrator of the issuer's Employee Stock "
                        "Purchase Plan"]) == "espp"
    assert _kind(nature="By ESPP Trust") == "espp"


def test_sells_split_by_plan():
    assert _kind("S", "D", plan=True) == "sell_plan"
    # 2023-04 之前的计划没有勾选框, 只写在脚注里
    assert _kind("S", "D", notes=["effected pursuant to a Rule 10b5-1 trading plan "
                                  "adopted on May 1, 2022"]) == "sell_plan"
    assert _kind("S", "D") == "sell"


@pytest.mark.parametrize("code,kind", [("F", "tax"), ("M", "exercise"), ("C", "exercise"),
                                       ("A", "grant"), ("G", "gift"), ("J", "other")])
def test_other_codes(code, kind):
    assert _kind(code, "A" if code in "MCA" else "D") == kind


# ---------------------------------------------------------------- 交易行

def test_other_issuer_excluded():
    # 谷歌风投卖 Ethos 的 Form 4 出现在 Alphabet 的申报列表里
    f = filing("a", "2026-07-29", form4_xml(issuer="0002000000", rows=[txn(10_000, 20)]))
    assert rows_of([f]) == []


def test_window_is_by_trade_date_and_newest_first():
    f = filing("a", "2026-06-20", form4_xml(rows=[
        txn(100, 1, date="2024-12-31"), txn(100, 1, date="2026-06-15"),
        txn(100, 1, date="2026-06-18")]))
    got = rows_of([f], since="2025-01-01")
    assert [r["date"] for r in got] == ["2026-06-18", "2026-06-15"]


def test_espp_cluster_fallback_boundary():
    def many(n):
        return [filing(f"a{i}", "2026-09-09", form4_xml(
            owners=[owner(str(100 + i), f"VP{i}", isOfficer="1")],
            rows=[txn(1_000, 76.2, date="2026-09-07")])) for i in range(n)]
    assert {r["kind"] for r in rows_of(many(5))} == {"espp"}
    assert {r["kind"] for r in rows_of(many(4))} == {"buy"}


def test_identical_rows_in_one_filing_are_two_trades():
    # TSM 一份申报里同日两笔 1,000 股 @77.09 (家庭成员名下)
    f = filing("a", "2026-07-02", form4_xml(rows=[txn(1_000, 77.09), txn(1_000, 77.09)]))
    assert len(rows_of([f])) == 2


def test_amendment_correcting_shares_replaces_original():
    orig = filing("o", "2026-06-16", form4_xml(rows=[txn(30_000, 1.0, date="2026-06-15")]))
    fix = filing("f", "2026-06-20", form4_xml(orig="2026-06-16",
                                              rows=[txn(35_000, 1.0, date="2026-06-15")]), "4/A")
    assert [(r["acc"], r["shares"]) for r in rows_of([orig, fix])] == [("f", 35_000)]


def test_amendment_restating_and_adding_omitted_trade():
    days = ["2026-09-09", "2026-09-10", "2026-09-11"]
    orig = filing("o", "2026-09-11", form4_xml(rows=[txn(100, 1.0, date=d) for d in days]))
    fix = filing("f", "2026-09-14", form4_xml(
        orig="2026-09-11", rows=[txn(100, 1.0, date=d) for d in ["2026-09-08"] + days]), "4/A")
    got = rows_of([orig, fix])
    assert sorted(r["date"] for r in got) == ["2026-09-08"] + days
    assert {r["acc"] for r in got} == {"f"}


def test_partial_amendment_keeps_original_other_days():
    orig = filing("o", "2026-06-17", form4_xml(rows=[txn(100, 1.0, date="2026-06-15"),
                                                      txn(100, 1.0, date="2026-06-16")]))
    fix = filing("f", "2026-06-20", form4_xml(orig="2026-06-17",
                                              rows=[txn(200, 1.0, date="2026-06-16")]), "4/A")
    assert sorted((r["date"], r["acc"]) for r in rows_of([orig, fix])) == \
        [("2026-06-15", "o"), ("2026-06-16", "f")]


def test_amendment_only_touches_its_own_original_and_owner():
    orig = filing("o", "2026-06-17", form4_xml(rows=[txn(100, 1.0, date="2026-06-15")]))
    other_orig = filing("f", "2026-06-20", form4_xml(
        orig="2026-06-01", rows=[txn(100, 1.0, date="2026-06-15")]), "4/A")
    assert len(rows_of([orig, other_orig])) == 2
    other_owner = filing("g", "2026-06-20", form4_xml(
        owners=[owner("2", "B")], orig="2026-06-17",
        rows=[txn(100, 1.0, date="2026-06-15")]), "4/A")
    assert len(rows_of([orig, other_owner])) == 2


def test_amendment_without_orig_date_falls_back_to_earlier_same_owner():
    # 没写 dateOfOriginalSubmission 的 4/A: 按"同一人更早的申报"覆盖同一天的行
    orig = filing("o", "2026-06-16", form4_xml(rows=[txn(100, 1.0, date="2026-06-15")]))
    fix = filing("f", "2026-06-20", form4_xml(rows=[txn(120, 1.0, date="2026-06-15")]), "4/A")
    assert [(r["acc"], r["shares"]) for r in rows_of([orig, fix])] == [("f", 120)]


def test_latest_of_two_amendments_wins():
    rows = [txn(100, 1.0, date="2026-04-14")]
    orig = filing("o", "2026-04-16", form4_xml(rows=rows))
    a1 = filing("a1", "2026-08-31", form4_xml(orig="2026-04-16", rows=rows), "4/A")
    a2 = filing("a2", "2026-08-31", form4_xml(orig="2026-04-16",
                                              rows=[txn(110, 1.0, date="2026-04-14")]), "4/A")
    assert [(r["acc"], r["shares"]) for r in rows_of([a2, orig, a1])] == [("a2", 110)]


def test_amendment_recoding_purchase_removes_it():
    # 原申报误记成 P, 4/A 改成 A: 那天的买入要作废, 换成授予
    orig = filing("o", "2026-07-29", form4_xml(rows=[txn(3363, 1.49, date="2026-07-27")]))
    fix = filing("f", "2026-08-07", form4_xml(
        orig="2026-07-29", rows=[txn(3363, 1.49, date="2026-07-27", code="A")]), "4/A")
    assert [r["kind"] for r in rows_of([orig, fix])] == ["grant"]


def test_late_days_counts_business_days_past_deadline():
    # 周五成交: 下周二申报 = 第 2 个工作日 (不迟); 周三 = 迟 1 天
    on_time = filing("a", "2026-06-23", form4_xml(rows=[txn(100, 1, date="2026-06-19")]))
    late = filing("b", "2026-06-24", form4_xml(rows=[txn(100, 1, date="2026-06-19")]))
    assert rows_of([on_time])[0]["late_days"] == 0
    assert rows_of([late])[0]["late_days"] == 1
    assert fi._busdays("2026-06-19", "2026-06-22") == 1


# ---------------------------------------------------------------- 取数

def _transport(subs: dict, docs: dict, calls: list, fail=False):
    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if fail:
            return httpx.Response(503)
        name = req.url.path.rsplit("/", 1)[-1]
        if req.url.host == "data.sec.gov":
            return httpx.Response(200, json=subs[name])
        return httpx.Response(200, content=docs[name])
    return httpx.MockTransport(handler)


def _subs(entries, files=()):
    return {"filings": {"recent": {
        "form": [e[0] for e in entries], "accessionNumber": [e[1] for e in entries],
        "filingDate": [e[2] for e in entries],
        "primaryDocument": [f"xslF345X06/{e[3]}" for e in entries]},
        "files": list(files)}}


@pytest.fixture
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(fi, "REQUEST_GAP", 0)
    monkeypatch.setattr(fi, "CACHE_DIR", tmp_path / "insider_cache")
    monkeypatch.setattr(fi.time, "sleep", lambda s: None)
    return tmp_path


def test_build_uses_disk_cache_and_skips_old_and_non_form4(fast):
    subs = {"CIK0001818874.json": _subs([
        ("4", "0001-26-2", "2026-10-02", "b.xml"),
        ("144", "0001-26-9", "2026-10-01", "x.xml"),
        ("4", "0001-26-1", "2026-06-16", "a.xml"),
        ("4", "0001-24-1", "2024-01-05", "old.xml")])}
    docs = {"a.xml": form4_xml(rows=[txn(13_888, 18.06)]),
            "b.xml": form4_xml(rows=[txn(500, 17.0, date="2026-10-01", code="S", ad="D")])}
    calls = []
    d = fi.build_insider("SOFI", "x@y.z", SOFI, 1, today=date(2026, 10, 5),
                         transport=_transport(subs, docs, calls))
    assert [r["kind"] for r in d["rows"]] == ["sell", "buy"]
    assert d["missing"] == 0 and len(calls) == 3        # submissions + 2 份 XML
    calls.clear()
    fi.build_insider("SOFI", "x@y.z", SOFI, 1, today=date(2026, 10, 5),
                     transport=_transport(subs, {}, calls))
    assert len(calls) == 1                               # 只剩 submissions


def test_build_pages_older_submissions(fast):
    subs = {"CIK0001818874.json": _subs([("4", "0001-26-1", "2026-06-16", "a.xml")],
                                        files=[{"name": "CIK0001818874-submissions-001.json",
                                                "filingFrom": "2024-01-01", "filingTo": "2026-01-31"}]),
            "CIK0001818874-submissions-001.json": {
                "form": ["4"], "accessionNumber": ["0001-25-1"],
                "filingDate": ["2025-12-01"], "primaryDocument": ["c.xml"]}}
    docs = {"a.xml": form4_xml(rows=[txn(100, 1.0)]),
            "c.xml": form4_xml(rows=[txn(100, 1.0, date="2025-11-28")])}
    d = fi.build_insider("SOFI", "x@y.z", SOFI, 1, today=date(2026, 10, 5),
                         transport=_transport(subs, docs, []))
    assert [r["date"] for r in d["rows"]] == ["2026-06-16", "2025-11-28"]


def test_build_caps_fetches_and_reports_missing(fast, monkeypatch):
    monkeypatch.setattr(fi, "MAX_FETCH", 1)
    subs = {"CIK0001818874.json": _subs([("4", "0001-26-2", "2026-10-02", "b.xml"),
                                         ("4", "0001-26-1", "2026-06-16", "a.xml")])}
    docs = {"a.xml": form4_xml(rows=[txn(100, 1.0)]),
            "b.xml": form4_xml(rows=[txn(100, 1.0, date="2026-10-01")])}
    d = fi.build_insider("SOFI", "x@y.z", SOFI, 1, today=date(2026, 10, 5),
                         transport=_transport(subs, docs, []))
    assert d["missing"] == 1 and len(d["rows"]) == 1


def test_build_sec_down_is_transient(fast):
    with pytest.raises(fi.InsiderError) as e:
        fi.build_insider("SOFI", "x@y.z", SOFI, 1, today=date(2026, 10, 5),
                         transport=_transport({}, {}, [], fail=True))
    assert e.value.transient
