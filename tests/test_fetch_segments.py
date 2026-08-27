# -*- coding: utf-8 -*-
"""fetch_segments 纯函数用例：rollup 剔除、改名缝合、整期取最新。"""
from valuation.fetch_segments import (_build_axes, _conc_group,
                                      _detect_aliases, _drop_rollups,
                                      _pick_cells, _tol)


def test_drop_rollups_single_parent():
    # AAPL：ProductMember(307,003) 与 iPhone/Mac/iPad/穿戴 并存，双计
    members = {"IPhone": 209_586e6, "Services": 109_158e6, "Wear": 35_686e6,
               "Mac": 33_708e6, "IPad": 28_023e6, "ProductMember": 307_003e6}
    total = 416_161e6
    kept, ok = _drop_rollups(members, total)
    assert ok and "ProductMember" not in kept and len(kept) == 5


def test_drop_rollups_child_pair():
    # NVDA：DataCenter = Compute + Networking，父与子并存
    members = {"DataCenter": 193_737e6, "Compute": 162_361e6,
               "Networking": 31_376e6, "Gaming": 16_042e6}
    total = 209_779e6  # DataCenter + Gaming
    kept, ok = _drop_rollups(members, total)
    assert ok
    assert ("DataCenter" not in kept) or \
           ("Compute" not in kept and "Networking" not in kept)


def test_drop_rollups_exact_untouched():
    members = {"A": 60.0e6, "B": 40.0e6}
    kept, ok = _drop_rollups(members, 100.0e6)
    assert ok and kept == members


def test_drop_rollups_unresolvable_flagged():
    # SOFI 分部含 Corporate/Other 调节项：对不上就诚实标 False，不乱删
    members = {"Lending": 725e6, "FS": 466e6, "Tech": 85e6}
    kept, ok = _drop_rollups(members, 1_219e6)
    assert ok is False and len(kept) == 3


def test_drop_rollups_no_total():
    kept, ok = _drop_rollups({"A": 1e6}, None)
    assert ok is None and kept == {"A": 1e6}


def test_detect_aliases_rename():
    # MSFT：SearchAndNewsAdvertising -> SearchAdvertising，比较期同值重标
    versions = {("product", "2025-01-01", "2025-03-31"): [
        ("2025-04-30", {"SearchAndNewsAdvertisingMember": 3_504e6}),
        ("2026-04-30", {"SearchAdvertisingMember": 3_504e6}),
    ]}
    aliases = _detect_aliases(versions)
    assert aliases["product"]["SearchAndNewsAdvertisingMember"] == \
        "SearchAdvertisingMember"


def test_detect_aliases_zero_guard():
    # LLY：近零值谁都能对上，曾把 FY22 的 COVID 抗体错配成 Zepbound
    versions = {("product", "2022-01-01", "2022-12-31"): [
        ("2023-02-01", {"Covid19AntibodiesMember": 0.0}),
        ("2024-02-01", {"ZepboundMember": 0.0}),
    ]}
    assert _detect_aliases(versions) == {}


def test_detect_aliases_ambiguous_skipped():
    # 新版里两个成员同值：无法唯一判定，不缝合
    versions = {("product", "2025-01-01", "2025-03-31"): [
        ("2025-04-30", {"Old": 100e6}),
        ("2026-04-30", {"NewA": 100e6, "NewB": 100e6}),
    ]}
    assert _detect_aliases(versions) == {}


def test_pick_cells_latest_filed_and_rename():
    versions = {("product", "2025-01-01", "2025-03-31"): [
        ("2025-04-30", {"Old": 100e6, "Stay": 50e6}),
        ("2026-04-30", {"New": 100e6, "Stay": 50e6}),
    ]}
    aliases = {"product": {"Old": "New"}}
    cells = _pick_cells(versions, aliases)
    filed, members = cells[("product", "2025-01-01", "2025-03-31")]
    assert filed == "2026-04-30" and members == {"New": 100e6, "Stay": 50e6}


def test_conc_group_includes_type_and_benchmark():
    # 10-Q 会复用年度跨度上下文标应收款集中度，分组不带基准会误杀营收组
    e1 = {"start": "2025-01-01", "end": "2025-12-31",
          "dims": {"ConcentrationRiskByBenchmarkAxis": "SalesRevenueNetMember",
                   "ConcentrationRiskByTypeAxis": "CustomerConcentrationRiskMember"}}
    e2 = {"start": "2025-01-01", "end": "2025-12-31",
          "dims": {"ConcentrationRiskByBenchmarkAxis": "AccountsReceivableMember",
                   "ConcentrationRiskByTypeAxis": "CustomerConcentrationRiskMember"}}
    assert _conc_group(e1) != _conc_group(e2)


def test_build_axes_kind_split():
    cells = {("segment", "2025-01-01", "2025-03-31"): ("f", {"A": 60e6, "B": 40e6}),
             ("segment", "2025-01-01", "2025-12-31"): ("f", {"A": 240e6, "B": 160e6})}
    totals = {("2025-01-01", "2025-03-31"): ("f", 100e6),
              ("2025-01-01", "2025-12-31"): ("f", 400e6)}
    axes = _build_axes(cells, totals)
    assert set(axes["segment"]["quarterly"]) == {"2025-03-31"}
    assert set(axes["segment"]["annual"]) == {"2025-12-31"}
    assert axes["segment"]["annual"]["2025-12-31"]["reconciled"] is True


def test_tol_floor():
    assert _tol(100e6) == 2e6  # 小公司容差有 $2M 下限
    assert _tol(1e12) == 5e9


# ---- instance 发现：EDGAR 目录收缩后的 -xbrl.zip 兜底 ----
import io
import json
import zipfile

import pytest

from valuation.fetch_segments import (SegmentsError, _fetch_instance,
                                      _instance_names)


class _FakeResp:
    status_code = 200

    def __init__(self, content: bytes):
        self.content = content

    def json(self):
        return json.loads(self.content)


class _FakeClient:
    """按 URL 尾段路由的假 httpx.Client，只覆盖 _get 用到的接口。"""

    def __init__(self, routes: dict):
        self.routes = routes

    def get(self, url: str):
        return _FakeResp(self.routes[url.rsplit("/", 1)[-1]])


def _index_json(names):
    return json.dumps(
        {"directory": {"item": [{"name": n} for n in names]}}).encode()


def test_instance_names_prefers_extracted():
    names = ["nvda-20260726_htm.xml", "nvda-20260726.xsd",
             "nvda-20260726_cal.xml", "nvda-20260726_lab.xml"]
    assert _instance_names(names) == ["nvda-20260726_htm.xml"]


def test_instance_names_legacy_excludes_linkbases():
    names = ["nvda-20180429.xml", "nvda-20180429_cal.xml",
             "nvda-20180429_pre.xml", "nvda-20180429.xsd"]
    assert _instance_names(names) == ["nvda-20180429.xml"]


def test_fetch_instance_direct():
    idx = _index_json(["nvda-20260426_htm.xml", "other.htm"])
    client = _FakeClient({"index.json": idx,
                          "nvda-20260426_htm.xml": b"<xbrl/>"})
    assert _fetch_instance(client, 1045810, "0001045810-26-000052") == b"<xbrl/>"


def test_fetch_instance_zip_fallback():
    # NVDA 2026-08-26 实况：目录只剩 index/txt/-xbrl.zip 四件，
    # instance 在 zip 里
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("nvda-20260726_htm.xml", "<xbrl>zip</xbrl>")
        zf.writestr("nvda-20260726_lab.xml", "<lab/>")
    idx = _index_json(["0001045810-26-000075-index.html",
                       "0001045810-26-000075.txt",
                       "0001045810-26-000075-xbrl.zip"])
    client = _FakeClient({"index.json": idx,
                          "0001045810-26-000075-xbrl.zip": buf.getvalue()})
    assert _fetch_instance(
        client, 1045810, "0001045810-26-000075") == b"<xbrl>zip</xbrl>"


def test_fetch_instance_missing_raises():
    idx = _index_json(["0000000000-00-000000.txt"])
    client = _FakeClient({"index.json": idx})
    with pytest.raises(SegmentsError):
        _fetch_instance(client, 1, "0000000000-00-000000")


def test_fetch_instance_bad_zip_raises():
    idx = _index_json(["a-xbrl.zip"])
    client = _FakeClient({"index.json": idx, "a-xbrl.zip": b"not a zip"})
    with pytest.raises(SegmentsError):
        _fetch_instance(client, 1, "0000000000-00-000001")
