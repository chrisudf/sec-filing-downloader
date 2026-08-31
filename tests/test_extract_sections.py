# -*- coding: utf-8 -*-
"""摘录抽取的通道与配额规则。

起因（AMZN 2026-08-31）：post_period_capital_events 是必填字段，但此前没有任何
关键词专门抓期后事项，只能靠别的命中窗口顺带扫到。实测摘录正好断在
    "...$13.7 billion invested in Q2 2026. Subsequent to June 30, 20"
金额落在 CTX_AFTER=900 之外，判断层拿不到原文明写的 $21.3B / $25.0B，
只能抓它能看见的最后一个数字。**判断层没错，它的输入被截断了。**
"""
import ast
import re
from pathlib import Path

import pytest

# extract_sections.py 是模块级脚本（import 即读 sys.argv 并写文件），不能直接
# import——按 test_pure.py 的手法从生产源码逐字抽函数 + 它依赖的模块级常量。
_SRC = (Path(__file__).resolve().parent.parent / "valuation"
        / "extract_sections.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)
_NEEDED = {"KEYWORDS", "KEYWORDS_RISK", "KEYWORDS_SUBSEQ", "CTX_BEFORE", "CTX_AFTER",
           "MAX_HITS", "MAX_TOTAL", "CTX_AFTER_SUBSEQ", "SUBSEQ_RESERVE", "RISK_RESERVE"}
_SEGS = []
for _n in _TREE.body:
    if isinstance(_n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in _NEEDED for t in _n.targets):
        _SEGS.append(ast.get_source_segment(_SRC, _n))
    elif isinstance(_n, ast.Assign) and isinstance(_n.targets[0], ast.Tuple) and any(
            isinstance(e, ast.Name) and e.id in _NEEDED for e in _n.targets[0].elts):
        _SEGS.append(ast.get_source_segment(_SRC, _n))
    elif isinstance(_n, ast.FunctionDef) and _n.name == "collect_hits":
        _SEGS.append(ast.get_source_segment(_SRC, _n))
_NS = {"re": re}
exec("\n".join(_SEGS), _NS)
collect_hits = _NS["collect_hits"]
SUBSEQ_RESERVE, RISK_RESERVE = _NS["SUBSEQ_RESERVE"], _NS["RISK_RESERVE"]
CTX_AFTER, CTX_AFTER_SUBSEQ = _NS["CTX_AFTER"], _NS["CTX_AFTER_SUBSEQ"]


AMZN_REAL = (
    "x" * 400 + "prices for our debt as of those dates. 17 Table of Contents "
    "Subsequent to June 30, 2026, we issued $ 25.0 billion of U.S. Dollar-denominated "
    "Notes for general corporate purposes with maturities between 2029 and 2066, "
    + "y" * 600 + " and the amounts are disclosed above. " + "z" * 400)


def _channels(hits):
    out = {}
    for h in hits:
        out.setdefault(h["channel"], []).append(h)
    return out


def test_subsequent_channel_captures_amount_beyond_old_window():
    """核心回归：金额距关键词 >900 字符也要抓到（旧 CTX_AFTER 会截掉）。"""
    hits, _ = collect_hits(AMZN_REAL, 40_000)
    subs = _channels(hits).get("subsequent") or []
    assert subs, "期后段落必须被 subsequent 通道抓到"
    assert "25.0 billion" in subs[0]["text"]


def test_subsequent_window_is_wider_than_fact():
    assert CTX_AFTER_SUBSEQ > CTX_AFTER


def test_subsequent_runs_first():
    """它是必填字段的唯一来源，被 fact/risk 挤掉等于那个字段没数据。"""
    text = AMZN_REAL + " effective tax rate " + "a" * 2000 + " impairment " + "b" * 2000
    hits, _ = collect_hits(text, 40_000)
    assert hits[0]["channel"] == "subsequent"


def test_all_three_channels_survive_tight_budget():
    """AMZN 实测：加了 subsequent 之后 fact 把 per_file 吃满，risk 掉到 0 条。
    静默吃掉一整个通道，正是这一系列 PR 在修的毛病。"""
    # 夹具要点：fact 内容必须**真的填得满**预算，否则 risk 无论有没有预留都能跑，
    # 测不出区别（变异「去掉 risk 预留」首轮就是这样逃掉的）。每段填充还要各不
    # 相同——去重按 snippet 前 200 字符比对，重复填充会被当成同一条吞掉。
    blob = AMZN_REAL
    for j, kw in enumerate(["effective tax rate", "reportable segments",
                            "capital expenditures", "guidance", "one-time"]):
        for k in range(3):
            blob += " %s " % kw + ("%d%d" % (j, k)) * 700
    for k in range(3):
        blob += " going concern " + ("R%d" % k) * 700
    hits, _ = collect_hits(blob, 12_000)
    ch = _channels(hits)
    assert set(ch) == {"subsequent", "fact", "risk"}, f"通道被饿死: {sorted(ch)}"


def test_subsequent_reserve_does_not_starve_fact():
    """反向：某份财报里 Subsequent to 满天飞时，不能把 fact 通道吃光。"""
    # 每段填充必须不同：去重按 snippet 前 200 字符比对，而第 2/3 条命中的前 150
    # 字符都是紧邻的填充字符——用同一个字符会被误判成重复吞掉，于是 subsequent
    # 永远吃不满预留，变异「不设上限」「full 不重置」双双逃掉。
    text = "".join("Subsequent to June 30, 2026, event %d. " % i + ("E%d" % i) * 1500
                   for i in range(6)) + " effective tax rate " + "f" * 1200
    hits, used = collect_hits(text, 20_000)
    ch = _channels(hits)
    sub_chars = sum(len(h["text"]) for h in ch.get("subsequent", []))
    assert sub_chars <= SUBSEQ_RESERVE
    assert "fact" in ch, "subsequent 用满预留后 fact 必须还能跑"


def test_budget_respected_overall():
    text = AMZN_REAL + (" effective tax rate " + "f" * 1000) * 50
    _, used = collect_hits(text, 8_000)
    assert used <= 8_000


def test_no_subsequent_section_is_fine():
    """AAPL 实测：财报里确实没有 Subsequent 段落 —— 不该硬造，0 条是正确输出。"""
    hits, _ = collect_hits(" effective tax rate " + "f" * 900, 40_000)
    assert "subsequent" not in _channels(hits)


def test_dedup_keeps_distinct_events():
    """AMZN 的两条期后事项相隔上千字符，不能被去重规则误判成同一条。"""
    text = ("Subsequent to June 30, 2026, we invested $ 21.3 billion in OpenAI. "
            + "p" * 2500
            + " Subsequent to June 30, 2026, we issued $ 25.0 billion of Notes. "
            + "q" * 2500)
    hits, _ = collect_hits(text, 40_000)
    subs = _channels(hits)["subsequent"]
    joined = " ".join(h["text"] for h in subs)
    assert "21.3 billion" in joined and "25.0 billion" in joined
