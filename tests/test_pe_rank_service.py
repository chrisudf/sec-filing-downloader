# -*- coding: utf-8 -*-
"""Watchlist PE 服务：最近结果选取、后台刷新的进度解析/失败/超时、并发闸。

子进程用假脚本代替 pe_rank.py——真跑要联网 5 分钟，而这里要验的是管道本身。
"""
import asyncio
import sys

import pytest

from app import pe_rank_service as ps
from app.edgar import EdgarError


def _fake(code):
    return [sys.executable, "-c", code]


def test_latest_file_picks_newest_and_ignores_others(tmp_path):
    assert ps.latest_file(tmp_path) is None
    for n in ("pe_rank_2026-09-19.json", "pe_rank_2026-09-26.json",
              "pe_rank_2026-09-26.md", ".pe_rank_2026-09-27.json.tmp",
              "pe_rank_latest.json"):
        (tmp_path / n).write_text("{}", encoding="utf-8")
    assert ps.latest_file(tmp_path).name == "pe_rank_2026-09-26.json"
    assert ps.latest_file(tmp_path / "missing") is None


def test_latest_endpoint_404_then_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "OUT_DIR", tmp_path)
    with pytest.raises(EdgarError) as e:
        asyncio.run(ps.pe_rank_latest())
    assert e.value.status == 404
    (tmp_path / "pe_rank_2026-09-26.json").write_text('{"asof": "2026-09-26"}', encoding="utf-8")
    assert asyncio.run(ps.pe_rank_latest()) == {"asof": "2026-09-26"}


def test_refresh_parses_progress_and_finishes():
    code = ("import sys\n"
            "for i, t in enumerate(['NVDA', 'MSFT', 'QQQ'], 1):\n"
            "    print(f'[{i}/3] {t}', file=sys.stderr, flush=True)\n"
            "print('已写出 x', file=sys.stderr)\n")
    st = {"status": "running", "done": 0}
    asyncio.run(ps._refresh(st, _fake(code)))
    assert st["status"] == "done" and st["total"] == 3 and st["done"] == 3
    assert st["current"] is None


def test_refresh_progress_is_live():
    # 进度要边跑边更新，不是跑完一次性写：卡在第 2 只时读到 done=1 / current=MSFT
    code = ("import sys, time\n"
            "print('[1/3] NVDA', file=sys.stderr, flush=True)\n"
            "print('[2/3] MSFT', file=sys.stderr, flush=True)\n"
            "time.sleep(30)\n")
    st = {"status": "running"}

    async def go():
        task = asyncio.create_task(ps._refresh(st, _fake(code), timeout=60))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if st.get("current") == "MSFT":
                break
        snap = dict(st)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return snap
    snap = asyncio.run(go())
    assert snap["current"] == "MSFT" and snap["done"] == 1 and snap["total"] == 3


def test_cancel_kills_child(monkeypatch):
    # 服务关停时任务被取消：子进程必须被杀掉并回收，不能继续在后台跑 5 分钟
    procs = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **k):
        p = await real(*a, **k)
        procs.append(p)
        return p
    monkeypatch.setattr(ps.asyncio, "create_subprocess_exec", spy)

    async def go():
        task = asyncio.create_task(
            ps._refresh({"status": "running"}, _fake("import time; time.sleep(30)")))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if procs:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert procs and procs[0].returncode is not None


def test_refresh_failure_reports_stderr_tail():
    code = ("import sys\n"
            "print('[1/2] NVDA', file=sys.stderr, flush=True)\n"
            "print('未配置 SEC 联系邮箱', file=sys.stderr)\n"
            "sys.exit(1)\n")
    st = {"status": "running"}
    asyncio.run(ps._refresh(st, _fake(code)))
    assert st["status"] == "failed" and "未配置 SEC 联系邮箱" in st["error"]
    assert "[1/2]" not in st["error"]        # 进度行不混进错误信息


def test_refresh_timeout_kills():
    st = {"status": "running"}
    asyncio.run(ps._refresh(st, _fake("import time; time.sleep(30)"), timeout=0.5))
    assert st["status"] == "failed" and "超时" in st["error"]


def test_refresh_rejects_concurrent(monkeypatch):
    # _refresh 必须打桩：闸门失效时这里会真起一次 pe_rank.py（联网全表），变异测试
    # 实测整个 pytest 卡死而不是失败——单测永远不许有这条退路
    started = []

    async def fake_refresh(state):
        started.append(state)
    monkeypatch.setattr(ps, "_state", {"status": "running", "done": 3})
    monkeypatch.setattr(ps, "_refresh", fake_refresh)
    monkeypatch.setattr(ps.edgar, "contact_email", lambda: "a@b.c")
    with pytest.raises(EdgarError) as e:
        asyncio.run(ps.pe_rank_refresh())
    assert e.value.status == 409
    assert not started and ps._state == {"status": "running", "done": 3}   # 进行中的状态没被清掉


def test_refresh_starts_background_job(monkeypatch):
    seen = {}

    async def fake_refresh(state):
        seen["state"] = state
        state.update(status="done")
    monkeypatch.setattr(ps, "_state", {"status": "idle"})
    monkeypatch.setattr(ps, "_refresh", fake_refresh)
    monkeypatch.setattr(ps.edgar, "contact_email", lambda: "a@b.c")

    async def go():
        r = await ps.pe_rank_refresh()
        await asyncio.sleep(0)            # 让后台任务跑一拍
        await asyncio.gather(*ps._bg_tasks)
        return r
    r = asyncio.run(go())
    assert r["status"] == "running" and r["done"] == 0
    assert seen["state"] is ps._state and ps._state["status"] == "done"


def test_weekly_task_script_has_bom():
    # Windows PowerShell 5.1（README 里的 `powershell -File`）把无 BOM 的 .ps1 当 ANSI 读，
    # 中文字符串被拆成乱码后直接 ParserError——任务根本注册不上。2026-09-25 实际踩到。
    ps1 = ps.ROOT / "scripts" / "pe_rank_weekly.ps1"
    assert ps1.read_bytes().startswith(b"\xef\xbb\xbf")
