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
# subsequent 通道（2026-08-31）：期后资本事件是 post_period_capital_events 这个
# **必填**字段的唯一事实来源，却此前没有任何关键词专门抓它——只能靠别的命中窗口
# 顺带扫到。AMZN 2026-08-31 实测：摘录正好断在
#   "...$13.7 billion invested in Q2 2026. Subsequent to June 30, 20"
# 金额落在 CTX_AFTER=900 之外，于是判断层拿不到原文里明写的
#   期后 OpenAI $21.3B、期后发债 $25.0B
# 只能抓它能看见的最后一个数字 $13.7B（且诚实标注"excerpt 未完整量化"）。
# **判断层没错，它的输入被截断了。**
KEYWORDS_SUBSEQ = ["Subsequent to", "Subsequent Event"]
CTX_BEFORE, CTX_AFTER, MAX_HITS, MAX_TOTAL = 150, 900, 3, 45_000
# 期后段落常一口气列好几笔（AMZN 那两条相隔上千字符），900 不够；
# 预算从 per_file 里**预留**而不是外加，总量不变、不撑大 prompt。
CTX_AFTER_SUBSEQ, SUBSEQ_RESERVE = 1_400, 4_000
# risk 也要留配额：加了 subsequent 之后 fact 会先把 per_file 吃满，risk 直接掉到 0 条
# （AMZN 实测 2 -> 0）。risk 多是模板披露没错，但**静默吃掉一整个通道**和这一系列
# PR 在修的毛病是同一个。留一小格，让每个通道都有代表。
RISK_RESERVE = 2_500


def collect_hits(text, per_file):
    """在一份财报正文里按通道抓摘录 -> (hits, 已用字符数)。

    通道顺序即优先级：subsequent 先行且**自带预留配额**（它是必填字段
    post_period_capital_events 的唯一来源，被 fact/risk 挤掉就等于那个字段没数据），
    然后 fact（财务事实），最后 risk（多为模板披露）。
    抽成函数是为了能直接单测——此前整段逻辑写在模块级，改动只能靠跑真财报验证。
    """
    hits, file_total, full = [], 0, False
    for channel, kws, ctx_after, budget in (
            ("subsequent", KEYWORDS_SUBSEQ, CTX_AFTER_SUBSEQ, min(SUBSEQ_RESERVE, per_file)),
            ("fact", KEYWORDS, CTX_AFTER, max(0, per_file - RISK_RESERVE)),
            ("risk", KEYWORDS_RISK, CTX_AFTER, per_file)):
        for kw in kws:
            for i, m in enumerate(re.finditer(re.escape(kw), text, re.IGNORECASE)):
                if i >= MAX_HITS:
                    break
                snippet = text[max(0, m.start() - CTX_BEFORE):
                               min(len(text), m.end() + ctx_after)]
                if any(snippet[:200] == h["text"][:200] for h in hits):
                    continue
                # 配额在追加前检查：追加后再查会让每个文件超额最多一个 snippet，
                # 多附件场景下累计突破 MAX_TOTAL。subsequent 额外受自己的预留封顶，
                # 免得某份财报里 "Subsequent to" 满天飞时把 fact 通道饿死。
                if file_total + len(snippet) > budget:
                    full = True
                    break
                hits.append({"keyword": kw, "channel": channel, "text": snippet})
                file_total += len(snippet)
            if full:
                break
        # 用满某个通道自己的配额不代表整份预算用完——只有 risk（最后一个通道，
        # 配额就是 per_file）撑满才是真的到顶
        full = False if channel in ("subsequent", "fact") else full
        if full:
            break
    return hits, file_total


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
    hits, file_total = collect_hits(text, per_file)
    total += file_total
    out[path.replace("\\", "/").rsplit("/", 1)[-1]] = hits

json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"sections -> {sys.argv[1]} ({total:,} chars from {len(sys.argv) - 2} files)")
