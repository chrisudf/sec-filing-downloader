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
import time
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
_bg_tasks: set = set()  # 事件循环对 task 只持弱引用，不留强引用可能被 GC 后 _running 永远卡 True
_running = False
JOB_TTL = 3 * 24 * 3600  # 任务留 3 天供回看，之后连工作目录一起清


def _cleanup_jobs() -> None:
    """jobs/ 目录与 _jobs 字典此前无限增长；每次新任务前清一次过期任务。
    按目录 mtime 清理也能带走服务重启后失去登记的孤儿目录。"""
    cutoff = time.time() - JOB_TTL
    for jid, job in list(_jobs.items()):
        if job.get("status") in ("done", "failed") and job.get("created", 0) < cutoff:
            _jobs.pop(jid, None)
    if JOBS.exists():
        for d in JOBS.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)


class ValuationRequest(BaseModel):
    ticker: str


def _validate_judgment(d: dict, mode: str = "standard") -> None:
    """LLM 输出的硬校验：结构、边界、margins 长度。不合格直接拒绝重试。"""
    if mode == "financials":
        _validate_judgment_financials(d)
        return
    need = ("fwd_shares", "net_cash", "net_cash_note", "adj_ni", "adj_note",
            "other_income", "fwd_label", "seg1", "seg2", "seg1_share",
            "scenarios", "rationale", "notes")
    for k in need:
        if k not in d:
            raise ValueError(f"缺少字段 {k}")
    if not 0 <= d["seg1_share"] <= 1:
        raise ValueError("seg1_share 必须在 0-1")
    # build_report.py 直接取这些 rationale 键，缺了会在花完 LLM 调用后才崩，这里提前拒绝
    if not isinstance(d["rationale"], dict):
        raise ValueError("rationale 必须是对象")
    for k in ("g", "opm", "pe", "m1", "rl", "wacc"):
        if k not in d["rationale"]:
            raise ValueError(f"rationale 缺少 {k}")
    if not isinstance(d["notes"], list) or not d["notes"]:
        raise ValueError("notes 必须是非空数组")
    if "ttm_revenue_override" in d:
        if not isinstance(d["ttm_revenue_override"], (int, float)) or d["ttm_revenue_override"] <= 0:
            raise ValueError("ttm_revenue_override 必须是正数（$M）")
        if not d.get("ttm_revenue_note"):
            raise ValueError("提供 ttm_revenue_override 时必须附 ttm_revenue_note（出处）")
    for sc in ("bear", "base", "bull"):
        if sc not in d["scenarios"]:
            raise ValueError(f"缺少情景 {sc}")
        s = d["scenarios"][sc]
        for k in ("g", "opm", "tax", "pe", "m1", "m2", "wacc", "tg", "g0", "gN", "margins"):
            if k not in s:
                raise ValueError(f"{sc} 缺少 {k}")
        if len(s["margins"]) != 10:
            raise ValueError(f"{sc}.margins 必须恰好 10 个值")
        if not all(isinstance(m, (int, float)) and -1 < m < 1 for m in s["margins"]):
            raise ValueError(f"{sc}.margins 必须是 (-1,1) 内的数字")
        if not 0.05 <= s["wacc"] <= 0.2:
            raise ValueError(f"{sc}.wacc 越界")
        if s["wacc"] - s["tg"] < 0.045:
            raise ValueError(f"{sc}: wacc-tg 需 >= 0.045")
        if not 0 < s["opm"] < 0.95 or not 0 <= s["tax"] < 0.5:
            raise ValueError(f"{sc}: opm/tax 越界")


def _validate_judgment_financials(d: dict) -> None:
    """金融股（银行/券商/fintech）判断层校验：P/E + P/TBV 假设集，无 DCF margins。"""
    need = ("fwd_shares", "adj_ni", "adj_note", "fwd_label", "scenarios", "rationale", "notes")
    for k in need:
        if k not in d:
            raise ValueError(f"缺少字段 {k}")
    if not isinstance(d["rationale"], dict):
        raise ValueError("rationale 必须是对象")
    for k in ("g", "nm", "pe", "ptbv", "wacc"):
        if k not in d["rationale"]:
            raise ValueError(f"rationale 缺少 {k}")
    if not isinstance(d["notes"], list) or not d["notes"]:
        raise ValueError("notes 必须是非空数组")
    for sc in ("bear", "base", "bull"):
        if sc not in d["scenarios"]:
            raise ValueError(f"缺少情景 {sc}")
        s = d["scenarios"][sc]
        for k in ("g", "nm", "pe", "ptbv", "wacc", "tg"):
            if k not in s or not isinstance(s[k], (int, float)):
                raise ValueError(f"{sc} 缺少数值字段 {k}")
        if not -0.5 < s["g"] < 1.5:
            raise ValueError(f"{sc}.g 越界")
        if not 0 < s["nm"] < 0.6:
            raise ValueError(f"{sc}.nm（净利率）需在 (0, 0.6)")
        if not 1 <= s["pe"] <= 60 or not 0.2 <= s["ptbv"] <= 8:
            raise ValueError(f"{sc}: pe/ptbv 越界")
        if not 0.05 <= s["wacc"] <= 0.25:
            raise ValueError(f"{sc}.wacc 越界")
        if s["wacc"] - s["tg"] < 0.045:
            raise ValueError(f"{sc}: wacc-tg 需 >= 0.045")


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
        # verify_report 的 FAIL 明细在 stdout，而 formulas 包把进度条打在 stderr——
        # 只取 (err or out) 会让用户看到一串进度条而不是哪个单元格不一致
        detail = b"\n".join(x for x in (err, out) if x).decode("utf-8", "ignore")
        raise RuntimeError(detail[-800:])
    return out.decode("utf-8", "ignore")


def _find_claude() -> str:
    """服务进程的 PATH 可能不含 npm 全局目录，按候选路径兜底。
    Windows 只能用 .cmd/.exe（子进程 shell 是 cmd.exe，跑不了 .ps1 垫片）。"""
    if os.environ.get("CLAUDE_CLI_PATH"):
        return os.environ["CLAUDE_CLI_PATH"]
    exe = shutil.which("claude")
    if exe and exe.lower().endswith(".ps1"):
        cmd_sibling = exe[:-4] + ".cmd"
        exe = cmd_sibling if Path(cmd_sibling).exists() else None
    if exe:
        return exe
    if os.name == "nt":
        candidates = (
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
            Path.home() / ".local" / "bin" / "claude.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
        )
        hint = "请在终端跑 (Get-Command claude).Source 找到路径"
    else:
        candidates = (
            Path.home() / ".local" / "bin" / "claude",
            Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/claude"),
            Path.home() / ".npm-global" / "bin" / "claude",
        )
        hint = "请在终端跑 which claude 找到路径"
    for cand in candidates:
        if cand.exists():
            return str(cand)
    raise RuntimeError(f"找不到 claude CLI：{hint}，然后设置环境变量 CLAUDE_CLI_PATH 指向它")


async def _claude(prompt: str) -> str:
    # VALUATION_JUDGMENT_CMD 可替换判断层命令（测试注入 / 将来切 Anthropic API）
    # VALUATION_MODEL 可换判断层模型：sonnet(默认) / opus / fable，或完整模型 ID
    cmd = os.environ.get("VALUATION_JUDGMENT_CMD")
    if not cmd:
        model = os.environ.get("VALUATION_MODEL", "sonnet")
        cmd = f'"{_find_claude()}" -p --model {model}'
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
    if facts.get("mode") == "financials":
        return _compact_facts_financials(facts)
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


def _compact_facts_financials(facts: dict) -> str:
    """金融股事实摘要：总净收入/税前/净利 + 净利率轨迹 + 权益/商誉/无形（TBV 原料）。"""
    lines = []
    ann = list(facts["revenue_annual"].items())[-6:]
    lines.append("年度 (期末: 总净收入/税前利润/净利/稀释EPS, $M | 净利率):")
    for k, _ in ann:
        rev = facts["revenue_annual"].get(k, 0)
        ni = facts["net_income_annual"].get(k, 0)
        nm = f"{ni/rev:.1%}" if rev else "n/a"
        lines.append(f"  {k}: {rev/1e6:,.0f} / "
                     f"{facts['pretax_income_annual'].get(k, 0)/1e6:,.0f} / "
                     f"{ni/1e6:,.0f} / {facts['eps_diluted_annual'].get(k, '?')} | {nm}")
    lines.append("季度尾8 (期末: 总净收入/税前/净利 | 净利率——观察拨备/一次性项目导致的波动):")
    for k in list(facts["revenue_quarterly"])[-8:]:
        rev = facts["revenue_quarterly"].get(k, 0)
        ni = facts["net_income_quarterly"].get(k, 0)
        nm = f"{ni/rev:.1%}" if rev else "n/a"
        lines.append(f"  {k}: {rev/1e6:,.0f} / "
                     f"{facts['pretax_income_quarterly'].get(k, 0)/1e6:,.0f} / "
                     f"{ni/1e6:,.0f} | {nm}")
    lines.append(f"TTM: { {k: (v.get('value') or 0)/1e6 for k, v in facts['ttm'].items()} }")
    eq = facts.get("equity_instant") or {}
    gw = facts.get("goodwill_instant") or {}
    it = facts.get("intangibles_instant") or {}
    if eq:
        k, v = list(eq.items())[-1]
        gv = list(gw.values())[-1] if gw else 0
        iv = list(it.values())[-1] if it else 0
        lines.append(f"资本（{k} 时点, $M）: 股东权益 {v/1e6:,.0f}, 商誉 {gv/1e6:,.0f}, "
                     f"无形资产 {iv/1e6:,.0f} → 有形账面价值 TBV {(v-gv-iv)/1e6:,.0f}"
                     "（引擎按此确定性计算，勿输出）")
    return "\n".join(lines)


async def _pipeline(job: dict, ticker: str, email: str) -> None:
    wd = Path(job["dir"])
    today = date.today().isoformat()

    job["step"] = "facts"
    await _run([PY, str(VAL / "fetch_facts.py"), ticker, str(wd / "facts.json"), email], wd)
    facts = json.loads((wd / "facts.json").read_text(encoding="utf-8"))
    mode = facts.get("mode", "standard")
    # 数据不全时在花 LLM 调用之前失败，给出可读的原因（engine.py 只会抛 TypeError/KeyError）
    ttm = facts.get("ttm", {})
    need = (("revenue", "net_income") if mode == "financials"
            else ("revenue", "op_income", "net_income", "cfo", "capex"))
    missing = [k for k in need if (ttm.get(k) or {}).get("value") is None]
    # 外国发行人常只有年度股数（20-F 无季度 XBRL），退回年度序列
    shares_series = facts.get("shares_diluted_quarterly") or facts.get("shares_diluted_annual")
    if missing or not shares_series:
        raise RuntimeError(
            f"XBRL 数据不完整（缺 TTM: {', '.join(missing) or '—'}"
            f"{'；缺稀释股本' if not shares_series else ''}），无法估值")

    job["step"] = "price"
    def _price():
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        return float(fi["lastPrice"]), float(fi["marketCap"])
    # yfinance 无超时：Yahoo 卡住会永久占死唯一的任务槽（_running 不复位）
    price, mcap = await asyncio.wait_for(asyncio.to_thread(_price), timeout=60)
    info = await edgar.company_info(ticker, email)

    job["step"] = "filings"
    groups = [edgar.QUARTER_FORMS, edgar.ANNUAL_FORMS]
    data, _ = await edgar.build_zip_latest(ticker, email, groups, 1)
    (wd / "filings.zip").write_bytes(data)
    fdir = wd / "filings"
    with zipfile.ZipFile(wd / "filings.zip") as zf:
        zf.extractall(fdir)
    htms = sorted(str(p) for p in fdir.iterdir() if p.suffix.lower() in (".htm", ".html"))
    manifest = (fdir / "manifest.csv").read_text(encoding="utf-8")

    job["step"] = "sections"
    await _run([PY, str(VAL / "extract_sections.py"), str(wd / "sections.json"), *htms], wd)
    sections = (wd / "sections.json").read_text(encoding="utf-8")

    job["step"] = "judgment"
    shares_ord = list(shares_series.values())[-1] / 1e6  # 普通股口径（百万股）
    # ADR 换算：yfinance 价格是 ADR，XBRL 股数是普通股。用 mcap÷普通股数反推每普通股
    # 隐含价，price/隐含价 即 ADR 比例（TSM 1:5）；接近 1 则视为无 ADR（股数口径误差）
    implied = mcap / (shares_ord * 1e6)
    adr_multiple = price / implied if implied > 0 else 1.0
    snapped = round(adr_multiple)
    if abs(adr_multiple - 1) < 0.08:
        adr_multiple = 1.0
    elif snapped >= 2 and abs(adr_multiple / snapped - 1) < 0.08:
        adr_multiple = float(snapped)
    shares = round(shares_ord / adr_multiple)  # ADR 等效股数：mcap ≈ price × shares
    prompt_file = ("judgment_prompt_financials.md" if mode == "financials"
                   else "judgment_prompt.md")
    base_prompt = (VAL / prompt_file).read_text(encoding="utf-8")
    caliber = ""
    if adr_multiple != 1.0:
        caliber += (f"\n口径说明：价格为 ADR 价（1 ADR = {adr_multiple:g} 普通股），"
                    f"shares 已折为 ADR 等效股数；你输出的 fwd_shares 也用 ADR 等效口径。")
    if facts.get("currency", "USD") != "USD":
        caliber += (f"\n口径说明：申报货币 {facts['currency']}，FACTS 已按现汇 "
                    f"{facts.get('fx_to_usd', 1):.5f} 折算美元（恒定汇率）；"
                    "历史增长率不受影响，绝对值以美元理解。")
    stale_days = 0
    if facts.get("data_latest"):
        stale_days = (date.today() - date.fromisoformat(facts["data_latest"])).days
        caliber += (f"\nXBRL 结构化数据最新期末为 {facts['data_latest']}；"
                    "若 SECTIONS 财报原文里有更新的季度数字，以原文为准做前瞻判断。")
        if stale_days > 550 and mode == "standard":
            caliber += ("数据已严重滞后：你必须按财报原文推出真实 TTM 营收，"
                        "输出 ttm_revenue_override（$M）与 ttm_revenue_note（出处），"
                        "并把所有 g 锚定在该基准上——缺失会被拒绝重试。")
    # 假设连续性（可选）：VALUATION_PREV_CONFIG 指向上次的 config_假设留档.json。
    # 注入后判断层被要求"仅在有新证据时修改假设并说明变更"，把运行间噪声
    # （同输入采样 base CV≈3.5%，弱模型更大）转化为可审计的假设变更记录。
    prev_section = ""
    prev_path = os.environ.get("VALUATION_PREV_CONFIG")
    if prev_path and Path(prev_path).exists():
        try:
            prev = json.loads(Path(prev_path).read_text(encoding="utf-8"))
            if prev.get("ticker") == ticker:
                prev_core = {k: prev.get(k) for k in
                             ("date", "adj_ni", "net_cash", "fwd_shares", "scenarios", "rationale")}
                prev_section = (
                    "\n\n# 上一次运行的假设（连续性基准）\n"
                    "连续性纪律：下面是上次运行的假设与理由。本次只在材料中出现**新证据**"
                    "（新财报数字、新指引、新风险披露）时才修改对应假设，并在 notes 里逐条说明"
                    "『相对上次的变更 + 依据的新证据』；没有新证据的假设保持上次原值。"
                    "事实类字段（adj_ni/net_cash）仍按本次最新财报独立计算，不受此约束。\n"
                    + json.dumps(prev_core, ensure_ascii=False, indent=1))
        except (ValueError, OSError):
            pass
    prompt = (f"{base_prompt}\n\n# 服务器注入的元数据（不要输出这些字段）\n"
              f"ticker={ticker} name={info['name']} date={today} price={price:.2f} "
              f"mcap={mcap/1e6:,.0f}M$ shares={shares}M股{caliber}\n\n"
              f"# FACTS（SEC XBRL）\n{_compact_facts(facts)}\n\n"
              f"# MANIFEST（本次分析的财报文件）\n{manifest}\n\n"
              f"# SECTIONS（财报关键章节摘录 JSON）\n{sections}{prev_section}\n")
    judgment = None
    last_err = ""
    for attempt in range(2):
        raw = await _claude(prompt if not last_err else
                            prompt + f"\n\n# 上次输出被拒绝，原因：{last_err}\n请修正后重新只输出 JSON。")
        try:
            judgment = _parse_json(raw)
            _validate_judgment(judgment, mode)
            # 陈旧 XBRL（外国发行人 6-K 无季度框架）下，没有原文重锚的 TTM 会让
            # 全部情景锚在多年前的营收基准上——硬性要求判断层给 override
            if (mode == "standard" and stale_days > 550
                    and not judgment.get("ttm_revenue_override")):
                raise ValueError(
                    f"XBRL 数据滞后（最新期末 {facts.get('data_latest')}）："
                    "必须按财报原文提供 ttm_revenue_override（TTM 营收 $M）与 ttm_revenue_note")
            break
        except (ValueError, json.JSONDecodeError, TypeError, KeyError) as e:
            # TypeError/KeyError：LLM 把数字写成字符串、scenarios 不是对象等畸形输出，
            # 同样应该带着原因重试而不是让整个任务崩掉
            last_err = repr(e)
            judgment = None
    if judgment is None:
        raise RuntimeError(f"判断层输出两次校验失败：{last_err}")

    cfg = dict(judgment, ticker=ticker, name=info["name"], date=today,
               price=round(price, 2), mcap=round(mcap / 1e6), shares=shares,
               mode=mode, adr_multiple=adr_multiple,
               currency=facts.get("currency", "USD"))
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
    email = edgar.contact_email()
    _cleanup_jobs()
    job_id = uuid.uuid4().hex[:12]
    wd = JOBS / f"{ticker}_{job_id}"
    wd.mkdir(parents=True, exist_ok=True)
    _jobs[job_id] = dict(status="running", step="facts", ticker=ticker, dir=str(wd),
                         created=time.time())
    _running = True
    task = asyncio.get_running_loop().create_task(_run_job(job_id, ticker, email))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"job_id": job_id}


@router.get("/api/valuation/{job_id}")
async def valuation_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise edgar.EdgarError(404, "任务不存在")
    return {k: v for k, v in job.items() if k not in ("dir", "created")} | {
        "step_label": STEP_LABELS.get(job["step"], job["step"])}


@router.get("/api/valuation/{job_id}/result")
async def valuation_result(job_id: str):
    job = _jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise edgar.EdgarError(404, "报告尚未生成")
    path = Path(job["result"])
    return FileResponse(path, media_type="application/zip", filename=path.name)
