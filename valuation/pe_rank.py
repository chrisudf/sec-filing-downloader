# -*- coding: utf-8 -*-
"""Watchlist 批量表：当前 PE 在近 10/5/3 年的历史分位 + 前瞻 PE，一票一行。

用法:
  python valuation/pe_rank.py [--tickers A,B] [--watchlist PATH] [--out-dir DIR]

票单默认读同级目录 ../watchlist-scanner/watchlist.toml；kind=etf/index 跳过（SEC 无 EPS）。
结果写 reports/pe_rank/pe_rank_YYYY-MM-DD.{md,csv}（reports/ 已 gitignore），同时打印到终端。
节奏：分母一季度才跳一次，每周跑一次 + 财报季补跑就够，天天跑没有信息量。

列的口径:
  TTM PE / 分位 —— pe_band trailing 口径（XBRL 已公告 TTM EPS × yfinance 日收盘，
    防前视/拆股归一/畸变剔除/近零剔除全在 pe_band）。10y、5y 各用自己窗口重算的带
    （近零地板按窗口中位数定，与 pe_band CLI 一致）；3y = 5y 带里近 3 年那段的秩，
    与 pe_band 的 recent 子窗同源。有效样本 < MIN_DAYS 的不给分位（SOFI/HOOD 型刚转盈）。
  † —— 带子末点比最新收盘早 > STALE_DAYS 天 = 当前 TTM 窗口被剔（一次性畸变/亏损/
    近零），显示的是末个有效点及其分位。AMZN/GOOG 2026Q2 私募重估就是这种，看营业线那组。
  营业线 PE —— metric=opeps，分母 = 营业利润×(1−21%)÷股数，营业线以下的一次性免疫。
  本财年 / 下财年 PE —— 收盘 ÷ yfinance 一致预期 0y / +1y（分析师口径，通常是调整后，
    与 TTM 的 GAAP 不同口径）。Yahoo 的 forwardPE 就是「下财年」这一列，不是 NTM——
    错位财年（NVDA 1 月底）会领先约 16 个月，看起来特别便宜。区间 = 收盘÷预期 high …
    收盘÷预期 low，low ≤ 0 的上沿写「含亏损」。
  分位与前瞻 PE 回答的是两个问题（历史位置 vs 预期兑现后的倍数），要一起读。
"""
import argparse
import csv
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
import pe_band as pb  # noqa: E402
from ref_table import fetch_consensus, fy_labels, one_time_warning  # noqa: E402
from app.edgar import contact_email  # noqa: E402

DEFAULT_WATCHLIST = ROOT.parent / "watchlist-scanner" / "watchlist.toml"
OUT_DIR = ROOT / "reports" / "pe_rank"
STALE_DAYS = 7    # 带子末点落后最新收盘超过这么多天 = 当前窗口被剔
MIN_DAYS = 250    # 与 pe_band 的 thin_coverage 同一门槛（约一个财年的交易日）
SKIP_KINDS = ("etf", "index")
METRICS = (("gaap", "eps", "pe_trailing"), ("op", "opeps", "peop_trailing"))


def load_watchlist(path, tickers=None):
    """-> [(ticker, kind)]。--tickers 给了就按它（kind 仍从票单查，查不到当 stock）。"""
    wl = {}
    if Path(path).exists():
        wl = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("tickers", {})
    elif not tickers:
        raise SystemExit(f"找不到票单 {path}（用 --watchlist 指定，或 --tickers 直接给）")
    names = [t.strip().upper() for t in tickers.split(",") if t.strip()] if tickers else list(wl)
    return [(t, wl.get(t, {}).get("kind", "stock")) for t in names]


def summarize(b10, b5, last_px_date, key):
    """两条带（10y/5y，trailing）-> 当前值与三个窗口的分位。纯函数。

    b5 可为 None（5 年窗口样本不足时 compute_band 抛错）；b5 需带 series。"""
    cur = b10["current"]
    x = cur[key]
    r10 = None if b10["thin_coverage"] else pb.rank_of(b10["_sorted"], x)
    r5 = r3 = p3 = None
    if b5 is not None:
        if not b5["thin_coverage"]:
            r5 = pb.rank_of(b5["_sorted"], x)
        start3 = pb.years_ago(3).isoformat()
        sv3 = sorted(s[key] for s in b5["series"] if s["date"] >= start3)
        if len(sv3) >= MIN_DAYS:
            r3 = pb.rank_of(sv3, x)
        p3 = (b5.get("recent") or {}).get("pctiles")
    lag = (last_px_date - date.fromisoformat(cur["date"])).days
    return {"pe": x, "date": cur["date"], "fresh": lag <= STALE_DAYS,
            "ttm_period": cur.get("ttm_period"), "days10": b10["days"],
            "thin": b10["thin_coverage"], "r10": r10, "r5": r5, "r3": r3, "p3": p3}


def band_reading(t, email, inputs, metric, key, last_px_date):
    try:
        b10 = pb.compute_band(t, email, 10, "trailing", metric=metric, inputs=inputs)
    except RuntimeError as e:
        return {"err": str(e).splitlines()[0][:100]}, None
    try:
        b5 = pb.compute_band(t, email, 5, "trailing", include_series=True,
                             metric=metric, inputs=inputs)
    except RuntimeError:
        b5 = None
    return summarize(b10, b5, last_px_date, key), b10


def forward(close, cons):
    """收盘 ÷ 一致预期 -> 本财年/下财年 PE 与下财年区间。纯函数。

    预期 ≤0 或 NaN（NaN 比较恒假）的 PE 记 None；区间两端方向相反：
    预期最高的分析师给出最低的 PE。"""
    pe = lambda eps: close / eps if eps is not None and eps > 0 else None  # noqa: E731
    out = {}
    for k in ("fy1", "fy2"):
        c = cons.get(k)
        if c:
            out[f"{k}_eps"], out[f"{k}_pe"], out[f"{k}_n"] = c["avg"], pe(c["avg"]), c["n"]
    c2 = cons.get("fy2")
    if c2:
        out["fy2_pe_lo"] = pe(c2["high"])
        out["fy2_pe_hi"] = pe(c2["low"])
        # NaN（无预期）不算亏损：NaN <= 0 恒假，区间上沿留「—」
        out["fy2_low_nonpos"] = c2["low"] <= 0
    return out


def yahoo_info(t):
    import yfinance as yf
    try:
        i = yf.Ticker(t).info
    except Exception:
        return {}
    fye = i.get("lastFiscalYearEnd")
    return {"tpe": i.get("trailingPE"), "peg": i.get("trailingPegRatio"),
            "last_fy_end": date.fromtimestamp(fye).isoformat() if fye else None}


def collect(t, kind, email):
    """一只票的全部读数（联网）。异常逐项兜住，一票失败不拖垮整张表。"""
    if kind in SKIP_KINDS:
        return {"ticker": t, "skip": f"{kind}：SEC 无 EPS"}
    row = {"ticker": t, "notes": []}
    try:
        inputs = pb.load_inputs(t, email, 10)
        hist = inputs["hist"]
        row["close"] = float(hist["Close"].iloc[-1])
        row["px_date"] = hist.index[-1].date()
    except Exception as e:
        return {"ticker": t, "skip": str(e).splitlines()[0][:100]}
    b10_eps = None
    for name, metric, key in METRICS:
        row[name], b10 = band_reading(t, email, inputs, metric, key, row["px_date"])
        if metric == "eps":
            b10_eps = b10
    row["yh"] = yahoo_info(t)
    try:
        cons, trend, _ = fetch_consensus(t)
        row["fwd"] = forward(row["close"], cons)
        # 判据复用 ref_table，文案不复用（那边的建议是钉 overrides，这张表不适用）
        if one_time_warning(trend):
            j0, j1 = (trend[p]["current"] / trend[p]["90daysAgo"] - 1 for p in ("0y", "+1y"))
            row["notes"].append(f"本财年一致预期 90 天内 {j0:+.0%}、下财年仅 {j1:+.0%}——"
                                "疑似一次性收益进了预期，本财年 PE 偏低不可信，看下财年")
    except (SystemExit, Exception) as e:   # fetch_consensus 取不到时 raise SystemExit
        row["fwd"] = {}
        row["notes"].append(f"一致预期取不到：{str(e).splitlines()[0][:80]}")
    # 财年标签：Yahoo 的 lastFiscalYearEnd 覆盖所有票（XBRL 带缺席的 TSM/RKLB 也有）
    lfy = row["yh"].get("last_fy_end")
    src = {"fiscal_years": [{"fy_end": lfy}]} if lfy else (b10_eps or {})
    row["fy_labels"] = fy_labels(src)
    return row


def _f(v, d=1, suffix=""):
    if v is None:
        return "—"
    # 分母趋零（RKLB/SPCX 型）的四位数倍数没有信息量，只说明「极高」
    return f">999{suffix}" if v >= 1000 else f"{v:.{d}f}{suffix}"


def _pc(v):
    return "—" if v is None else f"P{v:.0f}"


def _band_cells(m):
    if "err" in m:
        return ["n/a", "—", "—", "—", "—"]
    pe = f"{m['pe']:.1f}x" + ("" if m["fresh"] else f"†{m['date'][5:]}")
    p3 = m["p3"]
    b = "—" if not p3 else f"{p3[10]:.0f}/{p3[50]:.0f}/{p3[90]:.0f}"
    return [pe, _pc(m["r10"]), _pc(m["r5"]), _pc(m["r3"]), b]


def _fwd_cells(fw):
    if not fw:
        return ["—", "—", "—"]
    lo, hi = fw.get("fy2_pe_lo"), fw.get("fy2_pe_hi")
    if "fy2_pe_lo" not in fw:
        rng = "—"
    elif fw.get("fy2_low_nonpos"):
        rng = f"{_f(lo)}–含亏损"
    else:
        rng = f"{_f(lo)}–{_f(hi)}"
    return [_f(fw.get("fy1_pe"), suffix="x"), _f(fw.get("fy2_pe"), suffix="x"), rng]


HEADER = ["票", "收盘", "TTM PE", "10y", "5y", "3y", "3y P10/50/90",
          "营业线 PE", "10y", "5y", "3y", "3y P10/50/90",
          "本财年 PE", "下财年 PE", "下财年区间", "下财年", "Yahoo tPE", "PEG"]


def render(rows, asof):
    """-> (markdown 全文, 脚注列表)。纯函数。"""
    md = [f"# Watchlist PE 分位 · {asof}", "",
          "TTM PE 与分位 = SEC XBRL 已公告 TTM EPS × yfinance 收盘（pe_band trailing）；"
          "† = 当前窗口被剔，显示末个有效点；本/下财年 PE = 收盘 ÷ yfinance 一致预期"
          "（Yahoo forwardPE = 下财年列，不是 NTM）。口径细节见 valuation/pe_rank.py 文件头。", "",
          "| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    notes = []
    for r in rows:
        t = r["ticker"]
        if "skip" in r:
            md.append(f"| {t} | " + " | ".join(["—"] * (len(HEADER) - 2)) + f" | {r['skip']} |")
            notes.append(f"{t}: 跳过 — {r['skip']}")
            continue
        yh = r.get("yh", {})
        md.append("| " + " | ".join(
            [t, f"{r['close']:.2f}"] + _band_cells(r["gaap"]) + _band_cells(r["op"])
            + _fwd_cells(r.get("fwd")) + [r["fy_labels"][1],
                                          _f(yh.get("tpe")), _f(yh.get("peg"), 2)]) + " |")
        for lbl, m in (("TTM", r["gaap"]), ("营业线", r["op"])):
            if "err" in m:
                notes.append(f"{t} {lbl}: {m['err']}")
            elif not m["fresh"]:
                notes.append(f"{t} {lbl}: 当前 TTM 窗口被剔（一次性畸变/亏损/近零），"
                             f"末个有效点 {m['date']}")
            elif m["thin"]:
                notes.append(f"{t} {lbl}: 有效样本仅 {m['days10']} 天，不给分位")
        notes += [f"{t}: {n}" for n in r.get("notes", [])]
    md += ["", "## 脚注", ""] + [f"- {n}" for n in notes]
    return "\n".join(md) + "\n", notes


def csv_rows(rows):
    out = []
    for r in rows:
        rec = {"ticker": r["ticker"], "skip": r.get("skip"),
               "close": r.get("close"), "px_date": r.get("px_date")}
        for name, _, _ in METRICS:
            m = r.get(name) or {}
            for k in ("pe", "date", "fresh", "r10", "r5", "r3", "err"):
                rec[f"{name}_{k}"] = m.get(k)
            p3 = m.get("p3") or {}
            for q in (10, 50, 90):
                rec[f"{name}_3y_p{q}"] = p3.get(q)
        fw = r.get("fwd") or {}
        for k in ("fy1_eps", "fy1_pe", "fy1_n", "fy2_eps", "fy2_pe", "fy2_n",
                  "fy2_pe_lo", "fy2_pe_hi"):
            rec[k] = fw.get(k)
        labels = r.get("fy_labels") or (None, None)
        rec["fy1_label"], rec["fy2_label"] = labels
        yh = r.get("yh") or {}
        rec["yahoo_tpe"], rec["yahoo_peg"] = yh.get("tpe"), yh.get("peg")
        out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="逗号分隔，覆盖票单")
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    a = ap.parse_args()
    # Windows 控制台默认 cp1252，打印中文表会 UnicodeEncodeError（文件已写完也会丢终端输出）
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")

    email = contact_email()
    rows = []
    for t, kind in load_watchlist(a.watchlist, a.tickers):
        print(f"… {t}", file=sys.stderr, flush=True)
        rows.append(collect(t, kind, email))

    asof = date.today().isoformat()
    text, _ = render(rows, asof)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"pe_rank_{asof}.md").write_text(text, encoding="utf-8")
    recs = csv_rows(rows)
    with open(out / f"pe_rank_{asof}.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(recs[0]))
        w.writeheader()
        w.writerows(recs)
    print(text)
    print(f"已写出 {out / f'pe_rank_{asof}.md'} 与 .csv", file=sys.stderr)


if __name__ == "__main__":
    main()
