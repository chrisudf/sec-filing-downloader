# -*- coding: utf-8 -*-
"""线上部署开关：SEC_DISABLE_VALUATION 不注册估值路由、/api/features 如实报告；
SEC_MAX_FILES 覆盖下载上限。

开关在 import 期读环境变量，所以每种环境起一个子进程 import——不用 importlib.reload：
重载 app.edgar 会换出新的 EdgarError 类，后面 `from app.edgar import EdgarError` 的
测试里 pytest.raises 就接不住了。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROBE = """
import asyncio, json, sys
from app import main
print(json.dumps({
    "features": asyncio.run(main.features()),
    # 不读 app.routes：新版 FastAPI 把 include_router 包成 _IncludedRouter，没有 path
    "paths": sorted(main.app.openapi()["paths"]),
    "valuation_imported": "app.valuation_service" in sys.modules,
}))
"""


def _probe(**env):
    e = {k: v for k, v in os.environ.items()
         if k not in ("SEC_DISABLE_VALUATION", "SEC_MAX_FILES")}
    e.update(env, PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=e,
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_defaults_keep_local_behaviour():
    r = _probe()
    assert r["features"] == {"valuation": True, "max_files": 60}
    assert "/api/valuation" in r["paths"]
    assert "/api/valuation/{job_id}/result" in r["paths"]


def test_droplet_flags_drop_valuation_only():
    r = _probe(SEC_DISABLE_VALUATION="1", SEC_MAX_FILES="10")
    assert r["features"] == {"valuation": False, "max_files": 10}
    assert not [p for p in r["paths"] if p.startswith("/api/valuation")]
    # 不只是不挂路由：模块都不 import，省下它那一份内存
    assert r["valuation_imported"] is False
    # 线上要用的几块都还在
    for p in ("/api/financials/{ticker}", "/api/segments/{ticker}", "/api/insider/{ticker}",
              "/api/pe_rank", "/api/pe_rank/refresh", "/api/download", "/api/features"):
        assert p in r["paths"], p


def test_disable_flag_spellings():
    for v in ("true", "YES", " 1 "):
        assert _probe(SEC_DISABLE_VALUATION=v)["features"]["valuation"] is False, v
    for v in ("", "0", "false"):
        assert _probe(SEC_DISABLE_VALUATION=v)["features"]["valuation"] is True, v
