# -*- coding: utf-8 -*-
"""对比同一标的两次估值运行：基本面变化 / 假设漂移 / 目标价变动。

用法: python compare.py 旧_valuation.json 新_valuation.json
（valuation.json 在每次估值 bundle zip 里；建议每季财报后跑一次估值并留存 bundle，
本脚本就是"通过最新财报看基本面有没有变化"这一步的显式工具。）

三张表：
1. 基本面（TTM）——营收/利润/利润率的绝对变化与幅度，区分事实变化 vs 判断层调整
2. 假设漂移——三情景关键假设逐项对比，判断层这次为什么改了主意（结合两版 rationale 人工核）
3. 结论变动——各方法目标价、综合、距现价；变化大但基本面没动 = 噪声警报
"""
import json
import sys

old = json.load(open(sys.argv[1], encoding="utf-8"))
new = json.load(open(sys.argv[2], encoding="utf-8"))

if old["ticker"] != new["ticker"]:
    raise SystemExit(f"标的不一致：{old['ticker']} vs {new['ticker']}")
T = new["ticker"]
mode = new.get("mode", "standard")
if old.get("mode", "standard") != mode:
    print(f"⚠️  两次运行模式不同（{old.get('mode')} → {mode}），仅对比公共字段\n")
_sv_o, _sv_n = old.get("semantics_version", 1), new.get("semantics_version", 1)
if _sv_o != _sv_n:
    print(f"⚠️  估值语义版本不同（v{_sv_o} → v{_sv_n}）：v2（2026-07-22）起有跨情景联动"
          "约束与 SOTP 降级，v3（2026-08-14）起 base PE 默认锚历史 NTM 带子窗 P50——"
          "倍数类假设（pe/m1/m2）的漂移不可直接读作判断层改了主意，"
          "综合目标价口径与水平也可能不同\n")

# blend 权重对比：权重是口径不是假设——等权与缺省（老文件无该键）视为等价
def _wnorm(w):
    return {k: v for k, v in (w or {}).items()
            if isinstance(v, (int, float)) and abs(v - 1) > 1e-9}


_bw_o, _bw_n = _wnorm(old.get("blend_weights")), _wnorm(new.get("blend_weights"))
if _bw_o != _bw_n:
    print(f"⚠️  blend 权重不同（{_bw_o or '等权'} → {_bw_n or '等权'}）：综合目标价口径已变，"
          "③ 里综合的水平差异先归因权重，不要读作假设修正\n")

# 前瞻窗口对比：NTM 窗口随报告期滚动，跨季对比时不同是正常的（下面会如实打印）；
# 但**同一报告期**内两次运行窗口不同 = 口径分裂，g/eps1/pe 全部不可直接对比——
# 这正是趋势视图按 (fwd_label, semantics) 分组拒绝混聚的同一件事，两期对比也得拦
_fw_o = (old.get("meta") or {}).get("fwd_label")
_fw_n = (new.get("meta") or {}).get("fwd_label")
_re_o = ((old.get("meta") or {}).get("vintage") or {}).get("report_end")
_re_n = ((new.get("meta") or {}).get("vintage") or {}).get("report_end")
if _fw_o != _fw_n:
    if _re_o and _re_o == _re_n:
        print(f"⚠️  同一报告期（{_re_o}）下前瞻窗口不同（{_fw_o} → {_fw_n}）：口径分裂，"
              "②③ 里 g/eps1/PE/目标价的一切差异先归因于窗口，不要读作假设修正\n")
    else:
        print(f"ℹ️  前瞻窗口随报告期滚动：{_fw_o} → {_fw_n}"
              "（跨季对比属正常；g/eps1 的分母窗口不同，幅度对比留意口径）\n")


def chg(a, b, pct=False):
    if a in (None, 0) or b is None:
        return "—"
    d = b - a
    if pct:
        return f"{a:.1%} → {b:.1%} ({d:+.1%})"
    return f"{a:,.0f} → {b:,.0f} ({d/abs(a):+.1%})"


print(f"===== {T} 估值对比：{old['date']} → {new['date']} (mode={mode}) =====\n")

print("① 基本面（TTM，$M）——这是'事实'层，变化来自新财报本身")
rows = ([("总净收入", "revenue"), ("税前利润", "pretax_income"), ("报告净利", "net_income")]
        if mode == "financials" else
        [("营收", "revenue"), ("营业利润", "op_income"), ("报告净利", "net_income"),
         ("经营现金流", "cfo"), ("资本开支", "capex")])
for label, k in rows:
    if k in old.get("ttm", {}) and k in new.get("ttm", {}):
        print(f"  {label:<8} {chg(old['ttm'][k], new['ttm'][k])}")
o_m = old["ttm"].get("op_income" if mode != "financials" else "net_income")
n_m = new["ttm"].get("op_income" if mode != "financials" else "net_income")
if o_m and n_m:
    lbl = "营业利润率" if mode != "financials" else "净利率"
    print(f"  {lbl:<7} {chg(o_m/old['ttm']['revenue'], n_m/new['ttm']['revenue'], pct=True)}")
print(f"  调整后净利  {chg(old['adj_ni'], new['adj_ni'])}"
      f"{'   ← 调整口径也变了，对比两版 adj_note' if old.get('adj_note') != new.get('adj_note') else ''}")
if mode == "financials":
    print(f"  TBV      {chg(old['meta'].get('tbv'), new['meta'].get('tbv'))}")

print("\n② 假设漂移——这是'判断'层，变化应有新证据支撑（对照两版 rationale/notes）")
keys = (["g", "nm", "pe", "ptbv", "wacc", "tg"] if mode == "financials"
        else ["g", "opm", "tax", "pe", "wacc", "tg", "g0", "gN"])
hdr = "  {:<6}".format("") + "".join(f"{k:>16}" for k in keys)
print(hdr)
for sc in ("bear", "base", "bull"):
    o = old["scenarios"][sc]["assumptions"]
    n = new["scenarios"][sc]["assumptions"]
    cells = []
    for k in keys:
        a, b = o.get(k), n.get(k)
        if a is None or b is None:
            cells.append(f"{'—':>16}")
        elif a == b:
            cells.append(f"{b:>16g}")
        else:
            cells.append(f"{f'{a:g}→{b:g}':>16}")
    print(f"  {sc:<6}" + "".join(cells))

print("\n③ 结论变动")
methods = (["pe_target", "ptbv_ps"] if mode == "financials"
           else ["pe_target", "dcf_ps", "sotp_ps"])
names = {"pe_target": "PE法", "dcf_ps": "DCF", "sotp_ps": "SOTP", "ptbv_ps": "P/TBV法"}
for sc in ("bear", "base", "bull"):
    o, n = old["scenarios"][sc], new["scenarios"][sc]
    parts = [f"{names[m]} {o[m]:,.0f}→{n[m]:,.0f}" for m in methods if m in o and m in n]
    print(f"  {sc:<5} 综合 {o['blend']:,.1f} → {n['blend']:,.1f} "
          f"({(n['blend']/o['blend']-1):+.1%}) | 距现价 {o['upside']:+.1%} → {n['upside']:+.1%} | "
          + "，".join(parts))
print(f"  现价 {old['meta']['price']} → {new['meta']['price']}"
      f"（{new['meta']['price']/old['meta']['price']-1:+.1%}）")

moves = {sc: abs(new["scenarios"][sc]["blend"] / old["scenarios"][sc]["blend"] - 1)
         for sc in ("bear", "base", "bull") if old["scenarios"][sc]["blend"]}
worst_sc = max(moves, key=moves.get) if moves else None
rev_move = (abs(new["ttm"]["revenue"] / old["ttm"]["revenue"] - 1)
            if old["ttm"].get("revenue") and new["ttm"].get("revenue") else 0)
if worst_sc and moves[worst_sc] > 0.10 and rev_move < 0.03:
    print(f"\n⚠️  {worst_sc} 目标价变动 {moves[worst_sc]:.0%} 但 TTM 营收仅变 {rev_move:.1%}："
          "变化主要来自判断层假设而非新财报事实——先核对②里改了什么、rationale 是否有新证据，"
          "谨防把 LLM 运行间噪声当成基本面信号"
          "（NVDA 实验：同输入 5 次采样 base 目标价 CV≈3.5%，bear CV≈12%）")
