# -*- coding: utf-8 -*-
"""424B 封面解析（app/offering_cover.py）。

样本是 offering-price-study（2026-10-17）缓存的真实封面文本前 12,000 字符，
覆盖三种价格写法（内联 / 表头在上的表格 / 大宗承销商买入价）、三种前收盘写法
（last reported sale / closing price / last sale price），六类必须拒收的文件
（初步版、发债、ATM、DRIP、权证拍卖、转售注册），以及 ATM 额度的三种写法
（美元上限 / 股数上限 / 只修订既有额度）。
"""
from pathlib import Path

import pytest

from app.offering_cover import html_to_text, parse_cover

FIX = Path(__file__).parent / "fixtures" / "offering_covers"


def cover(name: str) -> dict:
    return parse_cover((FIX / f"{name}.txt").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name, want", [
    # INTC 2026-08 $20B 一级增发：内联「Public offering price $ 95.00」
    ("intc_2026_priced", {"price": 95.0, "price_how": "inline", "pricing_date": "2026-08-10",
                          "last_date": "2026-08-10", "last_price": 97.52, "shares": 210526315,
                          "who": "primary", "var_price": False, "concurrent_buyback": False}),
    # APP 2024-03 老股东减持 + 公司同步回购：表头在上「Price to Public (1) … Per share $ 56.00」
    ("app_2024_secondary_table", {"price": 56.0, "price_how": "table", "last_price": 59.72,
                                  "who": "secondary", "concurrent_buyback": True}),
    # ASTS 2023-06 大宗：没有公开发行价，只有承销商买入价，较前收 −28.6%
    ("asts_2023_block", {"price": 4.55, "price_how": "block", "last_price": 6.37,
                         "last_date": "2023-06-27", "var_price": True, "who": "primary"}),
    # CEG 2026-06：「the last sale price … was $287.75」；盘前定价，招股书日期晚于前收盘日
    ("ceg_2026_last_sale", {"price": 281.0, "pricing_date": "2026-06-01", "last_date": "2026-05-29",
                            "last_price": 287.75, "who": "secondary", "concurrent_buyback": True}),
    # SOFI 2025-07：股票增发走 424B3
    ("sofi_2025_424b3", {"price": 20.85, "last_date": "2025-07-28", "last_price": 21.02,
                         "who": "primary"}),
])
def test_priced_equity(name, want):
    r = cover(name)
    assert r["kind"] == "priced_equity"
    for k, v in want.items():
        assert r[k] == v, (k, r[k], v)


@pytest.mark.parametrize("name, reason", [
    ("intc_2026_prelim", "preliminary"),        # Subject to Completion，没有价格
    ("intc_2024_debt", "debt"),                 # 固定利率票据（标题含「% Notes due」）
    ("aep_2020_atm", "atm"),                    # 销售代理 ATM，没有单一价格
    ("o_2024_drip", "plan/resale/other"),       # 股息再投资计划
    ("bac_2010_warrants", "warrants_offering"),  # TARP 权证拍卖 $8.35 不是股票价格
    ("well_2026_resale", "no_price"),           # 老股东转售注册，按市价随时卖
])
def test_rejects(name, reason):
    r = cover(name)
    assert r["kind"] == "reject"
    assert r["reason"] == reason


def test_html_to_text_strips_markup():
    raw = ("<html><style>.x{color:red}</style><body><p>Public offering price</p>"
           "<td>$&nbsp;95.00</td><script>var a=1</script></body></html>")
    assert html_to_text(raw) == "Public offering price $ 95.00"


def test_fee_table_price_is_not_offering_price():
    # 注册费表的「Proposed Maximum Offering Price Per Share」不是发行价（ATM 补充文件常见）
    text = ("CALCULATION OF REGISTRATION FEE Title of Each Class Proposed Maximum Offering Price "
            "Per Share $70.6325 Filed Pursuant to Rule 424(b)(5) (To Prospectus dated May 1, 2021) "
            "$1,000,000,000 Common Stock We have entered into an equity distribution agreement "
            "with sales agents relating to shares of our common stock.")
    r = parse_cover(text)
    assert r["kind"] == "reject" and r["reason"] == "atm"


@pytest.mark.parametrize("name, usd, shares, amend", [
    ("rklb_2026_atm", 1_944_369_826.0, None, False),      # 「Up to $1,944,369,826 Common Stock」
    ("aep_2020_atm", 1_000_000_000.0, None, False),       # 「aggregate offering price of up to $1,000,000,000」
    ("nnn_2018_atm_shares", None, 12_000_000, False),     # 「12,000,000 Shares … Common Stock」
    ("rklb_2025_atm_amend", 500_000_000.0, None, True),   # Supplement No. 1：修订 2025-03 那份，不算新额度
])
def test_atm_program_size(name, usd, shares, amend):
    r = cover(name)
    assert r["kind"] == "reject" and r["reason"] == "atm"
    assert (r["atm_usd"], r["atm_shares"], r["atm_amendment"]) == (usd, shares, amend)


def test_atm_cover_last_price_beyond_cover_window():
    # 前收盘那句在销售代理长条款之后（超出 COVER_CHARS），仍要找到
    r = cover("rklb_2026_atm")
    assert (r["atm_last_date"], r["atm_last_price"]) == ("2026-08-10", 80.04)


def test_atm_rollover_and_prior_sold():
    # RKLB 2026-08-13：只承接 2026-05-20 那份 $30 亿协议未卖完的 $19.44 亿，不是新额度；
    # 同时披露旧协议已卖 $1,055,630,173
    r = cover("rklb_2026_atm")
    assert r["atm_rollover"] is True and r["atm_amendment"] is False
    assert (r["atm_prior_sold_usd"], r["atm_prior_date"]) == (1_055_630_173.0, "2026-05-20")


def test_atm_agent_addition_is_amendment():
    # GOOG 2026-08-06：只为增加销售代理而发的补充文件，额度仍是 6 月那份 $400 亿
    r = cover("goog_2026_atm_agents")
    assert r["reason"] == "atm" and r["atm_usd"] == 40_000_000_000.0 and r["atm_amendment"] is True
