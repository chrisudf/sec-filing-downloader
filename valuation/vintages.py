# -*- coding: utf-8 -*-
"""估值 vintage 归档：按**报告期**存快照，同一报告期的多次运行存成多个样本。

为什么键是报告期而不是运行日期
------------------------------
趋势视图要回答的是「这次估值变了，是新财报导致的，还是判断层抖的」。
判断层有可观的运行间噪声（MSFT 2026-08-05 实测 base 综合目标价 CV 2.4%、
bear/bull 3.5%；README 记录 NVDA base CV≈3.5%、bear≈12%）。若每次运行都
当成一个新 vintage，季度间"变化"里就混进了同一份财报下的采样噪声，趋势表
会把噪声读成基本面信号——这正是 compare.py:98 那条警报想拦的事，这里把它
从两期推广到 N 期并量化。

因此：**同一 report_end 的多次运行 = 同一格里的多个样本**，聚合用中位数
（n 小，中位数比均值抗离群），并保留组内离散度供显著性判断。

存放位置
--------
`vintages/{TICKER}/{report_end}.json`。不能放 jobs/——_cleanup_jobs 按 mtime
rmtree，3 天后连目录一起清（PREV_DIR 当初也是为此单独开的）。

gate_clean
----------
带 red 红旗的运行照存但打标记，读取侧默认只聚合 gate-clean 样本并显式报告
剔除了几个。直接丢弃会让样本有偏（坏假设往往偏向同一侧）；全都算进去又会
污染中位数——存下来、标出来、默认排除，是三者里唯一不丢信息的做法。
"""
import json
import os
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VINTAGE_DIR = ROOT / "vintages"


def _sample(val: dict, gate_clean: bool) -> dict:
    """从 valuation.json 抽出趋势视图需要的字段。

    刻意只存标量快照而非整份 valuation.json：归档要跨版本长期可读，
    存全量则 engine 输出结构一变，历史 vintage 就集体失效。
    """
    ttm = val.get("ttm", {})
    scen = {}
    for name, s in val.get("scenarios", {}).items():
        a = s.get("assumptions", {})
        w = s.get("warnings", [])
        scen[name] = {
            "blend": s.get("blend"), "upside": s.get("upside"),
            "eps1": s.get("eps1"), "pe": a.get("pe"), "fwd_pe": s.get("fwd_pe"),
            "pe_target": s.get("pe_target"), "dcf_ps": s.get("dcf_ps"),
            "sotp_ps": s.get("sotp_ps"),
            "g": a.get("g"), "opm": a.get("opm"), "wacc": a.get("wacc"),
            # 该情景的综合实际由哪几条腿构成：近零/负利润守卫会逐情景剔 PE 腿
            # （op1<=0 还会剔 SOTP），退化成 DCF 独腿。不存这个，trend 就会把
            # "DCF 独腿的 blend" 与 "PE/DCF 均值的 blend" 平均到一起
            "blend_methods": s.get("blend_methods"),
            "reds": sum(1 for lv, _ in w if lv == "red"),
            "yellows": sum(1 for lv, _ in w if lv == "yellow"),
            # 目标 PE 在该票自身历史前瞻 PE 分布中的位置（engine.pe_band_check 产出）
            "pe_pctile": (s.get("diagnostics") or {}).get("pe_vs_history", {}).get("pctile"),
        }
    return {
        "run_date": val.get("date") or date.today().isoformat(),
        "run_ts": time.time(),
        "gate_clean": bool(gate_clean),
        "semantics_version": val.get("semantics_version"),
        "blend_weights": val.get("blend_weights"),
        # PENDING_10Q 样本：TTM 基准来自 8-K 新闻稿滚动而非 XBRL，10-Q 落地后的
        # 同格样本与它口径略有差异（override vs XBRL），读取侧可据此单独审视
        "pending_10q": bool((val.get("meta", {}).get("vintage") or {}).get("pending_10q")),
        "price": val.get("meta", {}).get("price"),
        "fwd_label": val.get("meta", {}).get("fwd_label"),
        # SOTP 是否入综合（seg1_share >= 0.85 时降级为参考项）——等权下
        # blend_weights 归一化后看不出两腿/三腿之别，趋势分组要靠这个字段
        "sotp_in_blend": val.get("meta", {}).get("sotp_in_blend"),
        "adj_ni": val.get("adj_ni"), "adj_eps": val.get("adj_eps"),
        "ttm_revenue": ttm.get("revenue"), "ttm_op_income": ttm.get("op_income"),
        "ttm_net_income": ttm.get("net_income"),
        "filed": val.get("meta", {}).get("vintage", {}).get("filed"),
        "scenarios": scen,
    }


def record(val: dict, gate_clean: bool, root: Path | None = None) -> Path | None:
    """把一次运行追加进对应报告期的 vintage 文件。返回写入路径（无报告期则 None）。

    原子写：任务中断留下半个 JSON 会毒化之后所有趋势读取（PREV_DIR 同样处理）。
    """
    root = root or ROOT   # 默认值在 def 时求值，写成 None 才能在测试里覆盖 ROOT
    # 写入侧必须与 load() 的 ticker.upper() 一致：Windows 文件系统大小写不敏感
    # 掩盖了这个问题，但部署在 Linux 上时小写 ticker 会写进 vintages/aapl/ 而
    # load 去读 vintages/AAPL/，表现为"归档成功但趋势视图读不到"
    ticker = (val.get("ticker") or "").upper()
    _vin = val.get("meta", {}).get("vintage") or {}
    # PENDING_10Q 运行的归档键用前滚后的窗口末端（engine 写入 vintage_end）：
    # 判断层已按 8-K 滚动 TTM，估的是新季度——归进旧 report_end 的格子会让
    # 趋势视图把"最新业绩下的估值"当旧季度样本，10-Q 落地后的运行再与之混聚
    report_end = _vin.get("vintage_end") or _vin.get("report_end")
    if not ticker or not report_end:
        return None  # 无 manifest 的手工运行没有报告期，不归档好过归到错误的格子
    d = root / "vintages" / ticker
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{report_end}.json"
    rec = {"ticker": ticker, "report_end": report_end, "samples": []}
    if f.exists():
        try:
            _r = json.loads(f.read_text(encoding="utf-8"))
            # 只接受对象：合法 JSON 但顶层是数组/标量时（手工编辑或半截写入的残骸）
            # json.loads 不会抛，随后 rec.setdefault 直接 AttributeError，把整次运行
            # 的归档搞崩。坏形状与坏语法同等对待：以新记录覆盖。
            rec = _r if isinstance(_r, dict) else rec
        except json.JSONDecodeError:
            pass  # 坏文件不阻断新运行，直接以新记录覆盖
    rec.setdefault("samples", []).append(_sample(val, gate_clean))
    # 落地合并：pending 运行按 +3 日历月估计的 vintage_end 归档，与 10-Q 落地后的
    # 真实 report_end 常差几天（52/53 周财历、月末日）——一旦真实格子写入，把
    # ±7 天内"全 pending 样本"的幻影格子并进来删掉，趋势视图不再多出假报告期
    # 被并进来的幻影格子**在新记录落盘之后**才删：合并后的样本此刻只存在于内存
    # 的 rec 里，先 unlink 再 write_text/os.replace 的话，中间任何一次崩溃或写失败
    # 都会让 pending 样本永久消失——与本函数承诺的原子写自相矛盾。
    merged = []
    if not _vin.get("pending_10q"):
        _re = date.fromisoformat(report_end)
        for g in list(d.glob("*.json")):
            k = g.stem
            if k == report_end:
                continue
            try:
                if abs((date.fromisoformat(k) - _re).days) > 7:
                    continue
                old = json.loads(g.read_text(encoding="utf-8"))
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(old, dict):
                continue
            smp = old.get("samples") or []
            if smp and all(isinstance(x, dict) and x.get("pending_10q") for x in smp):
                rec["samples"] = smp + rec["samples"]
                merged.append(g)
    tmp = d / f".{report_end}.json.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, f)
    # 到这里新格子已经原子落盘，样本有两份；此时删幻影格子最坏只是留下重复，
    # 不会丢数据
    for g in merged:
        g.unlink(missing_ok=True)
    return f


def load(ticker: str, root: Path | None = None) -> list[dict]:
    """读出该标的全部 vintage，按报告期升序。"""
    d = (root or ROOT) / "vintages" / ticker.upper()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        # 顶层非对象（合法 JSON 的数组/标量）会让 r.get 抛 AttributeError，
        # 一个坏文件就能让整个趋势视图崩掉——与 record() 同一条守卫。
        # report_end 缺失也跳过：最后的 sort key 会 KeyError。
        if not isinstance(r, dict) or not r.get("report_end"):
            continue
        if r.get("samples"):
            out.append(r)
    return sorted(out, key=lambda r: r["report_end"])
