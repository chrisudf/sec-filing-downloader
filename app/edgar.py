"""SEC EDGAR 客户端：代码查询、公司信息、财报下载与打包。

数据源全部来自 SEC 官方公开接口：
- 代码->CIK 映射: https://www.sec.gov/files/company_tickers.json
- 公司提交记录:   https://data.sec.gov/submissions/CIK##########.json
- 文件原件:       https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}

SEC 合规要求：
- User-Agent 必须包含联系方式（邮箱），否则会被拒绝或限流
- 请求频率上限 10 次/秒，这里每次下载之间留 0.12s 间隔
"""
from __future__ import annotations

import asyncio
import csv
import io
import time
import zipfile
from datetime import date, timedelta

import httpx

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"
MAX_EXHIBITS = 8

QUARTER_FORMS = ("10-Q", "6-K")   # 季报（6-K 为中概股等外国发行人）
ANNUAL_FORMS = ("10-K", "20-F")   # 年报（20-F 为外国发行人）

REQUEST_GAP = 0.12   # SEC 限速 10 req/s，留出余量
MAX_FILES = 60       # 单次打包上限，防止误选超大范围
TICKER_TTL = 24 * 3600

_ticker_cache: dict = {"map": None, "ts": 0.0}


class EdgarError(Exception):
    """带 HTTP 状态码的业务异常，直接透传给前端展示。"""

    def __init__(self, status: int, msg: str):
        self.status = status
        self.msg = msg
        super().__init__(msg)


def _client(email: str) -> httpx.AsyncClient:
    headers = {"User-Agent": f"sec-filing-downloader/0.1 ({email})"}
    return httpx.AsyncClient(headers=headers, timeout=30.0, follow_redirects=True)


async def _get_json(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url)
    if resp.status_code == 403:
        raise EdgarError(502, "SEC 拒绝了请求（403），请确认邮箱格式真实有效")
    resp.raise_for_status()
    return resp.json()


async def _ticker_map(client: httpx.AsyncClient) -> dict[str, dict]:
    now = time.time()
    if _ticker_cache["map"] is None or now - _ticker_cache["ts"] > TICKER_TTL:
        raw = await _get_json(client, TICKERS_URL)
        _ticker_cache["map"] = {v["ticker"].upper(): v for v in raw.values()}
        _ticker_cache["ts"] = now
    return _ticker_cache["map"]


async def _submissions(client: httpx.AsyncClient, ticker: str) -> tuple[int, dict]:
    mapping = await _ticker_map(client)
    entry = mapping.get(ticker)
    if entry is None:
        raise EdgarError(404, f"SEC EDGAR 中未找到股票代码 {ticker}")
    cik = int(entry["cik_str"])
    subs = await _get_json(client, SUBMISSIONS_URL.format(name=f"CIK{cik:010d}.json"))
    return cik, subs


def quarter_start(year: int, quarter: int) -> date:
    return date(year, 3 * (quarter - 1) + 1, 1)


def quarter_end(year: int, quarter: int) -> date:
    if quarter == 4:
        return date(year, 12, 31)
    return quarter_start(year, quarter + 1) - timedelta(days=1)


def _rows(block: dict) -> list[dict]:
    keys = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument", "size")
    return [{k: block[k][i] for k in keys} for i in range(len(block["form"]))]


def _is_quarter_end(d: str) -> bool:
    """精确的季度末日（3/6/9/12 月最后一天）。TSM 类发行人的业绩 6-K
    reportDate 就是季度末；BABA 类用发布日，识别它们需要全文搜索（见 TODO）。"""
    try:
        dt = date.fromisoformat(d)
    except (ValueError, TypeError):
        return False
    return dt.month in (3, 6, 9, 12) and (dt + timedelta(days=1)).month != dt.month


def _pick_latest_6k(matches: list[dict], count: int) -> list[dict]:
    """6-K 智能挑选：外国发行人把新闻稿、人事公告、月度营收都按 6-K 提交，
    真正的季度业绩 6-K 特征是：报告期=季度末 + 文件体积远大于杂项公告。
    同一报告期只留最大的一份，优先季度末报告期。"""
    by_period: dict[str, dict] = {}
    for r in matches:
        key = r["reportDate"] or r["filingDate"]
        if key not in by_period or (r.get("size") or 0) > (by_period[key].get("size") or 0):
            by_period[key] = r
    candidates = list(by_period.values())
    qend = [r for r in candidates if _is_quarter_end(r["reportDate"])]
    pool = qend if qend else candidates
    pool.sort(key=lambda r: r["reportDate"] or r["filingDate"], reverse=True)
    return pool[:count]


async def company_info(ticker: str, email: str) -> dict:
    ticker = ticker.strip().upper()
    async with _client(email) as client:
        cik, subs = await _submissions(client, ticker)

    # recent 数组按提交时间倒序，取每种表单的第一条即最新
    latest: dict[str, dict] = {}
    for row in _rows(subs["filings"]["recent"]):
        form = row["form"]
        if form in QUARTER_FORMS + ANNUAL_FORMS and form not in latest:
            latest[form] = {"filingDate": row["filingDate"], "reportDate": row["reportDate"]}

    return {
        "ticker": ticker,
        "cik": cik,
        "name": subs.get("name") or "",
        "exchanges": [e for e in (subs.get("exchanges") or []) if e],
        "website": subs.get("website") or "",
        "investorWebsite": subs.get("investorWebsite") or "",
        "fiscalYearEnd": subs.get("fiscalYearEnd") or "",
        "latest": latest,
    }


async def _list_filings(
    client: httpx.AsyncClient, subs: dict, forms: list[str], dstart: date, dend: date
) -> list[dict]:
    rows = _rows(subs["filings"]["recent"])

    # recent 只含最近约 1000 条，更早的按需拉分页文件
    for extra in subs["filings"].get("files", []):
        if extra["filingTo"] >= dstart.isoformat() and extra["filingFrom"] <= dend.isoformat():
            older = await _get_json(client, SUBMISSIONS_URL.format(name=extra["name"]))
            rows += _rows(older)

    picked = []
    for row in rows:
        if row["form"] not in forms:
            continue
        basis = row["reportDate"] or row["filingDate"]
        try:
            d = date.fromisoformat(basis)
        except ValueError:
            continue
        if dstart <= d <= dend:
            picked.append(row)

    picked.sort(key=lambda r: r["reportDate"] or r["filingDate"])
    return picked


async def build_zip(
    ticker: str, email: str, forms: list[str], dstart: date, dend: date
) -> tuple[bytes, int]:
    """下载区间内所有匹配财报，重命名后打成 zip，附 manifest.csv。"""
    ticker = ticker.strip().upper()
    async with _client(email) as client:
        cik, subs = await _submissions(client, ticker)
        picked = await _list_filings(client, subs, forms, dstart, dend)

        if not picked:
            raise EdgarError(404, "该时间范围内没有找到符合条件的财报文件")
        if len(picked) > MAX_FILES:
            raise EdgarError(
                400, f"匹配到 {len(picked)} 个文件，超过单次 {MAX_FILES} 个上限，请缩小时间范围"
            )
        return await _pack(client, cik, ticker, picked)


async def build_zip_latest(
    ticker: str, email: str, form_groups: list[tuple[str, ...]], count: int
) -> tuple[bytes, int]:
    """每组表单（季报组/年报组）各取最新 count 份，打包返回。

    按报告期倒序取，10-K/20-F、10-Q/6-K 同组互斥（美国公司只会有前者，
    外国发行人只会有后者），所以组内直接取最新即可。
    注意：6-K 里最新的一份未必是业绩公告（可能是新闻稿），智能过滤在路线图里。
    """
    ticker = ticker.strip().upper()
    async with _client(email) as client:
        cik, subs = await _submissions(client, ticker)
        rows = _rows(subs["filings"]["recent"])

        picked: list[dict] = []
        seen: set[str] = set()
        for group in form_groups:
            matches = [r for r in rows if r["form"] in group]
            six_k = [r for r in matches if r["form"] == "6-K"]
            if six_k and len(six_k) == len(matches):
                top = _pick_latest_6k(six_k, count)
            else:
                matches.sort(key=lambda r: r["reportDate"] or r["filingDate"], reverse=True)
                top = matches[:count]
            for row in top:
                if row["accessionNumber"] not in seen:
                    seen.add(row["accessionNumber"])
                    picked.append(row)

        if not picked:
            raise EdgarError(404, "没有找到符合条件的财报文件")
        picked.sort(key=lambda r: r["reportDate"] or r["filingDate"])
        return await _pack(client, cik, ticker, picked)


async def _6k_exhibits(client: httpx.AsyncClient, cik: int, acc: str, primary: str) -> list[str]:
    """6-K 的正文往往只是封面信，季度业绩在同一提交的 EX-99 附件里，
    从 index.json 把附件 htm 一并带上（跳过图片/XBRL 等资源文件）。"""
    try:
        idx = await _get_json(client, INDEX_URL.format(cik=cik, acc=acc))
        items = idx.get("directory", {}).get("item", [])
    except Exception:
        return []
    out = []
    for it in items:
        name = it.get("name", "")
        low = name.lower()
        if name == primary or "index" in low or not low.endswith((".htm", ".html")):
            continue
        out.append(name)
    return out[:MAX_EXHIBITS]


async def _pack(
    client: httpx.AsyncClient, cik: int, ticker: str, picked: list[dict]
) -> tuple[bytes, int]:
    buf = io.BytesIO()
    manifest: list[dict] = []
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in picked:
            acc = row["accessionNumber"].replace("-", "")
            # 早期文件没有 primaryDocument，退回完整提交文本
            doc = row["primaryDocument"] or f"{row['accessionNumber']}.txt"
            url = DOC_URL.format(cik=cik, acc=acc, doc=doc)

            await asyncio.sleep(REQUEST_GAP)
            resp = await client.get(url)
            resp.raise_for_status()

            period = row["reportDate"] or row["filingDate"]
            ext = "." + doc.rsplit(".", 1)[-1] if "." in doc else ".txt"
            name = f"{ticker}_{row['form'].replace('/', '')}_{period}{ext}"
            if name in used:
                name = f"{ticker}_{row['form'].replace('/', '')}_{period}_{acc[-6:]}{ext}"
            used.add(name)

            zf.writestr(name, resp.content)
            manifest.append(
                {
                    "file": name,
                    "form": row["form"],
                    "reportDate": row["reportDate"],
                    "filingDate": row["filingDate"],
                    "accessionNumber": row["accessionNumber"],
                    "sourceUrl": url,
                }
            )

            if row["form"] == "6-K" and row["primaryDocument"]:
                for ex in await _6k_exhibits(client, cik, acc, row["primaryDocument"]):
                    ex_url = DOC_URL.format(cik=cik, acc=acc, doc=ex)
                    await asyncio.sleep(REQUEST_GAP)
                    ex_resp = await client.get(ex_url)
                    if ex_resp.status_code != 200:
                        continue
                    ex_name = f"{ticker}_6-K_{period}_ex_{ex.rsplit('/', 1)[-1]}"
                    if ex_name in used:
                        continue
                    used.add(ex_name)
                    zf.writestr(ex_name, ex_resp.content)
                    manifest.append(
                        {
                            "file": ex_name,
                            "form": "6-K (exhibit)",
                            "reportDate": row["reportDate"],
                            "filingDate": row["filingDate"],
                            "accessionNumber": row["accessionNumber"],
                            "sourceUrl": ex_url,
                        }
                    )

        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(manifest[0].keys()))
        writer.writeheader()
        writer.writerows(manifest)
        zf.writestr("manifest.csv", out.getvalue())

    return buf.getvalue(), len(picked)
