# -*- coding: utf-8 -*-
"""估值引擎：facts JSON + 假设 config JSON -> valuation JSON。

用法: python engine.py CONFIG.json FACTS.json OUT.json [MANIFEST.csv]

config 由分析者（人或 LLM 判断层）编写，schema 见 valuation/README.md：
所有数字计算在此确定性完成——LLM 只负责定假设并注明出处，不碰算术。
standard 模式：PE 法（下一财年 EPS × 目标 PE）、十年两段式 FCFF DCF、SOTP/倍数法。
financials 模式（银行/券商/fintech）：PE 法 + P/TBV 法（附 Gordon 公式
justified P/TBV = (ROTE−g)/(WACC−g) 交叉参考）；DCF/SOTP 对金融股不适用。
"""
import csv
import io
import json
import os
import sys
from datetime import date

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
facts = json.load(open(sys.argv[2], encoding="utf-8"))
OUT = sys.argv[3]
manifest = open(sys.argv[4], encoding="utf-8").read() if len(sys.argv) > 4 else ""

MODE = cfg.get("mode", facts.get("mode", "standard"))


def _vintage(manifest_text, run_date):
    """从 manifest 提取"这份估值基于哪个报告期"——价值投资的第一个检查项就是数据新鲜度。

    report_end 只看定期报告(10-K/10-Q/20-F/6-K)——8-K 新闻稿附件的
    reportDate 是公布日,混进来会让时效块在 XBRL 还停留在上季度时显示
    "1 天前",把警示反向变成误导。8-K 行单独输出 pending_8k_announced。"""
    try:
        rows = [r for r in csv.DictReader(io.StringIO(manifest_text)) if r.get("reportDate")]
        if not rows:
            return {}
        periodic = [r for r in rows if not r["form"].startswith("8-K")]
        if not periodic:
            return {}
        latest = max(periodic, key=lambda r: r["reportDate"])
        age = (date.fromisoformat(run_date) - date.fromisoformat(latest["reportDate"])).days
        out = {"report_end": latest["reportDate"], "filed": latest["filingDate"],
               "age_days": age}
        pend = [r for r in rows if r["form"].startswith("8-K")
                and r["filingDate"] > latest["filingDate"]]
        if pend:
            out["pending_8k_announced"] = max(r["filingDate"] for r in pend)
        return out
    except Exception:
        return {}


VINTAGE = _vintage(manifest, cfg["date"])


def dcf(rev0, g0, gN, margins, wacc, tg, net_cash, shares, years=10):
    """返回 (每股, 股权价值, diagnostics)。diagnostics 供 v2 合理性检查与报告红旗区
    使用——第 N 年营收倍数 / 终值占比在此前是函数局部变量，服务层若自行重算会造出
    第三份 DCF 实现（engine + Excel 公式区之外），口径必然漂移，因此由引擎唯一输出。"""
    growths = [g0 + (gN - g0) * i / (years - 1) for i in range(years)]
    rev, pv, fcf_last = rev0, 0.0, 0.0
    for i in range(years):
        rev *= 1 + growths[i]
        fcf = rev * margins[i]
        pv += fcf / (1 + wacc) ** (i + 1)
        fcf_last = fcf
    tv = fcf_last * (1 + tg) / (wacc - tg)
    tv_pv = tv / (1 + wacc) ** years
    equity = pv + tv_pv + net_cash
    diag = dict(yrN_rev_multiple=round(rev / rev0, 2) if rev0 else None,
                tv_pv_share=round(tv_pv / (pv + tv_pv), 4) if pv + tv_pv > 0 else None,
                pv_explicit=round(pv), tv_pv=round(tv_pv))
    return equity / shares, equity, diag


def tail(dic, n):
    items = list(dic.items())[-n:]
    return [k for k, _ in items], [v for _, v in items]


def _pctile_rank(pcts, x):
    """从稀疏分位点表反查 x 的百分位（线性插值）。facts.json 里 key 是字符串。"""
    ks = sorted(int(k) for k in pcts)
    vals = [pcts[str(k)] for k in ks]
    if x <= vals[0]:
        return float(ks[0])
    if x >= vals[-1]:
        return float(ks[-1])
    for i in range(len(ks) - 1):
        if vals[i] <= x <= vals[i + 1]:
            span = vals[i + 1] - vals[i]
            f = (x - vals[i]) / span if span > 0 else 0.0
            return ks[i] + f * (ks[i + 1] - ks[i])
    return 50.0


def pe_band_check(scenario_name, pe, band, ddiag):
    """目标 PE 相对该票自身历史「已实现前瞻 PE」分布的位置。

    口径对齐：band 的分母是各财年最终实现的稀释 EPS，引擎 s["pe"] 乘的是下一财年
    eps1——同为前瞻口径，可比。两者都是 GAAP 基础；判断层若改用剔 SBC 的非 GAAP
    EPS，分母变大、PE 变小，本诊断会系统性偏低，届时不可直接采信。

    阈值刻意保守：只在「该票历史上从未出现过的倍数」或「base 偏离中枢区间」时报
    yellow。低倍数本身不是错——价值下沿本就该落在历史低位附近，这里只要求留痕。
    """
    if not band or not band.get("pctiles"):
        return []
    pcts = band["pctiles"]
    rank = _pctile_rank(pcts, pe)
    ddiag["pe_vs_history"] = {
        "pctile": round(rank, 1), "median": round(pcts["50"], 1),
        "p10": round(pcts["10"], 1), "p90": round(pcts["90"], 1),
        "min": round(band["min"], 1), "max": round(band["max"], 1),
        "years": band["years"], "days": band["days"], "basis": band["basis"]}
    ctx = (f"（近 {band['years']} 年已实现前瞻 PE：中位 {pcts['50']:.1f}x，"
           f"实际区间 {band['min']:.1f}~{band['max']:.1f}x，{band['days']} 个交易日）")
    if not band["min"] <= pe <= band["max"]:
        return [["yellow", f"{scenario_name} 目标 PE {pe:.1f}x 落在该票近 {band['years']} 年"
                           f"从未出现过的区间之外{ctx}——不必然是错，"
                           "但请在 rationale 中给出依据"]]
    if scenario_name == "base" and not pcts["10"] <= pe <= pcts["90"]:
        return [["yellow", f"base 目标 PE {pe:.1f}x 处历史第 {rank:.0f} 百分位"
                           f"（界外 [P10,P90]）{ctx}——base 应为中枢情景"]]
    return []


ttm = facts["ttm"]
rev0 = ttm["revenue"]["value"] / 1e6

# XBRL 严重滞后时（外国发行人 6-K 无季度 XBRL；或业绩 8-K 已出而 10-Q 未交），
# 判断层可从财报原文提取真实 TTM 营收作为基准覆盖（与 net_cash/adj_ni 同属
# "LLM 提取事实并注明出处"，非算术）。必须在 MODE 分支之前生效：financials
# （银行/券商/fintech）恰好是 10-Q 滞后业绩公布 2-6 周的重灾区，此前这段落在
# 分支之后，financials 拿到 override 也会静默按上一季度基准估值。
rev0_note = ""
rev0_reported = rev0   # 覆盖前的 XBRL 口径营收——P/FCF 红旗按利润率换算用
if cfg.get("ttm_revenue_override"):
    rev0 = float(cfg["ttm_revenue_override"])
    rev0_note = cfg.get("ttm_revenue_note", "判断层按财报原文修正的 TTM 营收基准")

if MODE == "financials":
    # ================= 金融股：PE 法 + P/TBV 法 =================
    ttm_m = {k: round(ttm[k]["value"] / 1e6)
             for k in ("revenue", "pretax_income", "net_income")}
    ttm_m["revenue"] = round(rev0)   # 覆盖生效时与情景基准/Excel 公式区同源
    eq = facts["equity_instant"]
    gw = facts.get("goodwill_instant") or {}
    it = facts.get("intangibles_instant") or {}
    eq_end, eq_val = list(eq.items())[-1]
    tbv = round((eq_val - (list(gw.values())[-1] if gw else 0)
                 - (list(it.values())[-1] if it else 0)) / 1e6)
    tbv_ps = tbv / cfg["shares"]

    out = dict(
        ticker=cfg["ticker"], name=cfg["name"], date=cfg["date"], mode="financials",
        meta=dict(price=cfg["price"], mcap=cfg["mcap"], shares=cfg["shares"],
                  fwd_shares=cfg["fwd_shares"], fwd_label=cfg["fwd_label"],
                  tbv=tbv, tbv_ps=round(tbv_ps, 2), tbv_asof=eq_end,
                  adr_multiple=cfg.get("adr_multiple", 1.0),
                  currency=cfg.get("currency", "USD"), vintage=VINTAGE,
                  # XBRL 结构化数据的期末——与 vintage.report_end（定期报告期末）
                  # 是两回事：外国发行人 6-K 无季度 XBRL 时二者能差一年以上
                  # （TSM 实测 report_end=2026-06-30 而 data_latest=2024-12-31），
                  # 报告里标注"哪些行还停在旧窗口"必须用这个。
                  data_latest=facts.get("data_latest")),
        ttm=ttm_m, adj_ni=cfg["adj_ni"], adj_eps=round(cfg["adj_ni"] / cfg["shares"], 2),
        rote_ttm=round(cfg["adj_ni"] / tbv, 4),
        notes=cfg["notes"], rationale=cfg["rationale"], adj_note=cfg["adj_note"],
        rev0_note=rev0_note, scenarios={}, manifest=manifest,
    )

    for name, s in cfg["scenarios"].items():
        rev1 = rev0 * (1 + s["g"])
        ni1 = rev1 * s["nm"]
        eps1 = ni1 / cfg["fwd_shares"]
        pe_ps = eps1 * s["pe"]
        ptbv_ps = tbv_ps * s["ptbv"]
        rote1 = ni1 / tbv
        justified = (rote1 - s["tg"]) / (s["wacc"] - s["tg"])
        blend = (pe_ps + ptbv_ps) / 2
        out["scenarios"][name] = dict(
            assumptions=s, rev1=round(rev1), ni1=round(ni1), eps1=round(eps1, 2),
            pe_target=round(pe_ps, 1), ptbv_ps=round(ptbv_ps, 1),
            rote1=round(rote1, 4), justified_ptbv=round(justified, 2),
            justified_ps=round(tbv_ps * justified, 1),
            blend=round(blend, 1), upside=round(blend / cfg["price"] - 1, 4),
            fwd_pe=round(cfg["price"] / eps1, 1),
            method_spread=round(max(pe_ps, ptbv_ps) / min(pe_ps, ptbv_ps), 2)
            if min(pe_ps, ptbv_ps) > 0 else None,
        )

    # 敏感性：base 口径 justified P/TBV 每股价值 = f(WACC, 永续g)
    s = cfg["scenarios"]["base"]
    rote_b = out["scenarios"]["base"]["rote1"]
    sens = {}
    for w in (s["wacc"] - 0.01, s["wacc"] - 0.005, s["wacc"], s["wacc"] + 0.005, s["wacc"] + 0.01):
        row = {}
        for g in (s["tg"] - 0.01, s["tg"] - 0.005, s["tg"], s["tg"] + 0.005, s["tg"] + 0.01):
            row[round(g, 4)] = round(tbv_ps * (rote_b - g) / (round(w, 4) - g), 1)
        sens[round(w, 4)] = row
    out["sensitivity"] = sens

    a_keys, a_rev = tail(facts["revenue_annual"], 8)
    hist_a = dict(labels=a_keys, rev=[round(v / 1e6) for v in a_rev])
    for m, key in (("op", "pretax_income_annual"), ("ni", "net_income_annual")):
        hist_a[m] = [round(facts[key].get(k, 0) / 1e6) for k in a_keys]
    hist_a["eps"] = [facts["eps_diluted_annual"].get(k) for k in a_keys]
    q_keys, q_rev = tail(facts["revenue_quarterly"], 8)
    hist_q = dict(labels=q_keys, rev=[round(v / 1e6) for v in q_rev])
    for m, key in (("op", "pretax_income_quarterly"), ("ni", "net_income_quarterly")):
        hist_q[m] = [round(facts[key].get(k, 0) / 1e6) for k in q_keys]
    out["history"] = dict(annual=hist_a, quarterly=hist_q)

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"===== {cfg['ticker']} @ ${cfg['price']} (financials) =====")
    print(f"TTM: rev {ttm_m['revenue']:,} pretax {ttm_m['pretax_income']:,} "
          f"adjNI {cfg['adj_ni']:,} adjEPS {out['adj_eps']} | TBV {tbv:,} "
          f"(${out['meta']['tbv_ps']}/股, ROTE {out['rote_ttm']:.1%})")
    for n, v in out["scenarios"].items():
        print(f"{n:5s}| EPS1 {v['eps1']:7.2f} | PE法 {v['pe_target']:8.1f} | "
              f"P/TBV法 {v['ptbv_ps']:8.1f} (justified {v['justified_ptbv']:.2f}x) | "
              f"综合 {v['blend']:8.1f} | {v['upside']:+.1%}")
    sys.exit(0)

# ================= standard：PE + DCF + SOTP =================
ttm_m = {k: round(ttm[k]["value"] / 1e6) for k in ("revenue", "op_income", "net_income", "cfo", "capex")}
ttm_m["fcf"] = ttm_m["cfo"] - ttm_m["capex"]
ttm_m["revenue"] = round(rev0)   # 覆盖生效时 Excel 公式区与引擎共用同一基准

# P/FCF 红旗的分母：override 场景下 XBRL 的 fcf 绝对值是多年前的，直接除会对增长了
# 2-3 倍的公司产生每次必现的假红旗（还会因 reds 不清空而永久阻断连续性锚持久化）——
# 用"陈旧 FCF 利润率 × 当前营收基准"换算到同一量级
fcf_base = (ttm_m["fcf"] / rev0_reported * rev0) if rev0_reported else ttm_m["fcf"]

# v2（semantics_version=2, 2026-07-22）：主分部利润占比 >= 0.85 时 SOTP 与 PE 法
# 实为同一笔盈利乘两次倍数（无分部折价可解锁），退化为参考项不入综合——
# 综合 = PE 法与 DCF 各 50%。占比 < 0.85 时维持三法均值。
SOTP_SEG1_CAP = 0.85
sotp_in_blend = cfg["seg1_share"] < SOTP_SEG1_CAP

out = dict(
    ticker=cfg["ticker"], name=cfg["name"], date=cfg["date"], mode="standard",
    # 语义版本属于引擎实现而非 config 声明：本引擎无条件执行 v2 计算（SOTP 降级/
    # 诊断），重放 v1 老 config 时若透传声明值会得到"标 v1 却是两法综合"的自相矛盾
    # 输出，compare.py 的 1==1 还会静默掉它本该拦的口径跳变告警
    semantics_version=2,
    config_semantics_version=cfg.get("semantics_version", 1),
    meta=dict(price=cfg["price"], mcap=cfg["mcap"], shares=cfg["shares"],
              fwd_shares=cfg["fwd_shares"], net_cash=cfg["net_cash"], fwd_label=cfg["fwd_label"],
              seg1=cfg["seg1"], seg2=cfg["seg2"], seg1_share=cfg["seg1_share"],
              sotp_in_blend=sotp_in_blend,
              adr_multiple=cfg.get("adr_multiple", 1.0), currency=cfg.get("currency", "USD"),
              vintage=VINTAGE, data_latest=facts.get("data_latest")),
    ttm=ttm_m, adj_ni=cfg["adj_ni"], adj_eps=round(cfg["adj_ni"] / cfg["shares"], 2),
    notes=cfg["notes"], rationale=cfg["rationale"],
    net_cash_note=cfg["net_cash_note"], adj_note=cfg["adj_note"],
    other_income=cfg["other_income"], rev0_note=rev0_note, scenarios={}, manifest=manifest,
)

for name, s in cfg["scenarios"].items():
    rev1 = rev0 * (1 + s["g"])
    op1 = rev1 * s["opm"]
    ni1 = (op1 + cfg["other_income"]) * (1 - s["tax"])
    eps1 = ni1 / cfg["fwd_shares"]
    pe_target = eps1 * s["pe"]
    dcf_ps, dcf_eq, ddiag = dcf(rev0, s["g0"], s["gN"], s["margins"], s["wacc"], s["tg"],
                                cfg["net_cash"], cfg["shares"])
    sotp_eq = (op1 * cfg["seg1_share"] * s["m1"]
               + op1 * (1 - cfg["seg1_share"]) * s["m2"] + cfg["net_cash"])
    sotp_ps = sotp_eq / cfg["shares"]
    methods = [pe_target, dcf_ps] + ([sotp_ps] if sotp_in_blend else [])
    blend = sum(methods) / len(methods)
    spread = (round(max(methods) / min(methods), 2) if min(methods) > 0 else None)

    # ---- v2 经济合理性诊断（red=假设可修复，服务层可据此打回判断层一次；
    #      yellow=呈现层警示。全部随报告红旗区展示，不静默）----
    warnings = []
    if ddiag["yrN_rev_multiple"] and ddiag["yrN_rev_multiple"] > 8:
        warnings.append(["red", f"DCF 第10年营收为 TTM 的 {ddiag['yrN_rev_multiple']:.1f} 倍"
                                "（>8x，隐含 10 年 CAGR >23%）——增长路径过于激进"])
    if ddiag["tv_pv_share"] and ddiag["tv_pv_share"] > 0.75:
        warnings.append(["red", f"终值折现占 EV {ddiag['tv_pv_share']:.0%}（>75%）——"
                                "估值几乎全押在永续段，对 wacc-tg 极端敏感"])
    if fcf_base > 0.02 * rev0:
        p_fcf = dcf_eq / fcf_base
        ddiag["dcf_equity_over_ttm_fcf"] = round(p_fcf, 1)
        if not 5 <= p_fcf <= 90:
            warnings.append(["red", f"DCF 隐含股权价值为 TTM FCF 的 {p_fcf:.1f} 倍"
                                    "（界外 [5,90]）——路径假设与当前现金流量级脱节"])
    if cfg["adj_ni"] > 0:
        p_ni = blend * cfg["shares"] / cfg["adj_ni"]
        ddiag["blend_p_adjni"] = round(p_ni, 1)
        if not 6 <= p_ni <= 60:
            warnings.append(["yellow", f"综合目标价隐含 P/调整后净利 {p_ni:.1f}x（界外 [6,60]）"])
    if spread and spread > 2:
        warnings.append(["yellow", f"方法离散度 {spread}x（>2x）——各法分歧大，综合可信度降低"])
    warnings += pe_band_check(name, s["pe"], facts.get("pe_band"), ddiag)

    out["scenarios"][name] = dict(
        assumptions=s, rev1=round(rev1), op1=round(op1), ni1=round(ni1),
        eps1=round(eps1, 2), pe_target=round(pe_target, 1),
        dcf_ps=round(dcf_ps, 1), sotp_ps=round(sotp_ps, 1),
        blend=round(blend, 1), upside=round(blend / cfg["price"] - 1, 4),
        fwd_pe=round(cfg["price"] / eps1, 1),
        method_spread=spread, diagnostics=ddiag, warnings=warnings,
    )

# base 锚检查：综合与市价偏离 >35% 本身不是错（估值可以偏离市价），
# 但必须显式看到并辩护，而不是三档整队随 base 静默平移
_dev = out["scenarios"]["base"]["blend"] / cfg["price"] - 1
if abs(_dev) > 0.35:
    out["scenarios"]["base"]["warnings"].append(
        ["yellow", f"base 综合较现价偏离 {_dev:+.0%}（>±35%）——请核对 base 假设"
                   "或在注记中显式说明为何与市场定价分歧"])

# 反向 DCF：base 口径下现价隐含的起始增速
s = cfg["scenarios"]["base"]
lo, hi = -0.20, 0.80
for _ in range(60):
    mid = (lo + hi) / 2
    _, eq, _d = dcf(rev0, mid, s["gN"], s["margins"], s["wacc"], s["tg"],
                    cfg["net_cash"], cfg["shares"])
    if eq < cfg["mcap"]:
        lo = mid
    else:
        hi = mid
out["reverse_dcf"] = round(mid, 4)

# base 敏感性 WACC × 永续g
sens = {}
for w in (s["wacc"] - 0.01, s["wacc"] - 0.005, s["wacc"], s["wacc"] + 0.005, s["wacc"] + 0.01):
    row = {}
    for g in (s["tg"] - 0.01, s["tg"] - 0.005, s["tg"], s["tg"] + 0.005, s["tg"] + 0.01):
        ps, _, _d = dcf(rev0, s["g0"], s["gN"], s["margins"], round(w, 4), round(g, 4),
                        cfg["net_cash"], cfg["shares"])
        row[round(g, 4)] = round(ps)
    sens[round(w, 4)] = row
out["sensitivity"] = sens

a_keys, a_rev = tail(facts["revenue_annual"], 8)
hist_a = dict(labels=a_keys, rev=[round(v / 1e6) for v in a_rev])
for m, key in (("op", "op_income_annual"), ("ni", "net_income_annual"),
               ("cfo", "cfo_annual"), ("capex", "capex_annual")):
    hist_a[m] = [round(facts.get(key, {}).get(k, 0) / 1e6) for k in a_keys]
hist_a["eps"] = [facts["eps_diluted_annual"].get(k) for k in a_keys]

q_keys, q_rev = tail(facts["revenue_quarterly"], 8)
hist_q = dict(labels=q_keys, rev=[round(v / 1e6) for v in q_rev])
for m, key in (("op", "op_income_quarterly"), ("ni", "net_income_quarterly")):
    hist_q[m] = [round(facts[key].get(k, 0) / 1e6) for k in q_keys]
out["history"] = dict(annual=hist_a, quarterly=hist_q)

json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

print(f"===== {cfg['ticker']} @ ${cfg['price']} (semantics v{out['semantics_version']}) =====")
print(f"TTM: rev {ttm_m['revenue']:,} op {ttm_m['op_income']:,} adjNI {cfg['adj_ni']:,} "
      f"adjEPS {out['adj_eps']} FCF {ttm_m['fcf']:,}")
print(f"反向DCF隐含起始增速: {out['reverse_dcf']:.1%}（base 利润率/WACC 条件下）")
if not sotp_in_blend:
    print(f"SOTP 降级为参考项（主分部利润占比 {cfg['seg1_share']:.0%} >= 85%），综合 = PE/DCF 均值")
for n, v in out["scenarios"].items():
    print(f"{n:5s}| EPS1 {v['eps1']:8.2f} | PE法 {v['pe_target']:9.1f} | DCF {v['dcf_ps']:9.1f} | "
          f"SOTP {v['sotp_ps']:9.1f}{'*' if not sotp_in_blend else ' '}| "
          f"综合 {v['blend']:9.1f} | {v['upside']:+.1%}")
    for lv, msg in v["warnings"]:
        print(f"      {'⛔' if lv == 'red' else '⚠️'} {msg}")
