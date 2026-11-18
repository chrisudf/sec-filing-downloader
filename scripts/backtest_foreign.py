# -*- coding: utf-8 -*-
"""外国发行人 TTM PE 兜底（valuation/foreign_band.py）的多标的长窗口回测。

用法:
  python scripts/backtest_foreign.py [--tickers SAP,NVO,...] [--years 20] [--out PATH]

对每只票跑与生产相同的函数链（形态判定 → Yahoo 公告值 → 20-F 逐年核对 → 财报季度对照 →
逐日 TTM PE），价格窗口拉到 --years 年，输出：
  1. 形态 / 申报币种 / ADR 换股比（登记值 vs 推断值 vs EXPECT 里的 20-F 封面值）
  2. 20-F 逐年核对：通过率、各年 Yahoo 币种、原始 / 重述口径、剔除原因
  3. 独立期中核对：companyfacts 里 6-K / 20-F 的季度、半年、九个月 EPS（若有）vs 同期
     Yahoo 季度之和——这一路与年度核对独立，抓得到年度容差藏住的单季误差
  4. 逐日序列：有效天数、被剔天数、PE 范围、非公告日的单日 PE 跳变 > 20%（抓数量级 / 币种错）
  5. 当前 TTM PE vs Yahoo tPE（Yahoo 的非美元票用旧汇率，差几个点正常）
只用于验算，不进生产路径。
"""
import argparse
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "valuation"))
import foreign_band as fb  # noqa: E402
import pe_band as pb  # noqa: E402
from app.edgar import contact_email  # noqa: E402

# 20-F 封面上的 ADR 换股比（1 ADR = N 股普通股），用来检验 infer_ratio
EXPECT = {"ASML": 1, "TSM": 5, "SAP": 1, "NVO": 1, "SONY": 1, "TM": 10, "BABA": 8,
          "INFY": 1, "NVS": 1, "HSBC": 5, "SHEL": 2, "AZN": 0.5, "UL": 1, "BHP": 2, "RIO": 1}
DEFAULT = list(EXPECT)


def interim_eps(facts, ccy):
    """companyfacts 里非年度的摊薄 EPS（6-K 期中 / 20-F 比较期）-> [(start, end, days, val, form)]。"""
    out = {}
    for t in pb.EPS_TAGS:
        for f in ((facts.get(t) or {}).get("units") or {}).get(f"{ccy}/shares", []):
            s, e = f.get("start"), f.get("end")
            if not s or not e:
                continue
            d = (date.fromisoformat(e) - date.fromisoformat(s)).days
            if 80 <= d <= 100 or 170 <= d <= 195 or 260 <= d <= 285:
                k = (s, e)
                if k not in out or (f.get("filed") or "") < out[k][4]:
                    out[k] = (s, e, d, float(f["val"]), f.get("filed") or "", f.get("form"))
    return sorted(out.values())


def run(t, years, H):
    import yfinance as yf
    L = [f"## {t}", ""]
    inp = pb.load_inputs(t, contact_email(), years)
    tk = yf.Ticker(t)
    shape = fb.foreign_shape(inp)
    ccy, annual, splits = fb.load_annual(inp)
    L.append(f"- 形态：taxonomy {inp['taxonomy']}，外国发行人形态 {'是' if shape else '否'}，"
             f"20-F 摊薄 EPS 币种 {ccy}，年度 {len(annual)} 年"
             + (f"（{min(annual)[:4]}–{max(annual)[:4]}）" if annual else "")
             + "".join(f"；拆股归一 {s['fy_end'][:4]} 年起 ÷{s['factor']:g}" for s in splits))
    if not shape:
        L += ["- **生产路径不会启用兜底**（有季度 XBRL，走 pe_band）", ""]
        return L, None
    quarters = fb.parse_yahoo_quarters(tk.get_earnings_dates(limit=100))
    last_px = inp["hist"]["Close"].dropna().index[-1].date()
    if not annual or len(quarters) < 4:
        L += [f"- Yahoo 公告值 {len(quarters)} 季、20-F 年度 {len(annual)} 年 -> 兜底不出数", ""]
        return L, None
    lo = (date.fromisoformat(min(annual)) - timedelta(days=370)).isoformat()
    fx_daily = fb.fx_history(ccy, lo, (last_px + timedelta(days=1)).isoformat())
    fx, fx_at = fb.fx_annual(annual, fx_daily, ccy), fb.fx_lookup(fx_daily)
    inferred = fb.infer_ratio(quarters, annual, fx, ccy)
    ratio = fb.ADR_RATIO.get(t) or inferred
    exp = EXPECT.get(t)
    L.append(f"- Yahoo 公告值 {len(quarters)} 季（{quarters[0]['date']} 起）；ADR 换股比：登记 "
             f"{fb.ADR_RATIO.get(t)}，推断 {inferred}，20-F 封面 {exp}"
             + ("" if inferred == exp else "  **← 推断与封面不符**"))
    if ratio is None:
        L += ["- 换股比定不下来 -> 兜底不出数", ""]
        return L, None
    checks = fb.crosscheck(quarters, annual, fx, ratio, ccy)
    n_ok = sum(c["ok"] for c in checks)
    L += [f"- 20-F 逐年核对：{n_ok}/{len(checks)} 年通过", "",
          "| 财年末 | Yahoo 币种 | 口径 | Yahoo 4 季和 | 20-F 折算 | 偏差 | 结果 |", "|---|---|---|---|---|---|---|"]
    for c in checks:
        L.append(f"| {c['fy_end']} | {c['unit'] or '—'} | {c.get('basis') or '—'} | "
                 f"{'—' if c['yahoo'] is None else format(c['yahoo'], '.2f')} | "
                 f"{'—' if c['sec'] is None else format(c['sec'], '.2f')} | "
                 f"{'—' if c['dev'] is None else format(c['dev'], '+.1%')} | "
                 f"{'通过' if c['ok'] else '剔除：' + c.get('why', '')} |")
    ok, tail = fb.accepted_quarters(quarters, checks)
    L.append("")
    if ok is None:
        L += [f"- **整组不出**：{tail}", ""]
        return L, {"t": t, "status": "拒绝", "why": tail, "n_ok": n_ok, "n": len(checks),
                   "ratio_ok": inferred == exp}
    stmt = {}
    try:
        stmt = fb.parse_statement_eps(tk.quarterly_income_stmt)
    except Exception:  # noqa: BLE001
        pass
    q2, recon = fb.reconcile_statement(quarters, ok, stmt, ccy, fx_at)
    ok = {d: u for d, u in ok.items() if d not in {r["date"] for r in recon["dropped"]}}
    L.append(f"- 年报未出的尾部 {tail} 季；财报季度对照：{recon['n']} 季，"
             f"修正 {len(recon['fixed'])}，剔除 {len(recon['dropped'])}，{recon['skipped'] or '口径一致'}")
    for f in recon["fixed"]:
        L.append(f"  - 季末 {f['period_end']}：公告值 {f['yahoo']:.2f} -> 财报 {f['stmt']:.2f} {f['unit']}")
    for f in recon["dropped"]:
        L.append(f"  - 季末 {f['period_end']}：公告值 {f['yahoo']:.2f} vs 财报 {f['stmt']:.2f} {f['unit']} 差太多 -> 剔除")

    # 3) 独立期中核对
    inter = interim_eps(inp["facts"], ccy)
    bad_int, n_int = [], 0
    for s, e, d, v, filed, form in inter:
        lo_d = date.fromisoformat(s)
        qs = [q for q in q2 if ok.get(q["date"])
              and 0 < (date.fromisoformat(q["date"]) - lo_d).days <= d + fb.Q_WIN_LAG
              and (date.fromisoformat(q["date"]) - lo_d).days > 60]
        need = round(d / 91)
        if len(qs) != need or not v:
            continue
        tot = 0.0
        for q in qs:
            if ok[q["date"]] == ccy:
                tot += q["eps"]
            else:
                r = fx_at(date.fromisoformat(q["date"]))
                tot = None if (tot is None or not r) else tot + q["eps"] * r
            if tot is None:
                break
        if tot is None:
            continue
        n_int += 1
        dev = tot / (v * ratio) - 1
        tol = fb.STMT_TOL_LOCAL * 2 if all(ok[q["date"]] == ccy for q in qs) else fb.XCHECK_TOL
        if abs(dev) > tol:
            bad_int.append(f"{s}~{e}（{form} {filed}）Yahoo {tot:.2f} vs XBRL {v * ratio:.2f} {dev:+.1%}")
    L.append(f"- 独立期中核对（companyfacts 的季度 / 半年 / 九个月 EPS）：可比 {n_int} 段，"
             f"超容差 {len(bad_int)} 段" + ("" if inter else "（该发行人没有期中 XBRL）"))
    L += [f"  - {b}" for b in bad_int[:8]]

    # 4) 逐日序列
    closes = [(d.date(), float(v)) for d, v in inp["hist"]["Close"].dropna().items()]
    ser = fb.build_series(q2, ok, closes, fx_at)
    ann = {q["date"] for q in q2}
    jumps = []
    for a, b in zip(ser, ser[1:]):
        if b["ttm_period"] != a["ttm_period"]:
            continue                       # 换窗口日：TTM 本来就会跳
        r = b["pe_trailing"] / a["pe_trailing"] - 1
        if abs(r) > 0.20:
            jumps.append(f"{b['date']} {a['pe_trailing']:.1f}->{b['pe_trailing']:.1f}x")
    win_jumps = []
    for a, b in zip(ser, ser[1:]):
        if b["ttm_period"] != a["ttm_period"] and abs(b["ttm_eps"] / a["ttm_eps"] - 1) > 0.6:
            win_jumps.append(f"{b['date']} TTM {a['ttm_eps']:.2f}->{b['ttm_eps']:.2f}")
    pes = sorted(s["pe_trailing"] for s in ser)
    L.append(f"- 逐日序列：{len(ser)}/{len(closes)} 个交易日有值（{ser[0]['date'] if ser else '—'} 起），"
             f"PE 中位 {pb.pctile(pes, 50):.1f}x、P5–P95 {pb.pctile(pes, 5):.1f}–{pb.pctile(pes, 95):.1f}x"
             if ser else "- 逐日序列为空")
    L.append(f"- 同窗口内单日 PE 跳 >20%（只能是价格本身）：{len(jumps)} 次"
             + (f"，例 {', '.join(jumps[:3])}" if jumps else ""))
    L.append(f"- 换窗口时 TTM EPS 变动 >60%（疑数量级 / 币种错）：{len(win_jumps)} 次"
             + (f"：{', '.join(win_jumps[:4])}" if win_jumps else ""))
    tpe = None
    try:
        tpe = tk.info.get("trailingPE")
    except Exception:  # noqa: BLE001
        pass
    cur = ser[-1] if ser else None
    if cur:
        L.append(f"- 当前：{cur['date']} TTM PE {cur['pe_trailing']:.1f}x（TTM EPS ${cur['ttm_eps']:.2f}）"
                 f"，Yahoo tPE {tpe if tpe is None else round(tpe, 1)}")
    L.append("")
    return L, {"t": t, "status": "出数" if ser else "空", "n_ok": n_ok, "n": len(checks),
               "ratio_ok": inferred == exp, "fixed": len(recon["fixed"]), "int_bad": len(bad_int),
               "n_int": n_int, "jumps": len(win_jumps), "days": len(ser),
               "pe": cur and cur["pe_trailing"], "tpe": tpe, "units": sorted({c["unit"] for c in checks if c["ok"]})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=",".join(DEFAULT))
    ap.add_argument("--years", type=int, default=20)
    ap.add_argument("--out")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    out, summ = [f"# 外国发行人 TTM PE 兜底回测（{date.today()}，价格窗口 {a.years} 年）", ""], []
    for t in [x.strip().upper() for x in a.tickers.split(",") if x.strip()]:
        try:
            L, s = run(t, a.years, None)
        except Exception as e:  # noqa: BLE001
            L, s = [f"## {t}", "", f"- 运行出错：{type(e).__name__}: {e}",
                    "```", traceback.format_exc()[-600:], "```", ""], {"t": t, "status": "出错"}
        out += L
        if s:
            summ.append(s)
        print("\n".join(L), flush=True)
    tbl = ["## 汇总", "", "| 票 | 结果 | 20-F 年度核对 | 换股比推断 | Yahoo 币种 | 财报修正 | 期中核对超差 | TTM 跳变 | 有效天数 | 当前 PE | Yahoo tPE |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in summ:
        tbl.append(f"| {s['t']} | {s['status']}{('：' + s['why']) if s.get('why') else ''} | "
                   f"{s.get('n_ok', '—')}/{s.get('n', '—')} | {'✓' if s.get('ratio_ok') else '✗'} | "
                   f"{'/'.join(s.get('units') or []) or '—'} | {s.get('fixed', '—')} | "
                   f"{s.get('int_bad', '—')}/{s.get('n_int', '—')} | {s.get('jumps', '—')} | {s.get('days', '—')} | "
                   f"{'—' if not s.get('pe') else format(s['pe'], '.1f') + 'x'} | "
                   f"{'—' if not s.get('tpe') else format(s['tpe'], '.1f')} |")
    text = "\n".join(out[:2] + tbl + [""] + out[2:]) + "\n"
    print("\n".join(tbl))
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
