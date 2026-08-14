# -*- coding: utf-8 -*-
"""formulas 包（纯 Python）独立求值工作簿，与引擎 JSON 交叉核对 16~17 个关键单元格
（有交易区间块时多核对一项 P50 价格）。
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
        # 标量结果（含 numpy 整型/浮点）不可下标，float() 归一化后再比较；
        # 无法转成 float 的值（XlError、非数字字符串）保持原样，由 isinstance 判 FAIL
        try:
            return float(v)
        except Exception:
            return v


S = d["scenarios"]
if d.get("mode") == "financials":
    # 金融股布局：PE 法 + P/TBV 法（行号见 build_report 金融股分支）
    checks = [
        ("TTM调整后EPS", ("情景假设", "B20"), d["adj_eps"], 0.02),
        ("每股TBV", ("情景假设", "B21"), d["meta"]["tbv_ps"], 0.02),
    ]
    for col, sc in zip("BCD", ("bear", "base", "bull")):
        checks += [
            (f"{sc} EPS1", ("情景假设", f"{col}27"), S[sc]["eps1"], 0.02),
            (f"{sc} PE目标", ("情景假设", f"{col}28"), S[sc]["pe_target"], 0.5),
            (f"{sc} PTBV目标", ("情景假设", f"{col}31"), S[sc]["ptbv_ps"], 0.5),
            (f"{sc} justified参考", ("情景假设", f"{col}32"), S[sc]["justified_ps"], 1.0),
        ]
    checks += [
        ("bull 综合", ("摘要", "D21"), S["bull"]["blend"], 0.5),
        ("base 综合", ("摘要", "D22"), S["base"]["blend"], 0.5),
        ("bear 综合", ("摘要", "D23"), S["bear"]["blend"], 0.5),
    ]
    fails = 0
    for name, (sheet, cell), expected, tol in checks:
        got = get(sheet, cell)
        ok = isinstance(got, (int, float)) and abs(float(got) - expected) <= tol
        fails += 0 if ok else 1
        print(f"{'OK ' if ok else 'FAIL'} {T} {name:14s} 表内={got}  引擎={expected}")
    print(f"\n{T} 结果:", "全部一致 ✓" if fails == 0 else f"{fails} 项不一致 ✗")
    sys.exit(1 if fails else 0)

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
# 交易区间块存在时核对 P50 价格（摘要第 30 行 = P50 PE × base EPS 活公式 vs 引擎值）。
# 列号按与 build_report 完全相同的规则算出来，而不是写死 D：那边的 _qs 是
# 「引擎给了哪几档分位就出哪几列」，哪天分位档位变了，写死的列会静默核对到隔壁
# 分位上——校验层比对错单元格比不比对更坏（会给出"全部一致 ✓"的假保证）。
_tr = d.get("trading_range") or {}
if (_tr.get("px") or {}).get("50") is not None:
    from openpyxl.utils import get_column_letter
    _qs = [q for q in ("10", "25", "50", "75", "90") if q in _tr["pe"]]
    _c = get_column_letter(2 + _qs.index("50"))
    checks.append(("交易区间中位", ("摘要", f"{_c}30"), _tr["px"]["50"], 0.5))
fails = 0
for name, (sheet, cell), expected, tol in checks:
    got = get(sheet, cell)
    ok = isinstance(got, (int, float)) and abs(float(got) - expected) <= tol
    fails += 0 if ok else 1
    print(f"{'OK ' if ok else 'FAIL'} {T} {name:12s} 表内={got}  引擎={expected}")
print(f"\n{T} 结果:", "全部一致 ✓" if fails == 0 else f"{fails} 项不一致 ✗")
sys.exit(1 if fails else 0)
