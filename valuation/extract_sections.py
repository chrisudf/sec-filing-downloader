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

# v2（2026-07-22）：关键词分两个通道。fact = 财务事实/口径；risk = 恶化信号。
# risk 命中多为风险因素章节的假设性模板文（"可能发生减值"）或会计机制说明——
# A/B 实测（NVDA，5+5 sonnet 采样）显示不加区分地混入 8 条/8.5k 字符风险文本时
# 判断层无法稳定区分"模板披露"与"已发生事件"；打上 channel 标签后由 prompt 明确
# "无已发生金额不构成恶化证据"，保留对真实 going concern/信贷拨备的捕获能力。
KEYWORDS = [
    "Segment income", "reportable segments", "income (loss) from operations by segment",
    "effective tax rate", "one-time", "Other income (expense), net",
    "capital expenditures", "Cash, cash equivalents, and marketable securities",
    "Cash and marketable investments", "repurchase", "guidance", "expect revenue",
]
# 基本面恶化信号——价值投资场景下判断层必须看到这些（若存在），但走独立 risk 通道
KEYWORDS_RISK = [
    "impairment", "restructuring", "going concern", "material weakness",
    "provision for credit losses", "covenant",
]
CTX_BEFORE, CTX_AFTER, MAX_HITS, MAX_TOTAL = 150, 900, 3, 45_000

out = {}
total = 0
files = sys.argv[2:]
# 预算按文件平分（总量不变）：全局先到先得会让排序靠前的文件挤占后面的——
# 两份财报打不满 45k，但 6-K 多附件场景（最多 1+8 个文件）会让后面的文件拿不到配额，
# 而判断层的净现金规则恰恰优先要最新 10-Q 的流动性章节
per_file = MAX_TOTAL // max(1, len(files))
for path in files:
    with open(path, encoding="utf-8", errors="ignore") as f:
        soup = BeautifulSoup(f.read(), "lxml")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    hits = []
    file_total = 0
    full = False
    # fact 通道先行：预算紧张时优先保财务事实摘录，risk 模板文靠后
    for channel, kws in (("fact", KEYWORDS), ("risk", KEYWORDS_RISK)):
        for kw in kws:
            for i, m in enumerate(re.finditer(re.escape(kw), text, re.IGNORECASE)):
                if i >= MAX_HITS:
                    break
                s = max(0, m.start() - CTX_BEFORE)
                e = min(len(text), m.end() + CTX_AFTER)
                snippet = text[s:e]
                if any(snippet[:200] == h["text"][:200] for h in hits):
                    continue
                # 配额在追加前检查：追加后再查会让每个文件超额最多一个 snippet，
                # 多附件场景下累计突破 MAX_TOTAL
                if file_total + len(snippet) > per_file:
                    full = True
                    break
                hits.append({"keyword": kw, "channel": channel, "text": snippet})
                file_total += len(snippet)
            if full:
                break
        if full:
            break
    total += file_total
    out[path.replace("\\", "/").rsplit("/", 1)[-1]] = hits

json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"sections -> {sys.argv[1]} ({total:,} chars from {len(sys.argv) - 2} files)")
