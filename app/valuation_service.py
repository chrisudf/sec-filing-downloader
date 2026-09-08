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
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import edgar

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from valuation.fetch_segments import build_segments  # noqa: E402 —— 同 segments_service 的摆法

VAL = ROOT / "valuation"
JOBS = ROOT / "jobs"
# 连续性锚存放处（v2）——不能放 jobs/：_cleanup_jobs 按 mtime rmtree，锚活不过 3 天
PREV_DIR = ROOT / "prev_configs"
PY = sys.executable

# 判断层单次调用上限。默认模型 opus 后（v2, 2026-07-22）在 ~45k 字符 prompt 上比
# sonnet 慢，且 v2 一次运行最多 3 次调用（schema retry + 经济复审），放宽到 600s
CLAUDE_TIMEOUT = 600


def _claude_timeout() -> int:
    """判断层超时秒数：VALUATION_CLAUDE_TIMEOUT（正整数秒）覆盖默认 600。

    垃圾值忽略并告警，不许毒化任务：用 int() 而非 float()——姊妹仓的教训是
    float('nan') 能通过 float() 转换，把轮询间隔毒成 NaN 引发 boot loop；
    int() 天然拒绝 'nan'/'inf'/小数字符串，再拦非正数即可。每次调用现读环境，
    与 VALUATION_JUDGMENT_CMD 同法（改环境无需重启进程）。"""
    raw = (os.environ.get("VALUATION_CLAUDE_TIMEOUT") or "").strip()
    if not raw:
        return CLAUDE_TIMEOUT
    try:
        v = int(raw)
    except ValueError:
        v = 0
    if v <= 0:
        print(f"警告: VALUATION_CLAUDE_TIMEOUT={raw!r} 不是正整数秒，"
              f"忽略并使用默认 {CLAUDE_TIMEOUT}s", file=sys.stderr)
        return CLAUDE_TIMEOUT
    return v

# 判断层登录失效的识别（stderr 文本匹配）。Claude Code 的鉴权错误不走独立退出码，
# 只能认文案——命中即归类为 auth，前端据此给「登录」按钮而不是一段红字。
# 认漏的代价只是退回普通错误展示，所以宁可宽一点。
_AUTH_PAT = re.compile(
    r"OAuth|Failed to authenticate|authentication_error|Invalid API key|"
    r"session expired|not logged in|please (?:run )?/?login|credentials",
    re.I)
# 凭证文件（Windows/Linux）。macOS 存 Keychain，此文件不存在属正常——探测一律
# "拿不准就放行"，绝不因为读不到文件而拦住任务。
_CRED_PATH = Path.home() / ".claude" / ".credentials.json"

STEP_LABELS = {
    "preflight": "⓪ 检查判断层登录状态…",
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


def _check_rev_override(d: dict) -> None:
    """ttm_revenue_override 的类型/配对校验——两个模式共用。

    engine 会拿它整体替换情景营收基准（rev0），无出处的覆盖等于让判断层
    静默改写事实层，所以 note 是硬要求。曾经这段只长在 standard 路径上，
    financials 提前 return 直接跳过——共用一个函数是为了不再漂移。"""
    if "ttm_revenue_override" not in d:
        return
    if not _isnum(d["ttm_revenue_override"]) or d["ttm_revenue_override"] <= 0:
        raise ValueError("ttm_revenue_override 必须是正数（$M）")
    if not d.get("ttm_revenue_note"):
        raise ValueError("提供 ttm_revenue_override 时必须附 ttm_revenue_note（出处）")


def _fcfm_for_validation(fcfm: float | None, override,
                         xbrl_rev_m: float | None) -> float | None:
    """校验用 TTM FCF 利润率的口径闸（0019）。

    ttm_revenue_override 只换营收基准；TTM cfo/capex 仍停在旧 XBRL 窗口。override
    偏离 XBRL TTM 营收 >10% 时（TSM 实测：旧口径 FCF 率 19.6% vs 真实 25.8%），
    margins 谷底下限（0.4×当前）与上界（1.2×当前）都锚在过期分母上——把陈旧的
    「当前」当锚比没有锚更糟。此时返回 None，_validate_judgment 自动落回历史年度
    FCF 利润率中位锚（prompt 已在 0013 写明该回退）。<=10% 的偏差属正常季度滚动，
    照用当前 TTM。override 为垃圾值时原样放行——_check_rev_override 会拒绝它，
    这里不抢校验层的活。"""
    if fcfm is None or not _isnum(override) or not override or not xbrl_rev_m:
        return fcfm
    if abs(override / xbrl_rev_m - 1) > 0.10:
        return None
    return fcfm


def _hist_fcfm_median(facts: dict) -> float | None:
    """历史年度 FCF 利润率中位——margins 谷底/上界的备用锚。

    与 engine.hist_fcf_margins 同口径（年度 CFO−capex ÷ 营收），取最近 10 个
    可算财年的中位。抽成函数（0022 C4）：此前只内联在 _pipeline 里，回归工具
    check_configs 根本拿不到这个锚——产线按历史中位放行的 config 被工具用陈旧
    TTM 锚 BLOCK，违反它自己「回归工具比产线严=假警报」的不变量。产线与工具
    必须共用同一实现。"""
    hm = []
    for k in sorted(facts.get("revenue_annual") or {}):
        r = (facts.get("revenue_annual") or {}).get(k)
        c = (facts.get("cfo_annual") or {}).get(k)
        x = (facts.get("capex_annual") or {}).get(k)
        if all(_isnum(v) for v in (r, c, x)) and r:
            hm.append((c - x) / r)
    if not hm:
        return None
    hm = sorted(hm[-10:])
    return hm[len(hm) // 2]


def _isnum(x):
    """数字判据：**排除 bool**。

        isinstance(True, (int, float))  ->  True    # Python 里 bool 是 int 的子类
        _isnum(True)                    ->  False   # 本函数存在的全部理由

    不排除的后果：JSON 里写 `"net_cash_impact_musd": true` 会悄悄通过校验、
    `float(True)` 变成 1.0、再被渲染成 "+1M"。校验器与引擎的金额/序列判断
    本来全踩这个洞（Copilot 在 PR #14 上点出两处，全仓实有 17 处，数处早于本轮）。

    ⚠️ 这段 docstring 自己被误伤过：批量把 `isinstance(..., (int, float))` 换成
    `_isnum(...)` 的正则扫全文件，把这里作为**反例**引用的 isinstance 也改了，
    于是解释变成了字面相反的意思（Copilot 在 PR #15 上点出）。
    改这类东西时正则不要扫注释和文档串。
    """
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _validate_judgment(d: dict, mode: str = "standard",
                       rev0: float | None = None,
                       fcf_margin: float | None = None,
                       band: dict | None = None,
                       hist_fcf_margin: float | None = None,
                       seg_facts: dict | None = None) -> None:
    """LLM 输出的硬校验：结构、边界、margins 长度。不合格直接拒绝重试。

    v2（semantics_version=2, 2026-07-22）新增：
    - 单参数边界（pe/m1/m2/g/g0/gN/margins）——standard 模式此前为零检查，
      是实测跑间漂移（NVDA bear 综合 $29-$77）的直接放大器
    - 跨情景排序（g/opm/pe/m1 须 bear<=base<=bull）
    - 反双重计数：情景盈利已收缩(扩张)时禁止再叠加谷底(峰值)倍数——
      市场对可修复的周期谷底给看穿周期的倍数；判断为永久受损时可设
      permanent_impairment=true + impairment_note（原文出处）豁免
    - margins 谷底下限 0.4×当前 TTM FCF 利润率（同一豁免）
    rev0/fcf_margin/band 由调用方从 facts 传入；为 None 时跳过对应规则。"""
    if mode == "financials":
        _validate_judgment_financials(d)
        return
    # fwd_label 已移出判断层（2026-08-06）：它可由 report_end 纯日期推导，属服务器
    # 注入的事实（见 fwd_window 的 docstring）。仍在此必填会让"要求不要输出"与
    # "缺字段就重试"互相打架，每次运行必然两轮 retry 后失败。
    # other_income_note 与 net_cash_note/adj_note 同级（2026-08-31）：此前
    # other_income 是 need 里**唯一**不用交推导的事实类字段，于是它成了唯一
    # 没有锚的数——AMZN 同日、同财报、同输入的两次运行，其他假设全部靠连续性
    # 机制逐字沿用，唯独它从 1500 漂到 1000（-33%），把 base 推了 0.15%。
    need = ("fwd_shares", "net_cash", "net_cash_note", "adj_ni", "adj_note",
            "other_income", "other_income_note", "seg1", "seg2", "seg1_share",
            "scenarios", "rationale", "notes")
    for k in need:
        if k not in d:
            raise ValueError(f"缺少字段 {k}")
    if not _isnum(d["fwd_shares"]) or d["fwd_shares"] <= 0:
        raise ValueError("fwd_shares 必须为正数（百万股）")
    if not _isnum(d["other_income"]):
        raise ValueError("other_income 必须是数字（$M）")
    # 类型墙（0014）：need 只保证键存在——net_cash="约 5,000"（字符串）、adj_note=null
    # 会穿过校验，烧完 LLM 调用后才崩在 engine（dcf 的 net_cash 加法）/build_report
    # （adj_note[:40] 切片）。financials 分支的 adj_ni _isnum 注释点名的正是这个失败
    # 模式，standard 一直没补。note 类字段镜像 other_income_note 的非空字符串检查。
    for _k in ("net_cash", "adj_ni"):
        if not _isnum(d[_k]):
            raise ValueError(f"{_k} 必须是数字（$M）")
    for _k in ("net_cash_note", "adj_note"):
        if not str(d.get(_k) or "").strip():
            raise ValueError(f"{_k} 必填且须为非空字符串（口径与出处）")
    if not str(d.get("other_income_note") or "").strip():
        raise ValueError(
            "other_income_note 必填：写清从财报哪一行取（通常是『Interest and "
            "other, net』或等价行）、剔除了哪些一次性项目、如何年化。"
            "FACTS 的「OI&E 组件」区已列出本票实际可用的组件序列与逐季 "
            "税前−营业利润 残差行，推导要落在这些数上（标注不可用的序列不要引用）")
    if not _isnum(d["seg1_share"]) or not 0 <= d["seg1_share"] <= 1:
        raise ValueError("seg1_share 必须是 0-1 的数字")
    # 期后资本事件（可选，2026-08-31）：报告期末之后发生的增发/回购/并购/分拆。
    # 引擎**不**用它自动调 net_cash——那需要 buyback/dividends 的 XBRL 抽取可靠，
    # 而实测 META/GOOG 的 buyback、PFE 的 dividends 存在整季空值，自动化会在最
    # 需要它的标的上静默失效。这里只做"声明并留痕"：写了就在红旗区列出并要求
    # 确认 net_cash 已含；龄 >45 天又没写，红旗区提示去核对。
    ppce = d.get("post_period_capital_events")
    if ppce is not None:
        if not isinstance(ppce, list):
            raise ValueError("post_period_capital_events 必须是数组（确认无事件则写 []）")
        for i, e in enumerate(ppce):
            if not isinstance(e, dict) or not {"date", "kind", "amount_musd", "note"} <= set(e):
                raise ValueError(
                    f"post_period_capital_events[{i}] 须含 date/kind/amount_musd/note"
                    "（amount_musd：现金流入为正、流出为负，单位 $M）")
            if not _isnum(e["amount_musd"]):
                raise ValueError(f"post_period_capital_events[{i}].amount_musd 必须是数字（$M）")
            # net_cash_impact_musd 可选但强烈建议：amount_musd 是现金流向，
            # 对 net_cash（现金−负债）的影响未必相同——发债现金 +X、债务 +X、
            # 净现金 0。缺了引擎不会替你猜，只会把合计标成"现金流向、非净现金影响"。
            if ("net_cash_impact_musd" in e
                    and not _isnum(e["net_cash_impact_musd"])):
                raise ValueError(
                    f"post_period_capital_events[{i}].net_cash_impact_musd 必须是数字（$M）"
                    "——该笔对 net_cash(现金−负债) 的影响，与 amount_musd(现金流向) 未必相同")
            # reflected_in_net_cash（v4）可选：布尔确认「该笔最终影响已计入 net_cash」。
            # 有金额的事件全带 true 时引擎把"请确认已计入"黄旗降级为 info 留痕行。
            # 引擎只认布尔 True——"true"（字符串）会静默不生效，宁可在这里拒绝
            if ("reflected_in_net_cash" in e
                    and not isinstance(e["reflected_in_net_cash"], bool)):
                raise ValueError(
                    f"post_period_capital_events[{i}].reflected_in_net_cash "
                    "必须是布尔 true/false（不接受字符串）")
            if not str(e.get("note") or "").strip():
                raise ValueError(f"post_period_capital_events[{i}].note 必填（原文出处）")
    # ppce_note（v4）可选：期后事件与 net_cash 的对账说明一句话，引擎附在 info 行后
    if "ppce_note" in d and not isinstance(d["ppce_note"], str):
        raise ValueError("ppce_note 必须是字符串（期后事件对账的一句话说明）")
    # build_report.py 直接取这些 rationale 键，缺了会在花完 LLM 调用后才崩，这里提前拒绝
    if not isinstance(d["rationale"], dict):
        raise ValueError("rationale 必须是对象")
    for k in ("g", "opm", "pe", "m1", "rl", "wacc"):
        if k not in d["rationale"]:
            raise ValueError(f"rationale 缺少 {k}")
    if not isinstance(d["notes"], list) or not d["notes"]:
        raise ValueError("notes 必须是非空数组")
    # 分部对照闸（0023）：放在 rationale 结构校验之后——本闸的出路就是让判断层
    # 往 rationale.sotp 里写理由，rationale 还不是 dict 时先报那个更根本的错
    _check_seg1_share(d, seg_facts)
    _check_rev_override(d)
    # pe 下限的 band 证据放行：锚纪律命令 base 默认锚子窗 P50，而历史 P50×1.15 < 8
    # 的低倍数票（汽车/能源类，GM/F 历史 NTM P50 约 4.5~7）上，[8,60] 静态下限与锚
    # 互相矛盾——唯一合法值 pe=8 必然吃黄旗或烧光 retry。带子本身就是"该票长期在
    # 8x 以下交易"的证据：此时下限放宽到 max(4, 0.6×锚窗P10)——低于历史 P10 打六折
    # 仍属崩盘定价，照旧走 permanent_impairment 通道。带子缺失时回退静态 8。
    #
    # thin_coverage 必须同门槛（PR #3 review）：0059 起「<250 天的分布没有锚的话语权」
    # 在 band_meta 注入 / pe_band_check / 交易区间 / 中枢检查四处都停用了薄带子，
    # 唯独这里漏了——一个 60 天的低倍数薄样本能把硬下限从 8 放宽到 4，而同一个
    # 带子在 prompt 里被明说"本次无历史锚"。放行与"带子算证据"必须是同一个闸门。
    pe_floor = 8.0
    _bp = ({} if (band or {}).get("thin_coverage") else
           ((band or {}).get("recent") or {}).get("pctiles") or (band or {}).get("pctiles") or {})
    if "10" in _bp and "50" in _bp and _bp["50"] * 1.15 < 8:
        pe_floor = max(4.0, round(0.6 * _bp["10"], 1))
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
        # 静态上界会连"维持现状"的路径都拒绝，两次 retry 撞同一堵墙后硬失败）。
        # `> 0` 不可省：烧钱标的 TTM FCF 率为负（RKLB 实测 -48%），只判真值会算出
        # m_cap = 1.2 × -0.48 = -0.58，与下界 -0.3 组成**空区间**——任何输出都过不了，
        # 两次 retry 后必然硬失败，报错文案还写着一个看不出矛盾的"上界"。
        # 0022（评审 C2/C12）：fcf_margin 为 None（0019 的 override >10% 偏差口径闸，
        # 或回归工具缺 facts）时，上界与谷底下限**一致地**改锚历史年度中位——此前
        # 只有下限换锚、上界静默塌回 0.65，真实 FCF 率 >0.54 的高利润率票连「维持
        # 现状」的 margins 都被拒，两次 retry 撞同一堵墙。负 TTM FCF（烧钱标的）
        # 仍固定 0.65（prompt 契约明写「<=0 固定 0.65」，不在本次换锚范围）。
        if fcf_margin and fcf_margin > 0:
            m_anchor, m_anchor_src = fcf_margin, "当前 TTM FCF 利润率"
        elif fcf_margin is None and hist_fcf_margin and hist_fcf_margin > 0:
            m_anchor, m_anchor_src = hist_fcf_margin, "历史年度 FCF 利润率中位"
        else:
            m_anchor, m_anchor_src = None, ""
        m_cap = max(0.65, min(0.9, 1.2 * m_anchor)) if m_anchor else 0.65
        # 上界含等号，与 prompt / TUNING.md 的 (-0.3, cap] 一致：模型给恰好等于上界
        # 的值（如 0.65）不该被误拒触发无谓 retry。拒绝文案点名本次上界锚在哪
        # （C12：自愿 override 换锚在 prompt 期不可知，retry 必须从拒绝文案获知）
        if not all(_isnum(m) and -0.3 < m <= m_cap for m in s["margins"]):
            raise ValueError(
                f"{sc}.margins 必须是 (-0.3, {m_cap:.2f}] 内的数字（FCF 利润率；上界锚="
                + (f"{m_anchor_src}({m_anchor:.0%})×1.2" if m_anchor else "静态 0.65")
                + "）")
        if not 0.05 <= s["wacc"] <= 0.2:
            raise ValueError(f"{sc}.wacc 越界")
        if s["wacc"] - s["tg"] < 0.045:
            raise ValueError(f"{sc}: wacc-tg 需 >= 0.045")
        # opm 下界为 -1.0 而非 0（2026-08-11）：结构性未盈利标的（RKLB TTM 营业利润率
        # -29%）在 NTM 窗口内不可能翻正，`0 < opm` 让判断层无论怎么答都被拒，两次
        # retry 后硬失败——这不是漂移防线，是把整类标的挡在门外。亏损照实建模，
        # 失效的估值腿由下面的 pe/m1/m2 = 0 显式声明（引擎据此把腿剔出综合）。
        if not -1.0 < s["opm"] < 0.95 or not 0 <= s["tax"] < 0.5:
            raise ValueError(f"{sc}: opm/tax 越界（opm 需在 (-1.0, 0.95)、tax 在 [0, 0.5)）")
        # NTM 盈利符号：PE 法与 SOTP 在盈利为负时数学上失效（负 EPS × 正倍数 = 负目标价，
        # 会静默混进综合）。rev0 缺失时退回按 opm 判断（引擎 op1 与 opm 同号）。
        _rev1 = rev0 * (1 + s["g"]) if rev0 else None
        _op1 = _rev1 * s["opm"] if _rev1 is not None else None
        _pretax1 = _op1 + d["other_income"] if _op1 is not None else None
        _loss_ni = _pretax1 <= 0 if _pretax1 is not None else s["opm"] <= 0
        _loss_op = _op1 <= 0 if _op1 is not None else s["opm"] <= 0
        _imp = (s.get("permanent_impairment") is True
                and str(s.get("impairment_note") or "").strip() != "")
        if _loss_ni:
            # 亏损情景下税率必须为 0：引擎的 ni1 = 税前 × (1-tax) 会把亏损按税率**缩小**，
            # 等于给亏损打折——递延所得税资产不是当期现金流，不在本模型口径内
            if s["tax"] != 0:
                raise ValueError(f"{sc}: NTM 税前为负（营业利润率 {s['opm']:.1%}），"
                                 "tax 必须为 0——(1-tax) 会把亏损按税率缩小")
            if s["pe"] != 0:
                raise ValueError(f"{sc}: NTM 盈利为负，目标 PE 必须写 0（PE 法不适用；"
                                 "负 EPS × 正倍数 = 负目标价）。引擎会把该腿剔出综合")
        elif not (4 if _imp else pe_floor) <= s["pe"] <= 60:
            raise ValueError(
                f"{sc}.pe 需在 [{pe_floor:g}, 60]——低于下限的『目标 PE』属于崩盘/永久受损"
                "定价，须设 permanent_impairment=true + impairment_note（下限放宽至 4）"
                + ("" if pe_floor != 8.0 else
                   "；历史 NTM 带子窗 P50×1.15<8 的低倍数票下限会自动放宽至 max(4, 0.6×子窗P10)"))
        if _loss_op:
            if s["m1"] != 0 or s["m2"] != 0:
                raise ValueError(f"{sc}: NTM 营业利润为负，m1/m2 必须写 0"
                                 "（EV/EBIT 对负 EBIT 不适用）。引擎会把 SOTP 腿剔出综合")
        elif not 0 <= s["m1"] <= 60 or not 0 <= s["m2"] <= 60:
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
    # margins 是 DCF 腿的**全部**输入，与标量参数同权重，但 v2 只排序了标量。
    # 实测坑（INTC 2026-08-30）：net_cash 停在 10-Q 旧时点 → bear 的 P/FCF 红旗是假的
    # → 判断层为消红旗把 bear.margins 从 2%→7% 上修成 4%→11%，越过了 base 的 3%→14%，
    # 前四年 bear >= base。gate 复审只看 red 红旗、不重跑排序，自相矛盾的假设直接进报告。
    # 长度已在上方强制为 10，zip 不会静默截断。
    for _t, (_mb, _ms, _mu) in enumerate(zip(sb["margins"], ss["margins"], su["margins"]), 1):
        if not _mb <= _ms <= _mu:
            raise ValueError(
                f"情景排序：margins 第 {_t} 年必须 bear <= base <= bull"
                f"（现为 {_mb:.0%} / {_ms:.0%} / {_mu:.0%}）。"
                "若 bear 谷底是被『>= 0.4×TTM FCF 利润率』的下限顶上来的，"
                "正确修法是抬高 base/bull 的路径，不是让 bear 越过 base")

    def _exempt(s):
        return (s.get("permanent_impairment") is True
                and str(s.get("impairment_note") or "").strip() != "")

    if rev0:
        eps = {n: _scenario_eps(d, d["scenarios"][n], rev0)
               for n in ("bear", "base", "bull")}

        def _forced_zero_keys(s):
            """亏损协议（上方逐情景块）强制写 0 的倍数键——同一判据同一式。

            0022（评审 C3）：0013 的 prompt 明写「营业亏损叠加大额利息收入可以
            税前为正——此时 m1/m2 仍须写 0，而 pe 照常规边界给」，而反双重计数
            此前对这个 split 形态照打「请上调 bear.m1」——上调又被「营业利润为负，
            m1/m2 必须写 0」拒绝，诚实配置无解、两次 retry 烧光后硬失败。被协议
            钉死为 0 的键不是判断层的假设，剔出双重计数检查。pe 无需在此豁免：
            pe=0 只在税前为负时强制，而那时情景 EPS 也为负，eps>0 闸已跳过。"""
            _r1 = rev0 * (1 + s["g"])
            return {"m1", "m2"} if _r1 * s["opm"] <= 0 else set()

        if eps["base"] > 0:
            r_bear, r_bull = eps["bear"] / eps["base"], eps["bull"] / eps["base"]
            # 比较两侧任一方被强制 0 都跳过：base 被强制 0 时比例检查同样失去意义
            # （bear 0<0.6×0 恒假无害，但 bull >1.4×0 会把合法的正倍数误判成双重计数）
            _skip_bear = _forced_zero_keys(sb) | _forced_zero_keys(ss)
            _skip_bull = _forced_zero_keys(su) | _forced_zero_keys(ss)
            # m2 仅在真双分部（次分部倍数非 0）时参与——它在 seg1_share<0.85 时
            # 承担近半 SOTP 权重，同样是独立采样漂移通道
            for key in (("pe", "m1", "m2") if sb.get("m2", 0) > 0 else ("pe", "m1")):
                # 亏损情景的倍数按规定写 0（见上），此时 r<0.8 与「倍数 < 0.6×base」
                # 恒同时成立——反双重计数会把"倍数腿已声明失效"误报成漂移
                if (eps["bear"] > 0 and r_bear < 0.8 and key not in _skip_bear
                        and sb[key] < 0.6 * ss[key] and not _exempt(sb)):
                    raise ValueError(
                        f"bear 双重计数：情景盈利已较 base 收缩至 {r_bear:.0%}，{key} 又 "
                        f"< 0.6×base——谷底盈利×谷底倍数会把周期惩罚计两次。请上调 bear.{key}"
                        "（市场对可修复的谷底给看穿周期的倍数），或判断为永久受损时设 "
                        "permanent_impairment=true 并在 impairment_note 给原文出处")
                if (eps["bull"] > 0 and r_bull > 1.25 and key not in _skip_bull
                        and su[key] > 1.4 * ss[key]):
                    raise ValueError(
                        f"bull 双重计数：情景盈利已较 base 扩张至 {r_bull:.0%}，{key} 又 "
                        f"> 1.4×base——景气顶点市场收敛倍数而非扩张。请下调 bull.{key}")
    # 锚的选择（2026-08-31）：capex 周期股的当期 FCF 可以为负（AMZN TTM −1.5%），
    # 原写法 `fcf_margin > 0.02` 不成立就**整条规则跳过**——而 FCF 为负恰恰是 DCF
    # 最不可靠的时候，护栏在最需要它的时候关掉了。负/近零时退到历史年度中位数
    # （AMZN 八个财年中位 5.4%），两者都不可用才真正放行。
    anchor, anchor_src = None, ""
    if fcf_margin and fcf_margin > 0.02:
        anchor, anchor_src = fcf_margin, "当前 TTM FCF 利润率"
    elif hist_fcf_margin and hist_fcf_margin > 0.02:
        anchor, anchor_src = hist_fcf_margin, "历史年度 FCF 利润率中位"
    if anchor:
        floor = 0.4 * anchor
        for n in ("bear", "base", "bull"):
            s = d["scenarios"][n]
            if min(s["margins"]) < floor and not _exempt(s):
                raise ValueError(
                    f"{n}.margins 谷底 {min(s['margins']):.0%} < 0.4×{anchor_src}"
                    f"({anchor:.0%})——比腰斩更深的常态化路径属于永久受损假设，"
                    "请抬高谷底或设 permanent_impairment=true + impairment_note")


def _validate_judgment_financials(d: dict) -> None:
    """金融股（银行/券商/fintech）判断层校验：P/E + P/TBV 假设集，无 DCF margins。

    v3（fin semantics_version=3, 2026-09-06）：standard 在 v2 就补的两道墙
    financials 一直没有——SOFI 实测一次运行零警告发货。
    - 跨情景排序（g/nm/pe/ptbv 须 bear<=base<=bull）：此前 bear.pe>bull.pe、
      倒挂的 g 全部放行，正是 v2 给 standard 修掉的那类漂移放大器
    - 亏损协议：nm 旧下界 0 让「刚扭亏 fintech 的 P20 bear 现实上是亏损」无法
      表达，prompt 还推着模型『压到微利』——假微利 × 15-30x PE 会静默混进综合
      （standard 的 COIN 微利除法事故同型，见 engine.py 近零利润守卫）。v3 起
      nm 下界放宽到 -0.5，nm<=0 的情景必须 pe=0（引擎把 PE 腿标 n.m. 剔出综合，
      综合退化为 P/TBV 单腿）；0<nm<1% 的微利由引擎打黄旗，不在此拒绝。"""
    # fwd_label 同 standard：已改为服务器注入，不再向判断层索要
    need = ("fwd_shares", "adj_ni", "adj_note", "scenarios", "rationale", "notes")
    for k in need:
        if k not in d:
            raise ValueError(f"缺少字段 {k}")
    # 顶层数值校验（与 standard 一致）：engine financials 分支直接 ni1/fwd_shares、
    # adj_ni/shares——fwd_shares 若为 "100"（字符串）会 TypeError、为 0 会 ZeroDivisionError，
    # 在花完 LLM 调用后才在引擎阶段崩，这里提前拒绝
    if not _isnum(d["fwd_shares"]) or d["fwd_shares"] <= 0:
        raise ValueError("fwd_shares 必须为正数（百万股）")
    if not _isnum(d["adj_ni"]):
        raise ValueError("adj_ni 必须是数字（$M）")
    if not isinstance(d["rationale"], dict):
        raise ValueError("rationale 必须是对象")
    for k in ("g", "nm", "pe", "ptbv", "wacc"):
        if k not in d["rationale"]:
            raise ValueError(f"rationale 缺少 {k}")
    if not isinstance(d["notes"], list) or not d["notes"]:
        raise ValueError("notes 必须是非空数组")
    _check_rev_override(d)
    for sc in ("bear", "base", "bull"):
        if sc not in d["scenarios"]:
            raise ValueError(f"缺少情景 {sc}")
        s = d["scenarios"][sc]
        for k in ("g", "nm", "pe", "ptbv", "wacc", "tg"):
            if k not in s or not _isnum(s[k]):
                raise ValueError(f"{sc} 缺少数值字段 {k}")
        if not -0.5 < s["g"] < 1.5:
            raise ValueError(f"{sc}.g 越界（需在 (-0.5, 1.5)）")
        # 下界 -0.5：比「总净收入的一半都亏掉」更深的 NTM 亏损属于崩溃定价，
        # 不是情景假设——与 standard 的 opm 下界哲学一致
        if not -0.5 < s["nm"] < 0.6:
            raise ValueError(f"{sc}.nm（净利率）需在 (-0.5, 0.6)")
        if s["nm"] <= 0:
            # 亏损情景：负 EPS × 正倍数 = 负目标价会静默混进综合。倍数腿失效
            # 必须显式声明（与 standard 亏损时 pe=0 的约定同构）
            if s["pe"] != 0:
                raise ValueError(
                    f"{sc}: NTM 净利率为负或零（{s['nm']:.1%}），目标 pe 必须写 0"
                    "（PE 法不适用；负 EPS × 正倍数 = 负目标价）。引擎会把该腿剔出综合")
        elif not 1 <= s["pe"] <= 60:
            raise ValueError(f"{sc}.pe 需在 [1, 60]")
        if not 0.2 <= s["ptbv"] <= 8:
            raise ValueError(f"{sc}.ptbv 需在 [0.2, 8]")
        if not 0.05 <= s["wacc"] <= 0.25:
            raise ValueError(f"{sc}.wacc 越界（需在 [0.05, 0.25]）")
        if s["wacc"] - s["tg"] < 0.045:
            raise ValueError(f"{sc}: wacc-tg 需 >= 0.045")

    # ---- v3 跨情景一致性（镜像 standard 的 v2 块）：拦『所有参数同取极端』----
    # 亏损协议与排序自洽：亏损情景 pe=0 天然 <= 盈利情景的正 pe，无须豁免
    sb, ss, su = d["scenarios"]["bear"], d["scenarios"]["base"], d["scenarios"]["bull"]
    for k in ("g", "nm", "pe", "ptbv"):
        if not sb[k] <= ss[k] <= su[k]:
            raise ValueError(f"情景排序：{k} 必须 bear <= base <= bull")


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


class JudgmentAuthError(RuntimeError):
    """判断层未登录/登录失效——与普通失败区分开，前端据此给「登录」按钮。"""


def _auth_state() -> dict:
    """尽力而为地判断本机 Claude Code 是否还登录着。

    返回 {"ok": bool, "reason": str}。**只在能确凿判定"没登录"时返回 False**：
    读不到文件、格式不认识、macOS 走 Keychain——一律 ok=True 放行，让真正的
    调用去报错。探测的价值是"1 秒失败"而不是"替 CLI 做鉴权"，误拦比漏拦贵得多。
    """
    try:
        if not _CRED_PATH.exists():
            return {"ok": True, "reason": "无凭证文件（macOS 走 Keychain，或用 API key）"}
        d = json.loads(_CRED_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 —— 探测失败不许影响主流程
        return {"ok": True, "reason": f"凭证探测跳过（{e!r:.80}）"}
    oauth = d.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return {"ok": True, "reason": "凭证结构不认识，交给 CLI 判断"}
    # 实测过的失效形态：token 被清空成 ""、expiresAt 归 0。此时 CLI 报
    # "OAuth session expired and could not be refreshed"——没有 refresh token
    # 可用，refresh 无从谈起，只能重新登录
    if not oauth.get("accessToken") and not oauth.get("refreshToken"):
        return {"ok": False, "reason": "凭证已被清空（accessToken/refreshToken 均为空）"}
    exp = oauth.get("refreshTokenExpiresAt")
    if _isnum(exp) and exp and exp / 1000 < time.time():
        return {"ok": False, "reason": "refresh token 已过期"}
    return {"ok": True, "reason": "凭证在位"}


def _login_argv() -> list[str] | None:
    """弹出一个交互式终端跑 `claude /login`。返回 None 表示本平台不支持自动弹窗。

    命令是写死的（只嵌入 _find_claude() 的结果），不接受任何请求参数——这个端点
    只在本机开发服务里存在，但仍然不给它拼接外部输入的机会。
    """
    exe = _find_claude()
    if os.name == "nt":
        # start 需要一个标题占位参数，否则带引号的路径会被当成标题
        return ["cmd", "/c", "start", "Claude 登录", "cmd", "/k", exe, "/login"]
    if sys.platform == "darwin":
        return ["osascript", "-e",
                f'tell application "Terminal" to do script "{exe} /login"',
                "-e", 'tell application "Terminal" to activate']
    for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        if shutil.which(term):
            return [term, "-e", f"{exe} /login"]
    return None


async def _claude(prompt: str) -> str:
    # VALUATION_JUDGMENT_CMD 可替换判断层命令（测试注入 / 将来切 Anthropic API）
    # VALUATION_MODEL 可换判断层模型：opus(默认) / sonnet / fable，或完整模型 ID。
    # 默认 opus（v2, 2026-07-22）：A/B 实测 sonnet 判断层同输入 4 次采样 base 目标价
    # 全距 25.7%（CV 11.2%），强模型 CV≈3.5%——假设质量是这条管线的地板，判断层
    # 频次低（每标的每季 1-3 次调用），用最强模型的成本可忽略；与 judge_openai_compat
    # 的默认（claude-opus-4-8）对齐。要快可显式 VALUATION_MODEL=sonnet
    cmd = os.environ.get("VALUATION_JUDGMENT_CMD")
    custom = bool(cmd)
    if not cmd:
        model = os.environ.get("VALUATION_MODEL", "opus")
        cmd = f'"{_find_claude()}" -p --model {model}'
    label = "判断层命令（VALUATION_JUDGMENT_CMD）" if custom else "claude -p"
    timeout = _claude_timeout()
    proc = await asyncio.create_subprocess_shell(
        cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{label} 超时（{timeout}s）")
    if proc.returncode != 0:
        msg = (err or out).decode("utf-8", "ignore")[-500:]
        # 登录失效单独成类：它是"去点一下登录"就能解决的状态问题，不该和
        # "模型输出跑偏"混在同一段红字里，前端要据此给按钮。
        # 只对本机 claude CLI 生效（0016）：自定义命令的 stderr 撞上 _AUTH_PAT
        # （比如自家网关打印 "Invalid API key"）会被误译成「本机未登录」，
        # 前端给出的「登录」按钮修不了它——自定义命令的失败按普通失败呈现
        if not custom and _AUTH_PAT.search(msg):
            raise JudgmentAuthError("判断层未登录或登录已失效: " + msg)
        raise RuntimeError(f"{label} 失败: " + msg)
    return out.decode("utf-8", "ignore")


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出中没有 JSON 对象")
    return json.loads(text[start:end + 1])


def _add_months(d: date, n: int) -> date:
    """加 n 个月，落在不存在的日期时退到当月最后一天（3-31 + 1月 -> 4-30）。

    **月末进月末**（end-of-month 约定，2026-08-17 修）：输入本身是所在月最后一天时，
    结果取目标月最后一天。此前只做"日不存在才回退"，于是 9-30 + 3月 = 12-30 而不是
    12-31——日历季末的公司做 PENDING_10Q 前滚一个季度后，TTM 末端差了一天，
    fwd_window 的财年对齐判据就认不出"NTM 正好是下一个完整财年"（实测
    Dec-FY 公司 Q3 前滚后被判 aligned=False，窗口写成 2026-12-31~2027-12-30）。
    非月末输入（AAPL 这类 52/53 周财历的 6-27）不受影响，行为逐位不变。

    只用 date/timedelta（不引 calendar）：tests/test_pure.py 按 AST 抽取本函数源码后
    在只注入这两个名字的命名空间里 exec，多一个模块级依赖就会 NameError。
    """
    def _eom(yy: int, mm: int) -> date:      # 该年月的最后一天
        return date(yy + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1)

    y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
    m += 1
    last_out = _eom(y, m).day
    if d.day == _eom(d.year, d.month).day:
        return date(y, m, last_out)
    return date(y, m, min(d.day, last_out))


def fwd_window(ttm_end: str, fy_end: str, rolled_quarters: int = 0) -> dict:
    """推导本次估值的**前瞻期**，返回 {start, end, label, straddle, aligned}。

    为什么这是"事实"而不是"判断"
    ----------------------------
    engine.py 算的是 `rev1 = rev0 × (1+g)`，而 `rev0` 恒为 **TTM 营收**。
    所以前瞻期在数学上永远是「TTM 窗口末端起的未来 12 个月」(NTM)，
    **不是任何一个财年**——只有 report_end 恰好等于财年末时两者才重合。

    此前 fwd_label 由判断层自己填、prompt 又要求"按公司财年"，与 g 的
    "vs TTM"定义直接冲突。冲突只在 report_end ≠ 财年末时暴露：
    AMZN（12月财年）2026-06-30 报告期跑 3 次，1 次填 FY2026E、2 次填 FY2027E，
    差整整一年增长，g 跟着裂成 0.06/0.13/0.21，三个目标价完全不可比。
    MSFT 报告期恰是财年末，两定义重合，所以 4 次全稳——问题被掩盖了很久。

    既然完全可由 report_end 推导，它就属于"服务器注入的事实"（与 price/shares
    同类），不该经过 LLM。这样失败模式从构造上消失，无需 schema 校验与 retry。

    rolled_quarters
    ---------------
    PENDING_10Q（业绩 8-K 已出、10-Q 未交）时判断层被强制给 ttm_revenue_override，
    TTM 基准已前滚一个季度，窗口要跟着 +3 个月。外国发行人 XBRL 陈旧那条路径
    （stale_days>550）滚了几个季度无从得知，此处不滚并在 straddle 里注明——
    见 TODO「前瞻期口径」条，那是本函数已知的最大近似。
    """
    te = date.fromisoformat(ttm_end)
    if rolled_quarters:
        te = _add_months(te, 3 * rolled_quarters)
    start, end = te + timedelta(days=1), _add_months(te, 12)
    out = {"start": start.isoformat(), "end": end.isoformat()}

    # 财年末对齐判断：TTM 窗口正好是一个完整财年时，NTM 也就正好是下一个。
    #
    # 不能要求月-日**精确**相等（2026-08-17 修）：52/53 周财历的公司（AAPL 财年末
    # = 9 月最后一个周六）每年的财年末日期本身就在漂移，2025 是 09-27、2026 是
    # 09-26——精确比较会把"这份 TTM 就是完整 FY2026"判成不对齐，label 里写成横跨
    # 两个财年，判断层于是拿到一个自相矛盾的前瞻期口径。容差取 ±4 天：52/53 周
    # 财历相邻年度最多差 7 天但同向漂移通常 1-2 天，4 天足够覆盖且远小于一个季度，
    # 不会把真正横跨的窗口（差一个季度以上）误判成对齐。
    _aligned = False
    if fy_end:
        try:
            _fy = date.fromisoformat(fy_end)
            # 把财年末搬到 te 所在年份再比，避免跨年比较把 12-31 vs 01-01 算成差 364 天
            for _y in (te.year, te.year - 1, te.year + 1):
                try:
                    if abs((te - _fy.replace(year=_y)).days) <= 4:
                        _aligned = True
                        break
                except ValueError:
                    continue    # 2-29 搬到平年
        except ValueError:
            _aligned = fy_end[5:] == te.isoformat()[5:]
    if _aligned:
        out["aligned"] = True
        out["straddle"] = f"= FY{end.year} 完整财年"
    else:
        out["aligned"] = False
        if fy_end:
            fy_md = fy_end[5:]
            # 窗口起点/终点各自落在哪个财年（财年以其结束日命名）
            fy1 = start.year if start.isoformat()[5:] <= fy_md else start.year + 1
            fy2 = end.year if end.isoformat()[5:] <= fy_md else end.year + 1
            out["straddle"] = (f"横跨 FY{fy1} 后段 + FY{fy2} 前段"
                               if fy1 != fy2 else f"落在 FY{fy1} 之内")
        else:
            out["straddle"] = "财年末未知"
    out["label"] = f"NTM {start:%Y-%m}~{end:%Y-%m}（{out['straddle']}）"
    return out


def _fcf_hist_lines(facts: dict) -> list[str]:
    """服务器实算的年度 FCF 利润率表（终值 margins 的锚）-> 注入 prompt 的行。

    prompt 清单 #5 命令判断层「锚在近十年 FCF 利润率区间」，而引擎
    （terminal_margin_warnings）与校验层（hist_fcf_margin）各自都算了这张表，
    唯独判断层一个数也拿不到——只能徒手从 FACTS 摘要外的口径拼凑，算错了
    引擎事后才打旗。窗口与 engine.hist_fcf_margins 严格同口径：revenue/cfo/capex
    三项齐全的财年取尾 10 个。覆盖缺口必须诚实标出：NVDA 的 capex_annual
    缺 FY13-21，只写「近十年」会让判断层高估这张表的覆盖。"""
    rev = facts.get("revenue_annual") or {}
    cfo = facts.get("cfo_annual") or {}
    cap = facts.get("capex_annual") or {}
    hist = []
    for k in sorted(rev):
        r, c, x = rev.get(k), cfo.get(k), cap.get(k)
        if all(_isnum(v) for v in (r, c, x)) and r:
            hist.append((k, r, c, x, (c - x) / r))
    window = hist[-10:]
    in_window = {h[0] for h in window}
    # 近 10 个营收财年里三项不齐的逐年点名缺什么——5 年覆盖不许被当成 10 年用
    gaps = []
    for k in sorted(rev)[-10:]:
        if k in in_window:
            continue
        miss = "/".join(n for n, v in (("cfo", cfo.get(k)), ("capex", cap.get(k)))
                        if not _isnum(v)) or "营收"
        gaps.append(f"{k[:4]}缺{miss}")
    if not window:
        return ["年度 FCF 利润率表（服务器实算）：无任何财年 revenue/cfo/capex 三项齐全"
                "——近十年 FCF 锚不可用，margins 路径须在 rationale.dcf_margin 写明替代依据"
                + (f"（{'; '.join(gaps)}）" if gaps else "")]
    lines = ["年度 FCF 利润率表（服务器实算 =(CFO−capex)÷营收，$M——终值 margins 的锚，"
             "引擎按同口径峰值打旗）:"]
    for k, r, c, x, m in window:
        lines.append(f"  {k}: 营收 {r/1e6:,.0f} / CFO {c/1e6:,.0f} / "
                     f"capex {x/1e6:,.0f} / FCF率 {m:.1%}")
    ms = sorted(m for *_, m in window)
    peak = max(window, key=lambda h: h[4])
    lines.append(f"  ← 覆盖 {len(window)} 个财年（{window[0][0]}~{window[-1][0]}）"
                 + (f"，缺口: {'; '.join(gaps)}——「近十年」按实际覆盖理解" if gaps else "")
                 + f"；峰值 {peak[4]:.1%}（{peak[0][:4]}）、中位 {ms[len(ms) // 2]:.1%}")
    return lines


# OI&E 组件的 (facts 键, 中文标签)。顺序即展示顺序，与 fetch_facts.SPEC 的
# 营业外组件区一一对应——加减组件两处要一起动
_OIE_KEYS = (("interest_income", "利息收入"),
             ("interest_expense_nonop", "利息支出(非经营)"),
             ("other_nonop", "其他非经营"),
             ("equity_inv_gain", "股权投资重估"),
             ("fx_gain", "汇兑损益"))


def _oie_lines(facts: dict) -> list[str]:
    """OI&E（营业外损益）组件矩阵 + 逐季「税前−营业利润」残差行 -> 注入 prompt 的行。

    other_income_note 的校验与 prompt 都要求推导「落在这些序列上」，此前却一个数
    也不注入；且组件标签覆盖极不均（AAPL 的 interest_income 整列为空），沉默会让
    判断层以为"说好的序列"都在。残差 = 税前 − 营业利润，是任何票都可推的
    other_income 推导基（实测多个判断层 agent 只能自己徒手减这两列）。
    不存在的序列显式点名——绝不许诺没有的数据。"""
    qkeys = list(facts.get("revenue_quarterly") or {})[-8:]   # 与「季度尾8」同窗同序
    akeys = list(facts.get("revenue_annual") or {})[-6:]

    def _fmt(v):
        return f"{v/1e6:+,.0f}" if _isnum(v) else "—"

    lines = ["OI&E 组件（营业外损益，$M——other_income 的推导基础）:"]
    pre_q = facts.get("pretax_income_quarterly") or {}
    op_q = facts.get("op_income_quarterly") or {}
    resid = []
    for k in qkeys:
        p, o = pre_q.get(k), op_q.get(k)
        resid.append(f"{k} {_fmt(p - o) if _isnum(p) and _isnum(o) else '?'}")
    resid_ok = any(not r.endswith("?") for r in resid)
    avail = [(k, lab) for k, lab in _OIE_KEYS
             if (facts.get(k + "_quarterly") or facts.get(k + "_annual"))]
    # 双缺口先裁（0022 C8）：残差与全部组件同时缺席时，旧文案两条 ⚠ 各指对方当
    # 回退（"以下方组件序列为准" vs "以残差行为准"）——互相甩锅，唯一可用的
    # 财报原文反而没被点名为主路径。回退指向必须只指真实存在的通道。
    if resid_ok:
        lines.append("  逐季残差 税前−营业利润（=利息+其他收益合计，最可靠的推导基）: "
                     + ", ".join(resid))
    elif avail:
        lines.append("  ⚠ 本票无 pretax_income XBRL 季度序列——残差行不可用，"
                     "other_income 推导以下方组件序列与财报原文为准")
    else:
        lines.append("  ⚠ 本票无 OI&E 结构化序列（税前利润残差行与全部组件序列均缺）"
                     "——other_income 需从财报原文推导，并在 other_income_note "
                     "写明原文出处；不要引用任何 XBRL 序列")
    if avail:
        lines.append("  组件序列（季8 列序同上；— = 该期无值）:")
        for k, lab in avail:
            q = facts.get(k + "_quarterly") or {}
            a = facts.get(k + "_annual") or {}
            lines.append(f"    {lab}[{k}] 季8: "
                         + " ".join(_fmt(q.get(kk)) for kk in qkeys)
                         + " | 年6: "
                         + " ".join(f"{kk[:4]}:{_fmt(a.get(kk))}" for kk in akeys))
    missing = [lab for k, lab in _OIE_KEYS
               if not (facts.get(k + "_quarterly") or facts.get(k + "_annual"))]
    # 双缺口时上面那条一致行已把话说完，缺失清单不再重复出（全缺 = 清单即全集）
    if missing and (resid_ok or avail):
        lines.append("  ⚠ 本票无以下 XBRL 序列：" + "、".join(missing)
                     + "——不要引用不存在的序列，推导以"
                     + ("残差行" if resid_ok else "上方组件序列")
                     + "与财报原文为准")
    return lines


def _compact_facts(facts: dict) -> str:
    """给判断层的事实摘要：年度尾6 + 季度尾8（含 净利/营业利润 比，暴露一次性项目）
    + 年度 FCF 利润率表 + OI&E 组件与残差行（v4 注入）。"""
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
    lines += _fcf_hist_lines(facts)
    lines.append("季度尾8 (期末: 营收/营业利润/净利 | 净利÷营业利润——比值异常=有一次性项目):")
    for k in list(facts["revenue_quarterly"])[-8:]:
        op = facts["op_income_quarterly"].get(k, 0)
        ni = facts["net_income_quarterly"].get(k, 0)
        ratio = f"{ni/op:.2f}" if op else "n/a"
        lines.append(f"  {k}: {facts['revenue_quarterly'][k]/1e6:,.0f} / {op/1e6:,.0f} / "
                     f"{ni/1e6:,.0f} | {ratio}")
    lines += _oie_lines(facts)
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


def _fwd_meta(fwd: dict | None, force_override: bool) -> str:
    """前瞻期(NTM)窗口的注入文案 -> prompt 段。纯函数（0014 抽出，可直接单测）。

    g 分母的契约必须跟着 override 走：PENDING_10Q / 陈旧 XBRL（stale_days>550）下
    校验层与引擎都强制以 ttm_revenue_override 为营收基准（rev0 整体换基），此前
    这里却硬写「TTM 即 FACTS 里的口径」——判断层照文档把 g 锚在旧 TTM 上，
    与 caliber 注入的「g 锚定在 override 基准上」自相矛盾，且 8-K 已滚进 override
    的那个季度会被再乘一次增速（前瞻窗口也已 +3 个月，两头都不该用旧分母）。
    force_override 与注入/强制 override 的门禁严格同源（pending_8k 或 stale>550）。
    """
    if not fwd:
        return ""
    denom = ("分母 = 你输出的 ttm_revenue_override（按财报原文前滚后的 TTM），"
             "**不是** FACTS 里的旧 TTM——校验层与引擎都按 override 整体换基"
             if force_override else "TTM 即 FACTS 里的口径")
    return (
        f"\n前瞻期(NTM)={fwd['start']}~{fwd['end']}（{fwd['straddle']}）"
        "\n  ← g / opm / tax / fwd_shares **全部针对这个 12 个月窗口**，不是某个财年。"
        f"\n  g 的定义 = 该窗口营收 ÷ TTM营收 − 1（{denom}）。"
        + ("\n  该窗口与公司财年重合，可直接套用财年指引。" if fwd["aligned"] else
           "\n  ⚠ 该窗口**不等于**任何一个财年：财报 guidance 与卖方一致预期都按财年给，"
           "必须先换算到这个窗口再定 g，不要直接搬用财年数字。")
        + "\n  fwd_label 由服务器生成，你不要输出该字段。")


def _band_meta(mode: str, facts: dict) -> str:
    """历史带锚注入文案（standard=NTM PE 带 / financials=P/TBV 带）。

    此前 band 只做 engine 事后 check，目标 PE 由判断层自由拍——与「前瞻 EPS ×
    历史 PE 带」类参考基准相比，base 水平会系统性漂移（漂移方向随判断层口味，
    且无证据可审计）。注入分位数并要求默认锚近 3 年子窗 P50：全窗把 2021 零利率
    regime 原样计入（AMZN 全窗 P50≈31x），直接锚全窗会把泡沫倍数抬进锚。
    与连续性纪律同哲学：无证据不偏离。
    三种形态都必须有话（0008）：有带给锚；thin_coverage 给「本次无历史锚」；
    带**整体缺席**（构建失败：外国发行人无季度 XBRL——TSM 实测、季节性剔穿、
    新上市）此前 band_meta=''，判断层既无锚也不知道无锚，自由发挥还以为有历史
    背书——缺席与薄覆盖同等处置，并把 pe_band_error 的原因一起给出。
    纯函数（mode+facts 进、文案出），便于直接单测。
    """
    band_meta = ""
    _b = facts.get("pe_band") or {}
    if mode == "standard" and _b.get("thin_coverage"):
        # 覆盖不足：不给锚（薄样本的 P50 没有话语权），但显式告诉判断层"没有锚"——
        # 沉默会让它自由发挥还以为有历史背书
        band_meta = (f"\n历史 NTM PE 带覆盖不足（仅 {_b.get('days')} 个交易日/"
                     f"{_b.get('years')} 年，原始数据缺口）——**本次无历史锚**：目标 PE 按"
                     "基本面第一性与可比经验判断，并在 rationale.pe 写明定价依据。")
    elif mode == "standard" and _b.get("pctiles"):
        _rc = _b.get("recent") or {}
        _rp = _rc.get("pctiles") or {}

        def _pfmt(pp):
            return " / ".join(f"P{q} {pp[str(q)]:.1f}x" for q in (10, 25, 50, 75, 90)
                              if str(q) in pp)
        _anchor_win = f"近{_rc['years']}年" if _rp else f"近{_b['years']}年"
        band_meta = (
            f"\n历史已实现 NTM PE 带（与上述前瞻期同口径，basis={_b['basis']}，"
            "一次性畸变窗口已剔除）："
            f"\n  全窗近{_b['years']}年: {_pfmt(_b['pctiles'])}（{_b['days']}天）"
            + (f"\n  近{_rc['years']}年子窗: {_pfmt(_rp)}（{_rc['days']}天）" if _rp else "")
            + f"\n  ← base 情景目标 PE **默认锚{_anchor_win} P50**；bear/bull 参照"
              " P25/P75 量级再叠加各自情景的盈利假设（量级参照，不受下述 ±15% 纪律"
              "约束）。**base** 偏离 P50 ±15% 以上必须在 rationale.pe 给出财报证据"
              "（增长/利润率结构变化、资本回报变化等），『保守起见』类无证据折价"
              "不接受——那会系统性压低所有标的。"
              "\n  若下方给出「上一次运行的假设（连续性基准）」，连续性优先——"
              "本锚只约束首次基线与失锚重建。")
        # 高倍数票的锚-上界死锁（0013）：锚纪律命令 base 锚锚窗 P50，而校验层硬上界
        # pe<=60（TSLA 锚窗 P50 ~230x、ISRG 61.6x 实测）——照锚必被拒、照上界又吃
        # 带偏离黄旗，两条纪律打架烧光 retry。上界不动（>60x 的"目标 PE"更多是动量
        # 而非估值），死锁从两端拆：这里预告封顶指令，engine.pe_band_check 对封顶值
        # 豁免偏离旗（同一条件，两处必须同门槛 60）
        _a50 = (_rp if _rp else _b["pctiles"]).get("50")
        if _isnum(_a50) and _a50 > 60:
            band_meta += (
                f"\n  ⚠ 锚窗 P50 {_a50:.1f}x 超出校验上界 pe<=60——按上界 60 封顶给出，"
                "并在 rationale.pe 说明封顶事实；封顶导致的带下沿偏离引擎已豁免，"
                "不追究，不要为凑纪律去压低其他假设。")
        # 滞后必须告知判断层（2026-08-17）：ntm 口径要求"该日之后满 4 个季度已披露"，
        # 最近约一年结构性无值。此前只把天数注进去，锚看起来像"截至今天的近3年"，
        # 于是判断层在市场已经重新定价的标的上照旧锚旧中枢却毫不知情（MSFT 实测：
        # 锚 P50 30.1x，而市场最近 10 个月付的 NTM 可比倍数只有 ~22x）。
        # 这里只给事实（滞后天数 + 无滞后 trailing 对照），不放松 ±15% 纪律——
        # "市场已重定价"是一类**合法证据**，但仍要在 rationale.pe 里写出来。
        _sp = _b.get("span") or {}
        if _sp.get("lag_days"):
            band_meta += (
                f"\n  ⚠ 本带止于 {_sp['end']}（滞后 {_sp['lag_days']} 天）："
                "ntm 口径的分母是「该日之后 12 个月**实际实现**的 EPS」，那个未来对最近"
                "约一年的交易日还没发生，因此**最近约一年的倍数不在本分布内**。")
            _tn = _b.get("trailing_nolag") or {}
            _tnp = {str(k): v for k, v in (_tn.get("pctiles") or {}).items()}
            # NaN 兜底（源头已在 pe_band 剔除 NaN 收盘；这里防旧 facts.json 回放）：
            # NaN 是 truthy，裸 truthiness 闸门会把「最新 nanx」注进判断层 prompt。
            # x == x 是唯一可靠的 NaN 判据（band_lag_warnings 同法）
            _tn50 = _tnp.get("50")
            _tn50 = _tn50 if _isnum(_tn50) and _tn50 == _tn50 else None
            _tncur = _tn.get("current")
            _tncur = _tncur if _isnum(_tncur) and _tncur == _tncur else None
            if _tn50:
                band_meta += (
                    f"\n    无滞后对照（trailing 口径，价÷过去12个月，{_tn['span']['start']}~"
                    f"{_tn['span']['end']}）：P50 {_tn50:.1f}x"
                    + (f"，最新 {_tncur:.1f}x" if _tncur is not None else "（最新值缺失）")
                    + "。"
                    "**不可与上面的 NTM 分位直接相减**——trailing 分母是过去 12 个月的"
                    "已实现 GAAP EPS，NTM 分位的分母是未来 12 个月的 EPS。"
                    "换算需要除以 **EPS 增速因子**（你给出的该情景 NTM EPS ÷ 当前 GAAP "
                    "TTM EPS），**不是营收增速 g**——利润率、税率、其他收益、股数变化"
                    "都会让 EPS 增速与营收增速显著分叉（利润率扩张叠加回购的票尤其）。"
                    "当前 GAAP TTM EPS 见 FACTS 的 TTM 净利 ÷ 稀释股数。")
                _gap = _tn.get("gap_since_main_band")
                if _gap:
                    band_meta += (f"\n    本带盲区那一段（{_gap['span']['start']}~"
                                  f"{_gap['span']['end']}）trailing P50 {_gap['p50']:.1f}x。")
            band_meta += ("\n    → 若折算后显示市场近一年的定价已明显偏离本带中枢，"
                          "那是**锚可能已过时**的证据：此时偏离 P50 属于有证据的偏离，"
                          "请在 rationale.pe 写明「近一年 regime 变化」并给出财报/定价依据。"
                          "反之若两者量级一致，锚照常适用。")
    elif mode == "standard":
        # 带整体缺席（fetch_facts 已在 pe_band_error 留痕）：与 thin_coverage 同等
        # 显式告知，附失败原因——判断层看得见"为什么没有锚"才不会把缺席当背书
        _err = facts.get("pe_band_error") or "原因未记录，见 fetch_facts 日志"
        band_meta = (f"\n本次无历史 PE 锚（带子未生成：{_err}）：目标 PE 按基本面"
                     "第一性与可比经验判断，并在 rationale.pe 写明定价依据。")
    # financials 的锚是 P/TBV 带（图3 的教科书结论：银行 E 带杠杆带周期，估值锚
    # 是 P/B 系）——与 standard 的 PE 锚同一纪律结构，锚 s["ptbv"]
    _tb = facts.get("ptbv_band") or {}
    if mode == "financials" and _tb.get("thin_coverage"):
        band_meta = (f"\n历史 P/TBV 带覆盖不足（仅 {_tb.get('days')} 个交易日）——本次无历史锚："
                     "目标 P/TBV 按 ROTE/资本回报第一性判断，并在 rationale.ptbv 写明依据。")
    elif mode == "financials" and _tb.get("pctiles"):
        _rc2 = _tb.get("recent") or {}
        _rp2 = _rc2.get("pctiles") or {}

        def _pfmt2(pp):
            return " / ".join(f"P{q} {pp[str(q)]:.2f}x" for q in (10, 25, 50, 75, 90)
                              if str(q) in pp)
        _aw2 = f"近{_rc2['years']}年" if _rp2 else f"近{_tb['years']}年"
        band_meta = (
            f"\n历史 P/TBV 带（trailing 口径，分母=当日已知每股有形账面价值，"
            "与引擎 tbv_ps 同构）："
            f"\n  全窗近{_tb['years']}年: {_pfmt2(_tb['pctiles'])}（{_tb['days']}天）"
            + (f"\n  近{_rc2['years']}年子窗: {_pfmt2(_rp2)}（{_rc2['days']}天）" if _rp2 else "")
            + f"\n  ← base 情景目标 P/TBV **默认锚{_aw2} P50**；bear/bull 参照 P25/P75"
              " 量级再叠加各自情景的 ROTE/信贷假设。**base** 偏离 P50 ±15% 以上必须在"
              " rationale.ptbv 给出财报证据（ROTE 结构变化、信贷周期位置、资本行动等）。"
              "\n  若下方给出「上一次运行的假设（连续性基准）」，连续性优先。")
        # fin 的锚-上界死锁预告（0022 C9，镜像 standard 的 pe<=60 预告，同门槛 8）：
        # 高 ROTE 票锚窗 P50 可超过校验硬上界 ptbv<=8——照锚必被拒、烧掉一次
        # retry。engine.pe_band_check 的 hard_cap=8 豁免早已就位（0013），此前
        # 只有 standard 有预告端，fin 的封顶指令一直缺席，engine.py:539 的
        # 「与 band_meta 封顶预告同门槛」对 fin 是空话。
        _a50f = (_rp2 if _rp2 else _tb["pctiles"]).get("50")
        if _isnum(_a50f) and _a50f > 8:
            band_meta += (
                f"\n  ⚠ 锚窗 P50 {_a50f:.2f}x 超出校验上界 ptbv<=8——按上界 8 封顶给出，"
                "并在 rationale.ptbv 说明封顶事实；封顶导致的带下沿偏离引擎已豁免，"
                "不追究，不要为凑纪律去压低其他假设。")
    elif mode == "financials":
        _err = facts.get("ptbv_band_error") or "原因未记录，见 fetch_facts 日志"
        band_meta = (f"\n本次无历史 P/TBV 锚（带子未生成：{_err}）：目标 P/TBV 按 "
                     "ROTE/资本回报第一性判断，并在 rationale.ptbv 写明依据。")
    # fin 的 PE 腿信息锚（0022 C13）：engine 自 0005 起对 fin 各情景的 s["pe"] 跑
    # pe_band_check（pe_band 对 fin facts 本就生成），而 fin prompt 只有泛可比
    # 区间（成长 fintech 15-30x）——检查与锚不在同一场对话里，regime 已切换的票
    # 每次运行都吃一条无法预辩护的中枢黄旗。这里把带子亮给判断层：**只作信息
    # 参照**，P/TBV 仍是主锚（银行估值惯例），pe 不受 ±15% 锚纪律约束。
    if mode == "financials":
        _pb = facts.get("pe_band") or {}
        _pbr = (_pb.get("recent") or {}).get("pctiles") or {}
        _pbp = _pbr or _pb.get("pctiles") or {}
        if _pbp and not _pb.get("thin_coverage"):
            _pbw = (f"近{(_pb.get('recent') or {})['years']}年子窗" if _pbr
                    else f"近{_pb.get('years')}年全窗")
            band_meta += (
                f"\n历史已实现 NTM PE 带（信息参照——P/TBV 仍是主锚）：{_pbw} "
                + " / ".join(f"P{q} {_pbp[str(q)]:.1f}x" for q in (10, 25, 50, 75, 90)
                             if str(q) in _pbp)
                + "\n  ← 引擎会按此带对各情景 pe 做界外/中枢检查（base 界外打黄旗）；"
                  "pe 明显偏离子窗 P50 时请在 rationale.pe 写明依据"
                  "（regime 变化、盈利结构切换等），带内则无须额外辩护。")
    return band_meta


def _trading_range_payload(mode: str, facts: dict, val: dict):
    """RESULT 载荷的 trading_range 块 -> (dict | None, 缺席原因 | None)。

    trading_range 为 null 时前端/下游此前只看到一个 null——为什么没有区间
    （带子构建失败？覆盖不足？base PE 腿 n.m.？financials 本就没有？）只活在
    引擎红旗区，RESULT 的消费者看不见。缺席原因必须跟着缺席走。纯函数可单测。
    """
    _tr = val.get("trading_range")
    if _tr and _tr.get("px"):
        return (dict(lo=_tr["px"].get("25"), mid=_tr["px"].get("50"),
                     hi=_tr["px"].get("75"), window=_tr["window"],
                     # 盈利窗口 + 倍数窗口真实起止/滞后：只写"近3年PE带"
                     # 会被读成区间的时间跨度（实测确实被这么问了）
                     eps_window=_tr.get("eps_window"),
                     span=_tr.get("span"),
                     # 现价当前位置与倍数回归归因——区间中位的涨幅按构造
                     # 全部来自倍数回归，不写出来读者看不见
                     fwd_pe_now=_tr.get("fwd_pe_now"),
                     # 带外只给关系不给截断分位（PR #5 review）
                     fwd_pe_now_position=_tr.get("fwd_pe_now_position"),
                     target_pe=_tr.get("target_pe"),
                     mult_reversion=_tr.get("mult_reversion_to_p50"),
                     # regime 失效红线（带外 + 带子滞后 >250 天，engine 置）：
                     # 均值回归前提可能已失效——载荷消费者必须能看到
                     regime_note=_tr.get("regime_note")), None)
    if mode == "financials":
        note = "financials 模式无交易区间（估值锚为 P/TBV，非 PE 分位）"
    elif facts.get("pe_band_error"):
        note = f"无历史 PE 带（{facts['pe_band_error']}）——区间无法构造"
    elif (facts.get("pe_band") or {}).get("thin_coverage"):
        note = (f"历史 PE 带覆盖不足（{(facts.get('pe_band') or {}).get('days')} 天"
                "<250）——区间停用")
    else:
        note = "base PE 腿 n.m. 或带子缺分位——区间停用（详见报告红旗区）"
    return None, note


def _adr_calibration(price: float, mcap: float,
                     shares_ord_m: float) -> tuple[float, float | None]:
    """ADR 比例标定 -> (adr_multiple, 口径失配比例|None)。纯函数（0018 抽出可单测）。

    yfinance 价是 ADR 价、XBRL 股数是普通股（TTM 加权稀释）：mcap÷普通股数反推
    每普通股隐含价，price÷隐含价即 ADR 比例（TSM 1:5）。真实 ADR 比例只会是
    整数或简单半数（2 普通股=1 ADR → 2；1 ADR=0.5 普通股 → 0.5），(0.5, 2) 内
    既不落 1±8% 也不落半数容差的值不是 ADR，是两侧股数口径的噪声——yfinance
    市值隐含股数与 XBRL 加权稀释股数本就能差几个百分点（回购/增发期两口径结构性
    分叉）。此前这类值原样放行：TSLA 实测 0.8963 被当成「1 ADR=0.896 普通股」
    发货——美股普通票挂上假 ADR 口径、shares 被 rebase、带子与每股值口径混掉。
    现在回退 adr_multiple=1.0 并返回失配比例（进 caliber 说明 + 引擎全局黄旗），
    shares 保持 XBRL 稀释口径。(0, 0.5] 与 [2, ∞) 之外圈维持原行为（整数 snap /
    原样放行），不在本次收紧范围。"""
    implied = mcap / (shares_ord_m * 1e6)
    raw = price / implied if implied > 0 else 1.0
    if abs(raw - 1) < 0.08:
        return 1.0, None
    snapped = round(raw)
    if snapped >= 2 and abs(raw / snapped - 1) < 0.08:
        return float(snapped), None
    # 半数 ADR（1 ADR = 0.5 普通股）真实存在——与整数分支同构的容差 snap
    if abs(raw - 0.5) < 0.04:
        return 0.5, None
    if 0.5 < raw < 2.0:
        return 1.0, abs(raw - 1)
    return raw, None


# 期后 filing 索引只留资本结构类表单：424B*（增发定价）/S-*（注册）/8-K（事件）/
# SC 13*（大额持股变动）/10-*（定期报告与修订）。Form 3/4/144 之类高频噪音会把 cap 吃光
_PPCE_FORM_PREFIXES = ("424B", "S-", "SC 13", "8-K", "10-")


def _postperiod_filing_index(rows: list[dict], periodic_end: str, cap: int = 30) -> str:
    """报告期后的资本结构类 filing 一行索引——ppce（期后资本事件）核对的线索。

    prompt 检查清单 #3 命令判断层「距报告期 >45 天必须查 8-K/424B」，此前却一份
    filing 清单也不给——实测 4/9 个判断层 agent 只能自己去 curl EDGAR。只做过滤
    与排版的纯函数：取数（edgar.recent_filings）与失败降级在 _pipeline。
    过滤按 filingDate > 报告期末——报告期内的事件已在财报正文里，不重复列。
    8-K 附 items（2.02=业绩、1.01=重大协议、3.02=非注册增发、8.01=其他），一眼可分类。"""
    hits = [r for r in rows or []
            if (r.get("filingDate") or "") > periodic_end
            and str(r.get("form") or "").strip().startswith(_PPCE_FORM_PREFIXES)]
    if not hits:
        return (f"报告期 {periodic_end} 之后无上述类型的新 filing——期后若真有增发/"
                "回购/并购/分红宣告，应已出现在这里")
    hits.sort(key=lambda r: (r.get("filingDate") or "", r.get("accessionNumber") or ""),
              reverse=True)
    lines = []
    for r in hits[:cap]:
        form = str(r.get("form") or "").strip()
        seg = f"  {r.get('filingDate')} {form}"
        items = str(r.get("items") or "").strip()
        if form.startswith("8-K") and items:
            seg += f" items={items}"
        if form.startswith("10-") and r.get("reportDate"):
            seg += f" 期末={r['reportDate']}"
        lines.append(seg)
    head = (f"共 {len(hits)} 份（新→旧"
            + (f"，仅列最近 {cap} 份" if len(hits) > cap else "") + "）:")
    return head + "\n" + "\n".join(lines)


# SOTP 降级线：必须与 engine.py 的 SOTP_SEG1_CAP 逐位一致（seg1_share >= 它就把
# SOTP 腿踢出综合）。两处各写一份会让"校验层放行的 config 在引擎里静默关腿"，
# test_seg_crosscheck 从 engine 源码里抠出常量把两边钉在一起。
SOTP_SEG1_CAP = 0.85


def _segment_facts(seg: dict | None) -> dict | None:
    """segments payload -> 最新一期分部营收结构。纯函数（可直接单测）。

    取 axes.segment 里最新的那一期（优先季度，缺则年度）。返回 None 表示
    "没有可用的分部申报"——单一分部发行人与取数失败都归到这里，两者对下游的
    处置相同：没有对照物就不判罚（不能拿缺数当证据反过来指控判断层）。
    """
    axis = ((seg or {}).get("axes") or {}).get("segment") or {}
    for freq in ("quarterly", "annual"):
        rows = axis.get(freq) or {}
        if not rows:
            continue
        k = max(rows)
        members = {m: v for m, v in ((rows[k].get("members") or {}).items())
                   if _isnum(v) and v > 0}
        # 单成员不算分部结构（有些发行人只标一个 member 占位）
        if len(members) < 2:
            continue
        total = sum(members.values())
        if not total:
            continue
        return {"period": k, "freq": freq, "total": total, "n": len(members),
                "top_share": max(members.values()) / total,
                "members": sorted(members.items(), key=lambda x: -x[1])}
    return None


def _seg_label(m: str) -> str:
    """XBRL 分部成员名 -> 可读标签：剥 Member 后缀 + 驼峰拆词。只做展示层美化，
    原 token 一并给出——判断层要能拿它回财报里核对。"""
    s = re.sub(r"(Segment)?Member$", "", str(m))
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s) or str(m)


def _segment_lines(sf: dict | None) -> str:
    """分部事实 -> 注入 prompt 的块。纯函数。

    判断层此前只能从 SECTIONS 正文里翻分部表，而 seg1_share >= 0.85 会让引擎把
    SOTP 腿降级为参考项——「本公司是单一业务」这一句自我声明就能悄悄关掉一整条
    估值腿，校验层还只查 0<=x<=1。MSFT 2026-09-08 实测正是这个形态：判断层给
    seg1="Microsoft 整体（软件/云一体化）"、seg1_share=1.0、rationale 里零理由，
    而发行人自己按三个分部申报（Intelligent Cloud 43.7% / Productivity 42.0% /
    More Personal Computing 14.3%）。修法与 PE 锚同构：先注入发行人自己申报的
    事实，再要求偏离给证据。
    """
    if not sf:
        return ("# 分部（发行人 XBRL 申报）\n"
                "本票无可用的分部营收申报（单一分部发行人，或分部数据取数失败）"
                "——seg1/seg2/seg1_share 按 SECTIONS 的分部表判断\n\n")
    lines = [f"# 分部（发行人 XBRL 申报，{sf['period']} "
             f"{'季度' if sf['freq'] == 'quarterly' else '年度'}营收，$M）",
             f"发行人自己按 {sf['n']} 个分部申报，营收占比："]
    for m, v in sf["members"]:
        lines.append(f"  {_seg_label(m)} [{m}]: {v / 1e6:,.0f}  {v / sf['total']:.1%}")
    lines.append(f"  ← 最大分部营收占比 {sf['top_share']:.1%}。**这是营收口径**，而 "
                 "seg1_share 要的是**营业利润**占比——两者可以显著不同（低利润率"
                 "分部拉低利润占比是常态），所以这张表是参照不是答案。")
    if sf["top_share"] < SOTP_SEG1_CAP:
        lines.append(
            f"  ⚠ 发行人按多分部申报且最大分部营收占比 {sf['top_share']:.1%} < "
            f"{SOTP_SEG1_CAP:.0%}。若你仍给出 seg1_share >= {SOTP_SEG1_CAP:.0%}"
            "（这会让引擎把 SOTP 腿降级为参考项、整条腿退出综合），**必须在 "
            "rationale.sotp 里写明利润为何比营收更集中**（分部营业利润率差异、"
            "总部费用分摊口径等，给财报出处）——无理由的高集中度声明会被校验层拒收。")
    return "\n".join(lines) + "\n\n"


def _check_seg1_share(d: dict, sf: dict | None) -> None:
    """seg1_share 与发行人 XBRL 分部申报的对照闸（0023）。

    只拦一种形态：发行人按多分部申报、最大分部营收占比低于 SOTP 降级线，而判断层
    声明 seg1_share >= 降级线且**不给任何理由**——此时 SOTP 腿被静默踢出综合，
    而校验层原先只查 0<=x<=1，一个字的证据都不要。

    不做数值等式检查：seg1_share 是营业利润占比、这张表是营收占比，两者合法地不同
    （MSFT 的 More Personal Computing 利润率远低于 Intelligent Cloud）。要的是
    **理由**，不是逼判断层去对齐一个口径不同的数。反方向（声明的集中度显著低于
    营收集中度）不拦——那只会让 SOTP 留在综合里，多一条腿是保守方向，由引擎的
    黄旗呈现即可。
    """
    if not sf or sf["top_share"] >= SOTP_SEG1_CAP or d["seg1_share"] < SOTP_SEG1_CAP:
        return
    if str((d.get("rationale") or {}).get("sotp") or "").strip():
        return
    raise ValueError(
        f"seg1_share={d['seg1_share']:.0%} >= {SOTP_SEG1_CAP:.0%} 会把 SOTP 腿降级为"
        f"参考项（退出综合），但发行人按 {sf['n']} 个分部申报、最大分部营收占比只有 "
        f"{sf['top_share']:.1%}（{sf['period']}）："
        + "、".join(f"{_seg_label(m)} {v / sf['total']:.0%}"
                    for m, v in sf["members"][:4])
        + "。利润集中度高于营收集中度是可能的，但必须在 rationale.sotp 里写明依据"
          "（分部营业利润率差异、总部费用分摊口径等，给财报出处）；"
          "否则请按实际分部结构给 seg1_share 与 seg2。")


# 连续性基准注入的字段集（0017）：假设 + 事实类锚。other_income/other_income_note
# 与 seg1/seg2/seg1_share 曾缺席——8/31 补 other_income_note 的动机正是 AMZN 同日
# 同输入两次运行 other_income 漂 -33%，而连续性注入偏偏不带这个字段：判断层看不到
# 上次的值，纪律对它管不着，漂移从连续性通道原样漏回来。seg1_share 同理（SOTP 权重
# 每次独立重拍）。
_PREV_CORE_KEYS = ("date", "adj_ni", "net_cash", "fwd_shares",
                   "other_income", "other_income_note",
                   "seg1", "seg2", "seg1_share", "scenarios", "rationale")


def _prev_core(prev: dict) -> dict:
    """上次运行 config -> 注入 prompt 的连续性基准子集。按键存在过滤：
    financials 配置没有 other_income/seg*，null 冒充『上次的假设』只会误导。"""
    return {k: prev[k] for k in _PREV_CORE_KEYS if k in prev}


def _persist_prev_config(ticker: str, cfg: dict, reds: list, latest_report) -> None:
    """连续性锚持久化（v2）：只有 gate-clean（无 red 红旗）的 config 才能成为下次
    运行的基准——带病假设冻结成锚会让偏差跨运行复利（方差可见，偏差不可见）。
    原子写：避免任务中断留下半个 JSON 毒化后续所有运行。

    2026-09-06 起 financials 同样持久化：load 路径（连续性失效触发器）本就按
    mode 支持 fin 语义，写路径却挂着 mode=="standard" 门禁——fin 的自动连续性
    结构上是 no-op，每次运行独立重采样，正是连续性机制要消灭的漂移源。
    financials 今日没有 red 类诊断，gate-clean 即 reds 为空，两模式同一表达式；
    跨版本复用由 load 侧的语义检查兜底（fin v3）。"""
    if reds or os.environ.get("VALUATION_NO_CONTINUITY"):
        return
    PREV_DIR.mkdir(exist_ok=True)
    _tmp = PREV_DIR / f".{ticker}.json.tmp"
    _tmp.write_text(json.dumps(dict(cfg, manifest_latest=latest_report),
                               ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(_tmp, PREV_DIR / f"{ticker}.json")


async def _pipeline(job: dict, ticker: str, email: str) -> None:
    wd = Path(job["dir"])
    today = date.today().isoformat()

    # 判断层登录探测放在第一步：④ 之前的取数/下载要跑好几分钟，登录失效却要等到
    # ⑤ 才暴露——用户白等一轮，SEC 也白请求一轮。探测只在能确凿判定未登录时拦。
    # VALUATION_JUDGMENT_CMD 下整个预检跳过（0016）：判断层不经本机 claude CLI，
    # 本机凭证过期与任务无关——照跑会让一份陈旧的 .credentials.json 拦死所有任务
    job["step"] = "preflight"
    if not os.environ.get("VALUATION_JUDGMENT_CMD"):
        _auth = _auth_state()
        if not _auth["ok"]:
            raise JudgmentAuthError(f"判断层未登录（{_auth['reason']}）")

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
    # 前瞻期推导必须用**定期报告**的报告期：8-K 的 reportDate 是公布日而非期末，
    # 混进来会把 NTM 窗口整体推错（engine._vintage 出于同样理由排除 8-K）
    periodic_end = max((r.get("reportDate") or "" for r in
                        csv.DictReader(io.StringIO(manifest))
                        if not (r.get("form") or "").startswith("8-K")), default="")
    # 财年末取最新一期年度报表的期末日（standard/financials 都用 revenue_annual）
    fy_end = max(facts.get("revenue_annual") or {}, default="")
    # PENDING_10Q 时 ttm_revenue_override 会把 TTM 前滚一个季度，窗口跟着滚
    FWD = (fwd_window(periodic_end, fy_end, rolled_quarters=1 if pending_8k else 0)
           if periodic_end else None)
    # 期后 filing 索引（v4）：submissions 服务器本来就取过（company_info /
    # build_zip_latest），这里只再取一次清单、不下载文档。索引是辅助线索，
    # 任何失败降级为显式的"索引不可用"——绝不连累估值任务
    filing_idx = ""
    if periodic_end:
        try:
            filing_idx = _postperiod_filing_index(
                await edgar.recent_filings(ticker, email), periodic_end)
        except Exception as e:  # noqa: BLE001 —— 线索取不到要说清原因，但不许杀任务
            filing_idx = (f"期后 filing 索引不可用（{type(e).__name__}: {e}），"
                          "按 MANIFEST 与 SECTIONS 摘录判断")
    # 分部申报（0023）：judgment 此前凭 SECTIONS 正文自拍 seg1_share，而它 >=0.85
    # 就会把 SOTP 腿踢出综合——注入发行人自己的分部营收结构当对照物。与 filing 索引
    # 同规格的降级：取不到就是 None（判断层照旧按 SECTIONS 判断、校验层不判罚），
    # 绝不连累估值任务。超时兜底：SEC 卡住不能拖死唯一的任务槽
    seg_facts = None
    try:
        seg_facts = _segment_facts(await asyncio.wait_for(
            asyncio.to_thread(build_segments, ticker, email, info["cik"], 3),
            timeout=240))
    except Exception as e:  # noqa: BLE001 —— 对照物缺失只降级为"不查"，不杀任务
        job["detail"] = f"分部对照不可用（{type(e).__name__}: {e!r:.80}），本次不做 seg1_share 对照"

    job["step"] = "sections"
    await _run([PY, str(VAL / "extract_sections.py"), str(wd / "sections.json"), *htms], wd)
    sections = (wd / "sections.json").read_text(encoding="utf-8")

    job["step"] = "judgment"
    shares_ord = list(shares_series.values())[-1] / 1e6  # 普通股口径（百万股）
    # ADR 换算见 _adr_calibration：整数/半数比例 snap，(0.5,2) 内的其余值是股数
    # 口径噪声而非 ADR，回退 1.0 并把失配比例带进 caliber 与引擎全局黄旗
    adr_multiple, adr_mismatch = _adr_calibration(price, mcap, shares_ord)
    shares = round(shares_ord / adr_multiple)  # ADR 等效股数：mcap ≈ price × shares
    prompt_file = ("judgment_prompt_financials.md" if mode == "financials"
                   else "judgment_prompt.md")
    base_prompt = (VAL / prompt_file).read_text(encoding="utf-8")
    caliber = ""
    if adr_multiple != 1.0:
        caliber += (f"\n口径说明：价格为 ADR 价（1 ADR = {adr_multiple:g} 普通股），"
                    f"shares 已折为 ADR 等效股数；你输出的 fwd_shares 也用 ADR 等效口径。")
    if adr_mismatch is not None:
        caliber += (f"\n口径说明：市值隐含股数与 XBRL 稀释股数差 {adr_mismatch:.1%}"
                    "（非整数/半数比例，判定为股数口径噪声而非 ADR，已按 1 ADR=1 股处理）"
                    "——shares 取 XBRL 加权稀释股数，net_cash/每股值一律按 XBRL 股数口径。")
    if facts.get("currency", "USD") != "USD":
        caliber += (f"\n口径说明：申报货币 {facts['currency']}，FACTS 已按现汇 "
                    f"{facts.get('fx_to_usd', 1):.5f} 折算美元（恒定汇率）；"
                    "历史增长率不受影响，绝对值以美元理解。")
    # 营业利润推导回退（0021）：发行人停报 OperatingIncomeLoss、TTM 由
    # rev−cogs−rnd−sga 推得——判断层必须知道这个数不是申报值
    if (ttm.get("op_income") or {}).get("derived"):
        caliber += ("\n口径说明：TTM 营业利润为推导值（rev−cogs−rnd−sga，发行人已停报 "
                    "OperatingIncomeLoss），可能含未单列的摊销/重组项——"
                    "opm 假设须与 SECTIONS 利润表原文核对后再定。")
    # financials 模式的营收口径是 RevenuesNetOfInterestExpense（净息后总净收入），
    # 照抄 standard 的"营收"会让判断层去取利息总收入或非 GAAP 的"调整后净收入"
    fin = mode == "financials"
    rev_term = "TTM 总净收入（净利息收入+非息收入）" if fin else "TTM 营收"
    stale_days = 0
    if facts.get("data_latest"):
        stale_days = (date.today() - date.fromisoformat(facts["data_latest"])).days
        caliber += (f"\nXBRL 结构化数据最新期末为 {facts['data_latest']}；"
                    "若 SECTIONS 财报原文里有更新的季度数字，以原文为准做前瞻判断。")
        if stale_days > 550:
            caliber += (f"数据已严重滞后：你必须按财报原文推出真实 {rev_term}，"
                        "输出 ttm_revenue_override（$M）与 ttm_revenue_note（出处），"
                        "并把所有 g 锚定在该基准上——缺失会被拒绝重试。")
    # mode 门禁已移除（2026-07-30）：PENDING_10Q 窗口正是为"10-Q 滞后业绩公布
    # 2-6 周"设计的，而那类发行人（银行/券商/fintech）全部走 financials——
    # 此前 financials 检测到 pending_8k 却拿不到指令，静默按上一季度基准估值
    if pending_8k:
        caliber += (
            f"\n⚠ PENDING_10Q：该公司 {pending_8k['filingDate']} 已公布业绩"
            "（SECTIONS 含其 8-K EX-99.1 新闻稿摘录，**未经审计、可能混非 GAAP"
            "口径**），但 10-Q 尚未提交——FACTS 的 XBRL/TTM 不含最新季度。你必须：\n"
            + (f"1) 前瞻判断（g/nm/信贷质量/指引）以新闻稿里的最新季度 **GAAP** 数字为准；\n"
               if fin else
               "1) 前瞻判断（g/opm/指引）以新闻稿里的最新季度 **GAAP** 数字为准；\n")
            + f"2) 用新闻稿计算真实 {rev_term}并输出 ttm_revenue_override（$M）与"
              " ttm_revenue_note（公式=旧TTM − 去年同季 + 本季，写明数字出处）"
              "——缺失会被拒绝重试；"
            + ("口径须与 FACTS 的总净收入一致：净利息收入（**已扣利息支出**）+ 非息收入，"
               "不是利息总收入，也不是新闻稿标题常用的『调整后净收入』/EBITDA 等非 GAAP 汇总；"
               "新闻稿若只突出非 GAAP 或分部数字，取 GAAP 利润表页的合计行；\n" if fin else "\n")
            + (f"3) 除{rev_term}基准外，勿把新闻稿数字与旧 XBRL 混算其他比率"
               "（TBV/ROTE 仍以 XBRL 时点的股东权益为准）；非 GAAP 数字仅作定性参考。"
               if fin else
               "3) 除营收基准外，勿把新闻稿数字与旧 XBRL 混算其他比率；"
               "非 GAAP 数字仅作定性参考。"))
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
                elif prev.get("semantics_version", 1) != (4 if mode == "standard" else 3):
                    stale = (f"语义版本 v{prev.get('semantics_version', 1)} != "
                             f"v{4 if mode == 'standard' else 3}")
                elif prev.get("manifest_latest") and latest_report \
                        and prev["manifest_latest"] != latest_report:
                    stale = f"出现新报告期 {latest_report}（上次基于 {prev['manifest_latest']}）"
                elif prev.get("price") and abs(price / prev["price"] - 1) > 0.15:
                    stale = f"现价较上次变动 {price / prev['price'] - 1:+.0%}（>15%）"
                if stale:
                    prev_section = (f"\n\n# 假设连续性说明\n上次运行（{prev.get('date')}）的假设"
                                    f"已失效：{stale}。本次独立重建全部假设。")
                else:
                    prev_core = _prev_core(prev)
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
    # 前瞻期是服务器算好的事实，不由判断层选——但必须显式告诉它窗口是哪一段，
    # 否则 g/opm/fwd_shares 会各自锚在不同的"下一财年"上（AMZN 实测三次裂成两种口径）。
    # g 分母在强制 override 场景切到 override（见 _fwd_meta docstring），门禁与
    # 下方"注入指令/拒收缺失"两处严格同源
    force_override = bool(pending_8k) or stale_days > 550
    if force_override and mode == "standard":
        # override 会把营收基准前滚，而 TTM cfo/capex 还停在旧 XBRL 窗口（0019）：
        # 偏离 >10% 时校验层不再拿陈旧 FCF 率当 margins 锚，谷底下限与上界（0022 C2）
        # 都改锚历史中位——提前写进口径说明，判断层被拒时才知道锚为什么换了。
        # 自愿 override（非 force 场景）在 prompt 期不可知，其换锚说明走拒绝文案
        # （margins 拒绝行点名上界/谷底锚，见 _validate_judgment），retry 仍是知情的
        caliber += ("\n口径说明：TTM 现金流（cfo/capex）口径滞后于 ttm_revenue_override"
                    "——override 偏离 FACTS 的 TTM 营收 >10% 时，margins 谷底下限"
                    "与上界（1.2×）自动改锚历史年度 FCF 利润率中位（见 FCF 利润率表）。")
    fwd_meta = _fwd_meta(FWD, force_override=force_override)
    # 历史带锚注入（standard=NTM PE / financials=P/TBV，含 thin/缺席两种
    # 「本次无历史锚」告知）——文案构造抽成 _band_meta 纯函数，缺席分支可单测
    band_meta = _band_meta(mode, facts)
    # Rule of 40（fetch_facts 计算）：营收增速+利润率，「高倍数值不值得给」的对照
    # 标尺——分数高支撑倍数带上沿，分数低而倍数高 = 增速在烧钱换。给判断层做
    # pe/g 组合合理性的参照，不是硬规则（校验层不执法）。
    ro40_meta = ""
    _r40 = facts.get("rule_of_40") or {}
    if mode == "standard" and (_r40.get("score_op") is not None
                               or _r40.get("score_fcf") is not None):
        ro40_meta = (
            "\nRule of 40（TTM）：营收增速 " + f"{_r40.get('rev_g_ttm', 0):+.1%}"
            + (f" + 营业利润率 {_r40.get('opm_ttm', 0):.1%} = {_r40['score_op']:.0f} 分"
               if _r40.get("score_op") is not None else "（营业利润无数据，OP 口径缺）")
            + (f"；FCF 口径 {_r40['score_fcf']:.0f} 分" if _r40.get("score_fcf") is not None else "")
            + (f"（剔 SBC 后 {_r40['score_fcf_ex_sbc']:.0f} 分，SBC 占营收 "
               f"{_r40['sbc_margin_ttm']:.1%}）" if _r40.get("score_fcf_ex_sbc") is not None else "")
            + (f"\n  ⚠ 口径注意：{_r40['caliber_note']}" if _r40.get("caliber_note") else "")
            + "\n  ← 软件/平台类 >40 算优秀。用作 pe/g 组合合理性的对照：分数低于 40"
              " 而你给的目标倍数落在历史带上沿时，rationale.pe 里要说清为什么。")
    # 期后 filing 索引紧跟 MANIFEST：它回答的是检查清单 #3「报告期之后发生了什么」，
    # 与 MANIFEST（报告期内的财报）正好衔接
    filing_meta = ""
    if filing_idx:
        filing_meta = (
            f"# 报告期后 filing 索引（服务器实查 EDGAR：报告期 {periodic_end} 之后"
            "提交的 424B*/S-*/8-K/SC 13*/10-*）\n"
            f"{filing_idx}\n"
            "  ← post_period_capital_events 的核对以此为线索（增发/发债→424B/8-K "
            "Item 1.01/3.02，回购/分红宣告→8-K Item 8.01 或新闻稿）；"
            "索引只是线索，金额与性质仍须回到原文\n\n")
    prompt = (f"{base_prompt}\n\n# 服务器注入的元数据（不要输出这些字段）\n"
              f"ticker={ticker} name={info['name']} date={today} price={price:.2f} "
              f"mcap={mcap/1e6:,.0f}M$ shares={shares}M股{fwd_meta}{band_meta}{ro40_meta}{caliber}\n\n"
              f"# FACTS（SEC XBRL）\n{_compact_facts(facts)}\n\n"
              f"# MANIFEST（本次分析的财报文件）\n{manifest}\n\n"
              f"{filing_meta}"
              # 分部对照（0023）紧邻 SECTIONS：判断层做 seg1/seg2/seg1_share 时
              # 两边要能对照着看——发行人的 XBRL 申报 vs 财报正文的分部表
              + (_segment_lines(seg_facts) if mode == "standard" else "")
              + f"# SECTIONS（财报关键章节摘录 JSON）\n{sections}{prev_section}\n")
    # v2 一致性规则的事实输入：情景 EPS 用的营收基准与 TTM FCF 利润率
    rev0_m = ttm["revenue"]["value"] / 1e6
    fcfm = None
    if mode == "standard":
        fcfm = (ttm["cfo"]["value"] - ttm["capex"]["value"]) / ttm["revenue"]["value"]
    # 负/近零 TTM FCF 时 margins 谷底/上界护栏的备用锚：历史年度 FCF 利润率中位。
    # 实现抽到 _hist_fcfm_median（0022 C4）——check_configs 回归工具与产线同源。
    hist_fcfm = _hist_fcfm_median(facts)
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
                               # override 偏离旧 TTM >10% 时现金流锚已过期，
                               # 传 None 让谷底下限落回历史中位（0019）
                               fcf_margin=_fcfm_for_validation(
                                   fcfm, judgment.get("ttm_revenue_override"), rev0_m),
                               band=facts.get("pe_band"),
                               hist_fcf_margin=hist_fcfm,
                               # 分部对照闸（0023）：两个调用点同源，复审换了
                               # seg1_share 也要按同一条规矩重查
                               seg_facts=seg_facts)
            # 陈旧 XBRL（外国发行人 6-K 无季度框架）下，没有原文重锚的 TTM 会让
            # 全部情景锚在多年前的营收基准上——硬性要求判断层给 override。
            # PENDING_10Q（业绩 8-K 已出、10-Q 未交）同理：不重锚等于用上季度
            # 基准估一家刚发完财报的公司。两模式同权，且与上面的注入条件严格
            # 同源——注入与强制若走不同门禁，会出现"没要求却拒收"的死循环
            if ((stale_days > 550 or pending_8k)
                    and not judgment.get("ttm_revenue_override")):
                raise ValueError(
                    ("PENDING_10Q：业绩新闻稿已在 SECTIONS，" if pending_8k
                     else f"XBRL 数据滞后（最新期末 {facts.get('data_latest')}）：")
                    + f"必须按财报原文提供 ttm_revenue_override（{rev_term} $M）与 ttm_revenue_note")
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

    # semantics_version=4（2026-09-06）：v3 的 PE 锚之上，服务器前置注入十年
    # FCF 利润率锚表、OI&E 组件+税前−营业利润残差行、期后 filing 索引——终值
    # margins 与 other_income 的锚从"判断层徒手拼"变成"服务器实算注入"，锚的
    # 来源改变 = 语义改变，与 v2→v3 的隔离理由同构（v3 =纳入判断层 PE 锚，
    # 2026-08-14）。锚前样本与锚后样本混在趋势视图里聚合会把语义变化读成基本面
    # 修正，必须靠版本号隔开。仅描述 standard 模式（financials 走 fin v3：跨情景
    # 排序 + 亏损协议，见 _validate_judgment_financials 与 engine fin 分支注释）。
    # manifest_latest 直接进 cfg：bundle 里的 config_假设留档.json 与自动锚同源，
    # 显式 VALUATION_PREV_CONFIG 指向 bundle config 时报告期失效触发器才有指纹可查
    def _build_cfg(j: dict) -> dict:
        """判断层输出 + 服务器注入的事实 -> engine 的 config。

        必须是**唯一**的 cfg 构造点：此前首轮与经济复审重试各写了一份等价的
        dict(...)，2026-08-06 给首轮加 fwd_label 注入时漏了重试那份，走复审
        路径的运行直接 KeyError 崩在 engine.py:250（AMZN 实测 3 次中 1 次）。
        """
        c = dict(j, ticker=ticker, name=info["name"], date=today,
                 price=round(price, 2), mcap=round(mcap / 1e6), shares=shares,
                 mode=mode, adr_multiple=adr_multiple,
                 currency=facts.get("currency", "USD"),
                 semantics_version=4 if mode == "standard" else 3,
                 manifest_latest=latest_report,
                 # PENDING_10Q 标记进 cfg：engine 据此把 vintage 归档键前滚到 8-K
                 # 覆盖的季度（fwd_window 已 +3 个月），否则这次运行会归进旧
                 # report_end 的格子，趋势视图把"最新业绩下的估值"错当旧季度样本
                 pending_10q=bool(pending_8k))
        # 发行人分部营收集中度（0023）：引擎据此对 seg1_share 的利润集中度声明
        # 打全局黄旗。同 share_count_mismatch 的摆法——只在拿到对照物时写键
        if seg_facts:
            c["segment_revenue_share"] = round(seg_facts["top_share"], 4)
            c["segment_count"] = seg_facts["n"]
        # ADR 标定回退（0018）留下的股数口径失配：引擎据此打全局黄旗。只在失配时
        # 写键——不给历史 config 无端加一个恒 null 字段
        if adr_mismatch is not None:
            c["share_count_mismatch"] = round(adr_mismatch, 4)
        # fwd_label 是可由 report_end 纯日期推导的事实，与 price/shares 同类：
        # 在此**覆盖**判断层的输出，让"选错财年"这个失败模式从构造上不存在。
        # 判断层若仍输出了该字段，静默被盖掉即可——prompt 已明确要求不要输出。
        # 兜底分支不可省：字段已从判断层 schema 移除，服务器再不给就没人给了，
        # engine 无条件读 cfg["fwd_label"]，缺失即崩。
        if FWD:
            c["fwd_label"] = FWD["label"]
            c["fwd_window"] = {k: FWD[k] for k in ("start", "end", "straddle", "aligned")}
        else:
            c["fwd_label"] = "NTM（起点未知：manifest 无定期报告期）"
        return c

    cfg = _build_cfg(judgment)
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
        # 全局通道（0015）一并计入：按构造它今天只有 yellow（vintage/带滞后都是
        # 数据事实），但 red 统计漏一个通道 = 未来某个全局 red 会静默放行连续性锚
        reds = ([f"[全局] {msg}" for lv, msg in val.get("warnings_global") or []
                 if lv == "red"]
                + [f"[{sc}] {msg}" for sc in ("bear", "base", "bull")
                   for lv, msg in (val["scenarios"].get(sc) or {}).get("warnings", [])
                   if lv == "red"])
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
                               # 与首轮同一道口径闸（0019）：复审输出换了 override
                               # 也要按它自己的偏差重新决定锚
                               fcf_margin=_fcfm_for_validation(
                                   fcfm, revised.get("ttm_revenue_override"), rev0_m),
                               band=facts.get("pe_band"),
                               hist_fcf_margin=hist_fcfm,
                               # 分部对照闸（0023）：两个调用点同源，复审换了
                               # seg1_share 也要按同一条规矩重查
                               seg_facts=seg_facts)
            # 陈旧 XBRL / PENDING_10Q 的强制 override 在复审通道同样成立——revised
            # 整体替换 judgment，若复审输出丢掉 override，全部情景会锚回旧营收基准
            if ((stale_days > 550 or pending_8k)
                    and not revised.get("ttm_revenue_override")):
                raise ValueError("复审输出丢失 ttm_revenue_override（陈旧 XBRL/PENDING_10Q 下必须保留）")
            judgment = revised
            cfg = _build_cfg(judgment)
        except (ValueError, json.JSONDecodeError, TypeError, KeyError,
                ZeroDivisionError, RuntimeError) as e:
            # 复审输出不合格：保留原假设出报告，红旗如实展示——绝不静默吞掉
            job["detail"] = f"复审输出未通过校验（{e!r:.120}），沿用原假设并保留红旗"
            break

    # 连续性锚持久化：gate 与写盘见 _persist_prev_config（2026-09-06 起 financials
    # 一并持久化——此前写路径挂 mode 门禁，fin 的自动连续性结构上是 no-op）
    _persist_prev_config(ticker, cfg, reds, latest_report)

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

    # vintage 归档（trend.py 的数据源）：按**报告期**存快照，同一报告期的多次运行
    # 追加为多个样本——组内离散就是判断层噪声的直接估计，趋势视图靠它把"新财报导致
    # 的变化"和"运行间抖动"分开。与 PREV_DIR 不同的是带红旗的运行这里照存（打
    # gate_clean 标记，读取侧默认排除）：直接丢弃会让样本有偏，坏假设往往偏向同一侧。
    # 归档失败绝不能连累已经跑完的报告——包在 try 里，只在 detail 留痕。
    try:
        if str(VAL) not in sys.path:
            sys.path.insert(0, str(VAL))
        import vintages
        vintages.record(val, gate_clean=not reds)
    except Exception as e:  # noqa: BLE001
        job["detail"] = f"vintage 归档失败（{e!r:.120}），不影响本次报告"

    # trading_range 走独立字段而非塞进 summary：前端 Object.entries(summary) 按
    # "情景名 $blend（upside%）"渲染，混入结构不同的键会渲染出 undefined。
    # 区间缺席时 trading_range_note 说明原因（见 _trading_range_payload）
    _trp, _trn = _trading_range_payload(mode, facts, val)
    job.update(status="done", step="done", result=str(bundle),
               summary={k: dict(blend=v["blend"], upside=v["upside"])
                        for k, v in val["scenarios"].items()},
               trading_range=_trp, trading_range_note=_trn)


async def _run_job(job_id: str, ticker: str, email: str) -> None:
    global _running
    job = _jobs[job_id]
    try:
        await _pipeline(job, ticker, email)
    except JudgmentAuthError as e:
        # error_kind 让前端能给出「登录」按钮而不是只显示一段无从下手的红字
        job.update(status="failed", error_kind="auth",
                   error=f"[{STEP_LABELS.get(job.get('step'), job.get('step'))}] {e}")
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


@router.get("/api/valuation/auth")
async def valuation_auth():
    """判断层登录状态。前端在弹出登录窗口后轮询它，登录完成即可重试。"""
    # 自定义命令模式（0016）：判断层不经本机 claude CLI，本机凭证状态与任务无关。
    # 不给 cli/can_popup——「登录」按钮在这个模式下什么也修不了
    if os.environ.get("VALUATION_JUDGMENT_CMD"):
        return {"ok": True, "reason": "判断层=自定义命令（VALUATION_JUDGMENT_CMD），无需本机登录",
                "cli": None, "can_popup": False}
    st = _auth_state()
    try:
        st["cli"] = _find_claude()
    except RuntimeError as e:
        st = {"ok": False, "reason": str(e), "cli": None}
    st["can_popup"] = bool(st.get("cli")) and _login_argv() is not None
    return st


@router.post("/api/valuation/login")
async def valuation_login():
    """在本机弹出一个交互式终端跑 `claude /login`。

    只能这样做：`claude -p` 是无头模式，登录本身是交互流程（要开浏览器做 OAuth
    并回填），没法在无头子进程里完成。所以"弹窗登录"= 起一个真终端窗口交给用户，
    登录完成后前端轮询 /api/valuation/auth 转绿即可重试。
    命令写死，不接受任何请求参数。
    """
    argv = _login_argv()
    if argv is None:
        raise edgar.EdgarError(
            501, "本平台无法自动弹出终端，请手动开一个终端运行： claude /login")
    try:
        # 不等它结束：这个终端窗口会一直开着直到用户登录完
        await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except OSError as e:
        raise edgar.EdgarError(500, f"启动登录终端失败：{e}") from e
    return {"started": True,
            "hint": "已弹出终端窗口，在其中完成登录后回到本页点「重试」"}


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
