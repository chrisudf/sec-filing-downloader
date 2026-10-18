# -*- coding: utf-8 -*-
"""10-Q / 10-K 附注里的 ATM 实际成交：股数、毛/净收入、均价。

ATM 是在一段时间里按市价陆续卖出，申报日收盘价不是成交价；真正的成交只在定期
报告附注里按期汇总。本模块是尽力而为的句式解析（纯函数），解析不到就返回空，
由前端退回「实际卖出见 10-Q」链接——宁缺勿错。

2026-10-17 对 12 家近期用 ATM 的公司实测（最近一份 10-Q）：句式能覆盖 RKLB / WELL /
SMMT / ORCL / AUR 五家；ASTS / GOOG / O / AGNC / CVNA / VTR / DLR 用表格披露或当季
未卖，返回空。认的句式：
- 「sold 7,783,458 shares … generating $ 1,055,630 in gross proceeds」（金额单位随报表
  「in thousands」，靠市价区间反推单位）
- 「sold 23,578,867 shares … at a weighted-average price of $ 16.18 per share」
- 「issued approximately 141 million shares … for net proceeds of $19.9 billion」
- 领口远期「sold an aggregate of 7,451,200 shares … minimum expected proceeds of approximately
  $ 474,000 and maximum expected proceeds of approximately $ 642,000」→ 价格区间
- 8-K 新闻稿的「先金额后股数」：「raising approximately $1.944 billion in gross proceeds through
  the issuance of 29.3 million shares」（RKLB 2026-09-15，季报之后才卖完的那笔只在这里）
"""
from __future__ import annotations

import re
from datetime import date, datetime

PARSE_VER = 1

_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
SALE_RE = re.compile(
    r"\b(?:sold|issued)\s+(?:an\s+aggregate\s+of\s+|a\s+total\s+of\s+|approximately\s+)?" + _NUM +
    r"\s*(?P<mil>million\s+)?(?:shares|common\s+shares)\b(?P<mid>[^$]{0,260}?)"
    r"(?:(?P<avgkw>(?:weighted[\s-]+)?average\s+(?:sales\s+|sale\s+|offering\s+)?price\s+of\s+(?:approximately\s+)?)"
    r"\$\s?(?P<avg>\d+(?:\.\d+)?)"
    r"|(?P<prockw>gross\s+proceeds\s+of\s+(?:approximately\s+)?|net\s+proceeds\s+of\s+(?:approximately\s+)?|"
    r"generating\s+(?:gross\s+)?(?:proceeds\s+of\s+)?(?:approximately\s+)?|"
    r"for\s+(?:aggregate\s+)?(?:gross\s+|net\s+)?(?:proceeds\s+of\s+)?(?:approximately\s+)?|"
    r"minimum\s+expected\s+proceeds\s+of\s+approximately\s+)"
    r"\$\s?(?P<amt>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?P<unit>million|billion|thousand)?)", re.I)
ATM_CTX_RE = re.compile(r"at[\s-]+the[\s-]+market|\bATM\b|distribution\s+agreement|sales\s+agreement|"
                        r"collared\s+forward|forward\s+sale", re.I)
RAISE_RE = re.compile(
    r"\b(?:raising|raised|generating|generated)\s+(?:approximately\s+|aggregate\s+)?\$\s?(?P<amt>\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
    r"\s*(?P<unit>million|billion|thousand)?\s+(?:in\s+)?(?P<kind>gross|net)?\s*proceeds\s+(?:through|from|by)\s+"
    r"(?:the\s+)?(?:issuance|sale)\s+of\s+(?:approximately\s+)?" + _NUM + r"\s*(?P<mil>million\s+)?(?:shares|common\s+shares)",
    re.I)
MAX_RE = re.compile(r"maximum\s+expected\s+proceeds\s+of\s+approximately\s+\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*"
                    r"(million|billion|thousand)?", re.I)
_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
PERIOD_RE = re.compile(r"(?P<span>(?:three|six|nine|twelve)\s+months|(?:fiscal\s+)?year|(?:first\s+|second\s+|third\s+|fourth\s+)?quarter)"
                       r"\s+ended\s+(" + _MONTH + r"\s+\d{1,2},\s+\d{4})|(?:as\s+of|through)\s+(" + _MONTH +
                       r"\s+\d{1,2},\s+\d{4})", re.I)
LABEL_RE = re.compile(r"(?:under|[Pp]ursuant\s+to)\s+(?:the\s+|our\s+)?((?:terminated\s+)?(?:[A-Z][a-z]+\s+)?(?:\d{4}\s+)?"
                      r"(?:Equity\s+Distribution\s+Agreement|ATM\s+Program|Sales\s+Agreement|ATM\s+Equity\s+Offering|ATM))")
UNIT = {"thousand": 1e3, "million": 1e6, "billion": 1e9}


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def _d(s: str) -> date | None:
    try:
        return datetime.strptime(re.sub(r"\s+", " ", s), "%B %d, %Y").date()
    except ValueError:
        return None


def _scale(amount: float, unit: str | None, shares: float, in_thousands: bool,
           price_range: tuple[float, float] | None) -> float | None:
    """金额 → 美元。有单位词直接用；没有时在 ×1 / ×1000 之间按市价区间择一（报表常是 in thousands）。"""
    if unit:
        return amount * UNIT[unit.lower()]
    cands = [1e3, 1.0] if in_thousands else [1.0, 1e3]
    if price_range:
        lo, hi = price_range
        for f in cands:
            if lo / 3 <= amount * f / shares <= hi * 3:
                return amount * f
        return None
    return amount * cands[0]


def parse_atm_sales(text: str, report_date: str,
                    price_range: tuple[float, float] | None = None) -> list[dict]:
    """返回本期（期末不早于报告期末 40 天前）的 ATM 成交条目，去重，按出现顺序。"""
    rep = date.fromisoformat(report_date)
    in_thousands = bool(re.search(r"in\s+thousands", text, re.I))
    out, seen = [], set()
    for m in SALE_RE.finditer(text):
        if not ATM_CTX_RE.search(text[max(0, m.start() - 1500):m.end() + 300]):
            continue
        shares = _f(m.group(1)) * (1e6 if m.group("mil") else 1)
        if shares < 1_000:
            continue
        before = text[max(0, m.start() - 260):m.start()]
        periods = [p for p in PERIOD_RE.finditer(before)]
        pend = _d(periods[-1].group(2) or periods[-1].group(3)) if periods else None
        span = (periods[-1].group("span") or "").lower() if periods else ""
        quarterly = span.startswith("three") or "quarter" in span
        months = (3 if quarterly else 6 if span.startswith("six") else 9 if span.startswith("nine")
                  else 12 if (span.startswith("twelve") or "year" in span) else None)
        if pend and (rep - pend).days > 40:
            continue                      # 上年同期对比数，不是本期
        lab = (LABEL_RE.search(text[m.end():m.end() + 260]) or LABEL_RE.search(m.group("mid") or "")
               or (list(LABEL_RE.finditer(before[-220:])) or [None])[-1])
        e = {"shares": int(shares), "period_end": pend.isoformat() if pend else None, "quarterly": quarterly,
             "span_months": months,
             "label": re.sub(r"\s+", " ", lab.group(1)) if lab else None,
             "kind": "sale", "avg_price": None, "avg_high": None, "proceeds_usd": None, "proceeds_kind": None}
        if m.group("avg"):
            e["avg_price"] = round(_f(m.group("avg")), 2)
        else:
            kw = m.group("prockw").lower()
            usd = _scale(_f(m.group("amt")), m.group("unit"), shares, in_thousands, price_range)
            if usd is None:
                continue
            e["proceeds_usd"] = usd
            e["proceeds_kind"] = "net" if "net" in kw else "gross"
            e["avg_price"] = round(usd / shares, 2)
            if "minimum" in kw:
                e["kind"] = "collar"
                mx = MAX_RE.search(text[m.end():m.end() + 200])
                if mx:
                    hi = _scale(_f(mx.group(1)), mx.group(2), shares, in_thousands, price_range)
                    e["avg_high"] = round(hi / shares, 2) if hi else None
        key = (e["shares"], e["avg_price"], e["kind"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    for m in RAISE_RE.finditer(text):
        if not ATM_CTX_RE.search(text[max(0, m.start() - 1500):m.end() + 300]):
            continue
        shares = _f(m.group(4)) * (1e6 if m.group("mil") else 1)
        usd = _scale(_f(m.group("amt")), m.group("unit"), shares, in_thousands, price_range) if shares else None
        if not usd or shares < 1_000:
            continue
        e = {"shares": int(shares), "period_end": None, "quarterly": False, "span_months": None,
             "label": None, "kind": "sale",
             "avg_price": round(usd / shares, 2), "avg_high": None, "proceeds_usd": usd,
             "proceeds_kind": (m.group("kind") or "gross").lower()}
        key = (e["shares"], e["avg_price"], e["kind"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    # 同一协议（同 label）既有本季又有年初至今累计时只留本季，避免两条重叠数字并列
    # （年初至今那句常拿不到协议名，label 为 None 也视作同一协议）
    q_labels = {e["label"] for e in out if e["quarterly"]}
    if q_labels:
        q_labels.add(None)
    return [e for e in out if e["quarterly"] or e["kind"] == "collar" or e["label"] not in q_labels]
