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
from datetime import date, timedelta

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
facts = json.load(open(sys.argv[2], encoding="utf-8"))
OUT = sys.argv[3]
manifest = open(sys.argv[4], encoding="utf-8").read() if len(sys.argv) > 4 else ""

MODE = cfg.get("mode", facts.get("mode", "standard"))

# ★ blend 权重政策（数字须按自己的纪律定；默认全 1 = 等权，历史行为逐位不变）。
# 「95% 的情况实际用的是倍数」的立场 argue 倍数腿占大头——但 DCF 腿留着有实证
# 价值（终值占比红旗、tv_pv 阈值巡航都是它抓的），所以做成权重而不是砍腿。
# 环境变量覆盖（非负，按当次参与综合的腿归一化）：
#   standard: VALUATION_BLEND_W_PE / _DCF / _SOTP；financials: _PE / _PTBV
# 权重随 valuation.json（blend_weights）进 Excel 综合公式与 compare/trend——
# 改权重 = 改口径，跨运行对比必须可见，不允许只活在环境变量里。
def _env_w(name):
    try:
        w = float(os.environ.get(name, 1) or 1)
    except ValueError:
        w = 1.0
    return max(w, 0.0)


BLEND_W = {"pe": _env_w("VALUATION_BLEND_W_PE"), "dcf": _env_w("VALUATION_BLEND_W_DCF"),
           "sotp": _env_w("VALUATION_BLEND_W_SOTP"), "ptbv": _env_w("VALUATION_BLEND_W_PTBV")}


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
# PENDING_10Q（业绩 8-K 已出、10-Q 未交）：本次运行的判断层已被强制按新闻稿滚动
# TTM（ttm_revenue_override），估的是 8-K 覆盖的那个季度——vintage 归档键若仍用
# 定期报告期末，会把"最新业绩下的估值"归进旧季度的格子，等 10-Q 落地后的运行
# 与它同格混聚。归档键前滚到滚动后的 TTM 末端（= fwd_window.start − 1 天）；
# report_end/age_days 保持定期报告口径不动——时效面板要展示的恰是"10-Q 还没来"。
if cfg.get("pending_10q") and (cfg.get("fwd_window") or {}).get("start") and VINTAGE:
    _fs = date.fromisoformat(cfg["fwd_window"]["start"])
    VINTAGE["vintage_end"] = (_fs - timedelta(days=1)).isoformat()
    VINTAGE["pending_10q"] = True


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


def pe_band_check(scenario_name, pe, band, ddiag, label="PE", diag_key="pe_vs_history"):
    """目标 PE 相对该票自身历史「已实现前瞻 PE」分布的位置。

    口径对齐：band 由 fetch_facts 以 basis="ntm" 写入——分母是「该日已知的最新
    TTM 期末之后 12 个月实现的稀释 EPS」（按 filed 切换，比日历日起算最多滞后一季）；引擎的 s["pe"] 乘的是 eps1 = TTM×(1+g)，前瞻期同样是
    NTM（见 valuation_service.fwd_window）。两者严格同源，可直接比分位。
    band["basis"] 字段可核对；若哪天改回 forward（按财年切），只在 report_end =
    财年末时才对得上，AMZN/META 这类 12 月财年公司在 Q1~Q3 报告期会系统性错位。

    两者都是 GAAP 基础；判断层若改用剔 SBC 的非 GAAP EPS，分母变大、PE 变小，
    本诊断会系统性偏低，届时不可直接采信。

    阈值刻意保守：只在「该票历史上从未出现过的倍数」或「base 偏离中枢区间」时报
    yellow。低倍数本身不是错——价值下沿本就该落在历史低位附近，这里只要求留痕。

    label/diag_key 参数化后同一套逻辑服务 financials 的 P/TBV 带（trailing 口径，
    分母=当日已知每股 TBV，与 engine 的 tbv_ps 同构）——检查结构完全同型：
    全窗 min/max 管「从未出现过」，base 管子窗中枢并集。
    """
    if not band or not band.get("pctiles"):
        return []
    if band.get("thin_coverage"):
        # 覆盖不足（<250 天）：分布不是"该票的历史"，锚与中枢检查都失去意义——
        # 停用并只在 base 上留一条可见的黄旗（三情景重复三遍是噪声）
        ddiag[diag_key] = {"thin_coverage": True, "days": band["days"],
                           "years": band["years"], "basis": band["basis"]}
        if scenario_name == "base":
            return [["yellow", f"历史{label}带覆盖不足（{band['days']} 天/{band['years']} 年，"
                               "原始数据缺口）——锚与中枢检查停用，倍数假设无历史对照"]]
        return []
    pcts = band["pctiles"]
    rank = _pctile_rank(pcts, pe)
    # base 中枢检查用与判断层 PE 锚同一个窗（近 3 年子窗，见 valuation_service 的
    # band_meta 注入）：锚了子窗 P50 却按全窗 [P10,P90] 打旗会自相矛盾——全窗含
    # 2021 regime，子窗中位低于全窗 P10 的票（倍数已下台阶）恰好每次被冤枉。
    # min/max 的「从未出现过」检查仍看全窗（问题本来就是全历史范围的）。
    _rc = (band.get("recent") or {})
    _rp = _rc.get("pctiles") or {}
    cpcts = _rp if ("10" in _rp and "90" in _rp) else pcts
    cwin = f"近{_rc['years']}年子窗" if cpcts is _rp else f"近{band['years']}年全窗"
    ddiag[diag_key] = {
        "pctile": round(rank, 1), "median": round(pcts["50"], 1),
        "p10": round(pcts["10"], 1), "p90": round(pcts["90"], 1),
        "min": round(band["min"], 1), "max": round(band["max"], 1),
        "anchor_window": cwin,
        "anchor_p50": (round(cpcts["50"], 1) if "50" in cpcts else None),
        "years": band["years"], "days": band["days"], "basis": band["basis"]}
    ctx = (f"（近 {band['years']} 年已实现{label}：中位 {pcts['50']:.1f}x，"
           f"实际区间 {band['min']:.1f}~{band['max']:.1f}x，{band['days']} 个交易日）")
    if not band["min"] <= pe <= band["max"]:
        return [["yellow", f"{scenario_name} 目标 {label} {pe:.1f}x 落在该票近 {band['years']} 年"
                           f"从未出现过的区间之外{ctx}——不必然是错，"
                           "但请在 rationale 中给出依据"]]
    if scenario_name == "base":
        # 容许区间 = 子窗 [P10,P90] ∪ 锚 P50 的 ±ANCHOR_TOL——prompt 里写的锚纪律就是
        # 「偏离 P50 ±15% 以上要给证据」，若这里只按 [P10,P90] 打旗，倍数分布窄的票
        # （MSFT/AMZN 子窗半宽仅 ±12%）会出现"按纪律给了证据的偏离照样吃黄旗"，
        # 两层规则各说各话。取并集后：纪律内不打旗，纪律外必打旗，口径一致。
        ANCHOR_TOL = 0.15
        lo_ok, hi_ok = cpcts["10"], cpcts["90"]
        if "50" in cpcts:
            lo_ok = min(lo_ok, cpcts["50"] * (1 - ANCHOR_TOL))
            hi_ok = max(hi_ok, cpcts["50"] * (1 + ANCHOR_TOL))
        if not lo_ok <= pe <= hi_ok:
            return [["yellow", f"base 目标 {label} {pe:.1f}x 界外 {cwin} [P10,P90]="
                               f"[{cpcts['10']:.1f},{cpcts['90']:.1f}]x（并 P50±"
                               f"{ANCHOR_TOL:.0%} 后为 [{lo_ok:.1f},{hi_ok:.1f}]x，"
                               f"全窗第 {rank:.0f} 百分位）{ctx}——base 应为中枢情景"]]
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
        # financials 语义 v2（2026-08-14）：P/TBV 带锚（判断层注入 + ptbv_band_check）
        # 改变 base ptbv 的产生方式与目标价水平——与 standard v2→v3 的理由同构，
        # 锚前(v1)/锚后(v2) 金融股样本在 trend/compare 里必须按版本隔离
        semantics_version=2,
        blend_weights={m: BLEND_W[m] for m in ("pe", "ptbv")},
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
        _wsum = BLEND_W["pe"] + BLEND_W["ptbv"]
        blend = ((pe_ps * BLEND_W["pe"] + ptbv_ps * BLEND_W["ptbv"]) / _wsum
                 if _wsum > 0 else (pe_ps + ptbv_ps) / 2)
        # P/TBV 带检查（standard 的 pe_band_check 同一套逻辑，label 换 P/TBV）：
        # financials 此前无 warnings/diagnostics 通道——TODO 里挂了两轮的项，
        # 带子进来时必须一起开通，否则检查结果没地方去
        ddiag = {}
        warnings = pe_band_check(name, s["ptbv"], facts.get("ptbv_band"), ddiag,
                                 label="P/TBV", diag_key="ptbv_vs_history")
        out["scenarios"][name] = dict(
            assumptions=s, rev1=round(rev1), ni1=round(ni1), eps1=round(eps1, 2),
            pe_target=round(pe_ps, 1), ptbv_ps=round(ptbv_ps, 1),
            rote1=round(rote1, 4), justified_ptbv=round(justified, 2),
            justified_ps=round(tbv_ps * justified, 1),
            blend=round(blend, 1), upside=round(blend / cfg["price"] - 1, 4),
            fwd_pe=round(cfg["price"] / eps1, 1),
            method_spread=round(max(pe_ps, ptbv_ps) / min(pe_ps, ptbv_ps), 2)
            if min(pe_ps, ptbv_ps) > 0 else None,
            diagnostics=ddiag, warnings=warnings,
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
        for lv, msg in v["warnings"]:
            print(f"      {'⛔' if lv == 'red' else '⚠️'} {msg}")
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
    # 输出，compare.py 的 1==1 还会静默掉它本该拦的口径跳变告警。
    # v3（2026-08-14）：判断层 PE 锚（历史 NTM 带子窗 P50）纳入语义——锚改变 base
    # PE 的产生方式与目标价水平，锚前(≤v2)/锚后(v3) 样本不可直接对比或混聚
    semantics_version=3,
    config_semantics_version=cfg.get("semantics_version", 1),
    # 只记录可能参与综合的腿：SOTP 降级为参考项时 W_SOTP 惰性——记进去会让
    # compare/trend 把两次数值完全相同的运行当成口径不同
    blend_weights={m: BLEND_W[m]
                   for m in (("pe", "dcf") + (("sotp",) if sotp_in_blend else ()))},
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
    # 近零利润守卫：eps1<=0 或情景 opm<2% 时 PE 腿病态——盈利不是市场给这类票
    # 定价的基础，「近零盈利 × 正常倍数 ≈ 0」不是估值是除法事故（COIN 实测
    # g 全负 + opm≈0，PE 腿把 blend 拖出 60% 全距）。PE 腿标 n.m. 退出综合，
    # 综合退化为 DCF(+SOTP)；有 ps_band 时在红旗区给 P/S 参考价（不入综合）。
    # pe_target 仍照算并展示——退出综合 ≠ 隐藏。
    pe_nm = eps1 <= 0 or s["opm"] < 0.02
    blend_methods = ((["pe"] if not pe_nm else [])
                     + ["dcf"] + (["sotp"] if sotp_in_blend else []))
    methods = ([] if pe_nm else [pe_target]) + [dcf_ps] + ([sotp_ps] if sotp_in_blend else [])
    _vals = {"pe": pe_target, "dcf": dcf_ps, "sotp": sotp_ps}
    _wsum = sum(BLEND_W[m] for m in blend_methods)
    blend = (sum(_vals[m] * BLEND_W[m] for m in blend_methods) / _wsum
             if _wsum > 0 else sum(methods) / len(methods))
    spread = (round(max(methods) / min(methods), 2)
              if len(methods) > 1 and min(methods) > 0 else None)

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
    if dcf_ps <= 0 and "dcf" in blend_methods:
        # margins 全程深负 + 净现金为负时 DCF 腿可以合法算出 <=0——PE 腿又被守卫
        # 退出时，负数会独腿成为"综合目标价"无声进产线。red：打回判断层复审
        warnings.append(["red", f"DCF 每股价值 {dcf_ps:.1f} <= 0（FCF 路径全程亏损或"
                                "净现金深负）——综合失去有效支撑腿，请修正 margins 路径"
                                "或声明永久受损"])
    if pe_nm:
        _ps_ref = ""
        _psb = facts.get("ps_band") or {}
        _psp = (_psb.get("recent") or {}).get("pctiles") or _psb.get("pctiles") or {}
        if "50" in _psp and cfg["fwd_shares"]:
            _rps1 = rev1 / cfg["fwd_shares"]
            ddiag["ps_ref"] = {
                "basis": _psb.get("basis"),
                "window": ("recent" if (_psb.get("recent") or {}).get("pctiles") else "full"),
                "ps_p50": round(float(_psp["50"]), 2), "rps1": round(_rps1, 2),
                "px": {q: round(float(_psp[q]) * _rps1, 1)
                       for q in ("25", "50", "75") if q in _psp}}
            _ps_ref = (f"；P/S 参考（不入综合）：历史 P/S P50 {float(_psp['50']):.2f}x × "
                       f"{name} 每股营收 {_rps1:.2f} ≈ {float(_psp['50']) * _rps1:.1f}")
        warnings.append(["yellow",
                         f"{name} PE 腿 n.m.（eps1 {eps1:.2f} / opm {s['opm']:.1%}——近零或"
                         "负利润下『盈利×倍数』无定价意义），综合退化为 "
                         + ("DCF+SOTP" if sotp_in_blend else "DCF") + _ps_ref])
    warnings += pe_band_check(name, s["pe"], facts.get("pe_band"), ddiag)

    out["scenarios"][name] = dict(
        assumptions=s, rev1=round(rev1), op1=round(op1), ni1=round(ni1),
        eps1=round(eps1, 2), pe_target=round(pe_target, 1),
        dcf_ps=round(dcf_ps, 1), sotp_ps=round(sotp_ps, 1),
        blend=round(blend, 1), upside=round(blend / cfg["price"] - 1, 4),
        # eps1<=0 时 fwd_pe 是负数假读数，置 None（报告/归档按缺失处理）
        fwd_pe=round(cfg["price"] / eps1, 1) if eps1 > 0 else None,
        # blend 由哪几条腿构成——build_report 的综合公式必须与引擎同构，
        # 否则 verify_report 的交叉核对会在 PE 腿 n.m. 时假 FAIL
        blend_methods=blend_methods,
        method_spread=spread, diagnostics=ddiag, warnings=warnings,
    )

# base 锚检查：综合与市价偏离 >35% 本身不是错（估值可以偏离市价），
# 但必须显式看到并辩护，而不是三档整队随 base 静默平移
_dev = out["scenarios"]["base"]["blend"] / cfg["price"] - 1
if abs(_dev) > 0.35:
    out["scenarios"]["base"]["warnings"].append(
        ["yellow", f"base 综合较现价偏离 {_dev:+.0%}（>±35%）——请核对 base 假设"
                   "或在注记中显式说明为何与市场定价分歧"])

# ---- 价值交易区间：历史已实现 NTM PE 分位 × base 前瞻 EPS ----
# 与三情景互为对照：情景是「基本面情景各自的公允价」（bear=EPS↓×PE↓ 双压），
# 这行回答「按该票自己的历史倍数分布，base 盈利下会交易在哪个区间」——
# 参考表用手拍带子回答的问题，这里用分位数回答。锚窗优先近 3 年子窗
# （pe_band.recent，避开 2021 regime），回退全窗；facts.json round-trip 后
# recent.pctiles 的键是字符串（与 pctiles 同，见 _pctile_rank 注）。
_band = facts.get("pe_band") or {}
_rc = _band.get("recent") or {}
_use = _rc.get("pctiles") or _band.get("pctiles") or {}
_e1 = out["scenarios"]["base"]["eps1"]
# base PE 腿 n.m.（eps1<=0 或 opm<2%，与情景守卫同判据）时整块跳过：PE 分位 ×
# 近零/负 EPS 渲染出的"交易区间"是除法事故不是估值（opm 校验只保证 op1>0，
# other_income 无下界，op1+other_income 可以为负）。显式说原因，不静默。
_base_pe_nm = "pe" not in out["scenarios"]["base"]["blend_methods"]
if _base_pe_nm and _use:
    out["scenarios"]["base"]["warnings"].append(
        ["yellow", f"base PE 腿 n.m.（前瞻 EPS {_e1:.2f}）——交易区间（历史 PE 分位 × "
                   "base EPS）无意义，本次不出该块；参考红旗区的 P/S 参考价"])
if _band.get("thin_coverage") and _use:
    out["scenarios"]["base"]["warnings"].append(
        ["yellow", f"历史 PE 带覆盖不足（{_band.get('days')} 天）——交易区间不出"])
if (not _base_pe_nm and not _band.get("thin_coverage")
        and all(q in _use for q in ("10", "25", "50", "75", "90"))):
    _tr_pe = {q: round(float(_use[q]), 2) for q in ("10", "25", "50", "75", "90")}
    out["trading_range"] = dict(
        basis=_band.get("basis"),
        window=(f"近{_rc['years']}年" if _rc.get("pctiles") else f"近{_band.get('years')}年"),
        days=(_rc.get("days") if _rc.get("pctiles") else _band.get("days")),
        eps1_base=_e1, pe=_tr_pe,
        px={q: round(v * _e1, 1) for q, v in _tr_pe.items()},
        full_window_p50=(round(float(_band["pctiles"]["50"]), 2)
                         if _band.get("pctiles") else None))

# Rule of 40 透传（fetch_facts 计算，standard 模式）：营收增速+利润率的标尺，
# 与 pe_band 同属"倍数值不值得给"的判断参照，进报告与 prompt 元数据
if facts.get("rule_of_40"):
    out["rule_of_40"] = facts["rule_of_40"]

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
if out.get("trading_range"):
    _tr = out["trading_range"]
    print(f"交易区间（{_tr['window']}已实现 NTM PE 分位 × base EPS {_tr['eps1_base']}）: "
          f"P25~P75 {_tr['px']['25']}~{_tr['px']['75']}  中位 {_tr['px']['50']}"
          f"  宽区间 P10~P90 {_tr['px']['10']}~{_tr['px']['90']}")
if not sotp_in_blend:
    print(f"SOTP 降级为参考项（主分部利润占比 {cfg['seg1_share']:.0%} >= 85%），综合 = PE/DCF 均值")
for n, v in out["scenarios"].items():
    print(f"{n:5s}| EPS1 {v['eps1']:8.2f} | PE法 {v['pe_target']:9.1f} | DCF {v['dcf_ps']:9.1f} | "
          f"SOTP {v['sotp_ps']:9.1f}{'*' if not sotp_in_blend else ' '}| "
          f"综合 {v['blend']:9.1f} | {v['upside']:+.1%}")
    for lv, msg in v["warnings"]:
        print(f"      {'⛔' if lv == 'red' else '⚠️'} {msg}")
