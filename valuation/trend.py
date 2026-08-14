# -*- coding: utf-8 -*-
"""估值 vintage 趋势视图：把最近 N 个报告期排成一行一期，看估计值怎么被修正。

用法:
  python trend.py TICKER                      # 渲染趋势表
  python trend.py TICKER --scenario bear      # 换情景（默认 base）
  python trend.py TICKER --ingest a.json ...  # 从历史 valuation.json 回填归档
  python trend.py TICKER --include-flagged    # 把带 red 红旗的运行也算进聚合

compare.py 是两期对比，本脚本是 N 期趋势——布局对齐所追踪的那份参考表
（一行一个季度快照，末行最新），但多做一件它做不到的事：**把季度间的变化
拿去和同一报告期内的采样噪声比**，回答"这次变了，是新财报导致的还是判断层抖的"。

显著性怎么判
------------
同一报告期跑 n 次得到 n 个样本，组内标准差 sd 就是判断层噪声的直接估计。
比较相邻两期的均值差时，标准误 SE = sqrt(sd1²/n1 + sd2²/n2)，ratio = |Δ|/SE：

  ratio < 1     变化落在噪声里，不可区分
  1 ≤ ratio < 2 弱信号
  ratio ≥ 2     显著

n < 2 时无法估计组内噪声，只报变化幅度、不下判断——**这不是统计检验**
（n=3 时任何秩检验都到不了 p<0.05），只是把噪声量级摆到变化旁边做量纲对照。
要让它有意义，每个报告期至少跑 3 次。
"""
import argparse
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vintages  # noqa: E402


def agg(samples, scenario, field, top=False):
    """取一组样本里某字段的 (中位数, n, 样本标准差)。top=True 读顶层字段。"""
    vals = [(s if top else s.get("scenarios", {}).get(scenario, {})).get(field)
            for s in samples]
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return None, 0, None
    return (st.median(vals), len(vals),
            st.stdev(vals) if len(vals) >= 2 else None)


def fmt(v, spec=",.1f", dash="—"):
    return dash if v is None else format(v, spec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--scenario", default="base", choices=("bear", "base", "bull"))
    ap.add_argument("--ingest", nargs="+", metavar="valuation.json",
                    help="从历史 valuation.json 回填归档（bundle zip 里那份）")
    ap.add_argument("--include-flagged", action="store_true",
                    help="把带 red 红旗的运行也算进聚合（默认排除）")
    a = ap.parse_args()
    T = a.ticker.upper()

    if a.ingest:
        for p in a.ingest:
            val = json.loads(Path(p).read_text(encoding="utf-8"))
            if val.get("ticker") != T:
                print(f"跳过 {p}：标的是 {val.get('ticker')} 不是 {T}")
                continue
            # 回填时按 engine 输出的红旗判定 gate_clean，与服务端在线判定口径一致
            reds = sum(1 for s in val.get("scenarios", {}).values()
                       for lv, _ in s.get("warnings", []) if lv == "red")
            f = vintages.record(val, gate_clean=(reds == 0))
            print(f"归档 {Path(p).name} -> {f}" if f
                  else f"跳过 {Path(p).name}：无 meta.vintage.report_end（缺 manifest）")
        print()

    recs = vintages.load(T)
    if not recs:
        raise SystemExit(f"{T} 无 vintage 归档。先跑几次估值，或用 --ingest 回填"
                         f"（valuation.json 在每次 bundle zip 里）")

    SC = a.scenario
    rows = []
    for r in recs:
        used = [s for s in r["samples"] if a.include_flagged or s.get("gate_clean")]
        dropped = len(r["samples"]) - len(used)

        # 前瞻口径必须一致才能聚合：同一份财报下，判断层可能把"下一财年"理解成
        # 本财年(FY2026E)或下一整年(FY2027E)——12 月财年的公司在 Q2 报告期尤其
        # 容易分歧（AMZN 2026-06-30 实测 3 次里 1 次 FY2026E、2 次 FY2027E）。
        # 两者差整整一年增长，混在一格取中位数得到的是无意义的数。
        # 处理：按 fwd_label 分组，只聚合最大的一组，其余显式报告。
        mixed = None
        if used:
            groups = {}
            for s in used:
                groups.setdefault(s.get("fwd_label") or "?", []).append(s)
            if len(groups) > 1:
                mixed = {k: len(v) for k, v in groups.items()}
                ranked = sorted(groups, key=lambda k: len(groups[k]), reverse=True)
                # 无严格多数（如 1:1）时拒绝聚合：任意挑一边等于把口径分歧藏起来，
                # 而这个分歧恰恰是要修的东西（判断层 prompt 没把"下一财年"钉死）。
                used = (groups[ranked[0]]
                        if len(groups[ranked[0]]) > len(groups[ranked[1]]) else [])
        if not used:
            rows.append({"report_end": r["report_end"], "n": 0, "dropped": dropped,
                         "mixed": mixed})
            continue
        blend, n, sd = agg(used, SC, "blend")
        rows.append({
            "report_end": r["report_end"], "n": n, "dropped": dropped, "mixed": mixed,
            "fwd_label": used[0].get("fwd_label"),
            "blend": blend, "blend_sd": sd,
            "eps1": agg(used, SC, "eps1")[0], "pe": agg(used, SC, "pe")[0],
            "pe_pctile": agg(used, SC, "pe_pctile")[0],
            "upside": agg(used, SC, "upside")[0],
            "price": agg(used, "", "price", top=True)[0],
            "adj_eps": agg(used, "", "adj_eps", top=True)[0],
            "ttm_revenue": agg(used, "", "ttm_revenue", top=True)[0],
            "run_dates": sorted({s["run_date"] for s in used}),
        })

    print(f"===== {T} 估值 vintage 趋势 · {SC} 情景 =====")
    print(f"{len(rows)} 个报告期 | 每期取样本中位数，组内离散 = 样本标准差/中位数"
          + ("" if a.include_flagged else " | 已排除带 red 红旗的运行"))
    print()
    hdr = (f"{'报告期':<12}{'n':>3}{'TTM营收':>12}{'调整后EPS':>10}{'下财年EPS':>10}"
           f"{'目标PE':>7}{'PE分位':>7}{'综合目标价':>11}{'组内离散':>9}"
           f"{'现价':>9}{'距现价':>8}")
    print(hdr)
    print("-" * (len(hdr) + 12))
    for r in rows:
        if not r["n"]:
            if r.get("mixed"):
                detail = "，".join(f"{k} {v}次" for k, v in r["mixed"].items())
                print(f"{r['report_end']:<12}{0:>3}   ⛔ 前瞻口径分裂且无多数（{detail}）"
                      f"——差一整年增长，拒绝聚合。请先在判断层 prompt 里钉死"
                      f"「下一财年」的定义再重跑"
                      + (f"（另有 {r['dropped']} 次带 red 红旗已排除）" if r["dropped"] else ""))
            else:
                print(f"{r['report_end']:<12}{0:>3}   （{r['dropped']} 次运行全部带 red 红旗，已排除）")
            continue
        disp = (f"±{100 * r['blend_sd'] / r['blend']:.1f}%"
                if r["blend_sd"] and r["blend"] else "n=1")
        print(f"{r['report_end']:<12}{r['n']:>3}{fmt(r['ttm_revenue'], ',.0f'):>12}"
              f"{fmt(r['adj_eps'], '.2f'):>10}{fmt(r['eps1'], '.2f'):>10}"
              f"{fmt(r['pe'], 'g'):>7}{fmt(r['pe_pctile'], '.0f'):>7}"
              f"{fmt(r['blend']):>11}{disp:>9}{fmt(r['price'], ',.2f'):>9}"
              f"{fmt(r['upside'], '+.1%'):>8}")
        if r["dropped"]:
            print(f"{'':<12}   （另有 {r['dropped']} 次带 red 红旗已排除）")
        if r.get("mixed"):
            detail = "，".join(f"{k} {v}次" for k, v in r["mixed"].items())
            print(f"{'':<12}   ⛔ 前瞻口径不一致（{detail}）——差一整年增长，不可混合聚合。"
                  f"已只取最大组「{r['fwd_label']}」，其余样本未计入")

    live = [r for r in rows if r["n"]]
    if len(live) >= 2:
        print(f"\n相邻期变化（综合目标价，{SC}）—— 与组内采样噪声对照")
        for p, q in zip(live, live[1:]):
            if not (p["blend"] and q["blend"]):
                continue
            d = q["blend"] / p["blend"] - 1
            if p["blend_sd"] and q["blend_sd"] and p["n"] >= 2 and q["n"] >= 2:
                se = (p["blend_sd"] ** 2 / p["n"] + q["blend_sd"] ** 2 / q["n"]) ** 0.5
                ratio = abs(q["blend"] - p["blend"]) / se if se else float("inf")
                verdict = ("噪声内，不可区分" if ratio < 1 else
                           "弱信号" if ratio < 2 else "显著")
                # 两位小数：0.98 显示成 1.0 会和"< 1 判噪声内"看起来自相矛盾
                tag = f"|Δ|/SE = {ratio:.2f} → {verdict}"
            else:
                tag = "样本不足（需每期 ≥2 次运行）→ 无法判定"
            print(f"  {p['report_end']} → {q['report_end']}  "
                  f"{p['blend']:,.1f} → {q['blend']:,.1f} ({d:+.1%})  |  {tag}")

        f, l = live[0], live[-1]
        print(f"\n累计修正（{f['report_end']} → {l['report_end']}）")
        for label, k, spec in (("下财年 EPS", "eps1", ".2f"),
                               ("目标 PE", "pe", "g"),
                               ("综合目标价", "blend", ",.1f"),
                               ("调整后 EPS", "adj_eps", ".2f")):
            if f.get(k) and l.get(k):
                print(f"  {label:<11} {format(f[k], spec)} → {format(l[k], spec)} "
                      f"({l[k] / f[k] - 1:+.1%})")
        print("\n  修正方向本身不判对错——要判，得等该财年实现值出来再回看"
              "（参照：MSFT FY26 市场估计四季内上修 +9.1%，实际落在起点附近）。")


if __name__ == "__main__":
    sys.exit(main())
