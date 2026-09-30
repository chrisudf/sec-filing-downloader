# -*- coding: utf-8 -*-
"""稀释股数量纲校正：MCD 2024 起 XBRL 把加权稀释股数按「百万股」数字申报。

现象（修前）：`pe_band.py MCD --basis trailing` 输出 TTM EPS 12,313,621.08、PE 0.0x、
分位全 0，另有 455 个陈旧交易日与 147 个被当成"分母塌缩"剔掉的真值日。
机制：2024-02 的 10-K 起 WeightedAverageNumberOfDilutedSharesOutstanding 申报
732.3 而不是 732,300,000（tag、unit 都没变，只是 iXBRL scale 漏标），
companyfacts 取 filed 最新值 → 2021 年以来全被重述成错量纲；FY 与三季量纲不一致
又让 derive_q4_avg 拒掉 Q4，滚动窗整段缺角。

夹具 tests/fixtures/mcd_companyfacts_min.json 是 SEC 原始数据（2026-09-29 下载），
只裁了 tag 与字段，数值未动——量纲错的形态必须是真的，手造的只会复现我以为的形态。
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from valuation import fetch_facts as ff
from valuation import pe_band as pb
from valuation import pe_rank as pr

FIX = Path(__file__).resolve().parent / "fixtures" / "mcd_companyfacts_min.json"
MCD = json.loads(FIX.read_text(encoding="utf-8"))
CLOSE = 233.60                       # MCD 2026-09-28 收盘
LAST_PX = date(2026, 9, 28)


# ---------------------------------------------------------------- 判据（纯函数）

@pytest.mark.parametrize("r, e", [
    (1e-6, -6), (1e-3, -3), (1e3, 3), (1e6, 6), (1e-9, -9),
    (1.4e-6, -6), (0.7e-6, -6),          # 容差内（EPS 舍入 / 两类股口径差）
    (1.0, 0), (1.3, 0), (0.8, 0),        # 同量纲
    (2, 0), (4, 0), (10, 0), (20, 0), (40, 0),   # 拆股口径差（NVDA 4×10=40）不是量纲错
    (0.05, 0), (0.025, 0),               # 反向拆股
    (100, 0), (1e4, 0), (1e5, 0),        # 不在 10^3k 的 ±50% 带里：判不了就不动
    (1.6e3, 0), (0.6e3, 0),
])
def test_share_scale_exp_grid(r, e):
    assert pb.share_scale_exp(r * 700e6, 700e6) == e


def test_share_scale_exp_degenerate_inputs():
    for val, imp in ((0, 1), (1, 0), (-5, 1), (5, -1), (None, 1), (1, None)):
        assert pb.share_scale_exp(val, imp) == 0


def test_two_copies_agree():
    """fetch_facts 与 pe_band 各有一份判据（两模块互不 import，同 OP_TAGS 惯例），
    必须逐点同答——一边改容差另一边没改时，带子与估值引擎会用两套股数。"""
    for k in range(-40, 41):
        r = 10 ** (k / 4)
        for jitter in (1.0, 1.45, 1 / 1.45, 1.55, 1 / 1.55):
            v = 700e6 * r * jitter
            assert pb.share_scale_exp(v, 700e6) == ff._share_scale_exp(v, 700e6)
    for val, e in ((718.2, -6), (12345.0, 3), (5.0, 0)):
        assert pb.unscale(val, e) == ff._unscale(val, e)


def test_unscale_exact():
    """除以 1e-06 会留尾巴（718200000.0000001），乘整数不会。"""
    assert pb.unscale(718.2, -6) == 718200000.0
    assert pb.unscale(12345.0, 3) == 12.345


# ---------------------------------------------------------------- fix_share_scale

def _r(v, filed="2026-08-07"):
    return {"val": v, "filed": filed, "first_filed": filed}


# (名, 股数, 净利, EPS, 期望校正 [(期末, e)], 期望校正后股数)——两份实现跑同一组
_Q = ("2025-03-31", "2025-06-30", "2025-09-30")
SCENARIOS = [
    # 同期 净利÷EPS 当锚：错的那期改、对的那期不动
    ("same_period_anchor",
     (717.6, 715.9e6), (2253e6, 2278e6), (3.14, 3.18),
     [("2025-03-31", -6)], (717.6e6, 715.9e6)),
    # |EPS|<0.05 舍入太粗：退到最近已核对期（该锚自己也是先被校正过的）
    ("tiny_eps_falls_back_to_corrected_neighbor",
     (718.2, 717.6, 715.9e6), (1868e6, 20e6, 2278e6), (2.60, 0.03, 3.18),
     [("2025-03-31", -6), ("2025-06-30", -6)], (718.2e6, 717.6e6, 715.9e6)),
    # 净利与 EPS 异号：对不上的期不当锚，改由邻期判——当成"已核对"会让它漏网
    ("sign_mismatch_not_an_anchor",
     (718.2e6, 717.6), (1868e6, -2253e6), (2.60, 3.14),
     [("2025-06-30", -6)], (718.2e6, 717.6e6)),
    # 一期 EPS 都没有：没有锚就不猜（残留跳变交给 compute_band 的哨兵拒绝）
    ("no_anchor_untouched",
     (717.6, 715.9e6), (None, None), (None, None),
     [], (717.6, 715.9e6)),
    # SCHW 型：错的是净利（按千美元入库），股数是对的。EPS 证人说股数大了一千倍，
    # 邻期股数证人说量级没变——两证人不一致，不动。只信 EPS 会把 18 亿股改成 180 万
    ("ni_misscaled_shares_right",
     (1834e6, 1809e6), (5942e6, 8852e3), (2.99, 4.65),
     [], (1834e6, 1809e6)),
    # MCD 近几年型：整条都被 EPS 判错、没有"没问题"的期可当第二证人，照 EPS 改；
    # 第三期 EPS 太小对不了，只能拿**已校正**的邻期当锚（锚池只收"没问题"的期会漏它）
    ("whole_series_misscaled",
     (718.2, 717.6, 715.9), (1868e6, 2253e6, 20e6), (2.60, 3.14, 0.03),
     [("2025-03-31", -6), ("2025-06-30", -6), ("2025-09-30", -6)],
     (718.2e6, 717.6e6, 715.9e6)),
]


def _pb_rows(vals):
    return {k: _r(v) for k, v in zip(_Q, vals) if v is not None}


@pytest.mark.parametrize("name, sh, ni, eps, fixed, after", SCENARIOS,
                         ids=[s[0] for s in SCENARIOS])
def test_fix_share_scale_pe_band(name, sh, ni, eps, fixed, after):
    rows = _pb_rows(sh)
    assert pb.fix_share_scale(rows, _pb_rows(ni), _pb_rows(eps)) == fixed
    assert tuple(rows[k]["val"] for k in _Q[:len(sh)]) == pytest.approx(after)


@pytest.mark.parametrize("name, sh, ni, eps, fixed, after", SCENARIOS,
                         ids=[s[0] for s in SCENARIOS])
def test_fix_share_scale_fetch_facts(name, sh, ni, eps, fixed, after):
    """fetch_facts 的序列是 {期末: 数}，按季度/年度各跑一遍，结果与 pe_band 同答。"""
    def _d(vals):
        return {k: v for k, v in zip(_Q, vals) if v is not None}
    out = {"shares_diluted_quarterly": _d(sh), "net_income_quarterly": _d(ni),
           "eps_diluted_quarterly": _d(eps)}
    ff._fix_share_scale(out)
    got = sorted((out.get("shares_diluted_rescaled") or {}).get("quarterly", {}).items())
    assert got == fixed
    q = out["shares_diluted_quarterly"]
    assert tuple(q[k] for k in _Q[:len(sh)]) == pytest.approx(after)


# ------------------------------------------ EPS 地板按申报货币判（ENIC，2026-09-30）
# pick 已把 EPS 折成美元；|EPS|<0.05 的地板防的是申报货币里的两位小数舍入。
# 按美元判时，ENIC（2.1 CLP/股 → $0.0021）整条序列都没有 EPS 证人，千股量纲错从没被查过。

_A = ("2022-12-31", "2023-12-31", "2024-12-31")


def _annual_out(sh, ni, eps, fx=None):
    out = {"shares_diluted_annual": dict(zip(_A, sh)),
           "net_income_annual": dict(zip(_A, ni)),
           "eps_diluted_annual": dict(zip(_A, eps))}
    if fx is not None:
        out["fx_to_usd"] = fx
    return out


def test_fx_floor_weak_currency_eps_is_a_witness():
    """ENIC 原数（折美元后，fx=0.001）：股数按千股入库，三期都改 −3。"""
    out = _annual_out((69166557.0,) * 3, (1252082258.0, 633455775.0, 145112153.0),
                      (0.0181, 0.00916, 0.0021), fx=0.001)
    ff._fix_share_scale(out)
    assert out["shares_diluted_rescaled"] == {"annual": dict.fromkeys(_A, -3)}
    assert out["shares_diluted_annual"]["2024-12-31"] == 69166557000.0


def test_fx_floor_strong_currency_tiny_eps_still_skipped():
    """反方向：GBP（fx=1.27）申报 0.04/股、折后 $0.0508——按美元判会越过地板，
    按申报货币判仍是舍入太粗的 0.04，不当证人；没有别的锚，就不动。"""
    out = _annual_out((717.6,), (20e6 * 1.27,), (0.04 * 1.27,), fx=1.27)
    ff._fix_share_scale(out)
    assert "shares_diluted_rescaled" not in out
    assert out["shares_diluted_annual"]["2022-12-31"] == 717.6


@pytest.mark.parametrize("eps_local, witness", [(0.06, True), (0.04, False)])
def test_fx_floor_boundary_in_reporting_currency(eps_local, witness):
    """地板本身钉在申报货币的 0.05 两侧（此前没有用例钉这个数）：CLP 0.06/股
    折后 $0.00006 仍是证人，0.04 不是。单期、无别的锚：是证人才会被改。"""
    fx = 0.001
    out = _annual_out((717.6,), (717.6e6 * eps_local * fx,), (eps_local * fx,), fx=fx)
    ff._fix_share_scale(out)
    assert ("shares_diluted_rescaled" in out) is witness


def test_fx_floor_usd_unchanged():
    """美元申报（缺 fx_to_usd 或 =1.0）地板仍是 0.05：tiny EPS 不当证人。"""
    for fx in (None, 1.0):
        out = _annual_out((717.6,), (20e6,), (0.03,), fx=fx)
        ff._fix_share_scale(out)
        assert "shares_diluted_rescaled" not in out


ENIC = json.loads((FIX.parent / "enic_companyfacts_min.json").read_text(encoding="utf-8"))


def test_enic_build_facts_rescales_and_passes_adr_guard(monkeypatch):
    """真实 ENIC companyfacts（CLP，20-F 只有年度）：2022-04-28 那份 20-F 起按千股
    申报（69,166,557），2019/2020 比较期一并被重述成错量纲；2015-2018 是对的
    （491 亿股，2018 增资后 639 亿），必须不动。修前估值管道算出 raw = 0.050003，
    被 _adr_calibration 的量级闸拦下；修后是 691.7 亿股，raw ≈ 50——正是 ENIC 的
    真实 ADR 比例（1 ADS = 50 股）。"""
    import yfinance
    from types import SimpleNamespace
    from app.valuation_service import _adr_calibration
    syms = []
    monkeypatch.setattr(ff, "_companyfacts", lambda *a, **k: ENIC)
    monkeypatch.setattr(yfinance, "Ticker", lambda s: syms.append(s) or
                        SimpleNamespace(fast_info={"lastPrice": 0.001}))
    out = ff.build_facts("ENIC", "x@example.com", cik=1659939)
    assert syms == ["CLPUSD=X"] and out["currency"] == "CLP"
    sha = out["shares_diluted_annual"]
    assert sha["2024-12-31"] == 69166557000.0
    rs = out["shares_diluted_rescaled"]["annual"]
    assert rs == {f"{y}-12-31": -3 for y in range(2019, 2025)}
    assert sha["2017-12-31"] == 49092772762.0          # 本来就对的期不动
    assert sha["2018-12-31"] == 63913359484.0
    # 2026-09-30 实测的 ENIC 现价 $4.24、yfinance 市值 $5.865B
    m, mismatch = _adr_calibration(4.24, 5.865e9, sha["2024-12-31"] / 1e6)
    assert (m, mismatch) == (50.0, None)


# ---------------------------------------------------------------- 委托书不是财报

def _proxy_facts():
    """SCHW 形态：10-K 的 FY2023 净利 $5,067M，2026-04 委托书薪酬-业绩表按千美元
    入库成 5,067,000（fp=null），filed 更晚。"""
    row = {"start": "2023-01-01", "end": "2023-12-31"}
    return {"NetIncomeLoss": {"units": {"USD": [
        dict(row, val=5067e6, filed="2024-02-23", fp="FY", form="10-K"),
        dict(row, val=5067e3, filed="2026-04-06", fp=None, form="DEF 14A")]}}}


def test_fetch_facts_pick_skips_proxy():
    """修前 SCHW 的年度净利取的是委托书值，Q4 = 年度 − 前三季 推出 −$6.4B，
    TTM 净利 $1.25B（应 ~$10.1B）。"""
    assert ff.pick(_proxy_facts(), ["NetIncomeLoss"], "annual") == {"2023-12-31": 5067e6}


def test_pe_band_pick_skips_proxy_even_without_fp():
    """pe_band 的年度行原本靠 fp=="FY" 挡住委托书（fp=null）——fp 缺键时 get 默认
    "FY"，那道挡板就没了。显式按表单排除，不靠巧合。"""
    f = _proxy_facts()
    del f["NetIncomeLoss"]["units"]["USD"][1]["fp"]
    got = pb.pick(f, ["NetIncomeLoss"], "annual", {"USD"})
    assert got["2023-12-31"]["val"] == 5067e6


@pytest.mark.parametrize("form, proxy", [
    ("DEF 14A", True), ("PRE 14A", True), ("DEFA14A", True), ("DEFC14A", True),
    ("DEFR14A", True), ("PREC14A", True), ("DEF 14C", True),
    ("10-K", False), ("10-K/A", False), ("10-Q", False), ("8-K", False),
    ("20-F", False), ("S-1", False), ("", False)])
def test_is_proxy(form, proxy):
    assert ff._is_proxy(form) is proxy


# ---------------------------------------------------------------- MCD 端到端（真实数据）

class _TS:
    def __init__(self, d):
        self._d = d

    def date(self):
        return self._d


class _Hist:
    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        for d, c in self._rows:
            yield _TS(d), {"Close": c}


def _mcd_inputs():
    """恒定收盘价：PE 只随 EPS 动，断言直接落在 EPS 上。价格日期写死到
    2026-09-28（真实末个收盘日），10 年窗口在 2036 年前都覆盖得到。"""
    rows, d = [], LAST_PX - timedelta(days=round(365.25 * 10.5))
    while d <= LAST_PX:
        if d.weekday() < 5:
            rows.append((d, CLOSE))
        d += timedelta(days=1)
    return {"cik": 63908, "facts": MCD["facts"]["us-gaap"], "dei": {},
            "hist": _Hist(rows), "splits": {}, "years": 10, "taxonomy": "us-gaap"}


def test_mcd_trailing_band_ttm_eps_and_pe():
    b = pb.compute_band("MCD", "x@example.com", years=10, basis="trailing",
                        include_series=True, inputs=_mcd_inputs())
    cur = b["current"]
    # GAAP 稀释 EPS 四季直加 3.18+3.03+2.78+3.32 = 12.31（调整后口径 ~12.55，
    # 差额是重组等非 GAAP 调整——本带子是 GAAP 口径）
    assert cur["ttm_period"] == "2026-06-30"
    assert cur["ttm_eps"] == pytest.approx(12.31, abs=0.01)
    assert cur["pe_trailing"] == pytest.approx(CLOSE / 12.31, abs=0.02)   # ≈ 18.98x
    assert any("股数量纲" in n and "10^-6" in n for n in b["split_notes"])
    assert not any("跳变" in n for n in b["split_notes"])
    # 修前 455 个陈旧日 + 147 个真值被当分母塌缩剔掉
    assert b["stale_days"] == 0
    assert b["near_zero_days"] == 0


def test_mcd_mixed_scale_fiscal_years_rebuild_q4():
    """FY2022 的 FY 行已被 2024/2025 的 10-K 重述成错量纲、三个季度还停在对的量纲
    ——修前 derive_q4_avg 拒掉 2022Q4，滚动窗缺角，就是那 455 个陈旧日的来源。"""
    b = pb.compute_band("MCD", "x@example.com", years=10, basis="trailing",
                        include_series=True, inputs=_mcd_inputs())
    by_period = {s["ttm_period"]: s["ttm_eps"] for s in b["series"]}
    assert by_period["2022-12-31"] == pytest.approx(8.33, abs=0.01)    # 申报 FY22 稀释 EPS
    assert by_period["2023-12-31"] == pytest.approx(11.56, abs=0.01)   # 申报 FY23 稀释 EPS
    assert all(3 < s["ttm_eps"] < 20 for s in b["series"])


def test_mcd_opeps_band_same_share_fix():
    """营业线口径带（pe_rank 第二组列）股数同源，一并修好。"""
    b = pb.compute_band("MCD", "x@example.com", years=10, basis="trailing",
                        metric="opeps", inputs=_mcd_inputs())
    assert 12 < b["current"]["peop_trailing"] < 25
    assert b["stale_days"] == 0


def test_mcd_pe_rank_reading():
    """pe_rank 走同一个 compute_band：修好后 MCD 行有值、新鲜、给分位。"""
    m, b10 = pr.band_reading("MCD", "x@example.com", _mcd_inputs(), "eps",
                             "pe_trailing", LAST_PX)
    assert "err" not in m
    assert m["pe"] == pytest.approx(CLOSE / 12.31, abs=0.02)
    assert m["fresh"] and m["r10"] is not None and m["r5"] is not None


def test_mcd_build_facts_shares_and_q4_eps(monkeypatch):
    """估值管道/图表同源：shares_ord 取稀释股数末值；修前是 0.0007 百万股，
    还会让 _guard_derived_q4_eps 把 FY2021 起每个 Q4 EPS 当混口径删掉。"""
    monkeypatch.setattr(ff, "_companyfacts", lambda *a, **k: MCD)
    out = ff.build_facts("MCD", "x@example.com", cik=63908)
    shq, sha = out["shares_diluted_quarterly"], out["shares_diluted_annual"]
    assert shq["2026-06-30"] == 711.1e6
    assert sha["2023-12-31"] == 732.3e6
    assert shq["2022-09-30"] == 739.5e6               # 本来就对的期不动
    rs = out["shares_diluted_rescaled"]
    assert set(rs["annual"]) == {"2021-12-31", "2022-12-31", "2023-12-31",
                                 "2024-12-31", "2025-12-31"}
    assert set(rs["annual"].values()) == set(rs["quarterly"].values()) == {-6}
    assert "2022-09-30" not in rs["quarterly"]
    eq = out["eps_diluted_quarterly"]
    assert eq["2021-12-31"] == pytest.approx(2.18, abs=0.01)   # 10.04 − 7.86
    assert eq["2024-12-31"] == pytest.approx(2.80, abs=0.01)
    assert eq["2025-12-31"] == pytest.approx(3.03, abs=0.01)


def test_fix_wired_before_q4_eps_guard():
    """接线哨兵：量纲校正必须在 Q4 EPS 守卫之前（守卫拿年度股数算隐含 EPS）。"""
    import inspect
    src = inspect.getsource(ff.build_facts)
    assert 0 < src.index("_fix_share_scale(out)") < src.index("_guard_derived_q4_eps(out)")


# ---------------------------------------------------------------- 无锚残留：拒绝出带

def _qends(n):
    t = date.today()
    cands = [date(y, m, d) for y in range(t.year - 12, t.year + 1)
             for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)) if (t - date(y, m, d)).days >= 75]
    return sorted(cands)[-n:]


def _qstart(e):
    m, y = e.month - 2, e.year
    if m <= 0:
        m, y = m + 12, y - 1
    return date(y, m, 1)


def test_unanchored_scale_jump_refuses_band():
    """没有 EPS 可对、相邻期股数差 1000 倍：哪边错判不了，整条不出（修前留痕后
    照出 0.0x 的带子，近零地板还把对的一侧剔掉）。"""
    q = _qends(24)
    ni, sh = [], []
    for i, e in enumerate(q):
        f = (e + timedelta(days=40)).isoformat()
        row = {"start": _qstart(e).isoformat(), "end": e.isoformat(), "filed": f}
        ni.append(dict(row, val=100.0))
        sh.append(dict(row, val=100.0 if i < 12 else 100e3))
    for y in {e.year for e in q}:
        if sum(1 for e in q if e.year == y) == 4:       # 年度净利 0 期会先报别的错
            ni.append({"start": f"{y}-01-01", "end": f"{y}-12-31", "fp": "FY",
                       "filed": f"{y + 1}-02-20", "val": 400.0})
    facts = {"NetIncomeLoss": {"units": {"USD": ni}},
             "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": sh}}}
    rows, d = [], date.today() - timedelta(days=round(365.25 * 6))
    while d <= date.today():
        if d.weekday() < 5:
            rows.append((d, 100.0))
        d += timedelta(days=1)
    inp = {"cik": 1, "facts": facts, "dei": {}, "hist": _Hist(rows), "splits": {},
           "years": 6}
    with pytest.raises(RuntimeError, match="量纲"):
        pb.compute_band("TST", "x@example.com", years=5, basis="trailing", inputs=inp)
