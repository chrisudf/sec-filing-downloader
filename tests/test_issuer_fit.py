# -*- coding: utf-8 -*-
"""发行人适配性分类（0020）。

回归对象：一轮 6 票实测有 4 票的失败诊断被单一假设带偏——COHR（op_income 停报）
被说成「未申报」、新上市票（2 个季度）被说成「未申报」+「外国发行人」、
GLD（商品信托）被说成「重组旧 CIK」、DRAM（映射无此代码）读起来像取数失败。
报错就是诊断，分类必须可行动。
"""
import pytest

from valuation import fetch_facts as ff
from valuation.fetch_facts import FactsError, _pick_taxonomy, classify_core_gaps
from valuation.pe_band import _has_nonusd_units, insufficient_q_msg


# ---------- classify_core_gaps：停报 / 新上市 / 从未申报 ----------

Q8 = [f"20{y}-{m:02d}-30" for y in (24, 25) for m in (3, 6, 9, 12)]


def _out(**over):
    d = {"mode": "standard",
         "ttm": {"revenue": {"value": 7_118e6, "quarters": Q8[-4:]},
                 "op_income": {"value": None, "error": "口径滞后：最新期 2024-06-30"},
                 "net_income": {"value": 900e6, "quarters": Q8[-4:]}},
         "revenue_annual": {f"20{y}-12-31": 7e9 for y in range(20, 26)},
         "revenue_quarterly": {k: 1.8e9 for k in Q8},
         "op_income_annual": {f"20{y}-06-30": 5e8 for y in range(15, 25)},
         "op_income_quarterly": {f"20{y}-{m:02d}-30": 1e8
                                 for y in range(21, 25) for m in (3, 6)},
         "net_income_annual": {}, "net_income_quarterly": {k: 2e8 for k in Q8}}
    d.update(over)
    return d


def test_stopped_tagging_classified_with_last_period():
    """COHR 形态：营收照常，op_income 停在 2024-06-30——不是「未申报」。"""
    (msg,) = classify_core_gaps(_out())
    assert "op_income" in msg and "停报" in msg
    assert "最后一期 2024-06-30" in msg
    assert "历史有 18 期" in msg          # 10 年度 + 8 季度
    assert "从未申报该概念" not in msg


def test_never_filed_still_says_so():
    out = _out(op_income_annual={}, op_income_quarterly={})
    (msg,) = classify_core_gaps(out)
    assert "op_income" in msg and "从未申报" in msg and "停报" not in msg


def test_recent_ipo_classified():
    """新上市形态：年度 0 期、季度 2 期——历史不足，不是未申报也不是外国发行人。"""
    q2 = {"2026-03-31": 1e8, "2026-06-30": 1.2e8}
    out = {"mode": "standard",
           "ttm": {"revenue": {"value": None, "error": "无季度亦无年度数据"},
                   "op_income": {"value": None}, "net_income": {"value": None}},
           "revenue_annual": {}, "revenue_quarterly": q2,
           "op_income_annual": {}, "op_income_quarterly": {},
           "net_income_annual": {}, "net_income_quarterly": {}}
    (msg,) = classify_core_gaps(out)
    assert "新上市发行人" in msg and "仅 2 季" in msg
    assert "2026-03-31" in msg and "2026-06-30" in msg   # 期末逐一列出
    assert "待后续 10-Q" in msg
    assert "从未申报" not in msg and "外国发行人" not in msg


def test_no_gap_no_problem():
    out = _out()
    out["ttm"]["op_income"] = {"value": 9e8, "quarters": Q8[-4:]}
    assert classify_core_gaps(out) == []


# ---------- _pick_taxonomy / _no_revenue_reason：信托与无营收概念 ----------

def test_trust_classified_by_entity_name():
    resp = {"facts": {}, "entityName": "SPDR GOLD TRUST"}
    with pytest.raises(FactsError, match="TRUST/FUND/ETF"):
        _pick_taxonomy(resp, "GLD", 1222333)


def test_no_taxonomy_vs_no_revenue_concept_distinguished():
    # taxonomy 整体缺席
    with pytest.raises(FactsError, match="无 us-gaap/ifrs-full taxonomy"):
        _pick_taxonomy({"facts": {}, "entityName": "ACME HOLDINGS INC"}, "ACME", 99)
    # taxonomy 在、营收概念探测全空：探测过的 tag 必须列出来（可核对）
    resp = {"facts": {"us-gaap": {"Assets": {"units": {"USD": []}}}},
            "entityName": "ACME HOLDINGS INC"}
    with pytest.raises(FactsError) as ei:
        _pick_taxonomy(resp, "ACME", 99)
    msg = str(ei.value)
    assert "探测了这些 tag" in msg and "us-gaap" in msg
    assert "RevenuesNetOfInterestExpense" in msg   # override 也在探测清单里
    assert "旧 CIK" in msg                          # 重组假设仍在，但已降为并列原因


def test_fund_word_must_be_whole_token():
    """FUNDAMENTAL/TRUSTCO 这类子串不许命中——按词切分。"""
    resp = {"facts": {}, "entityName": "FUNDAMENTAL HOLDINGS INC"}
    with pytest.raises(FactsError) as ei:
        _pick_taxonomy(resp, "FH", 7)
    assert "TRUST/FUND/ETF" not in str(ei.value)


# ---------- resolve_cik：映射取得但无此代码 ≠ 取数失败 ----------

class _Resp:
    status_code = 200

    @staticmethod
    def json():
        return {"0": {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."}}


def test_ticker_absent_from_mapping(monkeypatch):
    monkeypatch.setattr(ff.httpx, "get", lambda *a, **k: _Resp())
    with pytest.raises(FactsError) as ei:
        ff.resolve_cik("DRAM", {})
    msg = str(ei.value)
    assert "映射已取得" in msg and "退市" in msg and "cik=" in msg
    assert ei.value.transient is False   # 不是瞬态：重试不会变好


def test_ticker_present_still_resolves(monkeypatch):
    monkeypatch.setattr(ff.httpx, "get", lambda *a, **k: _Resp())
    assert ff.resolve_cik("AAPL", {}) == 320193


# ---------- pe_band：外国发行人措辞 vs 历史太短措辞 ----------

def test_band_msg_foreign_shapes():
    # ifrs taxonomy / 非美元申报 / 有年度无季度：三种形态都算外国发行人
    assert "外国发行人" in insufficient_q_msg("TSM", "净利 0 期", "ifrs-full",
                                              {}, ["NetIncomeLoss"], 0, 0)
    eur = {"NetIncomeLoss": {"units": {"EUR": []}}}
    assert _has_nonusd_units(eur, ["NetIncomeLoss"])
    assert "外国发行人" in insufficient_q_msg("ASML", "净利 2 期", "us-gaap",
                                              eur, ["NetIncomeLoss"], 5, 2)
    assert "外国发行人" in insufficient_q_msg("X", "净利 0 期", "us-gaap",
                                              {}, ["NetIncomeLoss"], 6, 0)


def test_band_msg_short_history_shape():
    """美股 IPO 形态（年度 0 期 + 少量季度）：给历史不足措辞与下一步。"""
    usd = {"NetIncomeLoss": {"units": {"USD": []}}}
    msg = insufficient_q_msg("IPOX", "净利 2 期", "us-gaap", usd,
                             ["NetIncomeLoss"], 0, 2)
    assert "历史太短" in msg and "待后续 10-Q" in msg
    assert "外国发行人" not in msg


def test_band_msg_usd_per_share_not_foreign():
    """USD/shares 这类带斜杠单位不是外币证据。"""
    f = {"EarningsPerShareDiluted": {"units": {"USD/shares": []}}}
    assert not _has_nonusd_units(f, ["EarningsPerShareDiluted"])


def test_compute_band_wires_classifier():
    """接线哨兵：compute_band 的两个不足报错都必须走分类器。"""
    import inspect
    from valuation.pe_band import compute_band
    assert inspect.getsource(compute_band).count("insufficient_q_msg(") == 2


# ---------- 0022 UV1：新开始申报（期数不足）不许被说成「停报」 ----------

def test_newly_reported_concept_not_called_stopped():
    """概念最新期与全票数据锚同步（op_income 只有最近 2 季、恰到 2025-12-31 锚）：
    发行人是**刚开始**申报该概念，不是「改了列报口径停报」——旧措辞把诊断带向
    完全相反的方向。"""
    out = _out(op_income_annual={},
               op_income_quarterly={"2025-09-30": 1e8, "2025-12-31": 1e8},
               data_latest="2025-12-31")
    (msg,) = classify_core_gaps(out)
    assert "仍在申报，非停报" in msg and "期数不足" in msg
    assert "最新一期 2025-12-31" in msg
    assert "之后停报" not in msg


def test_lagging_concept_still_called_stopped():
    """概念最新期落后于数据锚：照旧按停报/滞后分类（COHR 形态不回退）。"""
    (msg,) = classify_core_gaps(_out(data_latest="2025-12-31"))
    assert "最后一期 2024-06-30 之后停报" in msg


def test_anchor_falls_back_to_revenue_series_when_data_latest_absent():
    """老 facts 回放（无 data_latest 键）：锚退回营收序列最大期末，判定不变。"""
    (msg,) = classify_core_gaps(_out())     # 夹具本就无 data_latest
    assert "之后停报" in msg


# ---------- 0022 C7：load_inputs 的 taxonomy 判据与 facts 选择同为真值 ----------

def test_load_inputs_taxonomy_truthiness(monkeypatch):
    """空 us-gaap 桩 + 有数据的 ifrs-full 并存：facts 取了 IFRS，taxonomy 也必须
    是 ifrs-full——键存在判据会标成 us-gaap，把外国发行人分类成「历史太短」。"""
    import sys as _sys
    import types as _types

    from valuation import pe_band as pb

    class _Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    def _fake_get(url, headers=None, timeout=None):
        if "company_tickers" in url:
            return _Resp({"0": {"ticker": "DUAL", "cik_str": 7}})
        return _Resp({"facts": {"us-gaap": {},
                                "ifrs-full": {"ProfitLoss": {"units": {}}}}})

    monkeypatch.setattr(pb.httpx, "get", _fake_get)

    class _Hist:
        empty = False

    class _Tk:
        splits = {}

        def history(self, period=None, auto_adjust=False):
            return _Hist()

    monkeypatch.setitem(_sys.modules, "yfinance",
                        _types.SimpleNamespace(Ticker=lambda t: _Tk()))
    inp = pb.load_inputs("DUAL", "x@example.com")
    assert inp["taxonomy"] == "ifrs-full"
    assert inp["facts"] == {"ProfitLoss": {"units": {}}}
