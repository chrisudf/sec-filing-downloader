# -*- coding: utf-8 -*-
"""估值报告任务服务：判断层走本地 Claude Code（claude -p 无头模式），数字全部由
valuation/ 下的确定性脚本计算。LLM 只输出假设 config，服务器 schema 严格校验。

POST /api/valuation            -> {job_id}
GET  /api/valuation/{job_id}   -> {status, step, detail, error}
GET  /api/valuation/{job_id}/result -> zip（财报原件 + manifest + 估值.xlsx + config.json）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import uuid
import zipfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import edgar

ROOT = Path(__file__).resolve().parent.parent
VAL = ROOT / "valuation"
JOBS = ROOT / "jobs"
PY = sys.executable

CLAUDE_TIMEOUT = 480
STEP_LABELS = {
    "facts": "① XBRL 取数中…",
    "price": "② 获取现价…",
    "filings": "③ 下载最新 10-K / 10-Q…",
    "sections": "④ 提取财报关键章节…",
    "judgment": "⑤ AI 判断层定假设中（约 1-2 分钟）…",
    "engine": "⑥ 估值引擎计算…",
    "report": "⑦ 生成 Excel 报告…",
    "verify": "⑧ 公式交叉验证…",
    "bundle": "⑨ 打包…",
}

router = APIRouter()
_jobs: dict[str, dict] = {}
_running = False


class ValuationRequest(BaseModel):
    ticker: str
    email: str


def _validate_judgment(d: dict) -> None:
    """LLM 输出的硬校验：结构、边界、margins 长度。不合格直接拒绝重试。"""
    need = ("fwd_shares", "net_cash", "net_cash_note", "adj_ni", "adj_note",
            "other_income", "fwd_label", "seg1", "seg2", "seg1_share",
            "scenarios", "rationale", "notes")
    for k in need:
        if k not in d:
            raise ValueError(f"缺少字段 {k}")
    if not 0 <= d["seg1_share"] <= 1:
        raise ValueError("seg1_share 必须在 0-1")
    for sc in ("bear", "base", "bull"):
        if sc not in d["scenarios"]:
            raise ValueError(f"缺少情景 {sc}")
        s = d["scenarios"][sc]
        for k in ("g", "opm", "tax", "pe", "m1", "m2", "wacc", "tg", "g0", "gN", "margins"):
            if k not in s:
                raise ValueError(f"{sc} 缺少 {k}")
        if len(s["margins"]) != 10:
            raise ValueError(f"{sc}.margins 必须恰好 10 个值")
        if not 0.05 <= s["wacc"] <= 0.2:
            raise ValueError(f"{sc}.wacc 越界")
        if s["wacc"] - s["tg"] < 0.045:
            raise ValueError(f"{sc}: wacc-tg 需 >= 0.045")
        if not 0 < s["opm"] < 0.95 or not 0 <= s["tax"] < 0.5:
            raise ValueError(f"{sc}: opm/tax 越界")


async def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> str:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{cmd[1] if len(cmd) > 1 else cmd[0]} 超时（{timeout}s）")
    if proc.returncode != 0:
        raise RuntimeError((err or out).decode("utf-8", "ignore")[-800:])
    return out.decode("utf-8", "ignore")


def _find_claude() -> str:
    """服务进程的 PATH 可能不含 npm 全局目录，按候选路径兜底。
    只能用 .cmd/.exe（子进程 shell 是 cmd.exe，跑不了 .ps1 垫片）。"""
    if os.environ.get("CLAUDE_CLI_PATH"):
        return os.environ["CLAUDE_CLI_PATH"]
    exe = shutil.which("claude")
    if exe and exe.lower().endswith(".ps1"):
        cmd_sibling = exe[:-4] + ".cmd"
        exe = cmd_sibling if Path(cmd_sibling).exists() else None
    if exe:
        return exe
    for cand in (
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
        Path.home() / ".local" / "bin" / "claude.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
    ):
        if cand.exists():
            return str(cand)
    raise RuntimeError(
        "找不到 claude CLI：请在终端跑 (Get-Command claude).Source 找到路径，"
        "然后设置环境变量 CLAUDE_CLI_PATH 指向 claude.cmd/claude.exe")


async def _claude(prompt: str) -> str:
    # VALUATION_JUDGMENT_CMD 可替换判断层命令（测试注入 / 将来切 Anthropic API）
    cmd = os.environ.get("VALUATION_JUDGMENT_CMD")
    if not cmd:
        cmd = f'"{_find_claude()}" -p --model sonnet'
    proc = await asyncio.create_subprocess_shell(
        cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"claude -p 超时（{CLAUDE_TIMEOUT}s）")
    if proc.returncode != 0:
        raise RuntimeError("claude -p 失败: " + (err or out).decode("utf-8", "ignore")[-500:])
    return out.decode("utf-8", "ignore")


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出中没有 JSON 对象")
    return json.loads(text[start:end + 1])


def _compact_facts(facts: dict) -> str:
    """给判断层的事实摘要：年度尾6 + 季度尾8（含 净利/营业利润 比，暴露一次性项目）。"""
    lines = []
    ann = list(facts["revenue_annual"].items())[-6:]
    lines.append("年度 (期末: 营收/营业利润/净利/稀释EPS, $M):")
    for k, _ in ann:
        lines.append(f"  {k}: {facts['revenue_annual'].get(k, 0)/1e6:,.0f} / "
                     f"{facts['op_income_annual'].get(k, 0)/1e6:,.0f} / "
                     f"{facts['net_income_annual'].get(k, 0)/1e6:,.0f} / "
                     f"{facts['eps_diluted_annual'].get(k, '?')}")
    lines.append("季度尾8 (期末: 营收/营业利润/净利 | 净利÷营业利润——比值异常=有一次性项目):")
    for k in list(facts["revenue_quarterly"])[-8:]:
        op = facts["op_income_quarterly"].get(k, 0)
        ni = facts["net_income_quarterly"].get(k, 0)
        ratio = f"{ni/op:.2f}" if op else "n/a"
        lines.append(f"  {k}: {facts['revenue_quarterly'][k]/1e6:,.0f} / {op/1e6:,.0f} / "
                     f"{ni/1e6:,.0f} | {ratio}")
    lines.append(f"TTM: { {k: (v.get('value') or 0)/1e6 for k, v in facts['ttm'].items()} }")
    bs = []
    for label, key in (("现金", "cash_instant"), ("短期证券", "st_securities_instant"),
                       ("长期有价证券", "lt_securities_instant"), ("长期债务", "lt_debt_instant"),
                       ("流动债务", "current_debt_instant"), ("商业票据", "commercial_paper_instant")):
        d = facts.get(key) or {}
        bs.append(f"{label} {list(d.items())[-1] if d else '无'}")
    lines.append("资产负债时点(XBRL,可能滞后,净现金以10-Q原文优先): " + ", ".join(bs))
    return "\n".join(lines)


async def _pipeline(job: dict, ticker: str, email: str) -> None:
    wd = Path(job["dir"])
    today = date.today().isoformat()

    job["step"] = "facts"
    await _run([PY, str(VAL / "fetch_facts.py"), ticker, str(wd / "facts.json"), email], wd)
    facts = json.loads((wd / "facts.json").read_text(encoding="utf-8"))

    job["step"] = "price"
    def _price():
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        return float(fi["lastPrice"]), float(fi["marketCap"])
    price, mcap = await asyncio.to_thread(_price)
    info = await edgar.company_info(ticker, email)

    job["step"] = "filings"
    groups = [edgar.QUARTER_FORMS, edgar.ANNUAL_FORMS]
    data, _ = await edgar.build_zip_latest(ticker, email, groups, 1)
    (wd / "filings.zip").write_bytes(data)
    fdir = wd / "filings"
    with zipfile.ZipFile(wd / "filings.zip") as zf:
        zf.extractall(fdir)
    htms = sorted(str(p) for p in fdir.glob("*.htm"))
    manifest = (fdir / "manifest.csv").read_text(encoding="utf-8")

    job["step"] = "sections"
    await _run([PY, str(VAL / "extract_sections.py"), str(wd / "sections.json"), *htms], wd)
    sections = (wd / "sections.json").read_text(encoding="utf-8")

    job["step"] = "judgment"
    shares = round(list(facts["shares_diluted_quarterly"].values())[-1] / 1e6)
    base_prompt = (VAL / "judgment_prompt.md").read_text(encoding="utf-8")
    prompt = (f"{base_prompt}\n\n# 服务器注入的元数据（不要输出这些字段）\n"
              f"ticker={ticker} name={info['name']} date={today} price={price:.2f} "
              f"mcap={mcap/1e6:,.0f}M$ shares={shares}M股\n\n"
              f"# FACTS（SEC XBRL）\n{_compact_facts(facts)}\n\n"
              f"# MANIFEST（本次分析的财报文件）\n{manifest}\n\n"
              f"# SECTIONS（财报关键章节摘录 JSON）\n{sections}\n")
    judgment = None
    last_err = ""
    for attempt in range(2):
        raw = await _claude(prompt if not last_err else
                            prompt + f"\n\n# 上次输出被拒绝，原因：{last_err}\n请修正后重新只输出 JSON。")
        try:
            judgment = _parse_json(raw)
            _validate_judgment(judgment)
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_err = str(e)
            judgment = None
    if judgment is None:
        raise RuntimeError(f"判断层输出两次校验失败：{last_err}")

    cfg = dict(judgment, ticker=ticker, name=info["name"], date=today,
               price=round(price, 2), mcap=round(mcap / 1e6), shares=shares)
    (wd / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")

    job["step"] = "engine"
    await _run([PY, str(VAL / "engine.py"), str(wd / "config.json"), str(wd / "facts.json"),
                str(wd / "valuation.json"), str(fdir / "manifest.csv")], wd)

    job["step"] = "report"
    xlsx = wd / f"{ticker}_valuation_{today}.xlsx"
    await _run([PY, str(VAL / "build_report.py"), str(wd / "valuation.json"), str(xlsx)], wd)

    job["step"] = "verify"
    await _run([PY, str(VAL / "verify_report.py"), str(wd / "valuation.json"), str(xlsx)], wd, 600)

    job["step"] = "bundle"
    bundle = wd / f"{ticker}_valuation_bundle_{today}.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in fdir.iterdir():
            zf.write(p, p.name)
        zf.write(xlsx, xlsx.name)
        zf.write(wd / "config.json", "config_假设留档.json")
    val = json.loads((wd / "valuation.json").read_text(encoding="utf-8"))
    job.update(status="done", step="done", result=str(bundle),
               summary={k: dict(blend=v["blend"], upside=v["upside"])
                        for k, v in val["scenarios"].items()})


async def _run_job(job_id: str, ticker: str, email: str) -> None:
    global _running
    job = _jobs[job_id]
    try:
        await _pipeline(job, ticker, email)
    except Exception as e:  # noqa: BLE001 —— 任何一步失败都要报给前端
        job.update(status="failed", error=f"[{STEP_LABELS.get(job.get('step'), job.get('step'))}] {e}")
    finally:
        _running = False


@router.post("/api/valuation")
async def create_valuation(req: ValuationRequest):
    global _running
    if _running:
        raise edgar.EdgarError(409, "已有估值任务在运行，请等它完成")
    ticker = req.ticker.strip().upper()
    if not re.match(r"^[A-Z.\-]{1,10}$", ticker):
        raise edgar.EdgarError(400, "股票代码格式不对")
    job_id = uuid.uuid4().hex[:12]
    wd = JOBS / f"{ticker}_{job_id}"
    wd.mkdir(parents=True, exist_ok=True)
    _jobs[job_id] = dict(status="running", step="facts", ticker=ticker, dir=str(wd))
    _running = True
    asyncio.get_running_loop().create_task(_run_job(job_id, ticker, req.email.strip()))
    return {"job_id": job_id}


@router.get("/api/valuation/{job_id}")
async def valuation_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise edgar.EdgarError(404, "任务不存在")
    return {k: v for k, v in job.items() if k != "dir"} | {
        "step_label": STEP_LABELS.get(job["step"], job["step"])}


@router.get("/api/valuation/{job_id}/result")
async def valuation_result(job_id: str):
    job = _jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise edgar.EdgarError(404, "报告尚未生成")
    path = Path(job["result"])
    return FileResponse(path, media_type="application/zip", filename=path.name)
