# -*- coding: utf-8 -*-
"""scripts/verify_foreign_6k.py 的纯函数：6-K 摊薄 EPS 行解析、按 20-F / 累计列 / 上一年锚定列序。"""
import importlib.util
from pathlib import Path

import pytest

_p = Path(__file__).resolve().parent.parent / "scripts" / "verify_foreign_6k.py"
_spec = importlib.util.spec_from_file_location("verify_foreign_6k", _p)
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)


def test_eps_row_parses_footnote_and_negatives():
    t = ("Net income 291.9 432.1 <td>Diluted net income per ordinary share</td> 3 "
         "<td>0.71</td><td>1.00</td> 1.39 1.89 Number of ordinary shares")
    assert v.eps_row(t) == [0.71, 1.00, 1.39, 1.89]
    assert v.eps_row("Diluted net (loss) income per ordinary share (0.12) 0.35") == [-0.12, 0.35]
    assert v.eps_row("no table here") is None


# ASML 2026-11 实测 6-K 行（去年同季在前）
R2025 = [[3.11, 6.00], [4.01, 5.90, 7.12, 11.90], [5.28, 5.48, 12.39, 17.38], [6.84, 7.34, 19.24, 24.71]]
R2026 = [[6.00, 7.15], [5.90, 7.58, 11.90, 14.73], [5.48, 12.93, 17.38, 27.64]]


def test_resolve_year_with_annual():
    assert v.resolve_year(R2025, 24.71) == [6.00, 5.90, 5.48, 7.34]
    assert v.resolve_year(R2025, 30.0) is None


def test_resolve_year_without_annual_needs_prior_year_anchor():
    # 没有 20-F：两种列序的累计都自洽（去年各季也加得出去年累计）-> 只靠累计定不下
    assert v.resolve_year(R2026, None) is None
    assert v.resolve_year(R2026, None, prev=[6.00, 5.90, 5.48, 7.34]) == [7.15, 7.58, 12.93]


def test_resolve_year_current_first_layout():
    # 2012 年前后是本季在前
    rels = [[0.90, 0.25], [1.00, 0.54, 1.89, 0.79], [0.84, 0.61, 2.73, 1.40], [0.68, 0.93, 3.42, 2.33]]
    assert v.resolve_year(rels, 3.42) == [0.90, 1.00, 0.84, 0.68]
