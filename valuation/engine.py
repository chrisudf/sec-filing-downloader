# -*- coding: utf-8 -*-
"""估值引擎：facts JSON + 假设 config JSON -> valuation JSON。

用法: python engine.py CONFIG.json FACTS.json OUT.json [MANIFEST.csv]

config 由分析者（人或 LLM 判断层）编写，schema 见 valuation/README.md：
所有数字计算在此确定性完成——LLM 只负责定假设并注明出处，不碰算术。
standard 模式：PE 法（下一财年 EPS × 目标 PE）、十年两段式 FCFF DCF、SOTP/倍数法。
financials 模式（银行/券商/fintech）：PE 法 + P/TBV 法（附 Gordon 公式
justified P/TBV = (ROTE−g)/(WACC−g) 交叉参考）；DCF/SOTP 对金融股不适用。
"""
import json
import os
import sys

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
facts = json.load(open(sys.argv[2], encoding="utf-8"))
OUT = sys.argv[3]
manifest = open(sys.argv[4], encoding="utf-8").read() if len(sys.argv) > 4 else ""

MODE = cfg.get("mode", facts.get("mode", "standard"))


def dcf(rev0, g0, gN, margins, wacc, tg, net_cash, shares, years=10):
    growths = [g0 + (gN - g0) * i / (years - 1) for i in range(years)]
    rev, pv, fcf_last = rev0, 0.0, 0.0
    for i in range(years):
        rev *= 1 + growths[i]
        fcf = rev * margins[i]
        pv += fcf / (1 + wacc) ** (i + 1)
        fcf_last = fcf
    tv = fcf_last * (1 + tg) / (wacc - tg)
    equity = pv + tv / (1 + wacc) ** years + net_cash
    return equity / shares, equity


def tail(dic, n):
    items = list(dic.items())[-n:]
    return [k for k, _ in items], [v for _, v in items]


ttm = facts["ttm"]
rev0 = ttm["revenue"]["value"] / 1e6

if MODE == "financials":
    # ================= 金融股：PE 法 + P/TBV 法 =================
    ttm_m = {k: round(ttm[k]["value"] / 1e6)
             for k in ("revenue", "pretax_income", "net_income")}
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
                  currency=cfg.get("currency", "USD")),
        ttm=ttm_m, adj_ni=cfg["adj_ni"], adj_eps=round(cfg["adj_ni"] / cfg["shares"], 2),
        rote_ttm=round(cfg["adj_ni"] / tbv, 4),
        notes=cfg["notes"], rationale=cfg["rationale"], adj_note=cfg["adj_note"],
        scenarios={}, manifest=manifest,
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

out = dict(
    ticker=cfg["ticker"], name=cfg["name"], date=cfg["date"], mode="standard",
    meta=dict(price=cfg["price"], mcap=cfg["mcap"], shares=cfg["shares"],
              fwd_shares=cfg["fwd_shares"], net_cash=cfg["net_cash"], fwd_label=cfg["fwd_label"],
              seg1=cfg["seg1"], seg2=cfg["seg2"], seg1_share=cfg["seg1_share"],
              adr_multiple=cfg.get("adr_multiple", 1.0), currency=cfg.get("currency", "USD")),
    ttm=ttm_m, adj_ni=cfg["adj_ni"], adj_eps=round(cfg["adj_ni"] / cfg["shares"], 2),
    notes=cfg["notes"], rationale=cfg["rationale"],
    net_cash_note=cfg["net_cash_note"], adj_note=cfg["adj_note"],
    other_income=cfg["other_income"], scenarios={}, manifest=manifest,
)

for name, s in cfg["scenarios"].items():
    rev1 = rev0 * (1 + s["g"])
    op1 = rev1 * s["opm"]
    ni1 = (op1 + cfg["other_income"]) * (1 - s["tax"])
    eps1 = ni1 / cfg["fwd_shares"]
    pe_target = eps1 * s["pe"]
    dcf_ps, _ = dcf(rev0, s["g0"], s["gN"], s["margins"], s["wacc"], s["tg"],
                    cfg["net_cash"], cfg["shares"])
    sotp_eq = (op1 * cfg["seg1_share"] * s["m1"]
               + op1 * (1 - cfg["seg1_share"]) * s["m2"] + cfg["net_cash"])
    sotp_ps = sotp_eq / cfg["shares"]
    blend = (pe_target + dcf_ps + sotp_ps) / 3
    out["scenarios"][name] = dict(
        assumptions=s, rev1=round(rev1), op1=round(op1), ni1=round(ni1),
        eps1=round(eps1, 2), pe_target=round(pe_target, 1),
        dcf_ps=round(dcf_ps, 1), sotp_ps=round(sotp_ps, 1),
        blend=round(blend, 1), upside=round(blend / cfg["price"] - 1, 4),
        fwd_pe=round(cfg["price"] / eps1, 1),
    )

# 反向 DCF：base 口径下现价隐含的起始增速
s = cfg["scenarios"]["base"]
lo, hi = -0.20, 0.80
for _ in range(60):
    mid = (lo + hi) / 2
    _, eq = dcf(rev0, mid, s["gN"], s["margins"], s["wacc"], s["tg"],
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
        ps, _ = dcf(rev0, s["g0"], s["gN"], s["margins"], round(w, 4), round(g, 4),
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

print(f"===== {cfg['ticker']} @ ${cfg['price']} =====")
print(f"TTM: rev {ttm_m['revenue']:,} op {ttm_m['op_income']:,} adjNI {cfg['adj_ni']:,} "
      f"adjEPS {out['adj_eps']} FCF {ttm_m['fcf']:,}")
print(f"反向DCF隐含起始增速: {out['reverse_dcf']:.1%}")
for n, v in out["scenarios"].items():
    print(f"{n:5s}| EPS1 {v['eps1']:8.2f} | PE法 {v['pe_target']:9.1f} | DCF {v['dcf_ps']:9.1f} | "
          f"SOTP {v['sotp_ps']:9.1f} | 综合 {v['blend']:9.1f} | {v['upside']:+.1%}")
