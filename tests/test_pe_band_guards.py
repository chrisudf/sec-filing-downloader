# -*- coding: utf-8 -*-
"""pe_band 数据形态守卫：NaN 收盘、零 FY 新上市（0007）。

compute_band 支持注入 inputs（facts/hist/splits），全部用例走合成数据零联网。
hist 只需要 iterrows() 产 (有 .date() 的时间戳, {"Close": px})——不依赖 pandas。
日期必须相对 date.today() 生成：compute_band 的窗口起点/滞后天数都锚今天。
"""
import math
from datetime import date, timedelta

import pytest

from valuation.pe_band import compute_band


# ---------------------------------------------------------------- 合成数据

class _TS:
    def __init__(self, d):
        self._d = d

    def date(self):
        return self._d


class _Hist:
    """rows: [(date, close)]，close 可为 float('nan') 模拟 yfinance 空行。"""

    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        for d, c in self._rows:
            yield _TS(d), {"Close": c}


def _last_qend(min_age_days=75):
    """最近一个「filed（期末+40天）已落在过去」的日历季度末。"""
    t = date.today()
    cands = [date(y, m, dd) for y in (t.year - 1, t.year)
             for m, dd in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return max(d for d in cands if (t - d).days >= min_age_days)


def _quarter_ends(n, last=None):
    """last 起往回 n 个日历季度末（升序）。"""
    d = last or _last_qend()
    ends = []
    while len(ends) < n:
        ends.append(d)
        m, y = d.month - 3, d.year
        if m <= 0:
            m, y = m + 12, y - 1
        d = date(y, m, {3: 31, 6: 30, 9: 30, 12: 31}[m])
    return list(reversed(ends))


def _qstart(e):
    m, y = e.month - 2, e.year
    if m <= 0:
        m, y = m + 12, y - 1
    return date(y, m, 1)


def _facts(q_ends, ni_vals, shares=100.0, with_annual=True):
    """最小 us-gaap facts：季度净利 + 季度股数（+ 完整年的 FY 行）。"""
    ni_rows, sh_rows = [], []
    for e, v in zip(q_ends, ni_vals):
        filed = (e + timedelta(days=40)).isoformat()
        ni_rows.append({"start": _qstart(e).isoformat(), "end": e.isoformat(),
                        "val": v, "filed": filed})
        sh_rows.append({"start": _qstart(e).isoformat(), "end": e.isoformat(),
                        "val": shares, "filed": filed})
    if with_annual:
        by_year = {}
        for e, v in zip(q_ends, ni_vals):
            by_year.setdefault(e.year, []).append(v)
        for y, vals in by_year.items():
            if len(vals) != 4:
                continue
            end = date(y, 12, 31)
            filed = (end + timedelta(days=55)).isoformat()
            row = {"start": date(y, 1, 1).isoformat(), "end": end.isoformat(),
                   "filed": filed, "fp": "FY"}
            ni_rows.append(dict(row, val=sum(vals)))
            sh_rows.append(dict(row, val=shares))
    return {"NetIncomeLoss": {"units": {"USD": ni_rows}},
            "WeightedAverageNumberOfDilutedSharesOutstanding":
                {"units": {"shares": sh_rows}}}


def _hist(nan_dates=(), years=6, close=100.0):
    t = date.today()
    d = t - timedelta(days=round(365.25 * years))
    nan = set(nan_dates)
    rows = []
    while d <= t:
        if d.weekday() < 5:
            rows.append((d, float("nan") if d in nan else close))
        d += timedelta(days=1)
    return _Hist(rows)


def _inputs(facts, hist):
    return {"cik": 1, "facts": facts, "dei": {}, "hist": hist,
            "splits": {}, "years": 6}


def _weekdays_back(n, offset_days):
    """今天回退 offset_days 后往前数 n 个工作日。"""
    out, d = [], date.today() - timedelta(days=offset_days)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


# ---------------------------------------------------------------- NaN 收盘

def test_nan_close_dropped_from_distribution_and_trailing_current():
    """两个失效区各注 NaN：ntm 窗内（此前 pstdev 直接抛 AttributeError，整条带子
    死于 pe_band_error）与 trailing 尾段（此前 trailing_nolag.current=NaN 流进
    prompt/stdout 渲染成「nanx」）。修后两处都在源头剔除并计数。"""
    q = _quarter_ends(32)
    facts = _facts(q, [100.0] * 32)          # TTM EPS = 400/100 = 4.0，PE 恒 25
    mid = _weekdays_back(5, round(2.5 * 365))   # ntm 有值的窗内
    tail = _weekdays_back(3, 0)                 # trailing-only 尾段（最后 3 个交易日）
    band = compute_band("TST", "x@example.com", years=5, basis="ntm",
                        inputs=_inputs(facts, _hist(nan_dates=mid + tail)))
    assert math.isfinite(band["mean"]) and math.isfinite(band["stdev"])
    assert math.isfinite(band["min"]) and math.isfinite(band["max"])
    assert band["nan_close_days"] == 8
    tn = band["trailing_nolag"]
    assert tn is not None and math.isfinite(tn["current"])
    # current 落在最后一个非 NaN 交易日，而不是 NaN 行
    assert tn["current_date"] not in {d.isoformat() for d in tail}


def test_no_nan_baseline_counts_zero():
    q = _quarter_ends(32)
    band = compute_band("TST", "x@example.com", years=5, basis="ntm",
                        inputs=_inputs(_facts(q, [100.0] * 32), _hist()))
    assert band["nan_close_days"] == 0
    assert math.isfinite(band["trailing_nolag"]["current"])


# ---------------------------------------------------------------- 季节性豁免（0008）

# 峰值季占全年 470/1070 ≈ 43.9%，恰好越过 leave-one-out 判据的算术阈值
# 2.25/5.25 ≈ 42.9%（INTU 财年 Q3 占 60-70% 是更极端的同型）
SEASONAL = [200.0, 470.0, 200.0, 200.0]


def test_stable_seasonality_builds_band():
    """[200,470,200,200] 复现（评审 repro）：修前每一扇 TTM 窗都被标畸变、三口径
    全灭，compute_band 报「有效交易日仅 0 天」还把锅甩给披露滞后。修后同一财季
    跨年同向重现判季节性，窗口全部保留。"""
    q = _quarter_ends(28)                       # 7 年
    vals = [SEASONAL[i % 4] for i in range(28)]  # 周期=4 → 峰值恒同一日历季
    band = compute_band("SEAS", "x@example.com", years=5, basis="trailing",
                        inputs=_inputs(_facts(q, vals), _hist()))
    assert band["days"] >= 250 and not band["thin_coverage"]
    assert band["anom_windows"] == []
    assert len(band["seasonal_windows"]) > 0
    assert band["anom_days"] == {"trailing": 0, "ntm": 0}


def test_stable_seasonality_keeps_fiscal_years_forward_basis():
    """FY 层的同型豁免：稳定季节性票每一个 FY 都含 >1.25x 离群季，修前整段剔穿
    （forward 口径 eps_fy 清空、样本不足）。"""
    q = _quarter_ends(28)
    vals = [SEASONAL[i % 4] for i in range(28)]
    band = compute_band("SEAS", "x@example.com", years=5, basis="forward",
                        inputs=_inputs(_facts(q, vals), _hist()))
    assert band["anom_fys"] == []
    assert len(band["seasonal_fys"]) >= 2
    assert band["days"] >= 60


def test_true_one_off_still_culled():
    """校准过的真阳性（AMZN Anthropic 重估/税改型：单窗单期事件）不被豁免——
    同一财季其他年份不会同向离群。"""
    q = _quarter_ends(28)
    vals = [200.0] * 28
    vals[13] = 800.0                            # 约 3.5 年前的一次性收益季
    band = compute_band("ONEOFF", "x@example.com", years=5, basis="trailing",
                        inputs=_inputs(_facts(q, vals), _hist()))
    hit = q[13].isoformat()
    assert [w["quarter"] for w in band["anom_windows"]] == [hit] * 4  # 含该季的 4 扇窗
    assert band["seasonal_windows"] == []
    assert band["anom_days"]["trailing"] > 0
    assert band["anom_fys"] == [f"{q[13].year}-12-31"]               # FY 层同判


# ---------------------------------------------------------------- 季节性豁免的两道收口（0022 C0/C1）

# C0 复现形态：稳定季节峰值（第 4 槽 =3x，LOO 偏离 2.0）之外，某个非峰值季混进
# 一笔一次性收益（250，LOO 偏离 1.5——落在 ANOM_K 与峰值偏离之间）。旧代码只裁
# 单一 argmax：豁免掉 300 之后 250 被顺带放行，TTM EPS 吹大 25% 且窗口还标着
# "稳定季节性保留"。
SEASONAL_MASK = [100.0, 100.0, 100.0, 300.0]


def test_seasonal_exemption_does_not_mask_cooccurring_one_off():
    q = _quarter_ends(28)
    vals = [SEASONAL_MASK[i % 4] for i in range(28)]
    vals[14] = 250.0                      # 非峰值槽的一次性收益（~3.5 年前）
    band = compute_band("MASK", "x@example.com", years=5, basis="trailing",
                        inputs=_inputs(_facts(q, vals), _hist()))
    hit = q[14].isoformat()
    # 含一次性季的 4 扇窗全部照剔，anom_q 指向真凶（250 那季，不是季节峰值季）
    assert [w["quarter"] for w in band["anom_windows"]] == [hit] * 4
    assert band["anom_days"]["trailing"] > 0
    # 不含一次性季的窗口仍按季节性豁免保留（豁免机制本身不回退）
    assert len(band["seasonal_windows"]) > 0
    assert hit not in {w["quarter"] for w in band["seasonal_windows"]}
    # FY 层同判：含一次性季的财年整年剔除，其余季节性财年照旧豁免
    assert f"{q[14].year}-12-31" in band["anom_fys"]
    assert len(band["seasonal_fys"]) >= 2


def test_sparse_same_quarter_one_offs_not_seasonal():
    """C1 复现形态（商誉减值季）：14 年平季，其中 3 个稀疏年份的同一财季各挖一刀
    （年度减值测试结构性扎堆同一财季，但隔多年才来一次）。旧密度缺失下跨年同向
    重现 >=2 即豁免——当期减值季被当成"稳定季节性"留在分布里，TTM EPS 被打穿。
    密度闸：同财季其他年份里同向离群须占至少一半（3/14 远不够）。"""
    q = _quarter_ends(56)                 # 14 年
    vals = [100.0] * 56
    for idx in (7, 19, 55):               # 同一槽位（同财季）、稀疏三年，末年是当期
        vals[idx] = -150.0
    band = compute_band("GWIM", "x@example.com", years=5, basis="trailing",
                        inputs=_inputs(_facts(q, vals), _hist()))
    assert band["seasonal_windows"] == [] and band["seasonal_fys"] == []
    assert q[55].isoformat() in {w["quarter"] for w in band["anom_windows"]}


def test_dense_seasonality_survives_density_gate():
    """密度闸不许误伤真季节性：INTU 型逐年重现（既有 SEASONAL 用例之外再钉一次
    带密度视角的断言——7 年 7 次全中，密度 6/6）。"""
    q = _quarter_ends(28)
    vals = [SEASONAL[i % 4] for i in range(28)]
    band = compute_band("SEAS2", "x@example.com", years=5, basis="trailing",
                        inputs=_inputs(_facts(q, vals), _hist()))
    assert band["anom_windows"] == [] and len(band["seasonal_windows"]) > 0


# ---------------------------------------------------------------- 零 FY 新上市

def test_zero_fy_rows_raises_clean_runtime_error():
    """SPCX 型：有季度 XBRL（>=4 期，过外国发行人闸门）但 FY 长度行 0 期——
    此前走到财年边界表 fy_ends[-1] 直接 IndexError。"""
    q = _quarter_ends(6)
    facts = _facts(q, [100.0] * 6, with_annual=False)
    with pytest.raises(RuntimeError, match="年度净利 0 期"):
        compute_band("IPO", "x@example.com", years=5, basis="trailing",
                     inputs=_inputs(facts, _hist()))
