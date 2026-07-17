# -*- coding: utf-8 -*-
"""从 10-K/10-Q HTML 里定位估值判断层需要的关键章节，输出精简 JSON。

用法: python extract_sections.py OUT.json FILE1.htm [FILE2.htm ...]
关键词由代码固定——分部表、税率、资本开支指引、流动性、其他收益（一次性项目线索）。
每个命中只保留前后一段文本，控制总量（LLM 判断层不需要全文）。
"""
import json
import re
import sys

from bs4 import BeautifulSoup

KEYWORDS = [
    "Segment income", "reportable segments", "income (loss) from operations by segment",
    "effective tax rate", "one-time", "Other income (expense), net",
    "capital expenditures", "Cash, cash equivalents, and marketable securities",
    "Cash and marketable investments", "repurchase", "guidance", "expect revenue",
]
CTX_BEFORE, CTX_AFTER, MAX_HITS, MAX_TOTAL = 150, 900, 3, 45_000

out = {}
total = 0
for path in sys.argv[2:]:
    with open(path, encoding="utf-8", errors="ignore") as f:
        soup = BeautifulSoup(f.read(), "lxml")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    hits = []
    for kw in KEYWORDS:
        for i, m in enumerate(re.finditer(re.escape(kw), text, re.IGNORECASE)):
            if i >= MAX_HITS:
                break
            s = max(0, m.start() - CTX_BEFORE)
            e = min(len(text), m.end() + CTX_AFTER)
            snippet = text[s:e]
            if any(snippet[:200] == h["text"][:200] for h in hits):
                continue
            hits.append({"keyword": kw, "text": snippet})
            total += len(snippet)
            if total > MAX_TOTAL:
                break
        if total > MAX_TOTAL:
            break
    out[path.replace("\\", "/").rsplit("/", 1)[-1]] = hits

json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"sections -> {sys.argv[1]} ({total:,} chars from {len(sys.argv) - 2} files)")
