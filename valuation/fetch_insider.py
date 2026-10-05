# -*- coding: utf-8 -*-
"""内部人交易 (SEC Form 4) 取数与分类 —— dashboard「内部人交易」卡片的数据源.

与 watchlist-scanner 的 insider.py 同一套规则; 那边只推公开市场买入, 这里是
研究视图, 买卖全列。规则来自 2026-10 拉 watchlist 14 只个股一年 Form 4 和
EDGAR 全文检索的真实申报 (scanner 仓库 lesson.md 2026-10-05 有完整记录):

  1. 公司申报列表里混着它作为**别家**股东报的 Form 4 (GOOG 名下谷歌风投
     卖 Ethos、HOOD 名下卖 Robinhood Ventures Fund I) —— 核对 issuerCik。
  2. 代码 P = 公开市场**或私下**购买: 员工购股计划 (TSM 每月 30 多位高管
     同日同价) 和私募 / PIPE / 直接向公司买 / IPO 定向配售都记 P。按交易
     本身的脚注和持有方式分开, 写了 "open market" 的不算私募。
  3. 是/否字段有 "1"/"0" 也有 "true"/"false"。
  4. 修正申报 4/A 通常整份重报原申报再补漏/改错 —— 按 (申报人, 交易日) 让
     更新的 4/A 覆盖它修正的那份 (dateOfOriginalSubmission), 不按数字去重。
  5. 迟报常见 (TSM 7/2 的交易 9/4 才申报): 图上按交易日, 表里同时给申报日。
  6. 10b5-1 计划: 2023-04 起有勾选框 aff10b5One; 更早的计划只写在脚注里。

成交价单位 (TSM 高管买的是台股普通股, 美股 ADR 是 5 股一份) 在这里不影响
画图: 图上的点画在当天收盘价上, 申报价格只进 tooltip 和表格。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import httpx

REQUEST_GAP = 0.12        # SEC 限速 10 次/秒, 留余量
CACHE_DIR = Path(__file__).resolve().parent.parent / "jobs" / "insider_cache"
PARSE_VER = 1             # 改了 parse_form4 的输出就 +1, 旧缓存自动作废 (sweep 会清掉旧版本)
MAX_FETCH = 900           # 单次最多拉多少份新 XML (2 年大票约 400 份)
ESPP_CLUSTER_MIN = 5      # 同一天同一价格 ≥5 人 = 公司代买计划

_rate_lock = threading.Lock()
_last_request = [0.0]

# 公司代买类计划 (坑 2)。只看挂在交易本身 (证券名/日期/代码/数量价格) 上的
# 脚注和持有方式 —— 挂在"交易后持股"上的脚注常写"其中 N 股来自 ESPP",
# 说的是存量, 拿它判会误杀真买入
PLAN_RE = re.compile(
    r"employee stock purchase|\bESPP\b|employee stock ownership|\bESOP\b"
    r"|dividend reinvest|\bDRIP\b|401\s*\(k\)", re.I)
# 记成 P 的私下购买 (坑 2)。措辞取自 2026-06~09 的真实申报
PRIVATE_RE = re.compile(
    r"private placement|privately negotiated|\bPIPE\b|private units?\b"
    r"|placement units?\b|securities purchase agreement|subscription agreement"
    r"|directed share program|registered direct|rights offering"
    r"|underwritten (?:public )?offering"
    r"|\b(?:purchased|acquired|bought)\b[^.]{0,120}?\bfrom the (?:issuer|company)\b"
    r"|\b(?:in|through|in connection with)\b[^.]{0,40}?\b(?:initial )?public offering",
    re.I)
OPEN_MARKET_RE = re.compile(r"open[- ]market", re.I)
PLAN_10B51_RE = re.compile(r"10b5-1", re.I)
_TXN_PARTS = ("securityTitle", "transactionDate", "transactionCoding",
              "transactionAmounts")

# 交易代码 -> 卡片里的类别。P / S 另有细分 (classify)
CODE_KIND = {"F": "tax", "M": "exercise", "X": "exercise", "C": "exercise",
             "O": "exercise", "A": "grant", "G": "gift"}


class InsiderError(ValueError):
    """取数失败, 信息可直接展示。transient=True = 上游瞬态错误 (限速/维护),
    调用方映射成 5xx 而不是"没有数据"。"""

    def __init__(self, msg: str, transient: bool = False):
        self.transient = transient
        super().__init__(msg)


def _headers(email: str) -> dict:
    return {"User-Agent": f"sec-filing-downloader insider ({email})",
            "Accept-Encoding": "gzip"}


def _get(client: httpx.Client, url: str) -> httpx.Response:
    for attempt in (0, 1):
        with _rate_lock:
            wait = _last_request[0] + REQUEST_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.monotonic()
        r = client.get(url)
        if r.status_code == 200:
            return r
        if r.status_code in (403, 429, 500, 502, 503) and attempt == 0:
            time.sleep(1.5)  # 瞬态限速/维护, 退避一次再试
            continue
        break
    transient = r.status_code in (403, 429, 500, 502, 503)
    raise InsiderError(
        f"SEC 接口返回 {r.status_code}: {url.rsplit('/', 1)[-1]}", transient)


# --------------------------------------------------------------------------
# 解析 (纯函数)
# --------------------------------------------------------------------------

def _bool(v) -> bool:
    """坑 3: 两种写法都有。"""
    return (v or "").strip().lower() in ("1", "true")


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _text(node, path: str) -> str:
    return (node.findtext(path) or "").strip() if node is not None else ""


def parse_form4(xml: bytes) -> dict:
    """Form 4 原文 -> 发行人 / 申报人 / 全部非衍生品交易行。"""
    root = ET.fromstring(xml)
    foot = {f.get("id"): " ".join((f.text or "").split())
            for f in root.findall("footnotes/footnote")}
    owners = []
    for o in root.findall("reportingOwner"):
        rel = o.find("reportingOwnerRelationship")
        owners.append({
            "cik": _text(o, "reportingOwnerId/rptOwnerCik").lstrip("0"),
            "name": _text(o, "reportingOwnerId/rptOwnerName"),
            "director": _bool(_text(rel, "isDirector")),
            "officer": _bool(_text(rel, "isOfficer")),
            "ten_pct": _bool(_text(rel, "isTenPercentOwner")),
            "title": _text(rel, "officerTitle"),
        })
    rows = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        ids = set()
        for part in _TXN_PARTS:
            node = t.find(part)
            if node is not None:
                ids |= {f.get("id") for f in node.iter("footnoteId")}
        rows.append({
            "security": _text(t, "securityTitle/value"),
            "date": _text(t, "transactionDate/value")[:10],
            "code": _text(t, "transactionCoding/transactionCode"),
            "shares": _num(_text(t, "transactionAmounts/transactionShares/value")),
            "price": _num(_text(t, "transactionAmounts/transactionPricePerShare/value")),
            "ad": _text(t, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "after": _num(_text(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
            "direct": _text(t, "ownershipNature/directOrIndirectOwnership/value"),
            # 持有方式常带排版空格 ("Held  through  Silver Lake")
            "nature": " ".join(_text(t, "ownershipNature/natureOfOwnership/value").split()),
            "notes": [foot[i] for i in sorted(ids, key=str) if i in foot],
        })
    issuer = _text(root, "issuer/issuerCik").lstrip("0")
    return {
        "v": PARSE_VER,
        "issuer_cik": int(issuer) if issuer.isdigit() else 0,
        "owners": owners,
        "plan": _bool(_text(root, "aff10b5One")),
        "orig_date": _text(root, "dateOfOriginalSubmission")[:10],
        "rows": rows,
    }


_CEO_RE = re.compile(r"chief executive|\bCEO\b", re.I)
_CFO_RE = re.compile(r"chief financial|\bCFO\b", re.I)


def role_label(owners: list[dict]) -> str:
    """'董事/CEO' 这类短标签; 联名申报 (基金 + GP + 本人) 的身份合起来看。"""
    director = any(o["director"] for o in owners)
    officer = any(o["officer"] for o in owners)
    ten = any(o["ten_pct"] for o in owners)
    title = next((o["title"] for o in owners if o["officer"] and o["title"]), "")
    parts = []
    if director:
        parts.append("董事")
    if officer:
        if _CEO_RE.search(title):
            parts.append("CEO")
        elif _CFO_RE.search(title):
            parts.append("CFO")
        else:
            parts.append(title[:24] or "高管")
    if ten and not (director or officer):
        parts.append("10%股东")
    return "/".join(parts) or "其他"


def _owner_key(owners: list[dict]) -> str:
    return owners[0]["cik"] or owners[0]["name"]


def _owner_name(owners: list[dict]) -> str:
    name = owners[0]["name"] if owners else "?"
    return name if len(owners) <= 1 else f"{name} 等{len(owners)}个申报人"


def classify(row: dict, filing_plan: bool) -> str:
    """一行交易 -> 类别: buy / private / espp / sell / sell_plan / tax /
    exercise / grant / gift / other。"""
    code = row["code"]
    text = " ".join(row["notes"]) + " " + row["nature"]
    if code == "P" and row["ad"] == "A":
        if PLAN_RE.search(text):
            return "espp"
        if PRIVATE_RE.search(text) and not OPEN_MARKET_RE.search(text):
            return "private"
        return "buy"
    if code == "S" and row["ad"] == "D":
        # 坑 6: 勾选框是 2023-04 才有的; 更早的计划写在交易脚注里
        if filing_plan or PLAN_10B51_RE.search(" ".join(row["notes"])):
            return "sell_plan"
        return "sell"
    return CODE_KIND.get(code, "other")


def _superseded(f: dict, owner_key: str, txn_date: str, filings: list[dict]) -> bool:
    """f 里这一天的交易是否已被同一申报人更新的 4/A 覆盖 (坑 4)。

    4/A 里出现的交易日, 被它修正的那份申报 (申报日 == dateOfOriginalSubmission;
    或同一份原申报的更早修正) 同一天的行作废, 其他日期的行保留。4/A 没写
    原申报日就按"同一人更早的申报"处理。同一份申报内部两行一模一样是真实的
    两笔, 永不互相覆盖。"""
    for a in filings:
        if a["form"] != "4/A" or a["acc"] == f["acc"]:
            continue
        if (a["filed"], a["acc"]) <= (f["filed"], f["acc"]):
            continue
        if _owner_key(a["doc"]["owners"]) != owner_key:
            continue
        if txn_date not in {r["date"] for r in a["doc"]["rows"]}:
            continue
        orig = a["doc"].get("orig_date")
        if not orig:
            return True
        if f["form"] == "4/A" and f["doc"].get("orig_date") == orig:
            return True
        if f["form"] != "4/A" and f["filed"] == orig:
            return True
    return False


def _busdays(a: str, b: str) -> int:
    """a -> b 之间的工作日数 (不含 a, 含 b; 不算美国假日, 判迟报够用)。"""
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def transactions(filings: list[dict], issuer_cik: int, since: str) -> list[dict]:
    """[{acc, filed, form, doc}] -> 本公司、交易日 >= since、未被修正覆盖的
    全部交易行 (已分类), 新的在前。"""
    mine = [f for f in filings
            if f["doc"].get("issuer_cik") == issuer_cik and f["doc"].get("owners")]
    out = []
    for f in mine:
        doc = f["doc"]
        owners = doc["owners"]
        key = _owner_key(owners)
        for r in doc["rows"]:
            if not r["date"] or r["date"] < since or not r["shares"]:
                continue
            if _superseded(f, key, r["date"], mine):
                continue
            value = r["shares"] * r["price"] if r["price"] else None
            out.append({
                "date": r["date"], "filed": f["filed"], "form": f["form"],
                "acc": f["acc"], "owner_key": key, "owner": _owner_name(owners),
                "role": role_label(owners), "code": r["code"],
                "kind": classify(r, doc.get("plan", False)),
                "shares": r["shares"], "price": r["price"], "value": value,
                "after": r["after"], "indirect": r["direct"] == "I",
                "nature": r["nature"], "security": r["security"],
                "plan": doc.get("plan", False),
                "late_days": max(0, _busdays(r["date"], f["filed"]) - 2),
            })
    # 坑 2 兜底: 没写脚注的公司代买 —— 同日同价 ≥5 人
    owners_at = defaultdict(set)
    for t in out:
        if t["kind"] == "buy":
            owners_at[(t["date"], t["price"])].add(t["owner_key"])
    for t in out:
        if t["kind"] == "buy" and len(owners_at[(t["date"], t["price"])]) >= ESPP_CLUSTER_MIN:
            t["kind"] = "espp"
    out.sort(key=lambda t: (t["date"], t["filed"], t["acc"]), reverse=True)
    return out


# --------------------------------------------------------------------------
# 取数
# --------------------------------------------------------------------------

def _list_form4(client: httpx.Client, cik: int, cutoff: str) -> list[dict]:
    """申报日 >= cutoff 的 Form 4 / 4/A。recent 只装最近约 1000 份, 不够就
    翻 filings.files 分页 (同 fetch_segments._list_filings)。"""
    subs = _get(client, f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()

    def block_rows(block: dict) -> list[dict]:
        return [dict(zip(("form", "acc", "filed", "doc"), t))
                for t in zip(block["form"], block["accessionNumber"],
                             block["filingDate"], block["primaryDocument"])]

    rows = block_rows(subs["filings"]["recent"])
    oldest = min((r["filed"] for r in rows), default="")
    if oldest and oldest > cutoff:
        for extra in subs["filings"].get("files", []):
            if extra["filingTo"] >= cutoff:
                older = _get(client, "https://data.sec.gov/submissions/"
                             + extra["name"]).json()
                rows += block_rows(older)
    return [r for r in rows if r["form"] in ("4", "4/A") and r["filed"] >= cutoff]


def _cache_path(acc: str) -> Path:
    return CACHE_DIR / f"{acc.replace('-', '')}_v{PARSE_VER}.json"


def _parse_cached(client: httpx.Client, cik: int, row: dict) -> dict | None:
    """单份申报解析结果按 accession 落盘 (申报提交后不会改, 修正是另一份)。
    非 XML 的老申报返回 None。"""
    path = _cache_path(row["acc"])
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    # 故意去掉目录: submissions 的 primaryDocument 是 "xslF345X06/xxx.xml", 那是 SEC
    # 用 XSL 渲染出来的 HTML 页面 (text/html, XML 解析直接报错); 原始 XML 在申报
    # 根目录同名文件 (text/xml)。2026-10-06 对 SOFI 0001613438-26-000016 实测两边
    doc = row["doc"].rsplit("/", 1)[-1]
    if not doc.lower().endswith(".xml"):
        return None
    url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
           f"{row['acc'].replace('-', '')}/{doc}")
    parsed = parse_form4(_get(client, url).content)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(parsed, fh, ensure_ascii=False)
    os.replace(tmp, path)
    return parsed


def _sweep_stale_cache() -> None:
    """PARSE_VER 升级后的旧版本缓存与被杀进程残留的 .tmp 一起清掉。"""
    if not CACHE_DIR.exists():
        return
    keep = f"_v{PARSE_VER}.json"
    now = time.time()
    for f in CACHE_DIR.iterdir():
        try:
            if f.name.endswith(".tmp"):
                if now - f.stat().st_mtime > 3600:
                    f.unlink(missing_ok=True)
            elif f.name.endswith(".json") and not f.name.endswith(keep):
                f.unlink(missing_ok=True)
        except OSError:
            pass


def build_insider(ticker: str, email: str, cik: int, years: int = 1,
                  today: date | None = None,
                  transport: httpx.BaseTransport | None = None) -> dict:
    """-> {ticker, cik, since, rows, missing}。missing = 本次没拉到的申报份数
    (超出单次上限或个别失败), 前端要如实说"不完整"。transport 供测试注入。"""
    today = today or date.today()
    since = (today - timedelta(days=365 * years)).isoformat()
    # 按申报日多拉 30 天: 窗口起点附近的交易可能迟报进来
    cutoff = (today - timedelta(days=365 * years + 30)).isoformat()
    _sweep_stale_cache()
    filings, missing, fails = [], 0, 0
    with httpx.Client(headers=_headers(email), timeout=60,
                      follow_redirects=True, transport=transport) as client:
        listed = _list_form4(client, cik, cutoff)
        fetched = 0
        for row in listed:
            cached = _cache_path(row["acc"]).exists()
            if not cached and fetched >= MAX_FETCH:
                missing += 1
                continue
            try:
                doc = _parse_cached(client, cik, row)
            except (InsiderError, httpx.HTTPError, ET.ParseError):
                missing += 1
                fails += 1
                if fails >= 3 and not filings:
                    raise InsiderError("SEC 连续取数失败, 请稍后重试", transient=True)
                continue
            if not cached:
                fetched += 1
            if doc is not None:
                filings.append({"acc": row["acc"], "filed": row["filed"],
                                "form": row["form"], "doc": doc})
    return {"ticker": ticker.upper(), "cik": cik, "since": since,
            "rows": transactions(filings, cik, since), "missing": missing}
