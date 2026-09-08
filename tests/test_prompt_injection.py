# -*- coding: utf-8 -*-
"""数据前置注入（v4，2026-09-06）：_compact_facts 的 FCF 锚表 + OI&E 组件/残差行。

回归对象是 9 次实测运行共同的结构性浪费：prompt 许诺「FACTS 里有」的序列
服务器一个数也不注入，判断层徒手拼数、引擎事后打旗。
"""
import re
from pathlib import Path

from app.valuation_service import _compact_facts, _fwd_meta, _postperiod_filing_index

ROOT = Path(__file__).resolve().parent.parent


def _facts(**over):
    """最小 standard facts：10 个营收财年（2018/2019 缺 capex）+ 8 个季度。

    金额都是手算友好的整数（$M×1e6）：常规年 营收1,000/CFO300/capex100 → FCF率 20%；
    2023 CFO450 → 35%（峰值）。季度 税前 = 营业利润 + 12M → 残差恒 +12。
    """
    years = [f"20{y}-12-31" for y in range(17, 27)]
    quarters = ["2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30",
                "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    d = {
        "mode": "standard",
        "revenue_annual": {k: 1000e6 for k in years},
        "cfo_annual": {k: (450e6 if k.startswith("2023") else 300e6) for k in years},
        "capex_annual": {k: 100e6 for k in years if k[:4] not in ("2018", "2019")},
        "op_income_annual": {k: 200e6 for k in years},
        "net_income_annual": {k: 150e6 for k in years},
        "eps_diluted_annual": {k: 1.5 for k in years},
        "revenue_quarterly": {k: 250e6 for k in quarters},
        "op_income_quarterly": {k: 50e6 for k in quarters},
        "net_income_quarterly": {k: 40e6 for k in quarters},
        "pretax_income_quarterly": {k: 62e6 for k in quarters},
        "interest_income_quarterly": {k: 10e6 for k in quarters},
        # other_nonop：序列存在但最新一季缺值——单期缺口要画 "—"，不是整列消失
        "other_nonop_quarterly": {k: 3e6 for k in quarters[:-1]},
        "other_nonop_annual": {"2025-12-31": 12e6},
        "ttm": {"revenue": {"value": 1000e6}},
    }
    d.update(over)
    return d


# ---- (a) FCF 锚表 ----

def test_fcf_table_rows_and_margins():
    txt = _compact_facts(_facts())
    assert "年度 FCF 利润率表" in txt
    # 常规年 (300-100)/1000 = 20%，峰值年 (450-100)/1000 = 35%
    assert "2022-12-31: 营收 1,000 / CFO 300 / capex 100 / FCF率 20.0%" in txt
    assert "2023-12-31: 营收 1,000 / CFO 450 / capex 100 / FCF率 35.0%" in txt


def test_fcf_table_honest_gap_note():
    """2018/2019 缺 capex：表只覆盖 8 个财年，缺口逐年点名——判断层不许把
    8 年覆盖当「近十年」用（NVDA 实测 capex_annual 缺 FY13-21）。"""
    txt = _compact_facts(_facts())
    assert "覆盖 8 个财年（2017-12-31~2026-12-31）" in txt
    assert "2018缺capex" in txt and "2019缺capex" in txt
    assert "「近十年」按实际覆盖理解" in txt


def test_fcf_table_peak_and_median():
    """峰值/中位与 engine.terminal_margin_warnings 同口径（中位 = sorted[n//2]）。"""
    txt = _compact_facts(_facts())
    assert "峰值 35.0%（2023）" in txt
    assert "中位 20.0%" in txt


def test_fcf_table_unavailable_is_loud():
    """全缺 cfo：不给表可以，静默不行——要显式说锚不可用。"""
    txt = _compact_facts(_facts(cfo_annual={}))
    assert "近十年 FCF 锚不可用" in txt
    assert "rationale.dcf_margin 写明替代依据" in txt


# ---- (b) OI&E 组件 + 残差行 ----

def test_oie_residual_row():
    """逐季 税前−营业利润 = 62−50 = +12，8 个季度全出。"""
    txt = _compact_facts(_facts())
    row = next(l for l in txt.splitlines() if "逐季残差 税前−营业利润" in l)
    assert row.count("+12") == 8
    assert "2026-06-30 +12" in row


def test_oie_residual_unavailable_is_loud():
    d = _facts()
    d.pop("pretax_income_quarterly")
    txt = _compact_facts(d)
    assert "本票无 pretax_income XBRL 季度序列——残差行不可用" in txt


def test_oie_component_rows_only_existing_series():
    txt = _compact_facts(_facts())
    # interest_income 存在：8 个季度值 +10
    assert "利息收入[interest_income] 季8: +10 +10 +10 +10 +10 +10 +10 +10" in txt
    # other_nonop 存在但最新一季无值：单期缺口画 —，年度列也对齐
    assert "其他非经营[other_nonop] 季8: +3 +3 +3 +3 +3 +3 +3 —" in txt
    assert "2025:+12" in txt


def test_oie_missing_series_named_explicitly():
    """不存在的序列显式点名——prompt 不许再对判断层许诺没有的数据。"""
    txt = _compact_facts(_facts())
    assert "本票无以下 XBRL 序列" in txt
    for lab in ("利息支出(非经营)", "股权投资重估", "汇兑损益"):
        assert lab in txt.split("本票无以下 XBRL 序列")[1].splitlines()[0]


# ---- (d) semantics_version 一致性（grep 级：多处字面量必须一起动）----

def test_semantics_version_literals_consistent():
    vs = (ROOT / "app" / "valuation_service.py").read_text(encoding="utf-8")
    # cfg 构造点与连续性失效触发器必须同为 standard v4 / financials v3
    # （fin v3 = 跨情景排序 + 亏损协议，2026-09-06 补课）
    assert 'semantics_version=4 if mode == "standard" else 3' in vs
    assert '(4 if mode == "standard" else 3)' in vs
    eng = (ROOT / "valuation" / "engine.py").read_text(encoding="utf-8")
    # 只认代码赋值（行首缩进 + 赋值），不扫注释——历史注释里合法地留着旧版本号
    lits = set(re.findall(r"^\s*semantics_version=(\d+),", eng, re.M))
    assert "4" in lits, "standard 引擎输出未升到 v4"
    assert "3" in lits, "financials 引擎输出未升到 fin v3"
    assert "2" not in lits, "engine 里残留 semantics_version=2 赋值"
    br = (ROOT / "valuation" / "build_report.py").read_text(encoding="utf-8")
    assert 'd.get("semantics_version", 1) >= 4' in br, "报告的语义版本说明缺 v4 分支"


# ---- 期后 filing 索引（0002）：过滤/排版纯函数 ----

def _row(form, fdate, rdate="", items=""):
    return {"form": form, "filingDate": fdate, "reportDate": rdate,
            "items": items, "accessionNumber": fdate + form,
            "primaryDocument": "x.htm", "size": 1}


def test_filing_index_post_period_and_form_filter():
    rows = [
        _row("10-Q", "2026-07-30", rdate="2026-06-30"),   # 期后定期报告：留
        _row("8-K", "2026-06-15", items="2.02"),          # 报告期内：不重复列
        _row("8-K", "2026-08-18", items="1.01,3.02"),     # 期后事件：留，附 items
        _row("4", "2026-08-20"),                          # 高频噪音表单：滤掉
        _row("DEF 14A", "2026-08-21"),                    # 不在前缀清单：滤掉
        _row("424B5", "2026-08-19"),
        _row("SC 13G/A", "2026-08-22"),
        _row("S-8", "2026-08-23"),
    ]
    txt = _postperiod_filing_index(rows, "2026-06-30")
    assert "共 5 份" in txt
    assert "2026-08-18 8-K items=1.01,3.02" in txt
    assert "2026-07-30 10-Q 期末=2026-06-30" in txt
    assert "424B5" in txt and "SC 13G/A" in txt and "S-8" in txt
    assert "DEF 14A" not in txt and "2026-06-15" not in txt
    # 新→旧排序
    assert txt.index("2026-08-23") < txt.index("2026-07-30")


def test_filing_index_cap():
    rows = [_row("8-K", f"2026-08-{d:02d}", items="8.01") for d in range(1, 11)]
    txt = _postperiod_filing_index(rows, "2026-06-30", cap=3)
    assert "共 10 份" in txt and "仅列最近 3 份" in txt
    assert len([ln for ln in txt.splitlines() if ln.startswith("  ")]) == 3
    assert "2026-08-10" in txt and "2026-08-07" not in txt


def test_filing_index_empty_is_explicit():
    """期后无新 filing 本身就是信息——显式说出来，不许沉默。"""
    txt = _postperiod_filing_index([_row("8-K", "2026-05-01", items="2.02")],
                                   "2026-06-30")
    assert "之后无上述类型的新 filing" in txt


def test_filing_index_tolerates_junk_rows():
    """缺 filingDate/form 的行不许炸——submissions 老分页数据缺字段是常态。"""
    rows = [{"form": None, "filingDate": None},
            {"form": "8-K"},
            _row("8-K", "2026-08-01", items="8.01")]
    txt = _postperiod_filing_index(rows, "2026-06-30")
    assert "共 1 份" in txt and "2026-08-01" in txt


# ---- 带锚注入的三种形态（0008）：有带给锚 / thin 给「无锚」/ 缺席给「无锚+原因」----

from app.valuation_service import _band_meta, _trading_range_payload  # noqa: E402


def test_band_meta_absent_injects_notice_with_reason():
    """带整体缺席（TSM 实测：外国发行人无季度 XBRL，pe_band_error 留痕）此前
    band_meta=''——判断层既无锚也不知道无锚。缺席必须显式告知并带原因。"""
    txt = _band_meta("standard", {"pe_band_error":
                                  "RuntimeError: TSM 季度 XBRL 不足（净利 0 期）"})
    assert "本次无历史 PE 锚" in txt
    assert "TSM 季度 XBRL 不足" in txt
    assert "rationale.pe" in txt


def test_band_meta_absent_without_recorded_reason_still_speaks():
    txt = _band_meta("standard", {})
    assert "本次无历史 PE 锚" in txt and "原因未记录" in txt


def test_band_meta_financials_absent_injects_notice():
    txt = _band_meta("financials", {"ptbv_band_error": "RuntimeError: xxx"})
    assert "本次无历史 P/TBV 锚" in txt and "xxx" in txt


def test_band_meta_thin_and_present_branches_unchanged():
    thin = _band_meta("standard", {"pe_band": {"thin_coverage": True,
                                               "days": 61, "years": 5}})
    assert "覆盖不足" in thin and "本次无历史锚" in thin
    band = {"pe_band": {"basis": "ntm", "years": 5, "days": 1100,
                        "pctiles": {str(p): 20.0 + p / 10 for p in
                                    (1, 5, 10, 25, 50, 75, 90, 95, 99)}}}
    full = _band_meta("standard", band)
    assert "默认锚" in full and "P50 25.0x" in full
    # 缺席文案不得渗入正常路径
    assert "本次无历史 PE 锚" not in full


# ---- trading_range 缺席原因（0008）：null 必须带 why ----

def test_trading_range_note_absent_band():
    trp, note = _trading_range_payload(
        "standard", {"pe_band_error": "RuntimeError: y"}, {})
    assert trp is None and "无历史 PE 带" in note and "RuntimeError: y" in note


def test_trading_range_note_thin_band():
    trp, note = _trading_range_payload(
        "standard", {"pe_band": {"thin_coverage": True, "days": 61}}, {})
    assert trp is None and "覆盖不足" in note and "61" in note


def test_trading_range_note_financials():
    _, note = _trading_range_payload("financials", {}, {})
    assert "financials" in note and "P/TBV" in note


def test_trading_range_note_pe_nm_fallthrough():
    _, note = _trading_range_payload(
        "standard", {"pe_band": {"pctiles": {"50": 20.0}}}, {})
    assert "n.m." in note


def test_trading_range_present_no_note():
    val = {"trading_range": {
        "px": {"25": 100.0, "50": 120.0, "75": 140.0},
        "window": "近3年", "eps_window": "NTM", "span": {"lag_days": 300},
        "fwd_pe_now": 20.0, "fwd_pe_now_position": "带内第 40 百分位",
        "target_pe": 25, "mult_reversion_to_p50": 0.2}}
    trp, note = _trading_range_payload("standard", {}, val)
    assert note is None
    assert trp["lo"] == 100.0 and trp["mid"] == 120.0 and trp["hi"] == 140.0
    assert trp["fwd_pe_now"] == 20.0 and trp["target_pe"] == 25


# ---- 前瞻期注入（0014）：g 分母契约随强制 override 切换 ----

FWD_WIN = {"start": "2026-07-01", "end": "2027-06-30",
           "straddle": "跨 FY26/FY27", "aligned": False}


def test_fwd_meta_default_denominator_is_facts_ttm():
    txt = _fwd_meta(FWD_WIN, force_override=False)
    assert "TTM 即 FACTS 里的口径" in txt
    assert "ttm_revenue_override" not in txt
    assert "2026-07-01~2027-06-30" in txt


def test_fwd_meta_override_denominator_under_pending_or_stale():
    """PENDING_10Q / stale>550 时校验层与引擎都按 override 换基——文案必须跟着换，
    否则判断层照文档把 g 锚在旧 TTM 上，滚进 override 的那季被重复计入增速。"""
    txt = _fwd_meta(FWD_WIN, force_override=True)
    assert "ttm_revenue_override" in txt and "不是** FACTS 里的旧 TTM" in txt
    assert "TTM 即 FACTS 里的口径" not in txt


def test_fwd_meta_aligned_branch_and_absent_window():
    assert _fwd_meta(None, force_override=True) == ""
    txt = _fwd_meta(dict(FWD_WIN, aligned=True), force_override=False)
    assert "可直接套用财年指引" in txt and "不等于" not in txt


# ---- OI&E 双缺口（0022 C8）：回退指向必须只指真实存在的通道 ----

def test_oie_both_missing_single_coherent_line():
    """残差与全部组件同时缺席：旧文案两条 ⚠ 各指对方当回退（"以下方组件序列
    为准" vs "以残差行为准"）——互相甩锅，唯一可用的财报原文没被点名为主路径。"""
    txt = _compact_facts(_facts(
        pretax_income_quarterly={}, interest_income_quarterly={},
        other_nonop_quarterly={}, other_nonop_annual={}))
    assert "本票无 OI&E 结构化序列" in txt
    assert "other_income_note 写明原文出处" in txt.replace("\n", "")
    # 两条旧的互指行都不许再出现
    assert "以下方组件序列" not in txt
    assert "推导以残差行与财报原文为准" not in txt
    assert "本票无以下 XBRL 序列" not in txt   # 全缺时清单=全集，一致行已说完


def test_oie_missing_list_fallback_matches_reality():
    """残差缺、组件在：缺失清单的回退不再指向不存在的残差行。"""
    d = _facts()
    d.pop("pretax_income_quarterly")
    txt = _compact_facts(d)
    assert "残差行不可用" in txt and "以下方组件序列" in txt
    seg = txt.split("本票无以下 XBRL 序列")[1].splitlines()[0]
    assert "推导以上方组件序列与财报原文为准" in seg and "残差行" not in seg


def test_oie_residual_present_missing_list_unchanged():
    txt = _compact_facts(_facts())
    seg = txt.split("本票无以下 XBRL 序列")[1].splitlines()[0]
    assert "推导以残差行与财报原文为准" in seg


# ---- fin 的锚-上界死锁预告（0022 C9，镜像 standard 的 pe<=60 预告，门槛 8）----

def _tbband(p50):
    ps = {str(q): round(p50 * f, 2) for q, f in
          (("10", 0.6), ("25", 0.8), ("50", 1.0), ("75", 1.2), ("90", 1.4))}
    return {"years": 5, "days": 1100, "pctiles": dict(ps),
            "recent": {"years": 3, "days": 700, "pctiles": dict(ps)}}


def test_fin_band_meta_warns_when_anchor_p50_exceeds_ptbv_cap():
    """高 ROTE 票锚窗 P50 9.5x > 校验上界 8：不预告封顶，锚纪律与上界打架烧
    retry（engine 侧 hard_cap=8 的豁免早已就位，此前只有 standard 有预告端）。"""
    txt = _band_meta("financials", {"ptbv_band": _tbband(9.5)})
    assert "超出校验上界 ptbv<=8" in txt and "按上界 8 封顶给出" in txt


def test_fin_band_meta_no_cap_notice_below_cap():
    txt = _band_meta("financials", {"ptbv_band": _tbband(1.5)})
    assert "超出校验上界" not in txt


# ---- fin 的 PE 腿信息锚（0022 C13）：engine 检查什么，对话里就要给什么 ----

_FIN_PE_BAND = {"basis": "ntm", "years": 5, "days": 1100,
                "pctiles": {"10": 10.0, "25": 12.0, "50": 15.0,
                            "75": 20.0, "90": 25.0}}


def test_fin_band_meta_injects_pe_band_reference():
    """engine 自 0005 起对 fin 各情景的 pe 跑 pe_band_check，fin prompt 却只有
    泛可比区间——检查与锚不在同一场对话里。带子亮给判断层，P/TBV 仍是主锚。"""
    txt = _band_meta("financials", {"ptbv_band": _tbband(1.5),
                                    "pe_band": _FIN_PE_BAND})
    assert "信息参照" in txt and "P50 15.0x" in txt
    assert "P/TBV 仍是主锚" in txt and "rationale.pe" in txt


def test_fin_band_meta_pe_reference_gated_on_usable_band():
    """带缺席/薄覆盖：不给假锚（与 standard 的 thin 处置同哲学）。"""
    assert "信息参照" not in _band_meta("financials", {"ptbv_band": _tbband(1.5)})
    thin = {"ptbv_band": _tbband(1.5),
            "pe_band": dict(_FIN_PE_BAND, thin_coverage=True, days=60)}
    assert "信息参照" not in _band_meta("financials", thin)


def test_fin_band_meta_pe_reference_prefers_recent_window():
    band = dict(_FIN_PE_BAND,
                recent={"years": 3, "days": 700,
                        "pctiles": {"10": 8.0, "25": 9.0, "50": 11.0,
                                    "75": 13.0, "90": 14.0}})
    txt = _band_meta("financials", {"ptbv_band": _tbband(1.5), "pe_band": band})
    assert "近3年子窗" in txt and "P50 11.0x" in txt


def test_standard_band_meta_has_no_fin_pe_reference():
    """standard 的 PE 锚走既有主锚段——信息参照块只属于 fin。"""
    band = {"pe_band": {"basis": "ntm", "years": 5, "days": 1100,
                        "pctiles": {str(p): 20.0 + p / 10 for p in
                                    (1, 5, 10, 25, 50, 75, 90, 95, 99)}}}
    assert "信息参照" not in _band_meta("standard", band)
