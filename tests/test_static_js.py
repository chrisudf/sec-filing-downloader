# -*- coding: utf-8 -*-
"""前端脚本语法检查：static/*.js 逐个跑 `node --check`。

dashboard.js 是多张卡片共用的一个大文件，顶层函数同处一个作用域。2026-10-19 增发 patch
新加的 `function fmtShares` 撞上内部人卡片的 `const fmtShares`，浏览器报 SyntaxError、
整份脚本一行不执行，而 pytest 1028 条全绿——Python 测试碰不到 JS（见 LESSONS.md）。
本机没有 node 时跳过。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="没有 node，跳过前端语法检查")
@pytest.mark.parametrize("js", sorted(p.name for p in STATIC.glob("*.js")))
def test_static_js_parses(js):
    r = subprocess.run([NODE, "--check", str(STATIC / js)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
