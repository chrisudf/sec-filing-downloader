# -*- coding: utf-8 -*-
"""Watchlist PE 分位表服务：读 valuation/pe_rank.py 的最近一份结果 + 后台刷新。

GET  /api/pe_rank           -> 最近一份 reports/pe_rank/pe_rank_YYYY-MM-DD.json（没有则 404）
GET  /api/pe_rank/status    -> {status: idle|running|done|failed, done, total, current, error}
POST /api/pe_rank/refresh   -> 起一次后台重跑（同一时间只允许一次），返回任务状态

重跑走子进程（与估值管道 _run 同一做法）：yfinance/httpx 的阻塞调用不进服务进程，
进度从 stderr 的「[i/n] TICKER」行解析（格式定义在 pe_rank.main）。全表约 5 分钟。
结果文件由 pe_rank 原子写入，所以网页刷新与每周定时任务撞车也读不到半截文件；
页面只读文件、不读这里的内存状态，服务重启不丢结果。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from fastapi import APIRouter

from . import edgar

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "reports" / "pe_rank"
SCRIPT = ROOT / "valuation" / "pe_rank.py"
PY = sys.executable
TIMEOUT = 30 * 60   # 全表实测约 5 分钟；SEC/yfinance 慢时留足余量
PROGRESS = re.compile(r"^\[(\d+)/(\d+)\] (\S+)")
RESULT = re.compile(r"^pe_rank_\d{4}-\d{2}-\d{2}\.json$")

router = APIRouter()
_state: dict = {"status": "idle"}
_bg_tasks: set = set()


def latest_file(out_dir: Path | None = None) -> Path | None:
    """文件名即日期，字典序 = 时间序；临时文件（.pe_rank_*.tmp）与 md/csv 不算。"""
    d = out_dir or OUT_DIR
    files = sorted(p for p in d.glob("pe_rank_*.json") if RESULT.match(p.name))
    return files[-1] if files else None   # 目录不存在时 glob 为空，不抛


async def _refresh(state: dict, cmd: list[str] | None = None,
                   timeout: float = TIMEOUT) -> None:
    cmd = cmd or [PY, str(SCRIPT)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    tail: list[str] = []
    proc = None

    async def pump():
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "ignore").rstrip()
            m = PROGRESS.match(line)
            if m:
                state.update(done=int(m[1]) - 1, total=int(m[2]), current=m[3])
            elif line:
                tail.append(line)
                del tail[:-20]
        await proc.wait()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(ROOT), env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        state.update(status="failed", error=f"超时（{timeout / 60:.0f} 分钟）")
        return
    except Exception as e:  # noqa: BLE001 —— 任何失败都要报给前端
        state.update(status="failed", error=f"{type(e).__name__}: {e}")
        return
    finally:
        # 超时 / 任务被取消（服务关停）/ 异常：子进程不能留成孤儿继续打 SEC
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        state.update(status="failed",
                     error=("\n".join(tail) or f"退出码 {proc.returncode}")[-800:])
    else:
        state.update(status="done", done=state.get("total"), current=None,
                     finished=time.time())


@router.get("/api/pe_rank")
async def pe_rank_latest():
    f = latest_file()
    if f is None:
        raise edgar.EdgarError(404, "还没有结果——点「刷新」生成第一份（约 5 分钟）")
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise edgar.EdgarError(503, "结果文件读取失败，稍后再试")


@router.get("/api/pe_rank/status")
async def pe_rank_status():
    return dict(_state)


@router.post("/api/pe_rank/refresh")
async def pe_rank_refresh():
    if _state.get("status") == "running":
        raise edgar.EdgarError(409, "已有一次刷新在运行，请等它完成")
    edgar.contact_email()   # 没配 SEC 邮箱就当场报错，别等子进程跑起来才失败
    _state.clear()
    _state.update(status="running", done=0, total=None, current=None, started=time.time())
    task = asyncio.get_running_loop().create_task(_refresh(_state))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return dict(_state)
