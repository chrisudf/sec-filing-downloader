# -*- coding: utf-8 -*-
"""424B 招股书封面解析：只认「固定价格的股票发行」，抽发行价 / 定价日 / 前收盘 / 股数 / 一二级。

纯函数，不做网络请求（下载与缓存在 offering_service）。规则来自 2026-10-17 的
offering-price-study：R1000 2010-2026 共 19,451 份 424B 逐份解析，1,166 笔事件里
92% 的发行价能被封面自己写的前收盘价与行情逐笔对上。排除口径：

- 初步版（Subject to Completion）——没有价格；
- 发债、优先股、可转债、权证发行（TARP 权证拍卖之类）；
- ATM / 销售代理协议——没有单一价格；
- DRIP、员工计划、转售注册、并购代理书、配股；
- IPO（没有前收盘价可对）。

大宗交易（承销商以固定价买入、再按市价分批卖出）取承销商买入价，var_price=True。
ATM 虽然拒收为定价增发（reason="atm"），但仍抽额度上限（美元或股数）与是否只是修订既有
额度——只靠 ATM 融资的公司（RKLB 之类）这才是该看到的稀释信息。
解析不到就返回 reject 及原因，宁缺勿错。
"""
from __future__ import annotations

import html
import re
from datetime import datetime

PARSE_VER = 1        # 解析口径变更时 +1，使落盘缓存失效
COVER_CHARS = 9_000  # 封面页文本窗口


def html_to_text(raw: str) -> str:
    """HTML → 单行纯文本（去 script/style 与标签、反转义、压空白）。"""
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|th)>", " ", raw)
    t = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    t = t.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", t).strip()


MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
DATE_RE = MONTH + r"\s+\d{1,2}\s*,\s+\d{4}"
NUM = r"\$\s?([\d,]+(?:\.\d+)?)"

ANCHOR_RE = re.compile(r"(?<!maximum\s)(?<!aggregate\s)(?:public\s+offering\s+price|price\s+to\s+(?:the\s+)?public|"
                       r"(?<!maximum\s)offering\s+price)", re.I)
PER_SHARE_NUM = re.compile(r"per\s+(?:share|ADS|common\s+share)\s*(?:\(\d\)\s*)?" + NUM, re.I)
FIRST_NUM = re.compile(NUM)
BLOCK_RE = re.compile(r"underwriters?\s+(?:has|have)\s+agreed\s+to\s+purchase[^.]{0,300}?\bat\s+(?:a\s+price\s+of\s+)?"
                      + NUM + r"\s+per\s+(?:share|ADS)", re.I)
VARPRICE_RE = re.compile(r"from\s+time\s+to\s+time[^.]{0,200}?market\s+prices\s+prevailing|"
                         r"at\s+prevailing\s+market\s+prices|at\s+negotiated\s+prices", re.I)
LAST_RE = re.compile(r"last\s+(?:reported\s+)?(?:sale|sales|closing)\s+price\s+[^.]{0,160}?(?:on|as\s+of)\s+(" + DATE_RE +
                     r")[^.$]{0,60}?\s+was\s+" + NUM, re.I)
LAST_RE2 = re.compile(r"On\s+(" + DATE_RE + r"),\s+the\s+(?:last\s+reported\s+|reported\s+closing\s+|last\s+|closing\s+)"
                      r"(?:sale\s+|sales\s+|closing\s+)?price\s+"
                      r"[^.$]{0,200}?\s+was\s+" + NUM, re.I)
DATE_SUPP_RE = re.compile(r"(?:date\s+of\s+this\s+prospectus(?:\s+supplement)?\s+is|"
                          r"(?<!to\s)(?<!To\s)(?<!TO\s)(?<!\()prospectus(?:\s+supplement)?\s+dated)\s+(" + DATE_RE + r")", re.I)
DISC_RE = re.compile(r"underwriting\s+discounts?(?:\s+and\s+commissions)?\s*(?:\(\d\)\s*)?" + NUM, re.I)
SHARES_RE = re.compile(r"(?<!additional\s)(\d{1,3}(?:,\d{3})+)\s+(?:shares|American\s+Depositary)", re.I)
DOLLAR_DEAL_RE = re.compile(r"^\s*\$\s?([\d,]{7,})\s+(?:of\s+)?(?:shares\s+of\s+)?(?:our\s+)?(?:class\s+[a-c]\s+)?common", re.I)

EQUITY_RE = re.compile(r"common\s+stock|ordinary\s+shares|common\s+shares|American\s+Depositary\s+Shares|"
                       r"class\s+[a-c]\s+(?:common|ordinary|subordinate)|common\s+units|shares\s+of\s+beneficial\s+interest", re.I)
DEBT_RE = re.compile(r"\d+(?:\.\d+)?%\s+(?:senior\s+|subordinated\s+|fixed|floating|notes|debentures)|"
                     r"\bnotes\s+due\b|debentures\s+due|floating\s+rate\s+notes", re.I)
PREF_RE = re.compile(r"preferred\s+stock|depositary\s+shares,?\s+each\s+representing|preference\s+shares|"
                     r"mandatory\s+convertible|convertible\s+senior\s+notes|tangible\s+equity\s+units|equity\s+units|"
                     r"preferred\s+shares|series\s+[a-z]\s+preferred", re.I)
ATM_RE = re.compile(r"at[\s-]+the[\s-]+market|sales\s+agents?\b|equity\s+distribution\s+agreement|"
                    r"distribution\s+agents?\b|sales\s+agreement|open\s+market\s+sale|equity\s+sales\s+agreement", re.I)
PRELIM_RE = re.compile(r"subject\s+to\s+completion|preliminary\s+prospectus|is\s+not\s+complete\s+and\s+may\s+be\s+changed", re.I)
DRIP_RE = re.compile(r"dividend\s+reinvestment|direct\s+stock\s+purchase|employee\s+stock|stock\s+purchase\s+plan|"
                     r"redemption\s+of\s+(?:class\s+a\s+)?common\s+units|proxy\s+statement|exchange\s+offer|rights\s+offering", re.I)
IPO_RE = re.compile(r"initial\s+public\s+offering|no\s+public\s+market|prior\s+to\s+this\s+offering,?\s+there\s+has\s+been\s+no", re.I)
SELLER_RE = re.compile(r"selling\s+(?:stockholder|shareholder|securityholder|unitholder)s?", re.I)
NOPROCEEDS_RE = re.compile(r"(?:we|the\s+company|[A-Z][\w.&]+(?:\s+[A-Z][\w.&]+){0,3})\s+will\s+not\s+receive\s+any\s+"
                           r"(?:of\s+the\s+)?proceeds", re.I)
WE_OFFER_RE = re.compile(r"\bwe\s+are\s+(?:offering|selling)\b|are\s+being\s+(?:offered|sold)\s+by\s+(?:us|the\s+company)|"
                         r"\bis\s+offering\s+[\d,]+\s+shares\s+of\s+its", re.I)
FORWARD_RE = re.compile(r"forward\s+(?:sale|purchaser|seller)", re.I)
WARRANT_RE = re.compile(r"warrants?\s+to\s+purchase|pre-funded\s+warrants|accompanying\s+warrants|common\s+warrants", re.I)
REGDIRECT_RE = re.compile(r"registered\s+direct|placement\s+agents?\b", re.I)
CONCURRENT_BUYBACK_RE = re.compile(r"concurrent\s+(?:share\s+)?repurchase|"
                                   r"we\s+(?:will|intend\s+to|have\s+agreed\s+to)\s+(?:purchase|repurchase)\s+from\s+the\s+underwriters", re.I)
WARRANT_OFFER_RE = re.compile(r"^.{0,500}?(?:\bwarrants\s+to\s+purchase\s+(?:up\s+to\s+)?[\d,]+\s+shares|"
                              r"[\d,]+\s+warrants,?\s+each\s+(?:to\s+purchase|of\s+which|representing)|"
                              r"is\s+offering\s+(?:to\s+sell\s+)?[\d,]+\s+warrants)", re.I)
# 修订既有额度：编号补充、修订协议、只为加销售代理而发的补充（GOOG 2026-08-06 / ORCL 2026-02-03：
# 「This Supplement is being filed to reflect the addition of … sales agents」）
ATM_AMEND_RE = re.compile(r"supplement\s+no\.\s*\d|amendment\s+no\.\s*\d|supplements\s+and\s+amends|"
                          r"amends\s+and\s+supplements|supplement\s+is\s+being\s+filed\s+to\s+reflect|"
                          r"additional\s+sales\s+agents\s+pursuant\s+to\s+a\s+joinder", re.I)
ATM_USD_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3}){2,})(?:\.\d+)?")
ATM_AGG_RE = re.compile(r"aggregate\s+(?:gross\s+)?(?:offering|sales)\s+(?:price|amount)\s+of\s+up\s+to\s+"
                        r"\$\s?(\d{1,3}(?:,\d{3}){2,})", re.I)
ATM_SHARES_RE = re.compile(r"(\d{1,3}(?:,\d{3})+)\s+(?:shares|common\s+shares)", re.I)
# 续展：新协议只承接旧协议没卖完的额度（RKLB 2026-08-13：「No additional amount beyond the
# unsold offering amount」），不是新开额度
ATM_ROLLOVER_RE = re.compile(r"no\s+additional\s+amount\s+beyond\s+the\s+unsold|amount\s+that\s+remained\s+unsold|"
                             r"equal\s+to\s+the\s+(?:amount|portion)\s+that\s+remain(?:s|ed)\s+unsold", re.I)
# 新封面披露的旧协议实际卖出额：「we have sold shares … aggregate gross sales price of approximately
# $749.4 million under the sales agreement, dated as of September 15, 2025」
ATM_PRIOR_SOLD_RE = re.compile(
    r"have\s+sold\s+(?:shares\s+of\s+(?:our\s+)?common\s+stock\s+)?with\s+an\s+aggregate\s+(?:gross\s+)?sales\s+price\s+of\s+"
    r"(?:approximately\s+)?\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(million|billion)?[^.]{0,160}?dated\s+(?:as\s+of\s+)?("
    + DATE_RE + ")", re.I)
FEE_RE = re.compile(r"calculation\s+of\s+(?:the\s+)?registration\s+fee", re.I)
FEE_END_RE = re.compile(r"\(\s*to\s+prospectus\s+dated|filed\s+pursuant\s+to\s+rule\s+424", re.I)


def _date(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", s))
    try:
        return datetime.strptime(s, "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def cover_head(text: str) -> str:
    """封面页文本：跳过 424 标记前的内容和注册费表，截到下一个 Table of Contents。"""
    t = text[:15_000]
    m = re.search(r"filed\s+pursuant\s+to\s+rule\s+424", t, re.I)
    t = text[m.start(): m.start() + COVER_CHARS + 3_000] if m and m.start() < 12_000 else text[:COVER_CHARS + 3_000]
    fee = FEE_RE.search(t[:3_000])
    if fee:
        end = FEE_END_RE.search(t, fee.end())
        t = t[: fee.start()] + " " + (t[end.start():] if end else t[fee.end() + 2_500:])
    toc = re.search(r"table\s+of\s+contents", t[700:], re.I)
    return t[: 700 + toc.start()] if toc else t[:COVER_CHARS]


def find_price(head: str) -> tuple[float | None, str]:
    for a in ANCHOR_RE.finditer(head):
        win = head[a.end(): a.end() + 450]
        m = PER_SHARE_NUM.search(win)
        if m and m.start() < 420:
            return _num(m.group(1)), "table"
        m = FIRST_NUM.match(re.sub(r"^\s*(?:per\s+(?:share|ADS)\s*)?(?:\(\d\)\s*)?[:\s]*", "", win))
        if m:
            return _num(m.group(1)), "inline"
        m = re.match(r"[^$.]{0,100}?\s+(?:was|is|of)\s+" + NUM, win, re.I)
        if m:
            return _num(m.group(1)), "sentence"
    m = BLOCK_RE.search(head)
    if m:
        return _num(m.group(1)), "block"
    return None, ""


def parse_atm(head: str, text: str = "") -> dict:
    """ATM 补充文件封面：额度上限（美元优先，其次股数）、是否只是修订既有额度、
    封面写的前收盘价。研究缓存 679 份 ATM 封面：美元 533、股数 114、仅修订 1、
    抽不到 31。ATM 封面常带一长段销售代理条款，前收盘那句可能落在 COVER_CHARS
    之外（RKLB 2026-08 在第 ~17,000 字），所以前收盘在全文前 25,000 字、第一个
    目录之前再找一次。"""
    body = re.sub(r"^.*?\(\s*to\s+(?:the\s+)?prospectus[^)]*\)", "", head[:2_500], count=1, flags=re.I | re.S)
    lead = body[:500]
    m = ATM_AGG_RE.search(head) or ATM_USD_RE.search(lead)
    usd = float(m.group(1).replace(",", "")) if m else None
    shares = None
    if usd is None:
        sm = ATM_SHARES_RE.search(lead[:350])
        shares = int(sm.group(1).replace(",", "")) if sm else None
    window = text[:25_000]
    toc = re.search(r"table\s+of\s+contents", window[1_000:], re.I)
    if toc:
        window = window[:1_000 + toc.start()]
    last = (LAST_RE2.search(head) or LAST_RE.search(head)
            or LAST_RE2.search(window) or LAST_RE.search(window))
    prior = ATM_PRIOR_SOLD_RE.search(head) or ATM_PRIOR_SOLD_RE.search(window)
    prior_usd = None
    if prior:
        prior_usd = _num(prior.group(1)) * {"million": 1e6, "billion": 1e9}.get((prior.group(2) or "").lower(), 1)
    return {"atm_usd": usd, "atm_shares": shares, "atm_amendment": bool(ATM_AMEND_RE.search(head[:1_500])),
            "atm_rollover": bool(ATM_ROLLOVER_RE.search(head) or ATM_ROLLOVER_RE.search(window)),
            "atm_prior_sold_usd": prior_usd,
            "atm_prior_date": _date(prior.group(3)) if prior else None,
            "atm_last_date": _date(last.group(1)) if last else None,
            "atm_last_price": _num(last.group(2)) if last else None}


def parse_cover(text: str) -> dict:
    """返回 {'kind': 'priced_equity'|'reject', 'reason':..., 字段...}。"""
    head = cover_head(text)
    title = head[:1_600]
    out: dict = {"kind": "reject"}
    if PRELIM_RE.search(head[:1_500]):
        return dict(out, reason="preliminary")
    if PREF_RE.search(title):
        return dict(out, reason="preferred/convertible")
    if DEBT_RE.search(title) and not re.search(r"\d[\d,]+\s+shares", title, re.I):
        return dict(out, reason="debt")
    if not EQUITY_RE.search(head):
        return dict(out, reason="not_equity")
    if DRIP_RE.search(title):
        return dict(out, reason="plan/resale/other")
    if WARRANT_OFFER_RE.search(re.sub(r"^.*?\(\s*to\s+prospectus\s+dated[^)]*\)", "", title, flags=re.I)):
        return dict(out, reason="warrants_offering")
    price, how = find_price(head)
    if price is None:
        if ATM_RE.search(head):
            return dict(out, reason="atm", **parse_atm(head, text))
        return dict(out, reason="no_price")
    if not (0.05 <= price <= 20_000):
        return dict(out, reason="bad_price")
    last = LAST_RE2.search(head) or LAST_RE.search(head)
    if IPO_RE.search(head) and not last:
        return dict(out, reason="ipo")

    d = DATE_SUPP_RE.search(head) or DATE_SUPP_RE.search(text[:60_000])
    disc = DISC_RE.search(head)
    shares = SHARES_RE.search(title) or SHARES_RE.search(head)
    deal_usd = DOLLAR_DEAL_RE.search(re.sub(r"^.{0,400}?\)\s*", "", title[:600]))

    sellers = bool(SELLER_RE.search(head))
    noproc = bool(NOPROCEEDS_RE.search(head))
    we = bool(WE_OFFER_RE.search(head))
    if sellers and (noproc or not we):
        who = "secondary"
    elif sellers and we:
        who = "mixed"
    else:
        who = "primary"

    disc_v = _num(disc.group(1)) if disc else None
    return {
        "kind": "priced_equity",
        "price": price,
        "price_how": how,
        "pricing_date": _date(d.group(1)) if d else None,
        "last_date": _date(last.group(1)) if last else None,
        "last_price": _num(last.group(2)) if last else None,
        "shares": int(shares.group(1).replace(",", "")) if shares else None,
        "deal_usd_text": _num(deal_usd.group(1)) if deal_usd else None,
        "uw_disc": disc_v if disc_v is not None and disc_v < price * 0.2 else None,
        "who": who,
        "forward": bool(FORWARD_RE.search(head)),
        "warrants": bool(WARRANT_RE.search(head)),
        "reg_direct": bool(REGDIRECT_RE.search(head)),
        "atm_words": bool(ATM_RE.search(head)),
        "var_price": bool(VARPRICE_RE.search(head)) or how == "block",
        "concurrent_buyback": bool(CONCURRENT_BUYBACK_RE.search(head)),
    }
