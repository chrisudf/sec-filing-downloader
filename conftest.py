# -*- coding: utf-8 -*-
"""pytest 根配置：把仓库根加进 sys.path，让 tests/ 能 import app / valuation。

collect_ignore：test_pure.py 与 test_engine_band.py 是早于 pytest 的独立哨兵
脚本（模块级 sys.exit 汇报结果），按 `python tests/test_pure.py` 单独跑；
让 pytest 收集它们会在 import 期就 SystemExit，整个 session 直接 INTERNALERROR。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

collect_ignore = ["tests/test_pure.py", "tests/test_engine_band.py"]
