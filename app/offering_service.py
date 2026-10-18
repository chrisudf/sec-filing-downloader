# -*- coding: utf-8 -*-
"""最近一次定价增发 + 期间开过的 ATM 额度：仪表盘顶部的一行信息。

GET /api/offering/{ticker}
扫描近 LOOKBACK_YEARS 年的 424B1/2/3/4/5/7，按申报日从新到旧逐份读主文档前
COVER_BYTES 字节，交给 offering_cover.parse_cover；第一份「固定价格的股票发行」
即停。途中遇到的 ATM 补充文件（比那笔定价增发更新的，或没有定价增发时窗口内
全部）记进 atm_programs——RKLB 这类只靠 ATM 融资的公司，没有这部分会显示成
「没有增发」，是误导。识别「续展」（只承接旧协议未卖完的额度，不算新额度）。

ATM 按市价陆续卖出，申报日收盘只是参考价（yfinance 一次取齐，拆股调整口径；取不到
退回封面前收盘）；实际成交股数与均价取自最近一份 10-Q/10-K 附注（app/atm_sales.py，
尽力而为，解析不到只给链接）。RKLB 2026-08 的教训：申报日收盘 $80.10 被当成「增发价」，
而那份是续展，最近实际成交是 2026Q2 均价 $135.62。单线程顺序请求、每份间隔 REQUEST_GAP（README 的 SEC 合规约定）；
逐份解析结果按 accession 落盘（jobs/offering_cache/），同一份申报只下载一次。

为什么只做一行、不画成关键价位：2026-10-17 的 offering-price-study（R1000
2010-2026，1,166 笔定价增发）预注册检验显示，增发价既不是顶部信号（增发后
60 日 CAR 比匹配对照 +2.1%），也不是持续的压力/支撑位（挡回率只比随机价位
高 1.1pp，门槛 8pp）；唯一可见的效应是发行后约 5 个交易日收盘扎堆在发行价
（承销商托底，T+1 为相邻档 1.6 倍）。所以这里只给事实和托底期提示。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter

from . import edgar
from .common import get_or_fetch, validate_ticker
from . import atm_sales
from .offering_cover import PARSE_VER, html_to_text, parse_cover

router = APIRouter()

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "jobs" / "offering_cache"

FORMS = frozenset({"424B1", "424B2", "424B3", "424B4", "424B5", "424B7"})
LOOKBACK_YEARS = 3
MAX_SCAN = 40          # 单次最多读多少份封面（冷启动约 MAX_SCAN×0.3 秒）
NOTE_HEAVY = 150       # 424B2/424B3 超过这个数 = 票据/存单定价补充发行人，跳过这两类
COVER_BYTES = 100_000  # 封面标记都在原始 HTML 前 ~70KB（研究实测）
STABILIZE_DAYS = 5     # 托底效应可见的交易日数
PRICE_TIMEOUT = 30     # yfinance 自身无超时，卡住会占死请求
REPORT_BYTES = 8_000_000   # 10-Q/10-K 附注在文档后半，需整份读（上限防超大申报）
PERIODIC_FORMS = ("10-Q", "10-K")

OFFER_TTL = 6 * 3600
CACHE_MAX = 256
_offer_cache: dict[str, tuple[float, dict]] = {}
_offer_locks: dict[str, asyncio.Lock] = {}

WHO_LABEL = {"primary": "一级发行", "secondary": "老股东减持", "mixed": "一级 + 老股东"}


def _weekdays_between(start: date, end: date) -> int:
    """start 之后到 end（含）之间的工作日数，不扣节假日——托底期提示够用。"""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


async def _offer_rows(client: httpx.AsyncClient, subs: dict, since: str) -> tuple[list[dict], list[str]]:
    """近窗口内的 424B 申报（含 submissions 历史分页），新→旧。返回 (rows, 被跳过的表格)。"""
    rows = edgar._rows(subs["filings"]["recent"])
    for f in subs["filings"].get("files", []):
        # 分页按申报日切片；edgar._list_filings 用 reportDate 过滤，对 424B 不适用
        if f.get("filingTo", "9999-12-31") < since:
            continue
        await asyncio.sleep(edgar.REQUEST_GAP)
        older = await edgar._get_json(client, edgar.SUBMISSIONS_URL.format(name=f["name"]))
        rows += edgar._rows(older)
    rows = [r for r in rows if r["form"] in FORMS and r["filingDate"] >= since]
    skipped = []
    for form in ("424B2", "424B3"):
        if sum(r["form"] == form for r in rows) > NOTE_HEAVY:
            rows = [r for r in rows if r["form"] != form]
            skipped.append(form)
    rows.sort(key=lambda r: r["filingDate"], reverse=True)
    return rows, skipped


def _cache_path(acc: str) -> Path:
    return CACHE_DIR / f"{acc}_v{PARSE_VER}.json"


def _cache_write(acc: str, parsed: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fo:
        json.dump(parsed, fo)
    os.replace(tmp, _cache_path(acc))


async def _cover(client: httpx.AsyncClient, cik: int, row: dict) -> dict:
    acc = row["accessionNumber"]
    p = _cache_path(acc)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    url = edgar.DOC_URL.format(cik=cik, acc=acc.replace("-", ""), doc=row["primaryDocument"])
    await asyncio.sleep(edgar.REQUEST_GAP)
    buf = bytearray()
    async with client.stream("GET", url) as resp:
        if resp.status_code == 403:
            raise edgar.EdgarError(502, "SEC 拒绝了请求（403），请确认邮箱格式真实有效")
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) >= COVER_BYTES:
                break
    parsed = parse_cover(html_to_text(buf[:COVER_BYTES].decode("utf-8", errors="ignore"))[:40_000])
    _cache_write(acc, parsed)
    return parsed


def _shape(row: dict, cik: int, c: dict, today: date) -> dict:
    fd = date.fromisoformat(row["filingDate"])

    def near(d: str | None) -> date | None:
        # 只认落在 [申报日−10 天, 申报日] 内的日期（封面上也会出现基础招股书日期）
        if not d:
            return None
        dt = date.fromisoformat(d)
        return dt if fd - timedelta(days=10) <= dt <= fd else None

    shown, kind = near(c.get("pricing_date")), "定价日"
    if shown is None:
        shown, kind = near(c.get("last_date")), "定价前收盘日"
    if shown is None:
        shown, kind = fd, "申报日"
    disc = None
    if c.get("last_price"):
        disc = round((c["price"] / c["last_price"] - 1) * 100, 2)
    acc = row["accessionNumber"]
    days = _weekdays_between(shown, today)
    return {
        "date": shown.isoformat(),
        "date_kind": kind,
        "filing_date": row["filingDate"],
        "form": row["form"],
        "price": c["price"],
        "last_price": c.get("last_price"),
        "last_date": c.get("last_date"),
        "discount_pct": disc,
        "who": c["who"],
        "who_label": WHO_LABEL.get(c["who"], c["who"]),
        "shares": c.get("shares"),
        "block": bool(c.get("var_price")),
        "forward": bool(c.get("forward")),
        "concurrent_buyback": bool(c.get("concurrent_buyback")),
        "trading_days_since": days,
        "stabilizing": days <= STABILIZE_DAYS,
        "url": edgar.INDEX_HTML_URL.format(cik=cik, acc=acc.replace("-", ""), acc_dashed=acc),
    }


def _shape_atm(row: dict, cik: int, c: dict) -> dict:
    acc = row["accessionNumber"]
    return {"date": row["filingDate"], "form": row["form"],
            "usd": c.get("atm_usd"), "shares": c.get("atm_shares"),
            "amendment": bool(c.get("atm_amendment")),
            "rollover": bool(c.get("atm_rollover")),
            "prior_sold_usd": c.get("atm_prior_sold_usd"), "prior_date": c.get("atm_prior_date"),
            "cover_last_date": c.get("atm_last_date"), "cover_last_price": c.get("atm_last_price"),
            "close": None, "close_date": None, "close_src": None,
            "url": edgar.INDEX_HTML_URL.format(cik=cik, acc=acc.replace("-", ""), acc_dashed=acc)}


def _yahoo_closes(ticker: str, start: date, end: date) -> dict[str, float]:
    """日收盘（Yahoo 的 Close 已做拆股调整），{'YYYY-MM-DD': close}。"""
    import yfinance as yf
    df = yf.Ticker(ticker.replace(".", "-")).history(
        start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), auto_adjust=False)
    return {d.strftime("%Y-%m-%d"): float(v) for d, v in zip(df.index, df["Close"]) if v == v}


async def _attach_closes(ticker: str, atms: list[dict], since: date | None = None,
                         until: date | None = None) -> tuple[str | None, dict[str, float]]:
    """给每份 ATM 填申报日（或之前最近一个交易日）收盘价——只作参考，不是成交价。
    返回 (需上浮的告警, 收盘序列)；收盘序列另用于反推 10-Q 金额单位。"""
    if not atms:
        return None, {}
    days = [date.fromisoformat(a["date"]) for a in atms]
    start = min(days + ([since] if since else [])) - timedelta(days=10)
    try:
        closes = await asyncio.wait_for(asyncio.to_thread(
            _yahoo_closes, ticker, start, max(days + ([until] if until else []))), PRICE_TIMEOUT)
    except Exception:  # noqa: BLE001 —— 行情是附加信息，任何失败都退回封面价
        closes = {}
    keys = sorted(closes)
    for a in atms:
        k = next((d for d in reversed(keys) if d <= a["date"]), None)
        if k:
            a["close"], a["close_date"], a["close_src"] = round(closes[k], 2), k, "行情"
        elif a["cover_last_price"]:
            a["close"], a["close_date"], a["close_src"] = a["cover_last_price"], a["cover_last_date"], "封面"
    if closes:
        return None, closes
    return ("行情获取失败，ATM 参考价改用封面写的前收盘价" if any(a["close"] for a in atms)
            else "行情获取失败，ATM 参考价缺失"), {}


async def _fetch_text(client: httpx.AsyncClient, url: str, cap: int) -> str:
    await asyncio.sleep(edgar.REQUEST_GAP)
    buf = bytearray()
    async with client.stream("GET", url) as resp:
        if resp.status_code == 403:
            raise edgar.EdgarError(502, "SEC 拒绝了请求（403），请确认邮箱格式真实有效")
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) >= cap:
                break
    return html_to_text(buf[:cap].decode("utf-8", errors="ignore"))


MAX_8K = 8               # 季报之后最多读几份 8-K（7.01/8.01）找 ATM 完成公告
EIGHTK_BYTES = 400_000
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_LABEL_YM = re.compile(r"(" + "|".join(_MONTHS) + r")\s+(\d{4})")


def _cached_entries(name: str) -> list | None:
    p = CACHE_DIR / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _cache_entries(name: str, entries: list) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fo:
        json.dump(entries, fo)
    os.replace(tmp, CACHE_DIR / name)


def _price_range(closes: dict[str, float], end: str) -> tuple[float, float] | None:
    lo = (date.fromisoformat(end) - timedelta(days=400)).isoformat()
    w = [v for d, v in closes.items() if lo <= d <= end]
    return (min(w), max(w)) if w else None


def _tag(entries: list, form: str, day: str, url: str) -> list:
    return [{**e, "source_form": form, "source_date": day, "source_url": url} for e in entries]


async def _report_sales(client, cik: int, rep: dict, closes: dict) -> list:
    acc = rep["accessionNumber"]
    name = f"sales_{acc}_v{atm_sales.PARSE_VER}.json"
    entries = _cached_entries(name)
    if entries is None:
        url = edgar.DOC_URL.format(cik=cik, acc=acc.replace("-", ""), doc=rep["primaryDocument"])
        text = await _fetch_text(client, url, REPORT_BYTES)
        entries = atm_sales.parse_atm_sales(text, rep["reportDate"], _price_range(closes, rep["reportDate"]))
        _cache_entries(name, entries)
    return _tag(entries, rep["form"], rep["filingDate"],
                edgar.INDEX_HTML_URL.format(cik=cik, acc=acc.replace("-", ""), acc_dashed=acc))


async def _eightk_sales(client, cik: int, row: dict, closes: dict) -> list:
    """8-K 附件新闻稿（EX-99*）里的 ATM 完成公告。逐份落盘。"""
    acc = row["accessionNumber"]
    name = f"sales8k_{acc}_v{atm_sales.PARSE_VER}.json"
    entries = _cached_entries(name)
    if entries is None:
        nd = acc.replace("-", "")
        await asyncio.sleep(edgar.REQUEST_GAP)
        idx = await edgar._get_json(client, edgar.INDEX_URL.format(cik=cik, acc=nd))
        docs = [it["name"] for it in idx.get("directory", {}).get("item", [])
                if re.search(r"ex-?99", it["name"], re.I) and it["name"].lower().endswith((".htm", ".html"))]
        entries = []
        for doc in docs[:3]:
            text = await _fetch_text(client, edgar.DOC_URL.format(cik=cik, acc=nd, doc=doc), EIGHTK_BYTES)
            entries += atm_sales.parse_atm_sales(text, row["filingDate"], _price_range(closes, row["filingDate"]))
        _cache_entries(name, entries)
    return _tag(entries, "8-K", row["filingDate"],
                edgar.INDEX_HTML_URL.format(cik=cik, acc=acc.replace("-", ""), acc_dashed=acc))


def _assign(entries: list, atms: list[dict], report_date: str | None) -> None:
    """把每笔成交对到所属 ATM 协议，算卖出窗口 [协议日, 下一份协议日 / 期末 / 8-K 日]。
    对应规则：label 里的「Month YYYY」→ 当月申报的协议；金额与某份协议额度相差 <0.5%
    （「卖完了」公告）→ 那份；8-K 再退到不晚于公告日的最近一份协议。"""
    progs = sorted((a for a in atms if not a["amendment"]), key=lambda a: a["date"])
    for e in entries:
        prog = None
        m = _LABEL_YM.search(e.get("label") or "")
        if m:
            ym = f"{m.group(2)}-{_MONTHS.index(m.group(1)) + 1:02d}"
            same = [a for a in progs if a["date"][:7] == ym]
            prog = next((a for a in same if not a["rollover"]), same[0] if same else None)
        if prog is None and e.get("proceeds_usd"):
            prog = next((a for a in progs if a["usd"] and abs(e["proceeds_usd"] / a["usd"] - 1) < 0.005), None)
        if prog is None and e["source_form"] == "8-K":
            prog = next((a for a in reversed(progs) if a["date"] <= e["source_date"]), None)
        nxt = next((a["date"] for a in progs if prog and a["date"] > prog["date"]), None)
        if e["source_form"] == "8-K":
            end = e["source_date"]
        else:
            end = min(x for x in (nxt, e.get("period_end") or report_date) if x) if (nxt or e.get("period_end") or report_date) else None
        start = prog["date"] if prog else None
        if start is None and e.get("span_months") and e.get("period_end"):
            # 对不上具体协议时按报告期推起点（「three months ended August 31」→ 6/1）
            pe = date.fromisoformat(e["period_end"])
            y, mth = pe.year, pe.month - e["span_months"] + 1
            while mth <= 0:
                mth, y = mth + 12, y - 1
            start = date(y, mth, 1).isoformat()
        e.update(program_date=prog["date"] if prog else None,
                 program_usd=prog["usd"] if prog else None,
                 program_rollover=bool(prog and prog["rollover"]),
                 window_start=start, window_end=end)


async def _atm_sales(client: httpx.AsyncClient, cik: int, subs: dict, atms: list[dict],
                     closes: dict[str, float]) -> dict | None:
    """ATM 实际成交：最近一份定期报告附注 + 之后的 8-K 完成公告（季报出来前卖完的那笔
    只在 8-K 里，RKLB 2026-08-13→09-15 即是），按卖出窗口时间排序。"""
    rows = edgar._rows(subs["filings"]["recent"])
    first = min(a["date"] for a in atms)
    reps = [r for r in rows if r["form"] in PERIODIC_FORMS and r["filingDate"] >= first]
    rep = max(reps, key=lambda r: r["filingDate"]) if reps else None
    entries = await _report_sales(client, cik, rep, closes) if rep else []
    after = rep["filingDate"] if rep else first
    eights = [r for r in rows if r["form"] == "8-K" and r["filingDate"] >= after
              and any(i in (r.get("items") or "") for i in ("7.01", "8.01"))]
    for r in sorted(eights, key=lambda r: r["filingDate"])[-MAX_8K:]:
        entries += await _eightk_sales(client, cik, r, closes)
    if rep is None and not entries:
        return None
    _assign(entries, atms, rep["reportDate"] if rep else None)
    entries.sort(key=lambda e: (e["window_start"] or "9999", e["kind"] == "collar", e["window_end"] or ""))
    report = None
    if rep:
        acc = rep["accessionNumber"]
        report = {"form": rep["form"], "report_date": rep["reportDate"], "filing_date": rep["filingDate"],
                  "url": edgar.INDEX_HTML_URL.format(cik=cik, acc=acc.replace("-", ""), acc_dashed=acc)}
    return {"report": report, "eightk_scanned": len(eights[-MAX_8K:]), "entries": entries}


async def find_offering(client: httpx.AsyncClient, ticker: str, today: date | None = None) -> dict:
    today = today or date.today()
    since = (today - timedelta(days=365 * LOOKBACK_YEARS)).isoformat()
    cik, subs = await edgar._submissions(client, ticker)
    rows, skipped = await _offer_rows(client, subs, since)
    found, scanned, failed, atms = None, 0, 0, []
    for row in rows[:MAX_SCAN]:
        scanned += 1
        try:
            c = await _cover(client, cik, row)
        except httpx.HTTPError:
            # 逐份容错：单份下载失败不拖垮整行，但要如实上浮
            failed += 1
            continue
        if c.get("kind") == "priced_equity":
            found = _shape(row, cik, c, today)
            break
        if c.get("reason") == "atm":
            atms.append(_shape_atm(row, cik, c))
    # 兜底去重：同额度、相隔 ≤5 天的第二份视作修订（同一协议补发/加代理）
    for i, a in enumerate(atms):
        for b in atms[i + 1:]:
            if (not b["amendment"] and a["usd"] and a["usd"] == b["usd"]
                    and (date.fromisoformat(a["date"]) - date.fromisoformat(b["date"])).days <= 5):
                a["amendment"] = True
    notes = []
    price_note, closes = await _attach_closes(ticker, atms, since=today - timedelta(days=500), until=today)
    if price_note:
        notes.append(price_note)
    sales = None
    if atms:
        try:
            sales = await _atm_sales(client, cik, subs, atms, closes)
        except httpx.HTTPError:
            notes.append("定期报告下载失败，ATM 实际成交缺失")
    if skipped:
        notes.append(f"{'/'.join(skipped)} 超过 {NOTE_HEAVY} 份（票据定价补充），未扫描这类表格")
    if failed:
        notes.append(f"{failed} 份招股书下载失败已跳过")
    if found is None and len(rows) > MAX_SCAN:
        notes.append(f"近 {LOOKBACK_YEARS} 年共 {len(rows)} 份 424B，只扫描了最新 {MAX_SCAN} 份")
    return {"ticker": ticker, "cik": cik, "name": subs.get("name") or "",
            "lookback_years": LOOKBACK_YEARS, "candidates": len(rows), "scanned": scanned,
            "offering": found, "atm_programs": atms, "atm_sales": sales,
            "warning": "；".join(notes) or None}


@router.get("/api/offering/{ticker}")
async def offering(ticker: str):
    ticker = validate_ticker(ticker)
    email = edgar.contact_email()

    async def fetch():
        try:
            async with edgar._client(email) as client:
                return await find_offering(client, ticker)
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as e:
            raise edgar.EdgarError(502, f"SEC 数据请求失败：{type(e).__name__}，请稍后重试")

    return await get_or_fetch(_offer_cache, _offer_locks, ticker,
                              OFFER_TTL, CACHE_MAX, fetch)
