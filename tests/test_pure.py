# -*- coding: utf-8 -*-
"""Pure-function tests for feat/improve-accuracy (no network, no heavy imports).

Extracts source verbatim from the worktree so we test production code, not a copy.
"""
import ast
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

# 被测 worktree：SFD_WT 环境变量覆盖，便于对不同 commit 复跑同一套哨兵；
# 默认取本文件所在 repo 根，直接 `python tests/test_pure.py` 即可
WT = Path(os.environ.get("SFD_WT") or Path(__file__).resolve().parent.parent)

FAILS = []
PASSES = []


def check(name, actual, expected):
    if actual == expected:
        PASSES.append(name)
    else:
        FAILS.append((name, f"actual={actual!r}", f"expected={expected!r}"))
        print(f"FAIL  {name}\n      actual   = {actual!r}\n      expected = {expected!r}")


def approx(name, actual, expected, tol=1e-9):
    if actual is None or abs(actual - expected) > tol:
        FAILS.append((name, f"actual={actual!r}", f"expected={expected!r}"))
        print(f"FAIL  {name}\n      actual   = {actual!r}\n      expected = {expected!r}")
    else:
        PASSES.append(name)


# ---- extract _add_months + fwd_window verbatim from app/valuation_service.py
vs_src = (WT / "app" / "valuation_service.py").read_text(encoding="utf-8")
tree = ast.parse(vs_src)
segs = [ast.get_source_segment(vs_src, node) for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in ("_add_months", "fwd_window")]
assert len(segs) == 2, segs
ns_vs = {"date": date, "timedelta": timedelta}
exec("\n\n".join(segs), ns_vs)
_add_months = ns_vs["_add_months"]
fwd_window = ns_vs["fwd_window"]

# ---- exec pe_band.py with httpx neutralized (module-level import only)
pb_src = (WT / "valuation" / "pe_band.py").read_text(encoding="utf-8")
pb_src = pb_src.replace("import httpx", "httpx = None")
ns_pb = {"__name__": "pe_band_under_test"}
exec(compile(pb_src, str(WT / "valuation" / "pe_band.py"), "exec"), ns_pb)
pctile = ns_pb["pctile"]
rank_of = ns_pb["rank_of"]
coverage = ns_pb["coverage"]
derive_q4 = ns_pb["derive_q4"]
derive_q4_avg = ns_pb["derive_q4_avg"]
normalize_splits = ns_pb["normalize_splits"]
build_ttm_eps = ns_pb["build_ttm_eps"]
backfill_shares_from_eps = ns_pb["backfill_shares_from_eps"]

# ---- import vintages + trend directly (stdlib only)
sys.path.insert(0, str(WT / "valuation"))
import vintages  # noqa: E402
import trend  # noqa: E402

# =====================================================================
# 1. _add_months
# =====================================================================
check("_add_months Jan31+1 (non-leap Feb)", _add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
check("_add_months Jan31+1 (leap Feb)", _add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
check("_add_months Mar31+1 -> Apr30", _add_months(date(2026, 3, 31), 1), date(2026, 4, 30))
check("_add_months Jun30+12 across years", _add_months(date(2026, 6, 30), 12), date(2027, 6, 30))
check("_add_months Feb29+12 -> Feb28", _add_months(date(2024, 2, 29), 12), date(2025, 2, 28))
check("_add_months Dec31+1 -> Jan31", _add_months(date(2025, 12, 31), 1), date(2026, 1, 31))
check("_add_months day27 fallback path", _add_months(date(2026, 1, 27), 1), date(2026, 2, 27))
check("_add_months day28 -> Feb28", _add_months(date(2026, 1, 28), 1), date(2026, 2, 28))
# quarter-end semantics: Sep30 + 3 months should be a quarter end (Dec 31);
# day-preserving arithmetic gives Dec 30
print("INFO _add_months(2026-09-30, +3) =", _add_months(date(2026, 9, 30), 3))
print("INFO _add_months(2026-12-31, +3) =", _add_months(date(2026, 12, 31), 3))
print("INFO _add_months(2026-03-31, +3) =", _add_months(date(2026, 3, 31), 3))
print("INFO _add_months(2026-06-30, +3) =", _add_months(date(2026, 6, 30), 3))

# =====================================================================
# 2. fwd_window
# =====================================================================
# MSFT: report_end == fy_end (June FY)
w = fwd_window("2026-06-30", "2026-06-30")
check("MSFT aligned", w["aligned"], True)
check("MSFT start", w["start"], "2026-07-01")
check("MSFT end", w["end"], "2027-06-30")
check("MSFT straddle mentions FY2027", "FY2027" in w["straddle"] and "完整财年" in w["straddle"], True)
print("INFO MSFT label:", w["label"])

# AMZN: Dec FY, Q2 report
w = fwd_window("2026-06-30", "2025-12-31")
check("AMZN aligned", w["aligned"], False)
check("AMZN start/end", (w["start"], w["end"]), ("2026-07-01", "2027-06-30"))
check("AMZN straddle", w["straddle"], "横跨 FY2026 后段 + FY2027 前段")
print("INFO AMZN label:", w["label"])

# rolled_quarters=1 from a Jun-30 quarter end (PENDING_10Q)
w = fwd_window("2026-06-30", "2025-12-31", rolled_quarters=1)
check("AMZN rolled start/end", (w["start"], w["end"]), ("2026-10-01", "2027-09-30"))
check("AMZN rolled aligned", w["aligned"], False)
check("AMZN rolled straddle", w["straddle"], "横跨 FY2026 后段 + FY2027 前段")

# rolled_quarters=1 from a Sep-30 quarter end of a Dec-FY company:
# true rolled TTM end is Dec 31 -> NTM is exactly FY2027; day-preserving +3mo gives Dec 30
w = fwd_window("2026-09-30", "2025-12-31", rolled_quarters=1)
print("INFO Q3+rolled Dec-FY:", w)
check("Q3+rolled should be aligned to FY2027 (true quarter end Dec-31)",
      (w["aligned"], w["start"], w["end"]),
      (True, "2027-01-01", "2027-12-31"))

# AAPL 52/53-week filer, Q3 report (window genuinely straddles)
w = fwd_window("2026-06-27", "2025-09-27")
check("AAPL Q3 aligned", w["aligned"], False)
check("AAPL Q3 start/end", (w["start"], w["end"]), ("2026-06-28", "2027-06-27"))
check("AAPL Q3 straddle", w["straddle"], "横跨 FY2026 后段 + FY2027 前段")
print("INFO AAPL Q3 label:", w["label"])

# AAPL true FY end 2026-09-26 (last Sat of Sep; fy_end from last annual = 2025-09-27).
# TTM window IS fiscal 2026; NTM IS fiscal 2027 (to within 52/53-week 1-day noise).
w = fwd_window("2026-09-26", "2025-09-27")
print("INFO AAPL FY-end case:", w)
check("AAPL FY-end report should read as aligned FY2027", w["aligned"], True)

# =====================================================================
# 3. pctile / rank_of / coverage
# =====================================================================
approx("pctile p50 even [1,2,3,4]", pctile([1, 2, 3, 4], 50), 2.5)
approx("pctile p50 odd [1,2,3,4,5]", pctile([1, 2, 3, 4, 5], 50), 3.0)
approx("pctile p25 [1,2,3,4]", pctile([1, 2, 3, 4], 25), 1.75)
approx("pctile p0", pctile([1, 2, 3, 4], 0), 1.0)
approx("pctile p100", pctile([1, 2, 3, 4], 100), 4.0)
approx("pctile p90 [10,20,30,40,50]", pctile([10, 20, 30, 40, 50], 90), 46.0)
approx("pctile single element", pctile([5.0], 75), 5.0)

approx("rank_of below min", rank_of([1, 2, 3, 4], 0.5), 0.0)
approx("rank_of interior 2.5", rank_of([1, 2, 3, 4], 2.5), 50.0)
approx("rank_of at max (bisect_left)", rank_of([1, 2, 3, 4], 4), 75.0)
approx("rank_of above max", rank_of([1, 2, 3, 4], 9), 100.0)
approx("rank_of dup values", rank_of([1, 2, 2, 4], 2), 25.0)

approx("coverage [2,3] of [1,2,3,4]", coverage([1, 2, 3, 4], 2, 3), 50.0)
approx("coverage full", coverage([1, 2, 3, 4], 1, 4), 100.0)
approx("coverage empty band", coverage([1, 2, 3, 4], 0.1, 0.9), 0.0)
approx("coverage inclusive ends", coverage([1, 2, 3, 4], 1, 1), 25.0)

# =====================================================================
# 4. derive_q4 / derive_q4_avg
# =====================================================================
def mk(val, filed):
    return {"val": val, "filed": filed, "first_filed": filed}

# net income: FY=100, Q1..Q3 = 20/25/30 -> Q4 = 25
q = {"2025-03-31": mk(20, "2025-05-01"), "2025-06-30": mk(25, "2025-08-01"),
     "2025-09-30": mk(30, "2025-11-01")}
a = {"2025-12-31": mk(100, "2026-02-01")}
out = derive_q4(dict(q), dict(a))
check("derive_q4 adds Q4", "2025-12-31" in out, True)
approx("derive_q4 Q4 value", out.get("2025-12-31", {}).get("val"), 25.0)
check("derive_q4 Q4 filed from annual", out.get("2025-12-31", {}).get("filed"), "2026-02-01")

# missing one quarter -> no Q4
q2 = {"2025-03-31": mk(20, "f"), "2025-09-30": mk(30, "f")}
out = derive_q4(dict(q2), dict(a))
check("derive_q4 missing quarter -> no Q4", "2025-12-31" in out, False)

# quarter from previous year (365d before FY end) must not count
q3 = {"2024-12-31": mk(99, "f"), "2025-03-31": mk(20, "f"), "2025-06-30": mk(25, "f"),
      "2025-09-30": mk(30, "f")}
out = derive_q4(dict(q3), dict(a))
approx("derive_q4 prev-year quarter excluded, Q4=25", out.get("2025-12-31", {}).get("val"), 25.0)

# already have Q4 -> untouched
q4 = dict(q3); q4["2025-12-31"] = mk(7, "orig")
out = derive_q4(dict(q4), dict(a))
approx("derive_q4 existing Q4 untouched", out["2025-12-31"]["val"], 7)

# shares: FY avg = 1000, Q1..Q3 = 1010/1000/990 -> Q4 = 4*1000-3000 = 1000
sq = {"2025-03-31": mk(1010, "f"), "2025-06-30": mk(1000, "f"), "2025-09-30": mk(990, "f")}
sa = {"2025-12-31": mk(1000, "2026-02-01")}
out = derive_q4_avg(dict(sq), dict(sa))
approx("derive_q4_avg normal Q4", out.get("2025-12-31", {}).get("val"), 1000.0)

# cross-split year: FY restated post-split blend 850, quarters pre-split 250 each
# -> q4 = 4*850-750 = 2650, gate 0.5*250 < 2650 < 2*250 fails -> must NOT be added
sq2 = {"2025-03-31": mk(250, "f"), "2025-06-30": mk(250, "f"), "2025-09-30": mk(250, "f")}
sa2 = {"2025-12-31": mk(850, "f")}
out = derive_q4_avg(dict(sq2), dict(sa2))
check("derive_q4_avg cross-split sanity gate rejects", "2025-12-31" in out, False)

# gate 收紧钉（0047：×÷1.5 对数对称，旧值 0.5/2）——三点差分把边界钉死：
# 回退旧 gate 时 1.6x/0.64x 两条会转绿，测试即失败
sqg = {"2025-03-31": mk(100.0, "f"), "2025-06-30": mk(100.0, "f"), "2025-09-30": mk(100.0, "f")}
out = derive_q4_avg(dict(sqg), {"2025-12-31": mk(115.0, "f")})   # q4=160=1.6x max
check("gate: q4=1.6x rejected (old 0.5/2 accepted)", "2025-12-31" in out, False)
out = derive_q4_avg(dict(sqg), {"2025-12-31": mk(91.0, "f")})    # q4=64=0.64x min
check("gate: q4=0.64x rejected (old accepted)", "2025-12-31" in out, False)
out = derive_q4_avg(dict(sqg), {"2025-12-31": mk(110.0, "f")})   # q4=140=1.4x max
approx("gate: q4=1.4x accepted", out.get("2025-12-31", {}).get("val"), 140.0)

# negative implied q4 rejected
sq3 = {"2025-03-31": mk(1000, "f"), "2025-06-30": mk(1000, "f"), "2025-09-30": mk(1000, "f")}
sa3 = {"2025-12-31": mk(100, "f")}
out = derive_q4_avg(dict(sq3), dict(sa3))
check("derive_q4_avg negative q4 rejected", "2025-12-31" in out, False)

# =====================================================================
# 5. normalize_splits: 逐条 filed 判定（四象限 + 逐条重述 + 多次拆股）
#    口径规则：某期「最新 filed 日」早于拆股生效日 => 拆前口径，应回补；
#    晚于 => 该期所在文件已按 ASC 260 追溯重述，不得再动。
# =====================================================================
def shares_dict(pre, post, pre_filed, post_filed):
    return {"2020-03-28": mk(pre, pre_filed), "2020-06-27": mk(pre, pre_filed),
            "2020-09-26": mk(post, post_filed), "2020-12-26": mk(post, post_filed)}

SPLIT = {date(2020, 8, 31): 4.0}

# forward 4:1, unrestated：拆前期间从未在拆后文件里重现（filed 停在拆股前）
sh = shares_dict(100.0, 400.0, "2020-07-30", "2021-01-28")
notes = normalize_splits(sh, SPLIT)
approx("fwd 4:1 unrestated: pre scaled x4", sh["2020-03-28"]["val"], 400.0)
approx("fwd 4:1 unrestated: post untouched", sh["2020-09-26"]["val"], 400.0)
check("fwd 4:1 unrestated note says adjusted", "回补" in notes[0], True)

# forward 4:1, restated：全部期间的最新 filed 都在拆股后（比较期已重述）
sh = shares_dict(400.0, 400.0, "2021-01-28", "2021-01-28")
notes = normalize_splits(sh, SPLIT)
approx("fwd 4:1 restated: untouched", sh["2020-03-28"]["val"], 400.0)
check("fwd 4:1 restated note says no-adjust", "无需调整" in notes[0], True)

REV = {date(2020, 8, 31): 0.1}  # 1-for-10 reverse split, yfinance ratio 0.1

# reverse 1:10, unrestated (pre-split counts are 10x of post)
sh = shares_dict(1000.0, 100.0, "2020-07-30", "2021-01-28")
notes = normalize_splits(sh, REV)
approx("rev 1:10 unrestated: pre scaled x0.1", sh["2020-03-28"]["val"], 100.0)
check("rev 1:10 unrestated note says adjusted", "回补" in notes[0], True)

# reverse 1:10, RESTATED (all already post-split) -> must be untouched
# 旧启发式在此象限必错：obs≈1 > 0.1*0.75 恒真，把重述好的序列再乘 0.1
sh = shares_dict(100.0, 100.0, "2021-01-28", "2021-01-28")
notes = normalize_splits(sh, REV)
approx("rev 1:10 restated: MUST stay 100", sh["2020-03-28"]["val"], 100.0)
print("INFO rev-restated note:", notes)

# NVDA 型逐条重述：拆股(2024-06-10 10:1)后的新报告只重述比较期（filed 前滚），
# 更老的期间停在拆前口径——必须只回补老期间，比较期一根手指都不能动
MIX = {date(2024, 6, 10): 10.0}
sh = {"2022-10-30": mk(2470.0, "2022-11-18"),   # 拆前口径（从未重现于拆后文件）
      "2023-10-29": mk(2470.0, "2023-11-21"),   # 拆前口径
      "2024-04-28": mk(24700.0, "2024-08-28"),  # 拆后文件里的比较期，已重述
      "2024-07-28": mk(24700.0, "2024-08-28"),  # 拆后首份 10-Q
      "2024-10-27": mk(24700.0, "2024-11-20")}
notes = normalize_splits(sh, MIX)
approx("mixed: old pre-split scaled x10", sh["2022-10-30"]["val"], 24700.0)
approx("mixed: old pre-split scaled x10 (2)", sh["2023-10-29"]["val"], 24700.0)
approx("mixed: restated comparative untouched", sh["2024-04-28"]["val"], 24700.0)
approx("mixed: post-split untouched", sh["2024-07-28"]["val"], 24700.0)
check("mixed note says adjusted", "回补" in notes[0], True)

# 多次拆股复合：filed 早于两次拆股的期间依次吃到两个系数
TWO = {date(2021, 7, 20): 4.0, date(2024, 6, 10): 10.0}
sh = {"2020-10-25": mk(618.0, "2020-11-18"),    # 两次都在其后 => ×4×10
      "2022-10-30": mk(2470.0, "2022-11-18"),   # 只吃 2024 的 10:1
      "2024-07-28": mk(24700.0, "2024-08-28")}
normalize_splits(sh, TWO)
approx("two splits compound x40", sh["2020-10-25"]["val"], 24720.0)
approx("two splits later only x10", sh["2022-10-30"]["val"], 24700.0)

# 缺 filed 日：无法判定口径，保守不动并留痕
sh = {"2020-03-28": {"val": 100.0}, "2020-09-26": mk(400.0, "2021-01-28")}
notes = normalize_splits(sh, SPLIT)
approx("missing filed: untouched", sh["2020-03-28"]["val"], 100.0)
check("missing filed noted", "缺 filed 日未动" in notes[0], True)

# 拆股日 4 天缓冲窗（分配日 vs yfinance ex-date 缝隙）：缝内 filed 不可判，不动留痕
sh = {"2020-03-28": mk(400.0, "2020-08-28"),   # 缓冲窗内（sd-3d）——已重述可能性存在
      "2020-06-27": mk(100.0, "2020-07-30"),   # 窗外拆前 => 回补
      "2020-09-26": mk(400.0, "2021-01-28")}
notes = normalize_splits(sh, SPLIT)
approx("buffer window: ambiguous untouched", sh["2020-03-28"]["val"], 400.0)
approx("buffer window: clear pre-split scaled", sh["2020-06-27"]["val"], 400.0)
check("buffer window noted", "缓冲窗内" in notes[0], True)

# =====================================================================
# 6. build_ttm_eps
# =====================================================================
ends = ["2024-03-30", "2024-06-29", "2024-09-28", "2024-12-28", "2025-03-29"]
filed = ["2024-05-01", "2024-08-01", "2024-11-01", "2025-02-01", "2025-05-01"]
ni = {e: mk(v, f) for e, v, f in zip(ends, [10, 20, 30, 40, 50], filed)}
sh = {e: mk(100, f) for e, f in zip(ends, filed)}
pts = build_ttm_eps(ni, sh)
check("ttm 5 consecutive quarters -> 2 points", len(pts), 2)
if len(pts) == 2:
    approx("ttm pt1 ni", pts[0]["ttm_ni"], 100)
    approx("ttm pt1 eps", pts[0]["ttm_eps"], 1.0)
    check("ttm pt1 period_end", pts[0]["period_end"], "2024-12-28")
    check("ttm pt1 known_from = max first_filed", pts[0]["known_from"], "2025-02-01")
    approx("ttm pt2 ni", pts[1]["ttm_ni"], 140)
    check("ttm pt2 known_from", pts[1]["known_from"], "2025-05-01")

# a later share first_filed dominates known_from
sh_late = {e: mk(100, f) for e, f in zip(ends, filed)}
sh_late["2024-06-29"] = mk(100, "2025-02-15")   # restated share count known late
pts = build_ttm_eps(ni, sh_late)
check("ttm known_from = max over shares too", pts[0]["known_from"], "2025-02-15")

# gap breaks window: drop 2024-06-29 quarter
ni_gap = {k: v for k, v in ni.items() if k != "2024-06-29"}
sh_gap = {k: v for k, v in sh.items() if k != "2024-06-29"}
pts = build_ttm_eps(ni_gap, sh_gap)
check("ttm gap breaks window -> 0 points", len(pts), 0)

# missing shares for one quarter skips windows containing it
sh_miss = {k: v for k, v in sh.items() if k != "2024-03-30"}
pts = build_ttm_eps(ni, sh_miss)
check("ttm missing shares -> only window 2", [p["period_end"] for p in pts], ["2025-03-29"])

# zero avg shares skipped
sh_zero = {e: mk(0, f) for e, f in zip(ends, filed)}
pts = build_ttm_eps(ni, sh_zero)
check("ttm zero shares -> 0 points", len(pts), 0)

# =====================================================================
# 7. vintages record/load + trend.agg
# =====================================================================
def mkval(ticker="aapl", report_end="2026-06-27", blend=250.0, fwd="NTM 2026-07~2027-06",
          reds=0):
    warns = [["red", "x"]] * reds + [["yellow", "y"]]
    return {
        "ticker": ticker, "date": "2026-08-06",
        "adj_ni": 95000.0, "adj_eps": 6.3,
        "ttm": {"revenue": 400000.0, "op_income": 120000.0, "net_income": 100000.0},
        "meta": {"price": 230.0, "fwd_label": fwd,
                 "vintage": {"report_end": report_end, "filed": "2026-08-01"}},
        "scenarios": {"base": {"blend": blend, "upside": 0.08, "eps1": 8.0,
                               "fwd_pe": 28.0, "pe_target": 260.0, "dcf_ps": 240.0,
                               "sotp_ps": None,
                               "assumptions": {"pe": 30, "g": 0.1, "opm": 0.3, "wacc": 0.09},
                               "warnings": warns,
                               "diagnostics": {"pe_vs_history": {"pctile": 55.0}}}},
    }

tmp = Path(tempfile.mkdtemp(prefix="vint-", dir=str(Path(__file__).parent)))

# lowercase ticker round-trip
p = vintages.record(mkval(), gate_clean=True, root=tmp)
# 按路径分量比较，不用字符串后缀：Windows 上 str(Path) 是反斜杠，
# 写死 "vintages/AAPL/..." 会在非 POSIX 平台假失败（这是哨兵自己的 bug）
check("record path uppercased", p.parts[-3:], ("vintages", "AAPL", "2026-06-27.json"))
recs = vintages.load("aapl", root=tmp)
check("load lowercase ticker finds it", len(recs), 1)
check("load sample count", len(recs[0]["samples"]), 1)
s = recs[0]["samples"][0]
check("sample fwd_label", s["fwd_label"], "NTM 2026-07~2027-06")
approx("sample price", s["price"], 230.0)
check("sample reds/yellows", (s["scenarios"]["base"]["reds"], s["scenarios"]["base"]["yellows"]), (0, 1))
approx("sample pe_pctile", s["scenarios"]["base"]["pe_pctile"], 55.0)

# append second sample
vintages.record(mkval(blend=260.0), gate_clean=True, root=tmp)
recs = vintages.load("AAPL", root=tmp)
check("second record appends", len(recs[0]["samples"]), 2)

# atomicity: no tmp files left behind
leftovers = list((tmp / "vintages" / "AAPL").glob("*.tmp")) + \
            list((tmp / "vintages" / "AAPL").glob(".*tmp"))
check("no tmp leftovers after record", leftovers, [])

# corrupted JSON file: record overwrites fresh; load skips other corrupt files
f = tmp / "vintages" / "AAPL" / "2026-03-28.json"
f.write_text("{corrupt!!", encoding="utf-8")
p = vintages.record(mkval(report_end="2026-03-28"), gate_clean=False, root=tmp)
rec = json.loads(f.read_text(encoding="utf-8"))
check("record over corrupt file -> fresh 1 sample", len(rec["samples"]), 1)
check("gate_clean False persisted", rec["samples"][0]["gate_clean"], False)

f2 = tmp / "vintages" / "AAPL" / "2025-12-27.json"
f2.write_text("also not json", encoding="utf-8")
recs = vintages.load("AAPL", root=tmp)
check("load skips corrupt, keeps 2 valid", [r["report_end"] for r in recs],
      ["2026-03-28", "2026-06-27"])

# stale tmp file from a crashed run must not poison load
(tmp / "vintages" / "AAPL" / ".2026-09-26.json.tmp").write_text("{", encoding="utf-8")
recs = vintages.load("AAPL", root=tmp)
check("stale .tmp invisible to load", [r["report_end"] for r in recs],
      ["2026-03-28", "2026-06-27"])

# no report_end -> None, nothing written
v = mkval(); v["meta"]["vintage"] = {}
check("record without report_end -> None", vintages.record(v, True, root=tmp), None)
v = mkval(); v["ticker"] = ""
check("record without ticker -> None", vintages.record(v, True, root=tmp), None)

# valid-JSON-but-not-dict file: docstring promises corrupt files don't block runs
f3 = tmp / "vintages" / "AAPL" / "2025-09-27.json"
f3.write_text("[]", encoding="utf-8")
try:
    vintages.record(mkval(report_end="2025-09-27"), True, root=tmp)
    check("record over JSON-array file survives", True, True)
except Exception as e:
    check(f"record over JSON-array file survives (raised {type(e).__name__}: {e})", False, True)
try:
    vintages.load("AAPL", root=tmp)
    check("load over JSON-array file survives", True, True)
except Exception as e:
    check(f"load over JSON-array file survives (raised {type(e).__name__}: {e})", False, True)

# trend.agg
samples = [{"scenarios": {"base": {"blend": 10.0}}, "price": 100.0},
           {"scenarios": {"base": {"blend": 12.0}}, "price": 110.0},
           {"scenarios": {"base": {"blend": 14.0}}, "price": None}]
m, n, sd = trend.agg(samples, "base", "blend")
approx("agg median", m, 12.0)
check("agg n", n, 3)
approx("agg sd", sd, 2.0)
m, n, sd = trend.agg(samples, "", "price", top=True)
approx("agg top median (None filtered)", m, 105.0)
check("agg top n", n, 2)
approx("agg top sd", sd, 7.0710678118654755)
m, n, sd = trend.agg([{"scenarios": {"base": {"blend": 10.0}}}], "base", "blend")
check("agg n=1 sd None", sd, None)
m, n, sd = trend.agg([], "base", "blend")
check("agg empty", (m, n, sd), (None, 0, None))
m, n, sd = trend.agg([{"scenarios": {"base": {"blend": "12"}}}], "base", "blend")
check("agg string value filtered", (m, n), (None, 0))

# =====================================================================
# 0060 股数兜底：净利÷稀释EPS 反推（双类股维度化缺口）
# =====================================================================
sh = {"2025-06-30": mk(1000.0, "2025-08-01")}
ni = {"2025-03-31": mk(2500.0, "2025-05-01"), "2025-06-30": mk(2600.0, "2025-08-01"),
      "2025-09-30": mk(30.0, "2025-11-01")}
eps = {"2025-03-31": mk(2.5, "2025-05-05"), "2025-09-30": mk(0.03, "2025-11-05")}
n = backfill_shares_from_eps(sh, ni, eps)
check("backfill count (1 filled, 1 present, 1 tiny-eps skipped)", n, 1)
approx("backfill implied shares", sh["2025-03-31"]["val"], 1000.0)
check("backfill filed follows EPS entry", sh["2025-03-31"]["filed"], "2025-05-05")
approx("backfill existing untouched", sh["2025-06-30"]["val"], 1000.0)
check("backfill tiny eps skipped", "2025-09-30" in sh, False)

# =====================================================================
# 0061 leave-one-out 畸变判据：双季同向畸变不再靠中位数隐身
# =====================================================================
ends = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
filed = ["2025-11-01", "2026-02-01", "2026-05-01", "2026-08-01"]
def mkwin(vals):
    niw = {e: mk(v, f) for e, v, f in zip(ends, vals, filed)}
    shw = {e: mk(100.0, f) for e, f in zip(ends, filed)}
    return build_ttm_eps(niw, shw)
# GOOG 型：两季连发吹大（旧判据 dev/median4 = 20/25 = 0.8 < 1.25 漏网）
pts = mkwin([10.0, 10.0, 40.0, 45.0])
check("LOO: double-dirty window flagged", pts[0]["anomalous"], True)
# 单季畸变照旧抓
pts = mkwin([10.0, 10.0, 10.0, 40.0])
check("LOO: single-dirty flagged", pts[0]["anomalous"], True)
# 正常季节性不误伤（Q4 高 50%）
pts = mkwin([10.0, 11.0, 12.0, 15.0])
check("LOO: seasonal not flagged", pts[0]["anomalous"], False)
# META OBBBA 型：单季负向压缩也抓（双侧）
pts = mkwin([20.0, 22.0, 24.0, -10.0])
check("LOO: negative one-off flagged", pts[0]["anomalous"], True)
# anom_k=None（rps）关闭
pts = build_ttm_eps({e: mk(v, f) for e, v, f in zip(ends, [10.0, 10.0, 40.0, 45.0], filed)},
                    {e: mk(100.0, f) for e, f in zip(ends, filed)}, anom_k=None)
check("LOO: anom_k=None disables", pts[0]["anomalous"], False)

# =====================================================================
# vintages 落地合并（评审修复）：pending 幻影格子在真实 report_end 写入时被并掉
# =====================================================================
_pend = {"ticker": "TT", "date": "2026-08-10", "semantics_version": 3,
         "meta": {"price": 10, "fwd_label": "NTM x",
                  "vintage": {"report_end": "2026-03-31", "filed": "2026-08-09",
                              "vintage_end": "2026-06-30", "pending_10q": True}},
         "scenarios": {}, "ttm": {}}
_land = {"ticker": "TT", "date": "2026-08-20", "semantics_version": 3,
         "meta": {"price": 10, "fwd_label": "NTM x",
                  "vintage": {"report_end": "2026-06-28", "filed": "2026-08-19"}},
         "scenarios": {}, "ttm": {}}
with tempfile.TemporaryDirectory() as _td:
    _root = Path(_td)
    vintages.record(_pend, gate_clean=True, root=_root)
    check("pending archived to vintage_end slot",
          (_root / "vintages" / "TT" / "2026-06-30.json").exists(), True)
    vintages.record(_land, gate_clean=True, root=_root)
    check("phantom slot merged away",
          (_root / "vintages" / "TT" / "2026-06-30.json").exists(), False)
    _rec = json.loads((_root / "vintages" / "TT" / "2026-06-28.json").read_text(encoding="utf-8"))
    check("landed slot holds both samples", len(_rec["samples"]), 2)
    check("pending sample flagged", _rec["samples"][0].get("pending_10q"), True)

# =====================================================================
# =====================================================================
# 10. 口径披露哨兵（2026-08-17 "带子滞后一年"复盘）
# ---------------------------------------------------------------------
# 事故形态：ntm 口径的带子结构性缺最近约 12 个月（分母是"该日之后 12 个月**实际
# 实现**的 EPS"，那个未来还没发生）。这是设计使然、代码里也写了，但**四个消费面
# 一个都没把它说出来**，于是 days=1055/"近5年" 看起来像"截至今天"，判断层照旧锚
# 一条止于 10 个月前的分布。教训见 LESSONS.md。
#
# 这一组是**源码级**断言而非行为断言：它防的正是"重构时把披露顺手删掉"这一类
# 静默回归——披露没了不会有任何用例变红，只会让报告重新开始说谎。
# =====================================================================
_DISCLOSE = (
    (("app", "valuation_service.py"), "判断层 prompt 注入 band_meta"),
    (("valuation", "engine.py"), "engine stdout / trading_range JSON"),
    (("valuation", "build_report.py"), "Excel 摘要交易区间块"),
    (("static", "index.html"), "前端一行摘要"),
)
for _rel, _name in _DISCLOSE:
    _src = WT.joinpath(*_rel).read_text(encoding="utf-8")
    check(f"滞后披露(lag_days)仍在: {_name}", "lag_days" in _src, True)

# 数据层必须产出这三样，否则上面四个面无从披露
_pb_text = (WT / "valuation" / "pe_band.py").read_text(encoding="utf-8")
for _k in ("span", "drift", "trailing_nolag"):
    check(f"pe_band 仍输出 {_k}", f'"{_k}"' in _pb_text, True)
# trailing_nolag 必须在 `key in s` 过滤**之前**取全序列——过滤之后同样带滞后，
# 那正是既有 other_basis_median 的坑
check("trailing_nolag 取自过滤前的全序列",
      _pb_text.index("_unfiltered = series") < _pb_text.index("trailing_nolag = None"), True)

print()
print(f"{len(PASSES)} passed, {len(FAILS)} FAILED")
for name, a, e in FAILS:
    print(f"  FAILED: {name}: {a} vs {e}")
# 非零退出：此前无论挂多少条都 exit 0，CI/自动化完全看不见回归
sys.exit(1 if FAILS else 0)
