# -*- coding: utf-8 -*-
"""SEC XBRL 分部营收提取：解析各期申报的 XBRL instance 文件。

companyfacts/frames 这类免费 JSON API 会剥掉全部维度数据，分部营收只存在于
每份申报的 instance（现代申报为 EDGAR 提取的 *_htm.xml）里，必须按申报解析。

用法: python fetch_segments.py TICKER OUT.json EMAIL [YEARS]
也可作为模块导入: from valuation.fetch_segments import build_segments

口径要点（每条都对应真实公司踩过的坑）：
- revenue concept 因公司而异，且 ASC 606 分拆表和 ASC 280 分部表常标在
  不同 concept 下（DE/BA/UNH）——concept 按轴独立选，不能全局取一个
- 同成员可能同时有裸维度和 ConsolidationItems=OperatingSegments 限定两个
  事实且数值不同（MO 的 AllOtherSegments），裸维度是外部口径，优先取
- 同一轴上父子层级混标（AAPL 的 ProductMember 与 iPhone/Mac 并存）会导致
  重复计数，用「成员和 - 合并总额 = 溢出」搜 1-3 个成员的组合剔除 rollup
- 同一期被多份申报覆盖（10-K 含比较期），整期按 filed 最新的申报取
- Q4 = 年度 - 前三季 按成员推导，但重述（NVDA 地区口径改归属）会让
  新旧口径相减出坏数——推导后必须按成员和 vs 总额重新对账，超容差就丢弃
- filings.recent 只保证最近 1000 份或 1 年（GS/JPM 恰好 1 年），
  窗口不够时要继续翻 filings.files 分页
- 每份申报解析结果不可变，按 accession 落盘缓存（原子写入，损坏自愈）
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path

import httpx

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"

REVENUE_LOCALNAMES = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    # 银行（GS/JPM/SOFI）与公用事业把分部营收标在行业口径下
    "RevenuesNetOfInterestExpense",
    "RegulatedAndUnregulatedOperatingRevenue",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
]
# 合并总额参照的优先序：RevenuesNetOfInterestExpense 一旦出现即为损益表
# 第一行（银行的 Total net revenue），必须先于 ASC 606 附注口径
TOPLINE_PRIORITY = (["RevenuesNetOfInterestExpense"]
                    + [ln for ln in REVENUE_LOCALNAMES
                       if ln != "RevenuesNetOfInterestExpense"])
# 轴按 local name 匹配：属性值里的前缀（srt:/us-gaap:）是申报方自选文本
TARGET_AXES = {
    "ProductOrServiceAxis": "product",
    "StatementBusinessSegmentsAxis": "segment",
    "StatementGeographicalAxis": "geo",
}

REQUEST_GAP = 0.12          # SEC 限速 10 req/s，全局（跨线程）生效
MAX_FILINGS = 48            # 防误选超大范围
MAX_INSTANCE_BYTES = 30_000_000
PARSE_VER = 6               # 解析逻辑变更时递增，旧缓存自动失效
# 集中度披露：带 1 的才是现行 us-gaap concept（无后缀版已弃用、返回零条）
CONC_TAGS = {"ConcentrationRiskPercentage1", "ConcentrationRiskPercentage"}
CACHE_DIR = Path(__file__).resolve().parent.parent / "jobs" / "segments_cache"

_rate_lock = threading.Lock()
_last_request = [0.0]


class SegmentsError(ValueError):
    """取数失败，信息可直接展示给用户。transient=True 表示上游瞬态错误
    （限速/维护），调用方应映射为 5xx 而不是「没有数据」。"""

    def __init__(self, msg: str, transient: bool = False):
        self.transient = transient
        super().__init__(msg)


def _headers(email: str) -> dict:
    return {"User-Agent": f"sec-filing-downloader segments ({email})",
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
            time.sleep(1.5)  # 瞬态限速/维护，退避一次再试
            continue
        break
    transient = r.status_code in (403, 429, 500, 502, 503)
    raise SegmentsError(
        f"SEC 接口返回 {r.status_code}: {url.rsplit('/', 1)[-1]}", transient)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _iso(text: str) -> str:
    """XBRL 期间允许 dateUnion（date 或 dateTime），只取日期部分。"""
    return text.strip()[:10]


def _instance_names(names: list) -> list:
    """从文件名列表里挑 XBRL instance：现代申报为 EDGAR 提取件 *_htm.xml，
    2019 年前的申报没有提取件，退回原始 instance（排除 linkbase 四件套）。"""
    cands = [n for n in names if n.endswith("_htm.xml")]
    if not cands:
        cands = [n for n in names
                 if re.search(r"-\d{8}\.xml$", n)
                 and not re.search(r"(_cal|_def|_lab|_pre)\.xml$", n)]
    return cands


def _fetch_instance(client: httpx.Client, cik: int, acc: str) -> bytes | None:
    """定位并下载申报的 XBRL instance；超过 MAX_INSTANCE_BYTES 返回 None。

    EDGAR 正在滚动把已披露申报的目录收缩成四个文件（index 两件 + 母版
    txt + -xbrl.zip）：刚披露的申报和被迁移的老申报都取不到单独的
    *_htm.xml（NVDA 2026-08-26 / 2024-11-20、AVGO 2026-06 实测），
    instance 只存在于 -xbrl.zip 里——必须解包兜底，否则分部卡
    恰好在财报日当天必挂。"""
    acc_nodash = acc.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}"
    idx = _get(client, f"{base}/index.json").json()
    names = [it["name"] for it in idx["directory"]["item"]]
    cands = _instance_names(names)
    if cands:
        content = _get(client, f"{base}/{sorted(cands)[0]}").content
        return None if len(content) > MAX_INSTANCE_BYTES else content
    zips = [n for n in names if n.endswith("-xbrl.zip")]
    if zips:
        blob = _get(client, f"{base}/{sorted(zips)[0]}").content
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                pick = None
                inner = _instance_names(zf.namelist())
                if inner:  # 2019 年前的原始 instance .xml 直接在包里
                    pick = sorted(inner)[0]
                else:
                    # 现代申报的原始包里没有提取件（*_htm.xml 是 EDGAR
                    # 播发时生成的，目录收缩后不复存在）：XBRL 以 iXBRL
                    # 内嵌在主文档里。主文档命名 <ticker>-YYYYMMDD.htm，
                    # 匹配不到再退回包里最大的 .htm
                    htms = [n for n in zf.namelist()
                            if n.lower().endswith(".htm")]
                    prim = [n for n in htms if re.search(r"-\d{8}\.htm$", n)]
                    if prim:
                        pick = sorted(prim)[0]
                    elif htms:
                        pick = max(htms, key=lambda n: zf.getinfo(n).file_size)
                if pick is not None:
                    # 解压前按声明尺寸挡 zip 炸弹，与直取路径同一上限
                    if zf.getinfo(pick).file_size > MAX_INSTANCE_BYTES:
                        return None
                    return zf.read(pick)
        except zipfile.BadZipFile:
            raise SegmentsError(f"申报 {acc} 的 -xbrl.zip 损坏") from None
    raise SegmentsError(f"申报 {acc} 里找不到 XBRL instance")


_IX_NS = ("http://www.xbrl.org/2013/inlineXBRL",
          "http://www.xbrl.org/2008/inlineXBRL")
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def _ix_number(el) -> float | None:
    """ix:nonFraction -> 数值。文本按 num-dot-decimal 清洗（US 申报的
    绝对主流），scale 缩放（NVDA 营收 '96,221'×10⁶、集中度 '22'×10⁻²），
    sign 取负。罕见 format（欧陆千分位等）解析不了就返回 None 跳过该事实。"""
    if el.get(_XSI_NIL) == "true":
        return None
    fmt = (el.get("format") or "").rsplit(":", 1)[-1]
    txt = "".join(el.itertext()).strip()
    if fmt == "fixed-zero" or txt in ("", "—", "–", "-"):
        val = 0.0
    elif fmt in ("", "num-dot-decimal"):
        try:
            val = float(txt.replace(",", "").replace("\xa0", ""))
        except ValueError:
            return None
    else:
        return None
    try:
        val *= 10.0 ** int(el.get("scale") or 0)
    except ValueError:
        return None
    return -val if el.get("sign") == "-" else val


def _fact_items(root):
    """统一两种载体的数值事实遍历，产出 (concept 局部名, contextRef, 值)。

    普通 instance：概念是顶层元素标签，值在 text；
    iXBRL（XHTML 内嵌，收缩目录申报的唯一形态）：事实是 ix:nonFraction，
    概念在 @name，值要按 format/scale/sign 还原。同一事实在 iXBRL 里
    常重复出现（封面+附注），调用方按 contextRef 覆盖去重。"""
    if _local(root.tag) == "html":
        for ns in _IX_NS:
            for el in root.iter(f"{{{ns}}}nonFraction"):
                cref = el.get("contextRef")
                if cref is None:
                    continue
                val = _ix_number(el)
                if val is not None:
                    yield _local(el.get("name", "")), cref, val
        return
    for el in root.iter():
        cref = el.get("contextRef")
        if cref is None or not el.text or not el.text.strip():
            continue
        try:
            yield _local(el.tag), cref, float(el.text.strip())
        except ValueError:
            continue


def _parse_instance(xml_bytes: bytes) -> dict:
    """单份 instance（普通 XBRL 或 iXBRL XHTML）-> {"periods":
    {(start,end): {"total": v|None, "axes": {axis_key: {member: value}}}}}，
    只收季度/年度跨度的营收事实。"""
    root = ET.fromstring(xml_bytes)

    contexts = {}
    for ctx in root.iter(f"{{{XBRLI}}}context"):
        period = ctx.find(f"{{{XBRLI}}}period")
        if period is None:
            continue
        s = period.find(f"{{{XBRLI}}}startDate")
        e = period.find(f"{{{XBRLI}}}endDate")
        if s is None or e is None:
            continue
        dims = {}
        typed = False
        for m in ctx.iter(f"{{{XBRLDI}}}explicitMember"):
            dims[_local(m.get("dimension", ""))] = _local(m.text or "")
        for _ in ctx.iter(f"{{{XBRLDI}}}typedMember"):
            typed = True
        try:
            start, end = _iso(s.text), _iso(e.text)
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        except (ValueError, TypeError):
            continue
        # 全部期间上下文都留着：营收事实在用的时候按季度/年度跨度过滤，
        # 集中度事实（10-Q 里常见半年/9个月跨度）不受限
        contexts[ctx.get("id")] = {"start": start, "end": end, "days": days,
                                   "dims": dims, "typed": typed}

    facts = {ln: {} for ln in REVENUE_LOCALNAMES}
    # (concept, contextRef) -> value：iXBRL 同一事实常重复出现（封面+附注），
    # 按键覆盖去重；两个集中度 concept 同 context 并存时互不挤占
    conc_facts: dict = {}
    for ln, cref, val in _fact_items(root):
        if cref not in contexts:
            continue
        if ln in facts:
            facts[ln][cref] = val
        elif ln in CONC_TAGS:
            conc_facts[(ln, cref)] = val

    def span_ok(c: dict) -> bool:
        return 80 <= c["days"] <= 100 or 340 <= c["days"] <= 380

    def axis_facts(ln: str, axis: str):
        """concept 在某轴上通过维度白名单的事实：{(start,end,member): (val, bare)}"""
        out = {}
        for cref, val in facts[ln].items():
            c = contexts[cref]
            if c["typed"] or axis not in c["dims"] or not span_ok(c):
                continue
            extra = {d: m for d, m in c["dims"].items() if d != axis}
            if extra and extra != {"ConsolidationItemsAxis": "OperatingSegmentsMember"}:
                continue
            key = (c["start"], c["end"], c["dims"][axis])
            bare = not extra
            # 同成员的裸维度事实是外部口径，优先于 OperatingSegments 限定值
            # （MO 的 AllOtherSegments 两者差 10 亿）；同 shape 重复时后者覆盖
            if key not in out or (bare and not out[key][1]):
                out[key] = (val, bare)
            elif bare == out[key][1]:
                out[key] = (val, bare)
        return {k: v for k, (v, _) in out.items()}

    def axis_facts_matrix(ln: str, axis: str):
        """产品/地区 × 经营分部 的矩阵事实按成员跨分部求和。SOFI 2023 起
        产品分拆只有双维度矩阵、不再标产品单维合计——仅在该 concept 的
        单维事实为空时启用，避免与合计双计。"""
        sums: dict = {}
        for cref, val in facts[ln].items():
            c = contexts[cref]
            if c["typed"] or axis not in c["dims"] or not span_ok(c):
                continue
            extra = {d: m for d, m in c["dims"].items()
                     if d not in (axis, "StatementBusinessSegmentsAxis")}
            if "StatementBusinessSegmentsAxis" not in c["dims"]:
                continue
            if extra and extra != {"ConsolidationItemsAxis": "OperatingSegmentsMember"}:
                continue
            key = (c["start"], c["end"], c["dims"][axis])
            sums[key] = sums.get(key, 0) + val
        return sums

    # 合并总额：按主线口径优先序跨 concept 取无维度事实（银行的
    # Total net revenue 必须压过 ASC 606 附注口径；GOOG 的总额和分部
    # 也不在同一 concept 下）
    ref_totals: dict = {}
    for ln in TOPLINE_PRIORITY:
        for cref, val in facts[ln].items():
            c = contexts[cref]
            if c["typed"] or c["dims"] or not span_ok(c):
                continue
            ref_totals.setdefault((c["start"], c["end"]), val)

    periods: dict = {}
    # concept 按轴独立选，且不能「第一个有事实的就用」：SOFI 的分部轴上
    # ASC 606 附注（子集，缺 Lending）和 ASC 280 分部表并存——按「成员和
    # 贴近合并总额」的对账质量挑 concept，平手按候选顺序
    for axis, axis_key in TARGET_AXES.items():
        cands = []
        for ln in REVENUE_LOCALNAMES:
            rows = axis_facts(ln, axis)
            if not rows and axis != "StatementBusinessSegmentsAxis":
                rows = axis_facts_matrix(ln, axis)
            if rows:
                cands.append((ln, rows))
        if not cands:
            continue
        best = None
        for order, (ln, rows) in enumerate(cands):
            sums: dict = {}
            for (s, e, _m), v in rows.items():
                sums[(s, e)] = sums.get((s, e), 0) + v
            diffs = [abs(msum - ref_totals[k]) / abs(ref_totals[k])
                     for k, msum in sums.items()
                     if k in ref_totals and ref_totals[k]]
            score = sorted(diffs)[len(diffs) // 2] if diffs else float("inf")
            if best is None or score < best[0] - 1e-9:
                best = (score, order, rows)
        rows = best[2]
        for (s, e, member), val in rows.items():
            slot = periods.setdefault((s, e), {"total": None, "axes": {}})
            slot["axes"].setdefault(axis_key, {})[member] = val

    for key, slot in periods.items():
        slot["total"] = ref_totals.get(key)

    # 集中度披露：ConcentrationRiskPercentage1 + 全部维度原样带出，
    # 分类（类型/基准/交易对手）留给服务端——基准轴必须保留，
    # 「占应收款 46%」标成「占营收 46%」是对标站踩过的错
    concentration = []
    for (_ln, cref), val in conc_facts.items():
        c = contexts[cref]
        if c["typed"]:
            continue
        concentration.append({"start": c["start"], "end": c["end"],
                              "days": c["days"], "value": val,
                              "dims": dict(c["dims"])})
    return {"periods": periods, "concentration": concentration}


def _parse_filing_cached(client: httpx.Client, cik: int, acc: str) -> dict:
    """按 accession 落盘缓存：申报内容不可变，解析一次终身复用。
    原子写入（tmp+replace），读到损坏文件就删掉重解析。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{cik}_{acc.replace('-', '')}_v{PARSE_VER}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache.unlink(missing_ok=True)
    content = _fetch_instance(client, cik, acc)
    if content is None:  # 超大 instance：跳过解析，与旧行为一致
        parsed = {"periods": {}, "concentration": []}
    else:
        try:
            parsed = _parse_instance(content)
        except ET.ParseError:
            # 畸形 instance：跳过该申报，不毒化请求
            parsed = {"periods": {}, "concentration": []}
    out = {"periods": {f"{s}|{e}": v for (s, e), v in parsed["periods"].items()},
           "concentration": parsed.get("concentration", [])}
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False))
    try:
        os.replace(tmp, cache)
    except FileNotFoundError:
        pass  # 并发清扫删了 tmp：数据已在手，丢一次落盘无害
    return out


def _tol(total: float) -> float:
    return max(abs(total) * 0.005, 2e6)


def _drop_rollups(members: dict, total) -> tuple[dict, bool | None]:
    """成员里混着父级汇总（rollup）时会重复计数。用 成员和-总额=溢出，
    在成员里找 1-3 个的组合恰好等于溢出的，剔掉它们。"""
    if not members or total is None:
        return members, None
    tol = _tol(total)
    ssum = sum(members.values())
    if abs(ssum - total) <= tol:
        return members, True
    excess = ssum - total
    if excess > 0:
        names = list(members)
        for size in (1, 2, 3):
            for combo in combinations(names, size):
                if abs(sum(members[n] for n in combo) - excess) <= tol:
                    kept = {n: v for n, v in members.items() if n not in combo}
                    if kept:
                        return kept, True
    return members, False


def _list_filings(client: httpx.Client, cik: int, cutoff: str) -> list[dict]:
    """10-K/10-Q 列表，reportDate >= cutoff。recent 只保证最近 1000 份或
    1 年（GS/JPM 恰好只剩 1 年），不够就继续翻 filings.files 分页。"""
    subs = _get(client, f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()

    def block_rows(block: dict) -> list[dict]:
        return [dict(zip(("form", "acc", "report", "filed"), t))
                for t in zip(block["form"], block["accessionNumber"],
                             block["reportDate"], block["filingDate"])]

    rows = block_rows(subs["filings"]["recent"])
    oldest_filed = min((r["filed"] for r in rows), default="")
    if oldest_filed and oldest_filed > cutoff:
        for extra in subs["filings"].get("files", []):
            if extra["filingTo"] >= cutoff:
                older = _get(client, "https://data.sec.gov/submissions/"
                             + extra["name"]).json()
                rows += block_rows(older)
    return [r for r in rows
            if r["form"] in ("10-K", "10-Q") and r["report"] and r["report"] >= cutoff]


def _conc_group(entry: dict):
    """集中度分组键（期间+类型+基准）：序数命名跨申报不稳定（FY24 的
    10-K 叫 CustomerOne、下年比较期叫 CustomerB，同一家客户），必须
    整组取 filed 最新；分组不带类型+基准又会被 10-Q 复用年度上下文的
    应收款事实把 10-K 的营收集中度整组误杀。"""
    bench = type_m = ""
    for axis, m in entry["dims"].items():
        if "Benchmark" in axis:
            bench = m
        elif "ConcentrationRiskByType" in axis:
            type_m = m
    return (entry["start"], entry["end"], type_m, bench)


def _collect_versions(client: httpx.Client, cik: int, picked: list[dict]):
    """逐申报收集：每期全部成员版本（比较期会被多份申报覆盖）、
    无维度总额、集中度分组。

    单份申报结构性缺 instance（zip 兜底后仍没有）只跳过该申报并记录，
    不再击杀整卡——否则其余十几份可解析申报陪葬，卡片只剩一行报错。
    瞬态失败（限速/维护）必须继续冒泡：吞掉会把空结果焊死进落盘缓存。"""
    versions: dict = {}
    totals: dict = {}
    conc_cells: dict = {}  # _conc_group -> (filed, [entries])
    skipped: list = []     # [(acc, 原因)]，上浮到 API warning
    for row in picked:
        try:
            parsed = _parse_filing_cached(client, cik, row["acc"])
        except SegmentsError as e:
            if e.transient:
                raise
            skipped.append((row["acc"], str(e)))
            continue
        for entry in parsed.get("concentration", []):
            k = _conc_group(entry)
            if k not in conc_cells or row["filed"] > conc_cells[k][0]:
                conc_cells[k] = (row["filed"], [entry])
            elif row["filed"] == conc_cells[k][0]:
                conc_cells[k][1].append(entry)
        for pkey, slot in parsed["periods"].items():
            s, e = pkey.split("|")
            if slot.get("total") is not None:
                if (s, e) not in totals or row["filed"] > totals[(s, e)][0]:
                    totals[(s, e)] = (row["filed"], slot["total"])
            for axis_key, members in slot.get("axes", {}).items():
                versions.setdefault((axis_key, s, e), []).append(
                    (row["filed"], members))
    return versions, totals, conc_cells, skipped


def _detect_aliases(versions: dict) -> dict:
    """跨申报改名成员缝合：MSFT 把 SearchAndNewsAdvertising 改名
    SearchAdvertising 并按新名重标比较期——同一期的新旧两版里，
    值完全相等且双向唯一的成员对视为改名，旧名统一映射到新名。"""
    aliases: dict = {}
    for (axis_key, _s, _e), vers in versions.items():
        if len(vers) < 2:
            continue
        vers.sort(key=lambda t: t[0])  # 只按 filed 排序（dict 不可比较）
        newest = vers[-1][1]
        for _, old in vers[:-1]:
            for om, ov in old.items():
                if om in newest or abs(ov) < 1e6:
                    # 近零值谁都能对上（LLY 曾把 FY22 的 COVID 抗体 0 值
                    # 错配成 Zepbound），$1M 以下不参与改名判定
                    continue
                hits = [nm for nm, nv in newest.items()
                        if nm not in old and abs(nv - ov) <= max(abs(ov) * 1e-6, 1)]
                if len(hits) == 1:
                    aliases.setdefault(axis_key, {})[om] = hits[0]
    return aliases


def _pick_cells(versions: dict, aliases: dict) -> dict:
    """整期按 filed 最新的申报取（分部重述时不混用新旧口径），
    成员名过一遍改名映射（防环）。"""
    def rename(axis_key: str, members: dict) -> dict:
        amap = aliases.get(axis_key)
        if not amap:
            return members
        out = {}
        for m, v in members.items():
            seen = {m}
            while m in amap and amap[m] not in seen:
                m = amap[m]
                seen.add(m)
            out.setdefault(m, v)
        return out

    cells: dict = {}
    for (axis_key, s, e), vers in versions.items():
        filed, members = max(vers, key=lambda t: t[0])
        cells[(axis_key, s, e)] = (filed, rename(axis_key, members))
    return cells


def _build_axes(cells: dict, totals: dict) -> dict:
    axes: dict = {}
    for (axis_key, s, e), (_, members) in cells.items():
        total = totals.get((s, e), (None, None))[1]
        members, reconciled = _drop_rollups(dict(members), total)
        days = (date.fromisoformat(e) - date.fromisoformat(s)).days
        kind = "quarterly" if days <= 100 else "annual"
        axes.setdefault(axis_key, {"annual": {}, "quarterly": {}})[kind][e] = {
            "members": members, "total": total,
            "reconciled": reconciled, "derived": False,
        }
    return axes


def _derive_q4(axes: dict) -> None:
    """Q4 推导：年度 - 同财年前三季（成员必须三季齐全才推）。
    推导后按成员和 vs 推导总额重新对账：重述会让新旧口径相减出坏数
    （NVDA 地区轴曾差 14-26%），超容差整期丢弃，宁缺勿错。"""
    for data in axes.values():
        for a_end, a_cell in data["annual"].items():
            if a_end in data["quarterly"]:
                continue
            ae = date.fromisoformat(a_end)
            in_year = {k: v for k, v in data["quarterly"].items()
                       if timedelta(0) < ae - date.fromisoformat(k)
                       < timedelta(days=340)}
            if len(in_year) != 3:
                continue
            q4 = {}
            for m, v in a_cell["members"].items():
                if all(m in q["members"] for q in in_year.values()):
                    q4[m] = v - sum(q["members"][m] for q in in_year.values())
            if not q4:
                continue
            total = None
            a_total = a_cell["total"]
            q_totals = [q["total"] for q in in_year.values()]
            if a_total is not None and all(t is not None for t in q_totals):
                total = a_total - sum(q_totals)
            reconciled = None
            if total is not None:
                reconciled = abs(sum(q4.values()) - total) <= _tol(total)
                if not reconciled:
                    continue
            data["quarterly"][a_end] = {
                "members": q4, "total": total,
                "reconciled": reconciled, "derived": True,
            }
    for data in axes.values():
        data["annual"] = dict(sorted(data["annual"].items()))
        data["quarterly"] = dict(sorted(data["quarterly"].items()))


def _dedupe_concentration(conc_cells: dict) -> list:
    """同一份申报内 inline-XBRL 可能重复吐同一事实，按维度+期间去重。"""
    seen: set = set()
    out = []
    for _, (_filed, entries) in sorted(conc_cells.items()):
        for e in entries:
            k = (tuple(sorted(e["dims"].items())), e["start"], e["end"])
            if k not in seen:
                seen.add(k)
                out.append(e)
    out.sort(key=lambda e: (e["end"], -e["value"]))
    return out


def _sweep_stale_cache() -> None:
    """PARSE_VER 升级后旧版本缓存全部变孤儿（曾积到六成死重），连同
    进程被杀残留的 .tmp 一起清掉；容忍并发（missing_ok）。"""
    if not CACHE_DIR.exists():
        return
    keep = f"_v{PARSE_VER}.json"
    now = time.time()
    for f in CACHE_DIR.iterdir():
        try:
            if f.name.endswith(".tmp"):
                # 只清一小时前的残留 tmp：并发冷取数正在写的不能碰
                if now - f.stat().st_mtime > 3600:
                    f.unlink(missing_ok=True)
            elif f.name.endswith(".json") and not f.name.endswith(keep):
                f.unlink(missing_ok=True)
        except OSError:
            pass  # Windows 上删被占用文件会 PermissionError，跳过即可


def build_segments(ticker: str, email: str, cik: int | None = None,
                   years: int = 3) -> dict:
    """返回 {ticker, cik, axes: {product|segment|geo: {"annual": {end: {...}},
    "quarterly": {end: {"members", "total", "reconciled", "derived"}}}},
    concentration: [...]}"""
    ticker = ticker.upper()
    with httpx.Client(headers=_headers(email), timeout=90,
                      follow_redirects=True) as client:
        if cik is None:
            m = _get(client, "https://www.sec.gov/files/company_tickers.json").json()
            cik = next((int(v["cik_str"]) for v in m.values()
                        if v["ticker"].upper() == ticker), None)
            if cik is None:
                raise SegmentsError(f"SEC EDGAR 中未找到 {ticker}")

        rows = _list_filings(
            client, cik,
            (date.today() - timedelta(days=int(years * 366) + 400)).isoformat())
        if not rows:
            raise SegmentsError(f"{ticker} 没有 10-K/10-Q 申报（外国发行人暂不支持分部图）")
        latest = max(r["report"] for r in rows)
        cutoff = (date.fromisoformat(latest)
                  - timedelta(days=int(years * 366) + 30)).isoformat()
        picked = sorted((r for r in rows if r["report"] >= cutoff),
                        key=lambda r: r["report"], reverse=True)[:MAX_FILINGS]

        _sweep_stale_cache()
        versions, totals, conc_cells, skipped = _collect_versions(
            client, cik, picked)

    aliases = _detect_aliases(versions)
    cells = _pick_cells(versions, aliases)
    axes = _build_axes(cells, totals)
    _derive_q4(axes)
    return {"ticker": ticker, "cik": cik, "axes": axes,
            "concentration": _dedupe_concentration(conc_cells),
            "skipped": skipped}


def main() -> None:
    ticker, out_path, email = sys.argv[1].upper(), sys.argv[2], sys.argv[3]
    years = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    try:
        out = build_segments(ticker, email, years=years)
    except SegmentsError as e:
        raise SystemExit(str(e))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    for axis_key, data in out["axes"].items():
        q, a = data["quarterly"], data["annual"]
        print(f"{axis_key}: 季度 {len(q)} 期 / 年度 {len(a)} 期")
        if q:
            end, cell = list(q.items())[-1]
            top = sorted(cell["members"].items(), key=lambda kv: -kv[1])[:6]
            print(f"  最新季 {end} (对账={cell['reconciled']}):",
                  {m: round(v / 1e6) for m, v in top})


if __name__ == "__main__":
    main()
