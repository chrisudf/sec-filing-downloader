# -*- coding: utf-8 -*-
"""参考表复刻：财年一致预期 EPS × PE 带 -> 价值交易区间 + 中位价 + EPS 修正轨迹。

用法:
  python ref_table.py TICKER EMAIL [--overrides PATH] [--basis forward]
                      [--years 5] [--no-snapshot]

参考表的公式只有两个输入（逐格核实过，分毫不差）:
  区间 = [PE低 × 财年EPS, PE高 × 财年EPS]，中位价 = PE中位 × 财年EPS。

两个输入的来源与本工具的对应:
  EPS —— 表里是付费源的卖方一致预期（non-GAAP ≈ GAAP 剔一次性，作者手工微调）。
    这里用 yfinance 免费一致预期（0y/+1y 财年口径，含 low/high/分析师数）与
    7/30/60/90 天修正轨迹。一次性项目进 GAAP consensus 的畸变（AMZN 2026
    Anthropic 重估：0y 90 天内 +41% 而 +1y 只 +5%）按「本财年跳幅远大于下财年」
    自动预警——修正靠 overrides 钉死。作者自己也是这么手调的，区别是这里的
    override 落在文件里有留痕。
  PE 带 —— 表里是作者手拍。这里默认用 pe_band 分位带（basis=forward 与财年
    EPS 同口径；近3年子窗优先，避开 2021 regime），overrides 可钉死任意带子
    （比如照抄参考表那组），输出同时给分位带对照 + 手拍带的逆向匹配
    （框住了历史交易日的百分之几、上下沿各在第几百分位）。

快照: 每次运行追加 ref_snapshots/{TICKER}.json——参考表「一行一个季度」的
修正轨迹，SEC 与免费源都没有历史存档（eps_trend 只回溯 90 天），只能自己攒；
渲染时最近几行按参考表布局排出（末行=本次）。

overrides 文件（默认读 repo 根 ref_table_overrides.json，样例见
ref_table_overrides.example.json）:
  {"AMZN": {"band": [23.5, 27.5, 31.5], "band_note": "参考表 2026-08",
            "eps_fy1": 9.06, "eps_fy1_note": "剔 Anthropic 一次性(税前 $53.4B)",
            "eps_fy2": null}}
"""
import argparse
import json
import math
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pe_band import compute_band, rank_of, coverage  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "ref_snapshots"


def _p(pcts, q):
    """分位表取值：进程内调用键是 int，经 JSON round-trip 是 str——两者都接。"""
    if pcts is None:
        return None
    return pcts.get(q) if q in pcts else pcts.get(str(q))


def fetch_consensus(ticker):
    """yfinance 一致预期：0y/+1y 财年 EPS（avg/low/high/#分析师）+ 修正轨迹 + 现价。"""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    ee = tk.earnings_estimate
    if ee is None or ee.empty or "0y" not in ee.index:
        raise SystemExit(f"{ticker} 取不到 yfinance 一致预期（earnings_estimate 为空）")
    trend = None
    try:
        et = tk.eps_trend
        if et is not None and not et.empty:
            trend = {per: {c: (float(et.loc[per, c]) if et.loc[per, c] == et.loc[per, c] else None)
                           for c in ("90daysAgo", "60daysAgo", "30daysAgo", "7daysAgo", "current")}
                     for per in ("0y", "+1y") if per in et.index}
    except Exception:
        pass  # 修正轨迹是增值信息，缺失不阻断
    px = float(tk.fast_info["lastPrice"])
    out = {}
    for per, name in (("0y", "fy1"), ("+1y", "fy2")):
        if per in ee.index:
            r = ee.loc[per]
            out[name] = {"avg": float(r["avg"]), "low": float(r["low"]),
                         "high": float(r["high"]), "n": int(r["numberOfAnalysts"])}
    return out, trend, px


def one_time_warning(trend):
    """本财年 90 天跳幅远大于下财年 => 疑似一次性项目进了 GAAP consensus。

    校准（AMZN 2026-08 实测）：Anthropic 重估后 0y 8.57→12.11（+41%）而 +1y
    只 +5.5%——真实的基本面上修会同时抬两年（下财年通常抬得更多），只抬本
    财年的大跳变几乎只有一次性项目一种解释。"""
    if not trend or "0y" not in trend or "+1y" not in trend:
        return None
    t0, t1 = trend["0y"], trend["+1y"]
    vals = [t0.get("90daysAgo"), t0.get("current"), t1.get("90daysAgo"), t1.get("current")]
    # 四个值必须**全为正且有限**才谈得上"变动百分比"：亏损或零穿越下除法没有方向
    # 意义——-1.00 → -0.50 是亏损腰斩（利好）却算出 j0=-50%，-0.10 → +0.50 是转盈
    # 却算出 -600%，两者都会触发一条完全反向的"疑似一次性项目"预警。原来的
    # truthy 判断只挡住了 0 和 None，负数照过。
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in vals):
        return None
    j0 = t0["current"] / t0["90daysAgo"] - 1
    j1 = t1["current"] / t1["90daysAgo"] - 1
    if abs(j0) > 0.15 and abs(j0) > 2.5 * abs(j1):
        return (f"⚠ FY1 consensus 90 天内变动 {j0:+.0%} 而 FY2 仅 {j1:+.0%}——疑似一次性"
                "项目进入 GAAP 口径 consensus（AMZN 2026 Anthropic 型）。参考表口径是"
                "剔一次性的，建议在 overrides 里钉死 eps_fy1（并写 note 留痕）。")
    return None


def fy_labels(band):
    """从 pe_band 的财年末序列推 FY1/FY2 标签（FY 以结束日历年命名 + 结束月）。"""
    ends = [f["fy_end"] for f in band.get("fiscal_years", []) if f["fy_end"] != "本财年至今"]
    if not ends:
        return "FY1", "FY2"
    last = date.fromisoformat(ends[-1])
    nxt = last.replace(year=last.year + 1)
    today = date.today()
    while nxt < today:   # 财报已出但年报未入 XBRL 的窗口里最多多滚一年
        nxt = nxt.replace(year=nxt.year + 1)
    f2 = nxt.replace(year=nxt.year + 1)
    return (f"FY{nxt.year}(至{nxt.strftime('%Y-%m')})",
            f"FY{f2.year}(至{f2.strftime('%Y-%m')})")


def snapshot(ticker, row, keep=True):
    """追加本次快照（原子写，惯例同 vintages）。返回历史行（含本次）。

    keep=False（--no-snapshot）时**一个字节都不落盘**，连目录都不建：只读检出
    /只读挂载下也要能出预览。已存在的目录照读，用于拼历史行。
    """
    f = SNAP_DIR / f"{ticker}.json"
    rec = {"ticker": ticker, "rows": []}
    if f.exists():
        try:
            _r = json.loads(f.read_text(encoding="utf-8"))
            # 快照文件被写成数组/标量时不能往下走：rows.setdefault 会 AttributeError
            rec = _r if isinstance(_r, dict) else rec
        except json.JSONDecodeError:
            pass
    rec.setdefault("rows", []).append(row)
    if keep:
        SNAP_DIR.mkdir(exist_ok=True)
        tmp = SNAP_DIR / f".{ticker}.json.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)
    return rec["rows"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("email")
    ap.add_argument("--overrides", default=str(ROOT / "ref_table_overrides.json"))
    ap.add_argument("--basis", choices=("forward", "ntm", "trailing"), default="forward",
                    help="分位带口径；forward=财年实现 EPS，与本表的财年 EPS 同口径（默认）")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--no-snapshot", action="store_true")
    a = ap.parse_args()
    T = a.ticker.upper()

    ov = {}
    if Path(a.overrides).exists():
        ov = json.loads(Path(a.overrides).read_text(encoding="utf-8")).get(T, {})

    cons, trend, px = fetch_consensus(T)
    try:
        band = compute_band(T, a.email, a.years, a.basis)
    except RuntimeError as e:
        raise SystemExit(f"{T} 分位带计算失败：{e}（overrides 里给 band 也无法逆向匹配）")
    sv = band.pop("_sorted")
    rc = band.get("recent") or {}
    rp = rc.get("pctiles") or {}
    pcts = band["pctiles"]

    # 带子：override 优先（照抄参考表的入口），否则近3年子窗 P25/50/75，回退全窗
    if ov.get("band"):
        # 手拍带子是外部输入边界，必须在这里校验：长度不对会抛 unpack traceback，
        # 反序/非正会让下游算出负覆盖率、反向价值区间和 nan 定位——都是"看起来
        # 出了数其实全错"的形态，比直接报错难查得多
        _b = ov["band"]
        if not isinstance(_b, (list, tuple)) or len(_b) != 3:
            raise SystemExit(f"overrides.{T}.band 必须是三个数 [PE低, PE中, PE高]，"
                             f"实际收到：{_b!r}")
        try:
            lo, mid, hi = (float(x) for x in _b)
        except (TypeError, ValueError) as e:
            raise SystemExit(f"overrides.{T}.band 含非数值：{_b!r}") from e
        if not all(math.isfinite(v) and v > 0 for v in (lo, mid, hi)):
            raise SystemExit(f"overrides.{T}.band 三个值必须为正且有限：{_b!r}")
        if not lo < mid < hi:
            raise SystemExit(f"overrides.{T}.band 必须严格升序 [低 < 中 < 高]：{_b!r}"
                             "（反序会产出反向的价值区间与负覆盖率）")
        band_src = f"override（{ov.get('band_note', '手拍')}）"
    elif band.get("thin_coverage"):
        # 覆盖不足的带子不能当证据——与 engine / valuation_service / pe_band_check
        # 同一个闸门（0059 起：<250 天的分布没有锚的话语权）。这个分支是两个 PR
        # 合流后才出现的：本分支从 main 开出时 compute_band 还不产出该字段。
        # 本表唯一的带子输入就是它，停用即无区间可出，所以直接要求手拍并留痕，
        # 而不是悄悄拿薄样本的 P25/P75 给一个没有历史背书的区间披上分位数外衣。
        raise SystemExit(
            f"{T} 历史{a.basis} PE 带覆盖不足（仅 {band.get('days')} 个交易日 / "
            f"{band.get('years')} 年，原始数据缺口）——本表不拿薄样本的分位当带子。\n"
            f"  请在 {a.overrides} 里为 {T} 手拍 band（并写 band_note 留痕），"
            "或放宽 --years 后重试。")
    elif rp:
        lo, mid, hi = _p(rp, 25), _p(rp, 50), _p(rp, 75)
        band_src = f"近{rc['years']}年{a.basis}分位 P25/P50/P75"
    else:
        lo, mid, hi = _p(pcts, 25), _p(pcts, 50), _p(pcts, 75)
        band_src = f"近{band['years']}年{a.basis}分位 P25/P50/P75（无子窗样本）"

    fy1_lab, fy2_lab = fy_labels(band)
    warn = one_time_warning(trend)

    print(f"\n===== {T} 参考表复刻 · {date.today().isoformat()} · 现价 {px:.2f} =====")
    print(f"PE 带: {lo:g} / {mid:g} / {hi:g}   [{band_src}]")
    if ov.get("band"):
        alt = rp or pcts
        alt_w = f"近{rc['years']}年" if rp else f"近{band['years']}年"
        print(f"  分位带对照（{alt_w}{a.basis}）: P25 {_p(alt, 25):.1f} / P50 {_p(alt, 50):.1f} / "
              f"P75 {_p(alt, 75):.1f}   全窗 P10~P90 {_p(pcts, 10):.1f}~{_p(pcts, 90):.1f}")
        print(f"  逆向匹配: 下沿 {lo:g} → 第 {rank_of(sv, lo):.0f} 百分位，"
              f"上沿 {hi:g} → 第 {rank_of(sv, hi):.0f} 百分位，"
              f"覆盖 {coverage(sv, lo, hi):.1f}% 的历史交易日")

    rows_now = {}
    for name, lab in (("fy1", fy1_lab), ("fy2", fy2_lab)):
        c = cons.get(name)
        eps_ov = ov.get(f"eps_{name}")
        if eps_ov is not None:
            eps, src = float(eps_ov), f"override（{ov.get(f'eps_{name}_note', '手拍')}）"
        elif c:
            eps, src = c["avg"], f"consensus {c['n']} 分析师，区间 {c['low']:.2f}~{c['high']:.2f}"
        else:
            continue
        # PE 法对非正 EPS 不成立：eps=0 会在下面 px/r_mid 直接 ZeroDivisionError，
        # eps<0 则 lo*eps > hi*eps，区间反向、pos 变 nan，还会打印一段"负价格区间"。
        # 与 pe_band 的口径一致（它计算带子时本就剔除亏损年），也与引擎里
        # 「负 EPS × 正倍数 = 负目标价，PE 腿 n.m.」是同一条原则。
        if not isinstance(eps, (int, float)) or not math.isfinite(eps) or eps <= 0:
            print(f"\n{lab}  EPS {eps} —— 非正或非有限，PE 法不适用，跳过该财年"
                  f"（来源：{src}）。亏损标的请看引擎的 DCF/P-S 口径，参考表只做 PE×EPS")
            continue
        r_lo, r_mid, r_hi = lo * eps, mid * eps, hi * eps
        pos = (px - r_lo) / (r_hi - r_lo) if r_hi > r_lo else float("nan")
        print(f"\n{lab}  EPS {eps:.2f}  [{src}]"
              + (f"\n  {warn}" if warn and name == "fy1" and eps_ov is None else "")
              + (f"\n  （consensus 原值 {c['avg']:.2f} 已被 override 覆盖）"
                 if eps_ov is not None and c else ""))
        print(f"  价值交易区间 {r_lo:,.1f} ~ {r_hi:,.1f}   中位价 {r_mid:,.1f}   "
              f"现价位于区间 {pos:.0%} 处，距中位 {px / r_mid - 1:+.1%}")
        rows_now[name] = {"eps": round(eps, 2), "lo": round(r_lo, 1),
                          "mid": round(r_mid, 1), "hi": round(r_hi, 1)}

    if trend and "0y" in trend:
        def _fmt(tt):
            ks = ("90daysAgo", "60daysAgo", "30daysAgo", "7daysAgo", "current")
            return " → ".join(f"{tt[k]:.2f}" for k in ks if tt.get(k) is not None)
        print(f"\nEPS 修正轨迹(近90天): FY1 {_fmt(trend['0y'])}"
              + (f"   FY2 {_fmt(trend['+1y'])}" if "+1y" in trend else ""))

    # fy1_lab/fy2_lab 必须落盘：财年滚动后 fy1_eps 指向的已经是**另一个**目标财年，
    # 不存标签的话历史表把新旧两个财年并进同一列 FY1 EPS，滚动看起来像一次
    # consensus 大幅修正（实际只是换了年）
    row = {"date": date.today().isoformat(), "price": round(px, 2),
           "band": [lo, mid, hi], "band_src": band_src,
           "fy1_lab": fy1_lab, "fy2_lab": fy2_lab, **{
               f"{k}_{f}": v for k, r in rows_now.items() for f, v in r.items()}}
    rows = snapshot(T, row, keep=not a.no_snapshot)
    if len(rows) > 1:
        print(f"\n历史快照（{SNAP_DIR.name}/{T}.json，参考表布局，末行=本次）:")
        # 按 fy1 目标财年分段：跨财年滚动的行不并成一列，否则"换了年"会被读成
        # "consensus 修正"。老快照没有 fy1_lab（本次之前不落盘），标记为"?"单列一段。
        shown = [r for r in rows[-8:] if r.get("fy1_eps") is not None]
        last_lab = object()
        for r in shown:
            lab = r.get("fy1_lab") or "?（旧快照未记录目标财年）"
            if lab != last_lab:
                print(f"  目标财年 {lab}")
                print(f"  {'日期':<12}{'FY1 EPS':>9}{'区间':>19}{'中位价':>9}{'现价':>9}")
                last_lab = lab
            print(f"  {r['date']:<12}{r['fy1_eps']:>9.2f}"
                  f"{r['fy1_lo']:>9,.1f}~{r['fy1_hi']:<9,.1f}"
                  f"{r['fy1_mid']:>9,.1f}{r['price']:>9,.2f}")
    print("\n口径注: 区间=PE带×财年一致预期EPS（与参考表同公式）。EPS 的 non-GAAP=GAAP"
          "剔一次性，免费 consensus 是 GAAP 口径——畸变预警触发时请用 override 钉死；"
          "带子默认分位数（可审计、每季自更新），override 可照抄参考表并自动给逆向匹配。")


if __name__ == "__main__":
    sys.exit(main())
