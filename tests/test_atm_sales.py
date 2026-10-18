# -*- coding: utf-8 -*-
"""10-Q/10-K 附注 ATM 实际成交解析（app/atm_sales.py）。

样本是 2026-10-17 实拉的五家最近一份 10-Q 附注片段（含 ATM 句子前后各约 1,500 字），
钉住五种句式：毛收入（金额单位随报表 in thousands）、加权均价、million/billion 写法、
领口远期区间、8-K 新闻稿「先金额后股数」；以及上年同期对比数要剔除、季度与年初至今
重叠时只留季度。
"""
from pathlib import Path

import pytest

from app.atm_sales import parse_atm_sales

FIX = Path(__file__).parent / "fixtures" / "atm_sales"


def parse(name, report_date, rng):
    return parse_atm_sales((FIX / f"{name}.txt").read_text(encoding="utf-8"), report_date, rng)


def test_rklb_gross_proceeds_in_thousands_and_collar():
    es = parse("rklb_2026q2", "2026-06-30", (17.0, 140.0))
    got = [(e["kind"], e["shares"], e["avg_price"], e["avg_high"], e["label"]) for e in es]
    # 「$ 1,055,630 in gross proceeds」是千美元：7,783,458 股 → 均价 $135.62
    assert got == [
        ("sale", 6_726_862, 70.47, None, "terminated March 2026 Equity Distribution Agreement"),
        ("sale", 7_783_458, 135.62, None, "May 2026 Equity Distribution Agreement"),
        ("collar", 7_451_200, 63.61, 86.16, "March 2026 Equity Distribution Agreement"),
    ]
    # 上年同期（six months ended June 30, 2025，15,142,133 股）不算本期
    assert all(e["shares"] != 15_142_133 for e in es)


def test_well_full_dollar_amount():
    es = parse("well_2026q2", "2026-06-30", (100.0, 260.0))
    assert [(e["shares"], e["avg_price"], e["proceeds_kind"]) for e in es] == [(21_571_496, 207.74, "gross")]


def test_smmt_weighted_average_price():
    es = parse("smmt_2026q2", "2026-06-30", (8.0, 40.0))
    assert [(e["shares"], e["avg_price"]) for e in es] == [(23_578_867, 16.18)]


def test_orcl_million_shares_billion_net_proceeds():
    es = parse("orcl_2027q1", "2026-08-31", (100.0, 350.0))
    assert [(e["shares"], e["avg_price"], e["proceeds_kind"], e["span_months"]) for e in es] == \
        [(141_000_000, 141.13, "net", 3)]


def test_aur_quarter_preferred_over_year_to_date():
    es = parse("aur_2026q2", "2026-06-30", (3.0, 15.0))
    assert [(e["shares"], e["avg_price"], e["quarterly"]) for e in es] == [(30_000_000, 7.5, True)]


def test_unit_rejected_when_no_price_scale_fits():
    # 金额无论按美元还是千美元，折出的均价都远离市价区间 → 宁缺勿错
    text = ("(In thousands) Under the ATM Program the Company sold 1,000,000 shares of common stock, "
            "generating $ 5 in gross proceeds.")
    assert parse_atm_sales(text, "2026-06-30", (40.0, 60.0)) == []


def test_press_release_proceeds_first():
    # RKLB 2026-09-15 8-K：08-13 续展的 $19.44 亿在季报出来前卖完，只在新闻稿里
    es = parse("rklb_2026_8k_ex99", "2026-09-15", (60.0, 85.0))
    assert [(e["shares"], e["avg_price"], e["proceeds_kind"]) for e in es] == [(29_300_000, 66.35, "gross")]
