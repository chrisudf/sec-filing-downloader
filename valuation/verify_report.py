# -*- coding: utf-8 -*-
"""formulas 包（纯 Python）独立求值工作簿，与引擎 JSON 交叉核对 16 个关键单元格。
本机无 LibreOffice 时以此替代 recalc；Excel 打开时会自行重算。

用法: python verify_report.py VALUATION.json REPORT.xlsx
"""
import json
import os
import sys
import formulas

d = json.load(open(sys.argv[1], encoding="utf-8"))
T = d["ticker"]
XLSX = sys.argv[2]

xl = formulas.ExcelModel().loads(XLSX).finish()
sol = xl.calculate()
base = os.path.basename(XLSX)


def get(sheet, cell):
    v = sol[f"'[{base}]{sheet}'!{cell}"].value
    try:
        return float(v[0][0])
    except Exception:
        return v


S = d["scenarios"]
checks = [
    ("TTM调整后EPS", ("情景假设", "B28"), d["adj_eps"], 0.02),
    ("bear EPS1", ("情景假设", "B34"), S["bear"]["eps1"], 0.02),
    ("base EPS1", ("情景假设", "C34"), S["base"]["eps1"], 0.02),
    ("bull EPS1", ("情景假设", "D34"), S["bull"]["eps1"], 0.02),
    ("bear PE目标", ("情景假设", "B35"), S["bear"]["pe_target"], 0.5),
    ("base PE目标", ("情景假设", "C35"), S["base"]["pe_target"], 0.5),
    ("bull PE目标", ("情景假设", "D35"), S["bull"]["pe_target"], 0.5),
    ("bear DCF", ("DCF", "B16"), S["bear"]["dcf_ps"], 1.0),
    ("base DCF", ("DCF", "B32"), S["base"]["dcf_ps"], 1.0),
    ("bull DCF", ("DCF", "B48"), S["bull"]["dcf_ps"], 1.5),
    ("bear SOTP", ("SOTP", "C12"), S["bear"]["sotp_ps"], 0.5),
    ("base SOTP", ("SOTP", "D12"), S["base"]["sotp_ps"], 0.5),
    ("bull SOTP", ("SOTP", "E12"), S["bull"]["sotp_ps"], 0.5),
    ("bull 综合", ("摘要", "E22"), S["bull"]["blend"], 1.0),
    ("base 综合", ("摘要", "E23"), S["base"]["blend"], 1.0),
    ("bear 综合", ("摘要", "E24"), S["bear"]["blend"], 1.0),
]
fails = 0
for name, (sheet, cell), expected, tol in checks:
    got = get(sheet, cell)
    ok = isinstance(got, float) and abs(got - expected) <= tol
    fails += 0 if ok else 1
    print(f"{'OK ' if ok else 'FAIL'} {T} {name:12s} 表内={got}  引擎={expected}")
print(f"\n{T} 结果:", "全部一致 ✓" if fails == 0 else f"{fails} 项不一致 ✗")
sys.exit(1 if fails else 0)
