# -*- coding: utf-8 -*-
"""集中度分类（分支最密的函数）与分部重塑的用例。"""
from app.segments_service import _conc_reshape, _reshape_axis
from app.zh_labels import _member_base, _member_label, _party_norm


def E(value, days=364, end="2026-06-30", start=None, **dims):
    """dims 用轴全名简写：type/bench/customer/geo/product/segments/range。"""
    axis_map = {"type": "ConcentrationRiskByTypeAxis",
                "bench": "ConcentrationRiskByBenchmarkAxis",
                "customer": "MajorCustomersAxis",
                "geo": "StatementGeographicalAxis",
                "product": "ProductOrServiceAxis",
                "segments": "StatementBusinessSegmentsAxis",
                "range": "RangeAxis"}
    return {"start": start or "2025-07-01", "end": end, "days": days,
            "value": value,
            "dims": {axis_map[k]: v for k, v in dims.items()}}


CUST = "CustomerConcentrationRiskMember"
REV = "SalesRevenueNetMember"
AR = "AccountsReceivableMember"


def test_benchmark_separation():
    # AAPL 教训：占应收款 ≠ 占营收，风险分级只认营收基准
    c = _conc_reshape([
        E(0.46, type=CUST, bench="NonTradeReceivableMember",
          customer="VendorOneMember"),
        E(0.12, type=CUST, bench="TradeAccountsReceivableMember",
          customer="CustomerOneMember"),
    ])
    assert c["risk"]["level"] == "low"
    assert {r["benchmark"] for r in c["latest"]} == {"非贸易应收款", "贸易应收款"}


def test_revenue_customer_drives_risk():
    c = _conc_reshape([E(0.22, type=CUST, bench=REV, customer="CustomerOneMember")])
    assert c["risk"]["level"] == "medium" and c["risk"]["top_party"] == "客户一"
    c = _conc_reshape([E(0.67, type=CUST, bench=REV, customer="CompanyMember")])
    assert c["risk"]["level"] == "high"


def test_aggregate_not_single_customer():
    # 群体口径（前五大合计）不冒充单一客户
    c = _conc_reshape([E(0.13, type=CUST, bench=REV,
                         customer="FiveLargestCustomersMember")])
    assert c["risk"]["aggregate"] is True
    assert c["latest"][0]["aggregate"] is True


def test_scope_qualified_dropped():
    # NVDA 出口管制披露：占「新加坡开票的受管制产品」营收 99% ≠ 占总营收
    c = _conc_reshape([
        E(0.99, type=CUST, bench=REV, customer="UsBasedEndCustomersMember",
          geo="SG", product="ControlledProductsMember"),
        E(0.22, type=CUST, bench=REV, customer="CustomerOneMember"),
    ])
    assert len(c["latest"]) == 1 and c["latest"][0]["pct"] == 22.0


def test_segments_axis_is_attribution_kept():
    # 分部轴只是归属注记，分母仍是总营收（评审驳回项确认），保留
    c = _conc_reshape([E(0.22, type=CUST, bench=REV,
                         customer="CustomerOneMember",
                         segments="ComputeSegmentMember")])
    assert c["latest"][0]["pct"] == 22.0


def test_geo_party_forced_geo_type():
    # RKLB：美国占营收 79% 是地域集中度，不进客户风险池
    c = _conc_reshape([E(0.79, type="GeographicConcentrationRiskMember",
                         bench=REV, geo="US")])
    assert c["latest"][0]["type"] == "地域"
    assert c["risk"]["level"] == "low"


def test_range_axis_merged():
    # LLY：三大批发商各占 16-24%（Min/Max 两条），归并成区间、风险取上限
    c = _conc_reshape([
        E(0.16, type=CUST, bench=REV, customer="ThreeLargestWholesalersMember",
          range="MinimumMember"),
        E(0.24, type=CUST, bench=REV, customer="ThreeLargestWholesalersMember",
          range="MaximumMember"),
    ])
    r = c["latest"][0]
    assert r["pct"] == 24.0 and r["pct_lo"] == 16.0
    assert len(c["latest"]) == 1


def test_benchless_dropped():
    # GS 信贷组合构成披露：无基准轴不可解读，整行丢弃
    c = _conc_reshape([E(1.0, type="CreditConcentrationRiskMember",
                         customer="InternalInvestmentGradeMember")])
    assert c is None


def test_stale_disclosure():
    c = _conc_reshape([E(0.42, end="2021-12-31", start="2021-01-01",
                         type=CUST, bench=REV, customer="CustomerOneMember")])
    assert c["risk"]["level"] == "stale"
    assert c["risk"]["last_end"] == "2021-12-31"


def test_trend_ordinal_dedup():
    # NVDA：10-K 用 CustomerA、10-Q 用 CustomerOne 指同一家，趋势不许双计
    c = _conc_reshape([
        E(0.12, type=CUST, bench=REV, customer="CustomerAMember"),
        E(0.12, type=CUST, bench=REV, customer="CustomerOneMember"),
    ])
    assert len(c["trend"]) == 1
    assert c["trend"][0]["sum"] == 12.0 and c["trend"][0]["count"] == 1


def test_value_normalization():
    # 0-1 是小数、1-100 是整数百分比、>100 丢弃
    c = _conc_reshape([
        E(0.46, type=CUST, bench=REV, customer="CustomerOneMember"),
        E(46.0, type=CUST, bench=AR, customer="CustomerOneMember"),
        E(460.0, type=CUST, bench=REV, customer="CustomerTwoMember"),
    ])
    assert {r["pct"] for r in c["latest"]} == {46.0}
    assert len(c["latest"]) == 2  # 营收 + 应收款 两个基准各一行


def test_party_helpers():
    assert _party_norm("CustomerA") == "客户一" == _party_norm("CustomerOne")
    assert _party_norm("VendorTwo") == "供应商二"
    assert _party_norm("Government") is None
    assert _member_base("GraphicsSegmentMember") == _member_base("GraphicsMember")
    assert _member_label("RestOfAsiaPacificSegmentMember") == "亚太其他"


def test_reshape_axis_window_and_placeholders():
    # GOOG 教训：Q4 被丢弃后不许拿窗口外老季度凑数，缺季补 null 占位
    def cell(members, total):
        return {"members": members, "total": total,
                "reconciled": True, "derived": False}
    cells = {"2019-06-30": cell({"A": 1e9}, 1e9),      # 窗口外，须被丢弃
             "2025-09-30": cell({"A": 2e9}, 2e9),
             # 2025-12-31 缺（模拟 Q4 推导失败）
             "2026-03-31": cell({"A": 3e9}, 3e9),
             "2026-06-30": cell({"A": 4e9}, 4e9)}
    shaped = _reshape_axis({"quarterly": cells, "annual": {}}, "quarterly", 1)
    labels = [p["label"] for p in shaped["periods"]]
    assert "Jun '19" not in labels                      # 窗口约束
    ph = [p for p in shaped["periods"] if p["placeholder"]]
    assert len(ph) == 1 and ph[0]["label"] == "Dec '25"  # 缺季占位
    i = [p["placeholder"] for p in shaped["periods"]].index(True)
    assert shaped["series"][0][i] is None and shaped["total"][i] is None


def test_reshape_axis_member_fold():
    def cell(members):
        t = sum(members.values())
        return {"members": members, "total": t,
                "reconciled": True, "derived": False}
    members = {f"M{i}": (10 - i) * 1e9 for i in range(9)}  # 9 个成员
    cells = {"2026-06-30": cell(members)}
    shaped = _reshape_axis({"quarterly": cells, "annual": {}}, "quarterly", 1)
    assert len(shaped["members"]) == 7                  # 上限 7
    assert shaped["other"][0] == (3 + 2) * 1e9          # 最小两个（3e9+2e9）折叠进其他
