# -*- coding: utf-8 -*-
"""中文标签数据与纯查表函数（分部成员/集中度分类）。

这些表和口径逻辑的增长模式不同：每看一只新票就可能要加词条。
数据独立成模块后，「加词条」的 diff 不再淹没「改口径逻辑」的 diff。
CONC_BENCH_ZH 是有序元组——顺序即命中优先级，别改成 dict。
"""
import re

AXIS_LABELS = {"product": "按业务线", "segment": "按经营分部", "geo": "按地区"}
MAX_MEMBERS = 7  # 调色板 8 档：7 个成员 + 「其他」

# 常见成员名的中文映射；不在表里的剥掉 Member 后缀按驼峰拆词展示
MEMBER_ZH = {
    "Americas": "美洲", "Europe": "欧洲", "GreaterChina": "大中华区",
    "Japan": "日本", "RestOfAsiaPacific": "亚太其他", "AsiaPacific": "亚太",
    "IPhone": "iPhone", "IPad": "iPad", "Mac": "Mac",
    "WearablesHomeandAccessories": "穿戴/家居/配件", "Service": "服务",
    "Product": "产品", "DataCenter": "数据中心", "Gaming": "游戏",
    "ProfessionalVisualization": "专业可视化", "Automotive": "汽车",
    "OEMAndOther": "OEM及其他", "OemAndOther": "OEM及其他",
    "ComputeAndNetworking": "计算与网络", "Graphics": "图形",
    "Hyperscale": "超大规模云", "AICloudsIndustrialEnterprise": "AI云/行业企业",
    "EdgeComputing": "边缘计算",
    "LaunchServices": "发射服务", "SpaceSystems": "空间系统",
    "US": "美国", "CN": "中国", "TW": "台湾", "JP": "日本", "KR": "韩国",
    "DE": "德国", "GB": "英国", "IE": "爱尔兰", "SG": "新加坡", "IL": "以色列",
    "UnitedStates": "美国", "China": "中国", "ChinaIncludingHongKong": "中国(含香港)",
    "NonUs": "美国以外", "OtherCountries": "其他国家/地区",
    "AllOtherCountries": "其他国家/地区", "International": "国际",
    "Domestic": "本土", "Foreign": "海外",
}


# ---- 集中度分类：类型/基准/交易对手 ----
CONC_TYPE_ZH = (("Customer", "客户"), ("Supplier", "供应商"), ("Vendor", "供应商"),
                ("Credit", "信用"), ("Lender", "贷款人"), ("Geographic", "地域"),
                ("Labor", "用工"), ("Reinsur", "再保险"), ("Product", "产品"))
# 基准轴决定「占什么的百分比」——占应收款和占营收是完全不同的风险，
# 必须分开标注（对标站曾把 AAPL 的应收款集中度标成营收集中度）
CONC_BENCH_ZH = (("NonTradeReceivable", "非贸易应收款"),
                 ("TradeAccountsReceivable", "贸易应收款"),
                 ("AccountsReceivable", "应收账款"),
                 ("Receivable", "应收款"),
                 # 分部营收基准要先于「营收」命中：占分部营收≠占公司总营收
                 ("SalesRevenueSegment", "分部营收"),
                 ("RevenueFromContractWithCustomer", "营收"),
                 ("SalesRevenue", "营收"), ("Revenue", "营收"),
                 ("AccountsPayable", "应付账款"), ("Purchase", "采购额"),
                 ("CostOfGoods", "采购成本"), ("Deposit", "存款"),
                 ("Loans", "贷款"), ("Assets", "资产"))
# 聚合口径的对手方（客户群体/前N大合计/按地域圈定的客户），不能当
# 单一客户进风险分级和趋势加总——NVDA 的「美国终端客户 99%」是群体
_AGG_RE = re.compile(
    r"(Customers|Suppliers|Vendors|Carriers|Largest|Top[A-Z0-9]|Based|"
    r"Aggregate|Combined|Group|Government)")
# 同一客户的两套序数命名归一：NVDA 的 10-K 用 CustomerA/B/C、10-Q 用
# CustomerOne/Two，不归并会在趋势里双倍计数、明细表出两行
_ORD_ZH = {"A": "一", "B": "二", "C": "三", "D": "四", "E": "五", "F": "六",
           "One": "一", "Two": "二", "Three": "三", "Four": "四",
           "Five": "五", "Six": "六"}
_ORD_RE1 = re.compile(r"^(Customer|Client|Vendor|Supplier)"
                      r"(A|B|C|D|E|F|One|Two|Three|Four|Five|Six)$")
_ORD_RE2 = re.compile(r"^(One|Two|Three|Four|Five|Six)"
                      r"(Customer|Client|Vendor|Supplier)$")


def _party_norm(base: str) -> str | None:
    m = _ORD_RE1.match(base)
    kind = ordn = None
    if m:
        kind, ordn = m.group(1), m.group(2)
    else:
        m = _ORD_RE2.match(base)
        if m:
            ordn, kind = m.group(1), m.group(2)
    if kind is None:
        return None
    return ("客户" if kind in ("Customer", "Client") else "供应商") + _ORD_ZH[ordn]
CONC_PARTY_ZH = {
    "CustomerOne": "客户一", "CustomerTwo": "客户二", "CustomerThree": "客户三",
    "CustomerFour": "客户四", "CustomerFive": "客户五",
    "VendorOne": "供应商一", "VendorTwo": "供应商二", "VendorThree": "供应商三",
    "Company": "未具名大客户", "CellularNetworkCarriers": "移动运营商",
}


def _zh(table, name: str) -> str | None:
    for key, zh in table:
        if key in name:
            return zh
    return None


def _member_base(name: str) -> str:
    """剥掉 Member/SegmentMember 类后缀作为合并键：同一分部跨申报改名
    （NVDA 的 GraphicsMember -> GraphicsSegmentMember）要归成一个系列，
    否则图上同一分部会中途换色、图例按名去重后对不上色块。"""
    return re.sub(r"(Segments?Member|SegmentMember|Member)$", "", name) or name


def _member_label(name: str) -> str:
    base = _member_base(name)
    if base in MEMBER_ZH:
        return MEMBER_ZH[base]
    # 驼峰拆词："RestOfAsiaPacific" -> "Rest Of Asia Pacific"
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base) or name
