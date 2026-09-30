"""年报封面 ADS 比例解析（app/adr_cover.py）。

样本是 2026-09-30 实拉的 51 份 10-K/20-F 封面 Section 12(b) 一栏的原文片段——
写法各家不同（five-ninths / every three representing two / 0.2 / one -fourth /
one hundred / forty-eight / each represented by / American Depositary Receipt），
逐条钉住；再钉住不许认的句式（比例变更句、没有 ADS 名词、没有比例的标题）。
"""
from fractions import Fraction as F

import pytest

from app.adr_cover import COVER_WINDOW, cover_text, parse_ads_ratio

HEAD = ("Securities registered or to be registered pursuant to Section 12(b) of the Act: "
        "Title of each class Trading Symbol(s) Name of each exchange on which registered ")


@pytest.mark.parametrize("ticker, title, want", [
    ("SKM", "American Depositary Shares , each representing five-ninths of one share of "
            "Common Stock SKM New York Stock Exchange", F(5, 9)),
    ("GOTU", "American Depositary Shares, every three representing two Class A ordinary "
             "shares, par value US$0.0001 per share GOTU New York Stock Exchange", F(2, 3)),
    ("TAL", "American Depositary Shares, each three representing one Class A common share * "
            "NYSE: TAL * Effective on August 16, 2017, the ratio of ADSs to Class A common "
            "shares was changed from one ADS representing two Class A common shares to three "
            "ADSs representing one Class A common share.", F(1, 3)),
    ("HDB", "American Depositary Shares, each representing three Equity Shares, Par value "
            "Rs. 1.0 per share HDB The New York Stock Exchange", F(3)),
    ("KT", "American Depositary Shares , each representing one-half of one share of "
           "ordinary share KT New York Stock Exchange", F(1, 2)),
    ("KEP", "American Depositary Shares, each representing one-half of share of common "
            "stock KEP New York Stock Exchange", F(1, 2)),
    ("PKX", "American Depositary Shares, each representing one -fourth of one share of "
            "common stock PKX New York Stock Exchange", F(1, 4)),
    ("VIPS", "American depositary shares , each representing 0.2 Class A ordinary shares, "
             "par value US$0.0001 per share VIPS New York Stock Exchange", F(1, 5)),
    ("XTLB", "American Depositary Shares, each representing one hundred Ordinary Shares, "
             "par value NIS 0.1 XTLB The Nasdaq Capital Market", F(100)),
    ("FENG", "American depositary shares, each representing forty-eight Class A ordinary "
             "shares FENG New York Stock Exchange", F(48)),
    ("CMCM", "American depositary shares, each representing fifty Class A ordinary shares "
             "Class A ordinary shares, par value US$0.000025 per share* CMCM", F(50)),
    ("BIDU", "American depositary shares (each American depositary share representing eight "
             "Class A ordinary shares, par value US$0.000000625 per share) BIDU", F(8)),
    ("IX", "(1) American depository shares (the “ADSs”), each of which represents one share "
           "IX New York Stock Exchange (2) Common stock without par value", F(1)),
    ("JD", "American depositary shares (one American depositary share representing two "
           "Class A ordinary shares, par value US$0.00002 per share) JD The Nasdaq", F(2)),
    ("BHP", "American Depositary Shares * BHP New York Stock Exchange Ordinary Shares ** BHP "
            "* Evidenced by American Depositary Receipts. Each American Depositary Receipt "
            "represents two ordinary shares of BHP Group Limited.", F(2)),
    ("INFY", "American Depositary Shares each represented by one Equity Share, par value "
             "₹5/- per share INFY New York Stock Exchange (NYSE)", F(1)),
    ("CANF", "American Depositary Shares, each representing 2 Ordinary Shares, no par "
             "value * CANF NYSE American", F(2)),
    ("BCS", "American Depositary Shares, each representing four 25p ordinary shares BCS "
            "New York Stock Exchange", F(4)),
    ("TM", "American Depositary Shares * Common Stock ** TM The New York Stock Exchange * "
           "Each American Depositary Share representing ten shares of the registrant’s "
           "Common Stock.", F(10)),
    ("MFG", "American depositary shares, each of which represents two shares of common "
            "stock Common Stock, without par value * MFG The New York Stock Exchange", F(2)),
    # TAK 正文的句式（封面标题没写比例）：a one-half interest in
    ("TAK", "listed in the form of American Depositary Shares (ADSs), with each ADS "
            "representing a one-half interest in an ordinary share", F(1, 2)),
])
def test_real_cover_titles(ticker, title, want):
    got = parse_ads_ratio(HEAD + title, ticker)
    assert got is not None and got[0] == want


def test_returns_source_snippet():
    q, snip = parse_ads_ratio(HEAD + "American Depositary Shares , each representing "
                              "five-ninths of one share of Common Stock SKM", "SKM")
    assert snip.startswith("each representing five-ninths of one share")


@pytest.mark.parametrize("text", [
    # 比例变更句：旧比例、新比例都不许当封面比例（CANF / IX 正文）
    "we effected a change in the ratio of our ADSs to ordinary shares from one (1) ADS "
    "representing three hundred (300) ordinary shares to a new ratio of one (1) ADS "
    "representing two (2) ordinary shares.",
    "we implemented a change in the ratio of our ADSs to underlying Shares from one ADS "
    "representing five underlying Shares to a ratio of one ADS representing one underlying Share.",
    "the ratio of ADSs to Class A common shares was changed from one ADS representing two "
    "Class A common shares to three ADSs representing one Class A common share.",
    # 标题里没有比例（TAK）
    HEAD + "American Depositary Shares Representing Common Stock Common Stock, no par value * TAK",
    # 数量后面不是股份
    HEAD + "American Depositary Shares, each representing 5 votes at general meetings",
    # 附近没有 ADS 名词
    HEAD + "Units, each representing one share of Class A common stock and one-half of one warrant",
    # 本土发行人
    HEAD + "Common Stock, par value $0.001 per share TSLA The Nasdaq Global Select Market",
    "",
])
def test_rejects_non_ratio_text(text):
    assert parse_ads_ratio(text, "X") is None


def test_ticker_picks_its_own_ads_line():
    """同一封面两个 ADS 品种：代码只在本条与下一条之间找，PBR 不许借用 PBR.A 那行。"""
    text = HEAD + ("American Depositary Shares, each representing 2 common shares PBR New York "
                   "Stock Exchange American Depositary Shares, each representing 3 preferred "
                   "shares PBR.A New York Stock Exchange")
    assert parse_ads_ratio(text, "PBR")[0] == 2
    assert parse_ads_ratio(text, "PBR.A")[0] == 3
    assert parse_ads_ratio(text, "ZZZ")[0] == 2          # 都没对上：取第一条
    # 反过来排：PBR 不许把 PBR.A 当成自己的代码
    rev = HEAD + ("American Depositary Shares, each representing 3 preferred shares PBR.A New "
                  "York Stock Exchange American Depositary Shares, each representing 2 common "
                  "shares PBR New York Stock Exchange")
    assert parse_ads_ratio(rev, "PBR")[0] == 2


def _html(title_html: str, pad: int = 0, header: str = "") -> str:
    return ("<html><body><ix:header>" + header + "</ix:header>"
            "<p>FORM 20-F</p><p>Securities registered or to be registered pursuant to "
            "Section&#160;12(b) of the Act:</p><table><tr><td>" + title_html +
            "</td></tr></table>" + "<p>" + "x " * pad + "</p></body></html>")


def test_cover_text_strips_tags_and_entities():
    raw = _html('<ix:nonNumeric name="dei:Security12bTitle">American Depositary Shares, each '
                'representing <span>five-ninths</span>&#160;of one share</ix:nonNumeric>'
                '<td>SKM</td>')
    t = cover_text(raw)
    assert t.startswith("Section 12(b)")
    assert parse_ads_ratio(t, "SKM")[0] == F(5, 9)


def test_cover_text_no_space_section_heading():
    """MFG 的封面写成 Section12(b)（没有空格）。"""
    raw = _html("American depositary shares, each of which represents two shares").replace(
        "Section&#160;12(b)", "Section12(b)")
    assert parse_ads_ratio(cover_text(raw), "MFG")[0] == 2


def test_cover_text_reaches_footnote_after_12g():
    """HMC：12(b) 一栏只写「American Depositary Shares **」，比例在封面末尾的脚注，
    离 12(b) 约 5.2k 字——窗口要够得着。"""
    raw = _html("Common Stock * American Depositary Shares ** HMC</td></tr></table>"
                "<p>Section 12(g) of the Act. None" + " y" * 2500 +
                "** American Depositary Receipts evidence American Depositary Shares, each "
                "American Depositary Share representing three shares of Common Stock.</p>")
    t = cover_text(raw)
    assert 5000 < t.index("each American Depositary Share") < COVER_WINDOW
    assert parse_ads_ratio(t, "HMC")[0] == 3


def test_cover_text_window_excludes_body_ratio_changes():
    """窗口之外（正文 Item 9/10）的比例句不进来。填充写死 12k 字：窗口（8k）是设计决定，
    改大到够得着正文时这条测试就该红。"""
    raw = _html("Common Stock * TAK", pad=6000) + (
        "<p>Our ADSs, each representing 5 shares.</p>")
    assert parse_ads_ratio(cover_text(raw), "TAK") is None


def test_cover_text_skips_ix_header():
    """iXBRL 的 ix:header 可达几 MB（IX 12(b) 在第 2.45M 字节）：定位不能被它干扰。"""
    raw = _html("American Depositary Shares, each representing ten ordinary shares VOD",
                header="<xbrli:context>" * 40000)       # 600KB，超过原始字节窗口
    assert parse_ads_ratio(cover_text(raw), "VOD")[0] == 10


def test_cover_text_without_section_12b_is_empty():
    assert cover_text("<html><body>no cover here</body></html>") == ""
