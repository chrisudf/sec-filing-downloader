# -*- coding: utf-8 -*-
"""Test engine.pe_band_check + _pctile_rank extracted verbatim (engine.py is a script)."""
import ast
import json
import os
import sys
from pathlib import Path

# 被测 worktree：SFD_WT 环境变量覆盖；默认取本文件所在 repo 根
WT = Path(os.environ.get("SFD_WT") or Path(__file__).resolve().parent.parent)
src = (WT / "valuation" / "engine.py").read_text(encoding="utf-8")
tree = ast.parse(src)
segs = [ast.get_source_segment(src, n) for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in ("_pctile_rank", "pe_band_check")]
assert len(segs) == 2
ns = {}
exec("\n\n".join(segs), ns)
_pctile_rank = ns["_pctile_rank"]
pe_band_check = ns["pe_band_check"]

fails = []
def check(name, actual, expected):
    ok = actual == expected
    print(("PASS " if ok else "FAIL ") + name, "" if ok else f"actual={actual!r} expected={expected!r}")
    if not ok:
        fails.append(name)

# band as produced by compute_band then round-tripped through facts.json (keys -> str)
band_py = {"pctiles": {p: float(20 + p / 10) for p in (1, 5, 10, 25, 50, 75, 90, 95, 99)},
           "min": 18.0, "max": 32.0, "years": 5, "days": 1100, "basis": "ntm"}
band = json.loads(json.dumps(band_py))  # keys become strings, as engine sees them

# _pctile_rank interpolation on the sparse table
check("_pctile_rank below min clamps to lowest key", _pctile_rank(band["pctiles"], 10.0), 1.0)
check("_pctile_rank above max clamps to highest key", _pctile_rank(band["pctiles"], 40.0), 99.0)
# pcts: 25 -> P50 (20+50/10), 27.5 -> P75; x=26.25 halfway -> P62.5
check("_pctile_rank interior interpolation", _pctile_rank(band["pctiles"], 26.25), 62.5)
check("_pctile_rank exact knot", _pctile_rank(band["pctiles"], 29.0), 90.0)

# in-range base pe (P10=21, P90=29): pe=25 is the median -> no warnings
dd = {}
w = pe_band_check("base", 25.0, band, dd)
check("in-range base -> no warnings", w, [])
check("ddiag pctile", dd["pe_vs_history"]["pctile"], 50.0)
check("ddiag basis passthrough", dd["pe_vs_history"]["basis"], "ntm")

# pe above historical max -> yellow (any scenario)
dd = {}
w = pe_band_check("bull", 35.0, band, dd)
check("above max -> 1 yellow", (len(w), w[0][0] if w else None), (1, "yellow"))

# base pe within min/max but outside [P10,P90] -> yellow
dd = {}
w = pe_band_check("base", 20.0, band, dd)   # 20 < P10=21, > min=18
check("base outside P10..P90 -> yellow", (len(w), w[0][0] if w else None), (1, "yellow"))

# same pe on bear -> no warning (only base gets the P10/P90 gate)
dd = {}
w = pe_band_check("bear", 20.0, band, dd)
check("bear low pe inside min/max -> no warning", w, [])

# missing band -> no-op
check("band None -> []", pe_band_check("base", 25.0, None, {}), [])
check("band without pctiles -> []", pe_band_check("base", 25.0, {"min": 1}, {}), [])

# what engine would do if handed compute_band output IN-PROCESS (int keys):
try:
    pe_band_check("base", 25.0, band_py, {})
    print("INFO int-key band works in-process")
except Exception as e:
    print(f"INFO int-key band (no JSON round-trip) raises {type(e).__name__}: {e} "
          "(production always round-trips via facts.json, so this is latent only)")

print()
print("FAILS:", fails if fails else "none")
# 非零退出：此前挂了也 exit 0，自动化无法检出回归
sys.exit(1 if fails else 0)
