# -*- coding: utf-8 -*-
"""估值报告任务服务：判断层走本地 Claude Code（claude -p 无头模式），数字全部由
valuation/ 下的确定性脚本计算。LLM 只输出假设 config，服务器 schema 严格校验。

POST /api/valuation            -> {job_id}
GET  /api/valuation/{job_id}   -> {status, step, detail, error}
GET  /api/valuation/{job_id}/result -> zip（财报原件 + manifest + 估值.xlsx + config.json）
"""
from __future__ import annotations

import asyncio
import csv
import io
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
# 连续性锚存放处（v2）——不能放 jobs/：_cleanup_jobs 按 mtime rmtree，锚活不过 3 天
PREV_DIR = ROOT / "prev_configs"
PY = sys.executable

# 判断层单次调用上限。默认模型 opus 后（v2, 2026-07-22）在 ~45k 字符 prompt 上比
# sonnet 慢，且 v2 一次运行最多 3 次调用（schema retry + 经济复审），放宽到 600s
CLAUDE_TIMEOUT = 600
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


def _scenario_eps(d: dict, s: dict, rev0: float) -> float:
    """与 engine.py 同式的情景 FY(下一财年) EPS——一致性规则用，不做估值。"""
    ni = (rev0 * (1 + s["g"]) * s["opm"] + d["other_income"]) * (1 - s["tax"])
    return ni / d["fwd_shares"]


def _validate_judgment(d: dict, mode: str = "standard",
                       rev0: float | None = None,
                       fcf_margin: float | None = None) -> None:
    """LLM 输出的硬校验：结构、边界、margins 长度。不合格直接拒绝重试。

    v2（semantics_version=2, 2026-07-22）新增：
    - 单参数边界（pe/m1/m2/g/g0/gN/margins）——standard 模式此前为零检查，
      是实测跑间漂移（NVDA bear 综合 $29-$77）的直接放大器
    - 跨情景排序（g/opm/pe/m1 须 bear<=base<=bull）
    - 反双重计数：情景盈利已收缩(扩张)时禁止再叠加谷底(峰值)倍数——
      市场对可修复的周期谷底给看穿周期的倍数；判断为永久受损时可设
      permanent_impairment=true + impairment_note（原文出处）豁免
    - margins 谷底下限 0.4×当前 TTM FCF 利润率（同一豁免）
    rev0/fcf_margin 由调用方从 facts 传入；为 None 时跳过对应规则。"""
    if mode == "financials":
        _validate_judgment_financials(d)
        return
    need = ("fwd_shares", "net_cash", "net_cash_note", "adj_ni", "adj_note",
            "other_income", "fwd_label", "seg1", "seg2", "seg1_share",
            "scenarios", "rationale", "notes")
    for k in need:
        if k not in d:
            raise ValueError(f"缺少字段 {k}")
    if not isinstance(d["fwd_shares"], (int, float)) or d["fwd_shares"] <= 0:
        raise ValueError("fwd_shares 必须为正数（百万股）")
    if not isinstance(d["other_income"], (int, float)):
        raise ValueError("other_income 必须是数字（$M）")
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
        # 上界随当前实际 FCF 利润率放宽（特许权/授权类公司 TTM FCF 率本身可 >65%，
        # 静态上界会连"维持现状"的路径都拒绝，两次 retry 撞同一堵墙后硬失败）
        m_cap = max(0.65, min(0.9, 1.2 * fcf_margin)) if fcf_margin else 0.65
        if not all(isinstance(m, (int, float)) and -0.3 < m < m_cap for m in s["margins"]):
            raise ValueError(f"{sc}.margins 必须是 (-0.3, {m_cap:.2f}) 内的数字（FCF 利润率）")
        if not 0.05 <= s["wacc"] <= 0.2:
            raise ValueError(f"{sc}.wacc 越界")
        if s["wacc"] - s["tg"] < 0.045:
            raise ValueError(f"{sc}: wacc-tg 需 >= 0.045")
        if not 0 < s["opm"] < 0.95 or not 0 <= s["tax"] < 0.5:
            raise ValueError(f"{sc}: opm/tax 越界")
        _imp = (s.get("permanent_impairment") is True
                and str(s.get("impairment_note") or "").strip() != "")
        if not (4 if _imp else 8) <= s["pe"] <= 60:
            raise ValueError(f"{sc}.pe 需在 [8, 60]——低于 8x 的『目标 PE』属于崩盘/永久受损"
                             "定价，须设 permanent_impairment=true + impairment_note（下限放宽至 4）")
        if not 0 <= s["m1"] <= 60 or not 0 <= s["m2"] <= 60:
            raise ValueError(f"{sc}.m1/m2 需在 [0, 60]")
        for k in ("g", "g0"):
            if not -0.35 < s[k] < 0.9:
                raise ValueError(f"{sc}.{k} 需在 (-0.35, 0.9)")
        if not 0 < s["gN"] <= 0.12:
            raise ValueError(f"{sc}.gN 需在 (0, 0.12]")

    # ---- v2 跨情景一致性（拦『所有参数同取极端』与周期双重计数）----
    sb, ss, su = d["scenarios"]["bear"], d["scenarios"]["base"], d["scenarios"]["bull"]
    for k in ("g", "opm", "pe", "m1", "m2"):
        if not sb[k] <= ss[k] <= su[k]:
            raise ValueError(f"情景排序：{k} 必须 bear <= base <= bull")

    def _exempt(s):
        return (s.get("permanent_impairment") is True
                and str(s.get("impairment_note") or "").strip() != "")

    if rev0:
        eps = {n: _scenario_eps(d, d["scenarios"][n], rev0)
               for n in ("bear", "base", "bull")}
        if eps["base"] > 0:
            r_bear, r_bull = eps["bear"] / eps["base"], eps["bull"] / eps["base"]
            # m2 仅在真双分部（次分部倍数非 0）时参与——它在 seg1_share<0.85 时
            # 承担近半 SOTP 权重，同样是独立采样漂移通道
            for key in (("pe", "m1", "m2") if sb.get("m2", 0) > 0 else ("pe", "m1")):
                if (r_bear < 0.8 and sb[key] < 0.6 * ss[key] and not _exempt(sb)):
                    raise ValueError(
                        f"bear 双重计数：情景盈利已较 base 收缩至 {r_bear:.0%}，{key} 又 "
                        f"< 0.6×base——谷底盈利×谷底倍数会把周期惩罚计两次。请上调 bear.{key}"
                        "（市场对可修复的谷底给看穿周期的倍数），或判断为永久受损时设 "
                        "permanent_impairment=true 并在 impairment_note 给原文出处")
                if r_bull > 1.25 and su[key] > 1.4 * ss[key]:
                    raise ValueError(
                        f"bull 双重计数：情景盈利已较 base 扩张至 {r_bull:.0%}，{key} 又 "
                        f"> 1.4×base——景气顶点市场收敛倍数而非扩张。请下调 bull.{key}")
    if fcf_margin and fcf_margin > 0.02:
        floor = 0.4 * fcf_margin
        for n in ("bear", "base", "bull"):
            s = d["scenarios"][n]
            if min(s["margins"]) < floor and not _exempt(s):
                raise ValueError(
                    f"{n}.margins 谷底 {min(s['margins']):.0%} < 0.4×当前 TTM FCF 利润率"
                    f"({fcf_margin:.0%})——比腰斩更深的常态化路径属于永久受损假设，"
                    "请抬高谷底或设 permanent_impairment=true + impairment_note")


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
    # VALUATION_MODEL 可换判断层模型：opus(默认) / sonnet / fable，或完整模型 ID。
    # 默认 opus（v2, 2026-07-22）：A/B 实测 sonnet 判断层同输入 4 次采样 base 目标价
    # 全距 25.7%（CV 11.2%），强模型 CV≈3.5%——假设质量是这条管线的地板，判断层
    # 频次低（每标的每季 1-3 次调用），用最强模型的成本可忽略；与 judge_openai_compat
    # 的默认（claude-opus-4-8）对齐。要快可显式 VALUATION_MODEL=sonnet
    cmd = os.environ.get("VALUATION_JUDGMENT_CMD")
    if not cmd:
        model = os.environ.get("VALUATION_MODEL", "opus")
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
    # need 必须与 engine.py 对应分支实际读取的 ttm 键一致（金融股分支还读 pretax_income）
    need = (("revenue", "pretax_income", "net_income") if mode == "financials"
            else ("revenue", "op_income", "net_income", "cfo", "capex"))
    missing = [k for k in need if (ttm.get(k) or {}).get("value") is None]
    if mode == "financials" and not facts.get("equity_instant"):
        missing.append("股东权益（TBV 原料）")
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
    # include_pending_8k：业绩已公布但 10-Q 未交时（大科技隔 0-2 天,银行/中盘
    # 2-6 周）,把 8-K 的 EX-99.1 新闻稿带给判断层；10-Q 一交,状态自动消失
    data, _ = await edgar.build_zip_latest(ticker, email, groups, 1,
                                           include_pending_8k=True)
    (wd / "filings.zip").write_bytes(data)
    fdir = wd / "filings"
    with zipfile.ZipFile(wd / "filings.zip") as zf:
        zf.extractall(fdir)
    htms = sorted(str(p) for p in fdir.iterdir() if p.suffix.lower() in (".htm", ".html"))
    manifest = (fdir / "manifest.csv").read_text(encoding="utf-8")
    latest_report = max((r.get("reportDate") or "" for r in
                         csv.DictReader(io.StringIO(manifest))), default="")
    pending_8k = next((r for r in csv.DictReader(io.StringIO(manifest))
                       if r["form"].startswith("8-K")), None)

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
    if pending_8k and mode == "standard":
        caliber += (
            f"\n⚠ PENDING_10Q：该公司 {pending_8k['filingDate']} 已公布业绩"
            "（SECTIONS 含其 8-K EX-99.1 新闻稿摘录，**未经审计、可能混非 GAAP"
            "口径**），但 10-Q 尚未提交——FACTS 的 XBRL/TTM 不含最新季度。你必须：\n"
            "1) 前瞻判断（g/opm/指引）以新闻稿里的最新季度 **GAAP** 数字为准；\n"
            "2) 用新闻稿计算真实 TTM 营收并输出 ttm_revenue_override（$M）与"
            " ttm_revenue_note（公式=旧TTM − 去年同季 + 本季，写明数字出处）"
            "——缺失会被拒绝重试；\n"
            "3) 除营收基准外，勿把新闻稿数字与旧 XBRL 混算其他比率；"
            "非 GAAP 数字仅作定性参考。")
    # 假设连续性（v2 起默认开启，2026-07-22）：无新证据不得改数、改数必须留痕，
    # 把运行间采样噪声（NVDA 实测三次 bear 综合 $29-$77）转化为可审计的假设变更记录。
    # 来源优先级：VALUATION_PREV_CONFIG 显式指定 > prev_configs/{ticker}.json 自动持久化。
    # VALUATION_NO_CONTINUITY=1 可整体关闭。失效触发器（自动作废 prev，本次独立重建）：
    #   1) 语义版本不符（v1 的倍数假设在 v2 联动约束下不可复用）
    #   2) 出现更新的报告期（新财报=新证据，禁止锚死在旧假设上——连续性最危险的
    #      失效模式就是财报后按构造低反应）
    #   3) 现价较上次运行变动 >15%（市场环境已变，倍数/wacc 假设需重估）
    prev_section = ""
    if not os.environ.get("VALUATION_NO_CONTINUITY"):
        prev_path = os.environ.get("VALUATION_PREV_CONFIG") or str(PREV_DIR / f"{ticker}.json")
        if Path(prev_path).exists():
            try:
                prev = json.loads(Path(prev_path).read_text(encoding="utf-8"))
                stale = None
                if prev.get("ticker") != ticker:
                    stale = "标的不符"
                elif prev.get("semantics_version", 1) != 2:
                    stale = f"语义版本 v{prev.get('semantics_version', 1)} != v2"
                elif prev.get("manifest_latest") and latest_report \
                        and prev["manifest_latest"] != latest_report:
                    stale = f"出现新报告期 {latest_report}（上次基于 {prev['manifest_latest']}）"
                elif prev.get("price") and abs(price / prev["price"] - 1) > 0.15:
                    stale = f"现价较上次变动 {price / prev['price'] - 1:+.0%}（>15%）"
                if stale:
                    prev_section = (f"\n\n# 假设连续性说明\n上次运行（{prev.get('date')}）的假设"
                                    f"已失效：{stale}。本次独立重建全部假设。")
                else:
                    prev_core = {k: prev.get(k) for k in
                                 ("date", "adj_ni", "net_cash", "fwd_shares", "scenarios", "rationale")}
                    prev_section = (
                        "\n\n# 上一次运行的假设（连续性基准）\n"
                        "连续性纪律：下面是上次运行的假设与理由。本次只在材料中出现**新证据**"
                        "（新财报数字、新指引、新风险披露）时才修改对应假设，并在 notes 里逐条说明"
                        "『相对上次的变更 + 依据的新证据』；没有新证据的假设保持上次原值。"
                        "事实类字段（adj_ni/net_cash）仍按本次最新财报独立计算，不受此约束。\n"
                        + json.dumps(prev_core, ensure_ascii=False, indent=1))
            except (ValueError, OSError, TypeError, KeyError):
                # 设计意图：基准文件任何读取/类型问题都降级为"忽略连续性"，绝不杀任务
                # （VALUATION_PREV_CONFIG 支持用户手工指定/编辑的文件）
                pass
    prompt = (f"{base_prompt}\n\n# 服务器注入的元数据（不要输出这些字段）\n"
              f"ticker={ticker} name={info['name']} date={today} price={price:.2f} "
              f"mcap={mcap/1e6:,.0f}M$ shares={shares}M股{caliber}\n\n"
              f"# FACTS（SEC XBRL）\n{_compact_facts(facts)}\n\n"
              f"# MANIFEST（本次分析的财报文件）\n{manifest}\n\n"
              f"# SECTIONS（财报关键章节摘录 JSON）\n{sections}{prev_section}\n")
    # v2 一致性规则的事实输入：情景 EPS 用的营收基准与 TTM FCF 利润率
    rev0_m = ttm["revenue"]["value"] / 1e6
    fcfm = None
    if mode == "standard":
        fcfm = (ttm["cfo"]["value"] - ttm["capex"]["value"]) / ttm["revenue"]["value"]
    judgment = None
    last_err = ""
    last_raw = ""
    for attempt in range(2):
        # v2：retry 必须带上次完整输出——只回传错误文本会让每轮变成独立重采样，
        # 模型看不到自己上次给了什么就谈不上"最小修改"，漂移会从 retry 通道漏回来
        if not last_err:
            p = prompt
        else:
            p = (prompt + "\n\n# 你上一次的输出（在此基础上最小修改，其余字段保持原值）\n"
                 + last_raw[:9000]
                 + f"\n\n# 上次输出被拒绝，原因\n{last_err}\n只修正违规字段后重新输出完整 JSON。")
        raw = await _claude(p)
        try:
            judgment = _parse_json(raw)
            _validate_judgment(judgment, mode,
                               rev0=float(judgment.get("ttm_revenue_override") or rev0_m),
                               fcf_margin=fcfm)
            # 陈旧 XBRL（外国发行人 6-K 无季度框架）下，没有原文重锚的 TTM 会让
            # 全部情景锚在多年前的营收基准上——硬性要求判断层给 override。
            # PENDING_10Q（业绩 8-K 已出、10-Q 未交）同理：不重锚等于用上季度
            # 基准估一家刚发完财报的公司
            if (mode == "standard" and (stale_days > 550 or pending_8k)
                    and not judgment.get("ttm_revenue_override")):
                raise ValueError(
                    ("PENDING_10Q：业绩新闻稿已在 SECTIONS，" if pending_8k
                     else f"XBRL 数据滞后（最新期末 {facts.get('data_latest')}）：")
                    + "必须按财报原文提供 ttm_revenue_override（TTM 营收 $M）与 ttm_revenue_note")
            break
        except (ValueError, json.JSONDecodeError, TypeError, KeyError,
                ZeroDivisionError) as e:
            # TypeError/KeyError/ZeroDivisionError：LLM 把数字写成字符串、scenarios
            # 不是对象、fwd_shares=0 等畸形输出，同样应该带着原因重试而不是让任务崩掉
            last_err = repr(e)
            last_raw = raw
            judgment = None
    if judgment is None:
        raise RuntimeError(f"判断层输出两次校验失败：{last_err}")

    # semantics_version=2（2026-07-22）：v2 一致性规则/诊断红旗/SOTP 降级口径，
    # 仅描述 standard 模式（financials 沿用 v1 情景语义，恒为 1）。
    # manifest_latest 直接进 cfg：bundle 里的 config_假设留档.json 与自动锚同源，
    # 显式 VALUATION_PREV_CONFIG 指向 bundle config 时报告期失效触发器才有指纹可查
    cfg = dict(judgment, ticker=ticker, name=info["name"], date=today,
               price=round(price, 2), mcap=round(mcap / 1e6), shares=shares,
               mode=mode, adr_multiple=adr_multiple,
               currency=facts.get("currency", "USD"),
               semantics_version=2 if mode == "standard" else 1,
               manifest_latest=latest_report)
    # ---- v2 经济合理性复审：engine 干跑一次，red 红旗（假设可修复类）打回判断层
    # 至多一次；复审仍越界则带红旗出报告（fail loud，不 fail hard——红旗区会显示）。
    # 总 claude 调用 <= 3（schema retry 1 + 经济复审 1）。诊断只读 engine 输出的
    # valuation.json，服务层绝不自行重算 DCF 量。
    for gate_attempt in range(2):
        (wd / f"config_attempt{gate_attempt}.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        (wd / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        job["step"] = "engine"
        await _run([PY, str(VAL / "engine.py"), str(wd / "config.json"), str(wd / "facts.json"),
                    str(wd / "valuation.json"), str(fdir / "manifest.csv")], wd)
        val = json.loads((wd / "valuation.json").read_text(encoding="utf-8"))
        reds = [f"[{sc}] {msg}" for sc in ("bear", "base", "bull")
                for lv, msg in (val["scenarios"].get(sc) or {}).get("warnings", [])
                if lv == "red"]
        if not reds or gate_attempt == 1 or mode != "standard":
            break
        job["step"] = "judgment"
        job["detail"] = "经济合理性复审：引擎诊断红旗打回判断层"
        retry_p = (prompt + "\n\n# 你上一次的输出（在此基础上最小修改，其余字段保持原值）\n"
                   + json.dumps(judgment, ensure_ascii=False)
                   + "\n\n# 估值引擎对上述假设的经济合理性红旗\n- "
                   + "\n- ".join(reds)
                   + "\n\n只调整导致红旗的假设（DCF 增速路径/margins/wacc-tg/倍数），"
                     "其余保持原值，重新只输出完整 JSON。若你坚持某项红旗假设，"
                     "必须在 notes 里给出财报原文依据（红旗会随报告展示）。")
        try:
            revised = _parse_json(await _claude(retry_p))
            _validate_judgment(revised, mode,
                               rev0=float(revised.get("ttm_revenue_override") or rev0_m),
                               fcf_margin=fcfm)
            # 陈旧 XBRL / PENDING_10Q 的强制 override 在复审通道同样成立——revised
            # 整体替换 judgment，若复审输出丢掉 override，全部情景会锚回旧营收基准
            if (mode == "standard" and (stale_days > 550 or pending_8k)
                    and not revised.get("ttm_revenue_override")):
                raise ValueError("复审输出丢失 ttm_revenue_override（陈旧 XBRL/PENDING_10Q 下必须保留）")
            judgment = revised
            cfg = dict(judgment, ticker=ticker, name=info["name"], date=today,
                       price=round(price, 2), mcap=round(mcap / 1e6), shares=shares,
                       mode=mode, adr_multiple=adr_multiple,
                       currency=facts.get("currency", "USD"),
                       semantics_version=2 if mode == "standard" else 1,
                       manifest_latest=latest_report)
        except (ValueError, json.JSONDecodeError, TypeError, KeyError,
                ZeroDivisionError, RuntimeError) as e:
            # 复审输出不合格：保留原假设出报告，红旗如实展示——绝不静默吞掉
            job["detail"] = f"复审输出未通过校验（{e!r:.120}），沿用原假设并保留红旗"
            break

    # 连续性锚持久化（v2）：只有 gate-clean（无 red 红旗）的 config 才能成为下次
    # 运行的基准——带病假设冻结成锚会让偏差跨运行复利（方差可见，偏差不可见）。
    # 原子写：避免任务中断留下半个 JSON 毒化后续所有运行
    if (mode == "standard" and not reds
            and not os.environ.get("VALUATION_NO_CONTINUITY")):
        PREV_DIR.mkdir(exist_ok=True)
        _tmp = PREV_DIR / f".{ticker}.json.tmp"
        _tmp.write_text(json.dumps(dict(cfg, manifest_latest=latest_report),
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(_tmp, PREV_DIR / f"{ticker}.json")

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
        # compare.py 的输入就是 valuation.json——不打包它，跨期对比就没有可分发的输入
        zf.write(wd / "valuation.json", "valuation.json")
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
