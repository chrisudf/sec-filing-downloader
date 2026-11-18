# -*- coding: utf-8 -*-
"""外国发行人 TTM PE 兜底（valuation/foreign_band.py）的历史验算：拿 SEC 6-K 季报原文当独立来源。

用法:
  python scripts/verify_foreign_6k.py [--ticker ASML] [--cik 937966] [--from 2011] [--out PATH]

做三件事：
  1. 逐季：Yahoo 公告值 vs 6-K 原文的摊薄 EPS（本币）。6-K 表格的列序各年不同（有时本季在前，
     有时去年同季在前），所以每个财年的 4 份季报枚举列序，取「4 季之和 = 20-F 年度 EPS」的那个；
     20-F 未出的年份用累计列（H1 = Q1+Q2、9M = H1+Q3）定列序。定不下来的季度标「未解析」。
  2. 逐年：foreign_band 的 20-F 年度核对结果（通过 / 剔除及原因）。
  3. 历史时点：每年 3 月初、9 月初各取一个交易日，生产管线（foreign_reading 同一套函数）
     算出的 TTM PE vs 用 6-K 原文季度 EPS ÷ 当日汇率独立重算的 TTM PE。
只用于验算，不进生产路径。6-K 原文缓存在 reports/verify_cache/（已 gitignore），重跑不再请求 SEC。
只支持本币 = 每 ADR 一股、6-K 用「Diluted net income per ordinary share」措辞的发行人（ASML 型）。
"""
import argparse
import html
import itertools
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "valuation"))
import httpx  # noqa: E402

import foreign_band as fb  # noqa: E402
import pe_band as pb  # noqa: E402
from app.edgar import contact_email  # noqa: E402

CACHE = ROOT / "reports" / "verify_cache"
EPS_RE = re.compile(r"(?i)diluted net (?:income|loss|\(loss\) income|income \(loss\)) per "
                    r"(?:ordinary )?share(?:\s*\(?\d\)?(?=\s))?((?:\s*\(?-?\d+\.\d+\s*\)?){1,6})")


def _get(url, headers):
    for i in range(6):
        r = httpx.get(url, headers=headers, timeout=60)
        if r.status_code == 200 and (not url.endswith(".json") or r.text.lstrip().startswith("{")):
            time.sleep(0.25)
            return r.text
        time.sleep(2 * (i + 1))
    r.raise_for_status()
    return r.text


def eps_row(text):
    """6-K 正文 -> 「摊薄每股收益」那一行的数字列表（括号 = 负数）；没有 -> None。纯函数。"""
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))
    m = EPS_RE.search(t)
    if not m:
        return None
    return [float(x.replace("(", "-").replace(")", "").strip())
            for x in re.findall(r"\(?-?\d+\.\d+\s*\)?", m.group(1).replace(" )", ")"))]


def fetch_rows(cik, quarters, headers):
    """每个 Yahoo 公告日 -> 0~3 天内提交的 6-K 里的 EPS 行。缓存到 CACHE/<cik>.json。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    cf = CACHE / f"{cik}.json"
    cache = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else {}
    need = [q["date"] for q in quarters if q["date"] not in cache]
    if need:
        sub = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers))
        rows = []
        for blk in [sub["filings"]["recent"]] + [
                json.loads(_get("https://data.sec.gov/submissions/" + f["name"], headers))
                for f in sub["filings"].get("files", [])]:
            rows += [r for r in zip(blk["form"], blk["filingDate"], blk["accessionNumber"])
                     if r[0] == "6-K"]
        for qd in need:
            hit = None
            for _, fd, acc in sorted(r for r in rows if 0 <= (date.fromisoformat(r[1])
                                                               - date.fromisoformat(qd)).days <= 3):
                base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
                for it in json.loads(_get(base + "/index.json", headers))["directory"]["item"]:
                    n = it["name"]
                    if re.search(r"\.(htm|html|txt)$", n, re.I) and "index" not in n.lower():
                        nums = eps_row(_get(f"{base}/{n}", headers))
                        if nums:
                            hit = {"filed": fd, "doc": f"{base}/{n}", "nums": nums}
                            break
                if hit:
                    break
            cache[qd] = hit
            cf.write_text(json.dumps(cache, indent=1), encoding="utf-8")
            print(f"  6-K {qd}: {hit and hit['nums']}", flush=True)
    return cache


def _close(a, b):
    """EPS 不严格可加（各季加权股数不同、逐季四舍五入）：容差 max(0.05, 1%)。"""
    return abs(a - b) <= max(0.05, 0.01 * abs(b))


def resolve_year(rels, annual_val, prev=None):
    """一个财年的季报 EPS 行（按时间）-> 本季值列表或 None。纯函数。

    列序枚举：每份取第 0 或第 1 个数为本季、另一个为去年同季；4 列的行同侧的累计（第 2/3
    个数）要等于本财年已出各季之和。有 20-F 时要求 4 季之和 = 年度 EPS；有上一年已定的
    本季值 prev 时，去年同季那一列必须等于它（只靠累计会有两解：去年各季也能加出去年累计）。
    行缺失的季度不进枚举、记 None。唯一可行组合才算定下。"""
    idx = [i for i, r in enumerate(rels) if r is not None and len(r) >= 2]
    if not idx:
        return None
    sols = set()
    for combo in itertools.product((0, 1), repeat=len(idx)):
        cur = dict.fromkeys(range(len(rels)))
        good = True
        for i, o in zip(idx, combo):
            r = rels[i]
            cur[i] = r[o]
            if prev and prev[i] is not None and not _close(r[1 - o], prev[i]):
                good = False
            if len(r) >= 4 and i > 0:
                if any(cur[j] is None for j in range(i)) or not _close(r[2 + o], sum(cur[j] for j in range(i + 1))):
                    if all(cur[j] is not None for j in range(i)):
                        good = False
        if good and annual_val is not None:
            good = len(idx) == 4 and _close(sum(cur.values()), annual_val)
        if good:
            sols.add(tuple(None if v is None else round(v, 2) for v in cur.values()))
    return list(next(iter(sols))) if len(sols) == 1 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="ASML")
    ap.add_argument("--cik", type=int, default=937966)
    ap.add_argument("--from", dest="since", type=int, default=2011)
    ap.add_argument("--out")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    t = a.ticker.upper()
    H = {"User-Agent": f"sec-filing-downloader verify ({contact_email()})"}
    import yfinance as yf
    tk = yf.Ticker(t)
    inp = pb.load_inputs(t, contact_email(), 10)
    quarters = fb.parse_yahoo_quarters(tk.get_earnings_dates(limit=100))
    ccy, annual, _ = fb.load_annual(inp)
    last_px = inp["hist"]["Close"].dropna().index[-1].date()
    lo = (date.fromisoformat(min(annual)) - timedelta(days=370)).isoformat()
    fx_daily = fb.fx_history(ccy, lo, (last_px + timedelta(days=1)).isoformat())
    fx_at, fx = fb.fx_lookup(fx_daily), fb.fx_annual(annual, fx_daily, ccy)
    ratio = fb.ADR_RATIO.get(t) or fb.infer_ratio(quarters, annual, fx, ccy)
    checks = fb.crosscheck(quarters, annual, fx, ratio, ccy)
    ok, _ = fb.accepted_quarters(quarters, checks)
    stmt = fb.parse_statement_eps(tk.quarterly_income_stmt)
    prod_q, recon = fb.reconcile_statement(quarters, ok, stmt, ccy, fx_at)
    ok = {d: u for d, u in ok.items() if d not in {r["date"] for r in recon["dropped"]}}
    closes = [(d.date(), float(v)) for d, v in inp["hist"]["Close"].dropna().items()]
    prod = {s["date"]: s for s in fb.build_series(prod_q, ok, closes, fx_at)}

    scope = [q for q in quarters if int(q["date"][:4]) >= a.since]
    raw = fetch_rows(a.cik, scope, H)

    # 1) 6-K 本季值：按财年分组定列序
    sixk, prev = {}, None
    years = sorted({int(e[:4]) for e in annual} | {last_px.year})
    for y in years:
        end = f"{y}-12-31"
        qs = [q for q in fb.quarters_of_year(scope, end)]
        if not qs:
            prev = None
            continue
        rels = [(raw.get(q["date"]) or {}).get("nums") for q in qs]
        # 6-K 是原始口径：先用 20-F 原始申报值定列序，不行再用重述后的值（ASML 2016/2017）
        vals = ([annual[end]["orig"] * ratio, annual[end]["val"] * ratio]
                if end in annual else [None])
        cur = None
        for val in vals:
            if not (len(qs) == 4 or val is None):
                continue
            cur = resolve_year(rels, val, prev)
            if cur is None and prev is not None:  # 锚点本身可能错（上一年没定下）：不带锚再试
                cur = resolve_year(rels, val)
            if cur:
                break
        for q, v in zip(qs, cur or [None] * len(qs)):
            sixk[q["date"]] = v
        prev = cur if cur and len(cur) == 4 else None

    L = [f"# {t} TTM PE 兜底历史验算（{date.today()}）", "",
         f"独立来源：SEC 6-K 季报原文的摊薄 EPS（{ccy}，每 ADR，1 ADR = {ratio:g} 股）。"
         "生产管线 = valuation/foreign_band.py（Yahoo 公告值 + 20-F 年度核对 + 财报季度对照）。"
         f"由 `scripts/verify_foreign_6k.py --ticker {t}` 生成。", ""]

    L += ["## 1. 逐季：Yahoo 公告值 vs 6-K 原文", "",
          "| 公告日 | Yahoo | Yahoo 币种 | 6-K 原文 (" + ccy + ") | 差 | 生产管线用值 |",
          "|---|---|---|---|---|---|"]
    n_cmp = n_bad = 0
    for q, pq in zip(quarters, prod_q):
        if q["date"] not in sixk:
            continue
        u = ok.get(q["date"]) or "剔除"
        v = sixk[q["date"]]
        ye = q["eps"] if u != "USD" else (q["eps"] * fx_at(date.fromisoformat(q["date"]))
                                          if fx_at(date.fromisoformat(q["date"])) else None)
        d = f"{ye / v - 1:+.1%}" if v and ye is not None else "—"
        if v and ye is not None:
            n_cmp += 1
            if abs(ye / v - 1) > 0.015:
                n_bad += 1
                d = f"**{d}**"
        used = "—" if u == "剔除" else f"{pq['eps']:.2f}" + (" ←财报" if pq["eps"] != q["eps"] else "")
        L.append(f"| {q['date']} | {q['eps']:.2f} | {u} | {'未解析' if v is None else f'{v:.2f}'}"
                 f" | {d} | {used} |")
    L += ["", f"可比 {n_cmp} 季，|差| > 1.5% 的 {n_bad} 季（美元季按公告日汇率折成 {ccy}，"
          "含汇率口径差）。", ""]

    L += ["## 2. 逐年：20-F 年度核对", "", "| 财年 | Yahoo 币种 | Yahoo 4 季和 | 20-F | 偏差 | 结果 |",
          "|---|---|---|---|---|---|"]
    for c in checks:
        if int(c["fy_end"][:4]) < a.since:
            continue
        ys = "—" if c["yahoo"] is None else f"{c['yahoo']:.2f}"
        ss = "—" if c["sec"] is None else f"{c['sec']:.2f}"
        dv = "—" if c["dev"] is None else f"{c['dev']:+.1%}"
        res = "通过" if c["ok"] else "剔除：" + c.get("why", "")
        L.append(f"| {c['fy_end'][:4]} | {c['unit'] or '—'} | {ys} | {ss} | {dv} | {res} |")
    L += ["", f"财报季度对照：{json.dumps(recon, ensure_ascii=False)}", ""]

    L += ["## 3. 历史时点：生产管线 PE vs 6-K 独立重算", "",
          "| 日期 | 收盘 | 生产管线 TTM PE | 6-K 重算 TTM PE | 差 | 6-K 窗口 (本币) |", "|---|---|---|---|---|---|"]
    px = dict(closes)
    days = sorted(px)
    targets = []
    for y in range(max(a.since, days[0].year), last_px.year + 1):
        for m in (3, 9):
            dd = next((d for d in days if d >= date(y, m, 1)), None)
            if dd and dd not in targets and (dd - date(y, m, 1)).days <= 7:
                targets.append(dd)
    targets.append(days[-1])
    diffs = []
    for dd in targets:
        known = [q for q in quarters if q["date"] < dd.isoformat()][-4:]
        vals = [sixk.get(q["date"]) for q in known]
        r = fx_at(dd)
        ind = (px[dd] / (sum(vals) / r) if len(vals) == 4 and None not in vals and r
               and sum(vals) > 0 else None)
        p = prod.get(dd.isoformat())
        pp = p["pe_trailing"] if p else None
        d = f"{pp / ind - 1:+.1%}" if pp and ind else "—"
        if pp and ind:
            diffs.append(abs(pp / ind - 1))
        L.append(f"| {dd} | {px[dd]:.2f} | {'剔除' if pp is None else f'{pp:.1f}x'} | "
                 f"{'—' if ind is None else f'{ind:.1f}x'} | {d} | "
                 f"{' + '.join('?' if v is None else f'{v:.2f}' for v in vals)} |")
    if diffs:
        L += ["", f"可比 {len(diffs)} 个时点：|差| 中位 {sorted(diffs)[len(diffs) // 2]:.1%}，"
              f"最大 {max(diffs):.1%}。「剔除」= 该日 TTM 窗口含未通过 20-F 核对的财年，生产管线不出点。"]
    text = "\n".join(L) + "\n"
    print(text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
