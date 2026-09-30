"""年报封面载明的 ADS 比例：Form 20-F / 10-K 封面 Section 12(b) 一栏的
「American Depositary Shares, each representing N shares」。纯函数，不联网。

ADR 标定的第二个证人（第一个是 市值÷XBRL 股数 反推的 raw，见
valuation_service._adr_calibration）。raw 只有数字：(0.5, 2) 带内的简单分数窗口
连成片、比例 >= 7 时相邻整数的间距小于口径噪声，光看数字分不出 CANF（封面 2、
raw 0.768）、JOYY（封面 20、raw 21.75 被就近取整成 22）这类。封面是一手原文——
但也不是真相：MFG 的 20-F 封面写 1 ADS = 2 股，东京股价折算实为 0.2。所以它只是
第二个证人，定案靠两者对拍（valuation_service._adr_cover_check）。

2026-09-30 实测 51 份 10-K/20-F（50 只 ADR + AAPL/TSLA）全部解析正确，写法都在
12(b) 一栏（HMC 在封面末尾的脚注，离 12(b) 约 5.2k 字；TAK 标题没写数字 → None）：
  each representing five-ninths of one share / one-half of one share / one -fourth
  every three representing two Class A / each three representing one Class A
  each representing 0.2 Class A / 20 B Shares / fifty / one hundred / forty-eight
  (each American depositary share representing eight Class A ...)
  (one American depositary share representing two Class A ...)（JD、BEKE、PDD）
  each of which represents one share（IX，拼作 depository）/ each represented by（INFY）
  Each American Depositary Receipt represents two ordinary shares（BHP）
  Each American Depositary Share representing ten shares（TM 脚注）
正文里的比例变更句（CANF「from one (1) ADS representing three hundred (300) ... to ...
one (1) ADS representing two (2)」、IX「to a ratio of one ADS representing one」、TAL
脚注「changed from one ADS representing two ... to three ADSs representing one」）
不认：one 开头的句式前文带 from/change/ratio 就跳过，其余句式要求 each/every 开头。
认不出就返回 None，调用方退回纯数字规则。
"""
from __future__ import annotations

import html
import re
from fractions import Fraction

_SMALL = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_SMALL.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
               "seventy": 70, "eighty": 80, "ninety": 90})
_DENOM = {"half": 2, "halves": 2, "third": 3, "thirds": 3, "quarter": 4, "quarters": 4,
          "fourth": 4, "fourths": 4, "fifth": 5, "fifths": 5, "sixth": 6, "sixths": 6,
          "seventh": 7, "sevenths": 7, "eighth": 8, "eighths": 8, "ninth": 9, "ninths": 9,
          "tenth": 10, "tenths": 10}

_NUMW = "(?:" + "|".join(sorted(_SMALL, key=len, reverse=True)) + ")"
_WORDS = rf"{_NUMW}(?:[\s-]+(?:{_NUMW}|hundred|thousand))*"
_DIGITS = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"
_FRAC = (rf"(?P<fn>{_NUMW})\s*-\s*(?P<fd>" + "|".join(sorted(_DENOM, key=len, reverse=True))
         + r")\b")
# 数量：分数词必须先试（one-half 不能被读成 one）；数字词后可带「(2)」
_QTY = rf"(?:{_FRAC}|(?P<qd>{_DIGITS})|(?P<qw>{_WORDS})(?:\s*\(\d+\))?)"
_ADS_NOUN = r"(?:american\s+deposit[ao]ry\s+(?:shares?|receipts?)|adss?|adrs?)"
_VERB = r"represent(?:s|ing|ed\s+by)"
# each [of which | ADS 名词] represent(s|ing) / represented by [a|an] QTY
_EACH = re.compile(
    rf"\beach\s+(?:of\s+which\s+|{_ADS_NOUN}\s+)?{_VERB}\s+(?:an?\s+)?{_QTY}", re.I)
# every/each N [ADS 名词] represent(s|ing) QTY  ->  QTY / N
_EVERY = re.compile(
    rf"\b(?:every|each)\s+(?P<nd>\d+|{_NUMW})\s+(?:{_ADS_NOUN}\s+)?(?:{_VERB}|represent)\s+{_QTY}",
    re.I)
# (one American depositary share representing two Class A ...)——JD、BEKE 的封面标题。
# 比例变更句也是这个句式（TAL「changed from one ADS representing two ... to」、IX「to a
# ratio of one ADS representing one」），前文带 from/change/ratio 的不认
_ONE = re.compile(rf"\bone\s+{_ADS_NOUN}\s+{_VERB}\s+(?:an?\s+)?{_QTY}", re.I)
_CHANGE = re.compile(r"\b(?:from|chang\w*|ratio)\b", re.I)
_ANCHOR = re.compile(rf"\b{_ADS_NOUN}\b", re.I)
_SHARE_WORD = re.compile(r"\b(?:shares?|stock)\b", re.I)
_SEC12B = re.compile(r"Section\s*12\s*\(\s*b\s*\)", re.I)
_RAW12B = re.compile(r"12\s*\(\s*b\s*\)")

COVER_WINDOW = 8000        # 12(b) 之后看多少字：12(b) 一栏 + 封面末尾的脚注（HMC 在 +5.2k）
_RAW_WINDOW = 400_000      # 原始 HTML 里从 12(b) 往后取多少字节去剥标签


def _words_to_int(s: str) -> int | None:
    total = cur = 0
    for tok in re.split(r"[\s-]+", s.lower()):
        if tok in _SMALL:
            cur += _SMALL[tok]
        elif tok == "hundred":
            cur = max(cur, 1) * 100
        elif tok == "thousand":
            total, cur = total + max(cur, 1) * 1000, 0
        else:
            return None
    return total + cur


def _qty(m: re.Match) -> Fraction | None:
    if m.group("fn"):
        num = _words_to_int(m.group("fn"))
        return Fraction(num, _DENOM[m.group("fd").lower()]) if num else None
    if m.group("qd"):
        return Fraction(m.group("qd").replace(",", ""))
    n = _words_to_int(m.group("qw"))
    return Fraction(n) if n else None


def cover_text(raw_html: str) -> str:
    """原始 HTML -> 封面窗口纯文本（从 Section 12(b) 起 COVER_WINDOW 字）；找不到 12(b) 返回 ""。
    iXBRL 文档前面是几百 KB 到 2MB+ 的 ix:header（IX 实测 12(b) 在第 2.45M 字节），
    所以先在原始字节里定位 12(b)，只剥它后面一段的标签。"""
    m = _RAW12B.search(raw_html)
    if not m:
        return ""
    chunk = raw_html[max(0, m.start() - 3000): m.start() + _RAW_WINDOW]
    chunk = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", chunk)
    text = html.unescape(re.sub(r"<[^>]*>", " ", chunk))
    text = re.sub(r"[\s​\xa0]+", " ", text)
    s = _SEC12B.search(text)
    return text[s.start(): s.start() + COVER_WINDOW] if s else ""


def parse_ads_ratio(text: str, ticker: str = "") -> tuple[Fraction, str] | None:
    """封面窗口纯文本 -> (1 ADS 代表的普通股数, 原文片段)；认不出返回 None。
    多条命中（多个 ADS 品种）时优先后面紧跟本票代码的那条，否则取第一条。"""
    hits = []
    for rx, per in ((_EACH, False), (_EVERY, True), (_ONE, False)):
        for m in rx.finditer(text):
            if not _ANCHOR.search(text, max(0, m.start() - 120), m.end()):
                continue          # 附近没有 ADS 名词：不是 ADS 比例句
            if rx is _ONE and _CHANGE.search(text, max(0, m.start() - 80), m.start()):
                continue          # 比例变更句里的旧/新比例
            if not _SHARE_WORD.search(text[m.end(): m.end() + 60].split(".")[0]):
                continue
            q = _qty(m)
            if per:
                nd = m.group("nd")
                n = int(nd) if nd.isdigit() else _words_to_int(nd)
                q = q / n if q and n else None
            if q and q > 0:
                hits.append((m.start(), q, text[m.start(): m.end() + 40].strip()))
    if not hits:
        return None
    hits.sort()
    if ticker:
        # 代码只在本条与下一条命中之间找：PBR 那行的窗口不许看到下一行的 PBR.A
        tk = re.compile(rf"(?<![A-Za-z.]){re.escape(ticker)}(?![A-Za-z]|\.[A-Za-z])")
        for i, (pos, q, snip) in enumerate(hits):
            end = min(pos + 250, hits[i + 1][0] if i + 1 < len(hits) else len(text))
            if tk.search(text, pos, end):
                return q, snip
    return hits[0][1], hits[0][2]
