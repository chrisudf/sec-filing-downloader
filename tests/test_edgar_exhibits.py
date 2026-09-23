# -*- coding: utf-8 -*-
"""10-K 以引用方式并入年报（EX-13）时自动带上附件。

动机：IBM 的 10-K 主文档只有封面/风险因素/交叉引用，财务报表、附注、关键会计
估计全在同一提交的 EX-13 里。只下主文档时判断层估了一个本该从税务附注里读出来
的数（2025Q4 约 $2B 税收利得），还声明"已核对 10-K 关键会计估计段"。
"""
import asyncio
import csv
import io
import zipfile

import httpx

from app import edgar

ACC = "0000051143-26-000010"
ACC_ND = ACC.replace("-", "")

IBM_10K = (b"<html><body><p>Documents incorporated by reference: Portions of IBM&#8217;s "
           b"Annual Report to Stockholders for the year ended December 31, 2025 are "
           b"incorporated by reference into Parts I, II and IV of this Form 10-K.</p>"
           b"</body></html>")
PLAIN_10K = (b"<html><body><p>Exhibit 3.1 is incorporated herein by reference to the "
             b"Form 8-K.</p><h2>Notes to Consolidated Financial Statements</h2></body></html>")

# 真实 -index.htm 的文档表形态（节选）：EX-13 文件名毫无规律，只能看 Type 列
INDEX_HTML = f"""
<table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>10-K</td><td><a href="/ix?doc=/Archives/edgar/data/51143/{ACC_ND}/ibm-20251231.htm">ibm-20251231.htm</a> iXBRL</td><td>10-K</td><td>1077083</td></tr>
<tr class="evenRow"><td>5</td><td>EX-13</td><td><a href="/ix?doc=/Archives/edgar/data/51143/{ACC_ND}/ibm-20251231_d2.htm">ibm-20251231_d2.htm</a> &nbsp;&nbsp;iXBRL</td><td>EX-13</td><td>4525399</td></tr>
<tr><td>6</td><td>EX-21</td><td><a href="/Archives/edgar/data/51143/{ACC_ND}/ibm-20251231x10kex21.htm">ibm-20251231x10kex21.htm</a></td><td>EX-21</td><td>91492</td></tr>
<tr><td>19</td><td>GRAPHIC</td><td><a href="/Archives/edgar/data/51143/{ACC_ND}/ibm-20251231_g1.jpg">ibm-20251231_g1.jpg</a></td><td>GRAPHIC</td><td>30416</td></tr>
</table>
"""


def test_incorporates_annual_report_ibm_shape():
    assert edgar._incorporates_annual_report(IBM_10K)


def test_plain_10k_not_flagged():
    # 普通 10-K 里 "incorporated by reference" 引用附件/委托书很常见，单凭它不触发
    assert not edgar._incorporates_annual_report(PLAIN_10K)


def test_phrase_split_by_tags_and_nbsp():
    doc = (b"<p>IBM&#8217;s 2025 <span>Annual&nbsp;Report</span> to "
           b"Stockholders</p><p>incorporated\nherein by reference</p>")
    assert edgar._incorporates_annual_report(doc)


def test_index_exhibits_by_type_column():
    assert edgar._index_exhibits(INDEX_HTML, "EX-13") == ["ibm-20251231_d2.htm"]
    assert edgar._index_exhibits(INDEX_HTML, "EX-21") == ["ibm-20251231x10kex21.htm"]
    assert edgar._index_exhibits(INDEX_HTML, "EX-99") == []


def test_index_exhibits_subnumbered_type():
    html = INDEX_HTML.replace("<td>EX-13</td><td>4525399", "<td>EX-13.1</td><td>4525399")
    assert edgar._index_exhibits(html, "EX-13") == ["ibm-20251231_d2.htm"]
    # EX-130 这类不是 EX-13
    html = INDEX_HTML.replace("<td>EX-13</td><td>4525399", "<td>EX-130</td><td>4525399")
    assert edgar._index_exhibits(html, "EX-13") == []


def _run_pack(primary_body, monkeypatch):
    monkeypatch.setattr(edgar, "REQUEST_GAP", 0)
    seen = []

    def handler(req: httpx.Request):
        url = str(req.url)
        seen.append(url)
        if url.endswith(f"{ACC}-index.htm"):
            return httpx.Response(200, text=INDEX_HTML)
        if url.endswith("ibm-20251231_d2.htm"):
            return httpx.Response(200, content=b"<html>Notes to Consolidated Financial "
                                               b"Statements ... income taxes</html>")
        if url.endswith("ibm-20251231.htm"):
            return httpx.Response(200, content=primary_body)
        return httpx.Response(404)

    row = {"accessionNumber": ACC, "form": "10-K", "primaryDocument": "ibm-20251231.htm",
           "reportDate": "2025-12-31", "filingDate": "2026-02-24"}

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await edgar._pack(c, 51143, "IBM", [row])

    data, n = asyncio.run(go())
    zf = zipfile.ZipFile(io.BytesIO(data))
    manifest = list(csv.DictReader(io.StringIO(zf.read("manifest.csv").decode("utf-8"))))
    return zf.namelist(), manifest, seen


def test_pack_brings_ex13_when_incorporated(monkeypatch):
    names, manifest, _ = _run_pack(IBM_10K, monkeypatch)
    assert "IBM_10-K_2025-12-31.htm" in names
    assert "IBM_10-K_2025-12-31_ex13_ibm-20251231_d2.htm" in names
    ex = [r for r in manifest if "ex13" in r["file"]][0]
    assert ex["form"].startswith("10-K (exhibit 13")
    assert ex["reportDate"] == "2025-12-31"          # 与主文档同报告期，时效块不受影响
    assert ex["sourceUrl"].endswith(f"/{ACC_ND}/ibm-20251231_d2.htm")


def test_pack_plain_10k_makes_no_extra_request(monkeypatch):
    names, manifest, seen = _run_pack(PLAIN_10K, monkeypatch)
    assert names == ["IBM_10-K_2025-12-31.htm", "manifest.csv"]
    assert not any("index" in u for u in seen)       # 判据不成立：零额外请求
