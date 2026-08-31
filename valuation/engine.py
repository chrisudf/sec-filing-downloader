# -*- coding: utf-8 -*-
"""估值引擎：facts JSON + 假设 config JSON -> valuation JSON。

用法: python engine.py CONFIG.json FACTS.json OUT.json [MANIFEST.csv]

config 由分析者（人或 LLM 判断层）编写，schema 见 valuation/README.md：
所有数字计算在此确定性完成——LLM 只负责定假设并注明出处，不碰算术。
standard 模式：PE 法（下一财年 EPS × 目标 PE）、十年两段式 FCFF DCF、SOTP/倍数法。
financials 模式（银行/券商/fintech）：PE 法 + P/TBV 法（附 Gordon 公式
justified P/TBV = (ROTE−g)/(WACC−g) 交叉参考）；DCF/SOTP 对金融股不适用。
"""
import csv
import io
import json
import math
import os
import sys
from datetime import date, timedelta

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
facts = json.load(open(sys.argv[2], encoding="utf-8"))
OUT = sys.argv[3]
manifest = open(sys.argv[4], encoding="utf-8").read() if len(sys.argv) > 4 else ""

MODE = cfg.get("mode", facts.get("mode", "standard"))

# ★ blend 权重政策（数字须按自己的纪律定；默认全 1 = 等权，历史行为逐位不变）。
# 「95% 的情况实际用的是倍数」的立场 argue 倍数腿占大头——但 DCF 腿留着有实证
# 价值（终值占比红旗、tv_pv 阈值巡航都是它抓的），所以做成权重而不是砍腿。
# 环境变量覆盖（非负，按当次参与综合的腿归一化）：
#   standard: VALUATION_BLEND_W_PE / _DCF / _SOTP；financials: _PE / _PTBV
# 权重随 valuation.json（blend_weights）进 Excel 综合公式与 compare/trend——
# 改权重 = 改口径，跨运行对比必须可见，不允许只活在环境变量里。
def _env_w(name):
    # nan/inf 必须挡在这里：float("nan") 不抛异常，max(nan, 0.0) 也照样返回 nan，
    # 于是 _wsum 变 nan、blend 变 nan，一路写进 valuation.json 并让 Excel 收到
    # 一个 `nan` 公式。非有限权重没有任何合法语义，直接退回 1.0（等权）。
    try:
        w = float(os.environ.get(name, 1) or 1)
    except ValueError:
        w = 1.0
    if not math.isfinite(w):
        print(f"⚠️  {name} 为非有限值（{os.environ.get(name)!r}），已退回等权 1.0")
        w = 1.0
    return max(w, 0.0)


BLEND_W = {"pe": _env_w("VALUATION_BLEND_W_PE"), "dcf": _env_w("VALUATION_BLEND_W_DCF"),
           "sotp": _env_w("VALUATION_BLEND_W_SOTP"), "ptbv": _env_w("VALUATION_BLEND_W_PTBV")}


def _vintage(manifest_text, run_date):
    """从 manifest 提取"这份估值基于哪个报告期"——价值投资的第一个检查项就是数据新鲜度。

    report_end 只看定期报告(10-K/10-Q/20-F/6-K)——8-K 新闻稿附件的
    reportDate 是公布日,混进来会让时效块在 XBRL 还停留在上季度时显示
    "1 天前",把警示反向变成误导。8-K 行单独输出 pending_8k_announced。"""
    try:
        rows = [r for r in csv.DictReader(io.StringIO(manifest_text)) if r.get("reportDate")]
        if not rows:
            return {}
        periodic = [r for r in rows if not r["form"].startswith("8-K")]
        if not periodic:
            return {}
        latest = max(periodic, key=lambda r: r["reportDate"])
        age = (date.fromisoformat(run_date) - date.fromisoformat(latest["reportDate"])).days
        out = {"report_end": latest["reportDate"], "filed": latest["filingDate"],
               "age_days": age}
        pend = [r for r in rows if r["form"].startswith("8-K")
                and r["filingDate"] > latest["filingDate"]]
        if pend:
            out["pending_8k_announced"] = max(r["filingDate"] for r in pend)
        return out
    except Exception:
        return {}


def vintage_warnings(cfg, vintage, dividends_quarterly=None):
    """报告期口径 与 当前值 混用的时点一致性护栏 -> [[level, msg], ...]。

    net_cash 与整张资产负债表停在 report_end，而 fwd_shares / price 是当前值。
    报告期后的增发/回购/并购/分拆只进股数和股价，不进 net_cash——两个时点混用，
    龄越大偏差越大，方向随事件而反：
      增发 = 股数↑已计、现金↑未计 -> 系统性**低估**
      回购 = 股数↓已计、现金↓未计 -> 系统性**高估**
    实测（INTC 2026-08-30，龄 64 天）：8/18 完成的 $20B 增发，210.5M 股已进
    fwd_shares，$19.7B 现金没进 net_cash，净债务少算 4.2% 市值；该偏差又把 bear
    推出 P/FCF 界外触发假红旗，判断层为消红旗上修 bear.margins，越过 base 破坏排序。

    **一律 yellow，绝不 red**：red 会把这条打回判断层，而它是*数据事实*不是假设，
    判断层能"修"它的唯一途径就是扭曲假设——那正是上面那条级联的成因。

    dividends_quarterly：facts 里的季度分红序列。分红是股数差检查唯一的盲区
    （分红不改股数），所以近四季有没有分红决定文案分叉——对不分红的标的
    （AMZN 实测该序列为空、INTC 2024-08 起停发）再提"请确认分红流出"是噪音。
    判据放在函数内而不是调用点，接线才一起被测到（此前调用点恒传 True
    的变异逃过了全部用例）。措辞用"未见分红记录"而非"无分红"：这是证据不是
    事实断言——抽取真出洞时不该由这条替它打包票。

    纯函数（不读全局、不写 out），便于按 test_pure 的 ast 抽取手法直接单测。
    """
    vt = vintage or {}
    age = vt.get("age_days")
    declared = "post_period_capital_events" in cfg
    ppce = cfg.get("post_period_capital_events") or []
    mcap = cfg.get("mcap") or 0
    end = vt.get("report_end", "?")

    if ppce:
        net = sum(float(e.get("amount_musd") or 0) for e in ppce)
        desc = "；".join(
            f"{e.get('date', '?')} {e.get('kind', '?')} {float(e.get('amount_musd') or 0):+,.0f}M"
            for e in ppce)
        return [["yellow", f"期后资本事件已声明（净 {net:+,.0f}M"
                           + (f"，占市值 {abs(net) / mcap:.1%}" if mcap else "") + f"）：{desc}。"
                           f"请确认 net_cash={cfg['net_cash']:,.0f}M 已把它们算进去——"
                           "引擎不自动调整，net_cash 的最终值由判断层负责"]]
    if age is None or age <= 45:
        return []

    # 声明 [] 不足以静默：AAPL 2026-08-31 实测——判断层把 fwd_shares 从 14,715 减到
    # 14,560（明确建模了净回购），net_cash 却停在报告期 +62,173M 一分没扣，然后写 []
    # 把警告关掉。10-Q 摆着九个月回购 $62,094M + 分红 $11,778M（约 $24.6B/季）。
    # 所以不听声明，改看**可机械证明的自相矛盾**：股数侧建模了回购/增发，
    # 现金侧却用报告期口径——同一件事只记了一半。
    d = cfg["shares"] - cfg["fwd_shares"]      # >0 净回购，<0 净增发
    if cfg["shares"] and abs(d) / cfg["shares"] > 0.005:
        what = "净回购" if d > 0 else "净增发"
        direction = ("现金流出未从 net_cash 扣除 → 系统性**高估**" if d > 0
                     else "现金流入未计入 net_cash → 系统性**低估**")
        tail_msg = ("post_period_capital_events 声明为空，与股数侧的假设矛盾，请复核"
                    if declared else
                    "请在 post_period_capital_events 里声明并让 net_cash 反映最终值")
        return [["yellow", f"时点不一致：fwd_shares {cfg['fwd_shares']:,.0f}M 较报告期股数 "
                           f"{cfg['shares']:,.0f}M 差 {abs(d):,.0f}M（{abs(d) / cfg['shares']:.1%}，"
                           f"已建模{what}），但 net_cash={cfg['net_cash']:,.0f}M 仍是 "
                           f"{end} 口径（龄 {age} 天）——{direction}。" + tail_msg]]

    # 股数差不显著只排除了增发/大额回购，**排不掉分红**——分红不改股数，上面那条
    # 机械检查对纯分红股完全失明。KO 2026-08-31 实测：股数只降 0.21%（低于阈值，
    # 不触发），但季度分红 $2.28B、龄 59 天 ≈ $1.5B 已流出 net_cash=-29,500M 却没扣，
    # 占净负债 5%。所以这条不设静默开关。
    _dq = dividends_quarterly or {}
    _pays_div = any(v for _, v in sorted(_dq.items())[-4:])
    if not declared:
        tail_msg = "期后资本事件未声明，请核对增发/回购/并购/分拆/分红后填写"
    elif _pays_div:
        tail_msg = ("期后资本事件已声明为无——但分红不改股数，"
                    "机械检查看不见它，请自行确认分红流出是否重大")
    else:
        # 两条可自动排除的路径都排除了：股数差不显著（无重大回购/增发）、
        # 该标的不分红。剩下的只有并购/分拆/发债偿债——它们既不进股数也不进
        # 分红，只能靠人看。说清楚"还剩什么"，比笼统提醒更有用。
        tail_msg = ("期后资本事件已声明为无，且近四季未见分红记录——"
                    "股数与分红两条路径均已排除，仅剩并购/分拆/发债偿债需人工确认")
    return [["yellow", f"报告期末已 {age} 天"
                       + ("（>100 天，严重滞后）" if age > 100 else "")
                       + f"，net_cash={cfg['net_cash']:,.0f}M 仍是 {end} 口径；" + tail_msg]]

def band_lag_warnings(band, span, now_pe, min_lag=270):
    """PE 带子滞后的时效提示 -> [[level, msg], ...]。

    已实现 NTM PE 的分母是**未来 12 个月真的发生的 EPS**，必须等它发生：
    最后可算日 ≈ 最新已披露季末 − 365，所以 ntm 口径的滞后天然在一年上下
    （2026-08-31 实测 AMZN 395 天、KO 404、AAPL 305）。**这不是 bug，改不掉。**

    但读者拿到的是"现价隐含 32.3x 落在带内第 82 百分位"这种看起来很确定的
    结论，而带子恰好看不见最近一年。此前只有现价跌出 P10/P90 时才提示去看
    无滞后对照，落在带内（最常见的情形）反而一声不吭——AMZN 2026-08-30 那份
    报告整篇只有一条"方法离散度"黄旗，395 天只出现在交易区间正文里。

    纯函数，便于按 test_pure 的 ast 抽取手法直接单测。
    """
    sp = span or {}
    lag = sp.get("lag_days")
    if not lag or lag <= min_lag:
        return []
    tn = (band or {}).get("trailing_nolag") or {}
    gap = tn.get("gap_since_main_band") or {}
    pos = f"（现价 {now_pe:.1f}x）" if now_pe else ""
    msg = (f"交易区间的带子止于 {sp.get('end', '?')}（滞后 {lag} 天）——"
           "已实现 NTM PE 要等未来 12 个月的盈利真的发生，滞后约一年是口径本身的"
           "下限、不是数据缺失。含义：**最近一年的倍数完全不在这个分布里**，"
           f"带内分位{pos}是拿一年前的分布给今天定位。")
    if gap.get("p50"):
        p50s = {str(k): v for k, v in (tn.get("pctiles") or {}).items()}.get("50")
        g_sp = gap.get("span") or {}
        msg += (f" 盲区 {g_sp.get('start', '?')}~{g_sp.get('end', '?')}"
                f"（{gap.get('days', '?')} 天）的 trailing P50 为 {gap['p50']:.1f}x")
        if p50s:
            msg += f"，同期无滞后 trailing 带 P50 {float(p50s):.1f}x"
        msg += "（trailing 分母是过去 12 个月，与主带差一个增长率，不可直接相减）"
    return [["yellow", msg]]

VINTAGE = _vintage(manifest, cfg["date"])
# PENDING_10Q（业绩 8-K 已出、10-Q 未交）：本次运行的判断层已被强制按新闻稿滚动
# TTM（ttm_revenue_override），估的是 8-K 覆盖的那个季度——vintage 归档键若仍用
# 定期报告期末，会把"最新业绩下的估值"归进旧季度的格子，等 10-Q 落地后的运行
# 与它同格混聚。归档键前滚到滚动后的 TTM 末端（= fwd_window.start − 1 天）；
# report_end/age_days 保持定期报告口径不动——时效面板要展示的恰是"10-Q 还没来"。
if cfg.get("pending_10q") and (cfg.get("fwd_window") or {}).get("start") and VINTAGE:
    _fs = date.fromisoformat(cfg["fwd_window"]["start"])
    VINTAGE["vintage_end"] = (_fs - timedelta(days=1)).isoformat()
    VINTAGE["pending_10q"] = True


def dcf(rev0, g0, gN, margins, wacc, tg, net_cash, shares, years=10):
    """返回 (每股, 股权价值, diagnostics)。diagnostics 供 v2 合理性检查与报告红旗区
    使用——第 N 年营收倍数 / 终值占比在此前是函数局部变量，服务层若自行重算会造出
    第三份 DCF 实现（engine + Excel 公式区之外），口径必然漂移，因此由引擎唯一输出。"""
    growths = [g0 + (gN - g0) * i / (years - 1) for i in range(years)]
    rev, pv, fcf_last = rev0, 0.0, 0.0
    for i in range(years):
        rev *= 1 + growths[i]
        fcf = rev * margins[i]
        pv += fcf / (1 + wacc) ** (i + 1)
        fcf_last = fcf
    tv = fcf_last * (1 + tg) / (wacc - tg)
    tv_pv = tv / (1 + wacc) ** years
    equity = pv + tv_pv + net_cash
    diag = dict(yrN_rev_multiple=round(rev / rev0, 2) if rev0 else None,
                tv_pv_share=round(tv_pv / (pv + tv_pv), 4) if pv + tv_pv > 0 else None,
                pv_explicit=round(pv), tv_pv=round(tv_pv))
    return equity / shares, equity, diag


def tail(dic, n):
    items = list(dic.items())[-n:]
    return [k for k, _ in items], [v for _, v in items]


def _pctile_rank(pcts, x):
    """从稀疏分位点表反查 x 的百分位（线性插值）。facts.json 里 key 是字符串。"""
    ks = sorted(int(k) for k in pcts)
    vals = [pcts[str(k)] for k in ks]
    if x <= vals[0]:
        return float(ks[0])
    if x >= vals[-1]:
        return float(ks[-1])
    for i in range(len(ks) - 1):
        if vals[i] <= x <= vals[i + 1]:
            span = vals[i + 1] - vals[i]
            f = (x - vals[i]) / span if span > 0 else 0.0
            return ks[i] + f * (ks[i + 1] - ks[i])
    return 50.0


def pe_band_check(scenario_name, pe, band, ddiag, label="PE", diag_key="pe_vs_history"):
    """目标 PE 相对该票自身历史「已实现前瞻 PE」分布的位置。

    口径对齐：band 由 fetch_facts 以 basis="ntm" 写入——分母是「该日已知的最新
    TTM 期末之后 12 个月实现的稀释 EPS」（按 filed 切换，比日历日起算最多滞后一季）；引擎的 s["pe"] 乘的是 eps1 = TTM×(1+g)，前瞻期同样是
    NTM（见 valuation_service.fwd_window）。两者严格同源，可直接比分位。
    band["basis"] 字段可核对；若哪天改回 forward（按财年切），只在 report_end =
    财年末时才对得上，AMZN/META 这类 12 月财年公司在 Q1~Q3 报告期会系统性错位。

    两者都是 GAAP 基础；判断层若改用剔 SBC 的非 GAAP EPS，分母变大、PE 变小，
    本诊断会系统性偏低，届时不可直接采信。

    阈值刻意保守：只在「该票历史上从未出现过的倍数」或「base 偏离中枢区间」时报
    yellow。低倍数本身不是错——价值下沿本就该落在历史低位附近，这里只要求留痕。

    label/diag_key 参数化后同一套逻辑服务 financials 的 P/TBV 带（trailing 口径，
    分母=当日已知每股 TBV，与 engine 的 tbv_ps 同构）——检查结构完全同型：
    全窗 min/max 管「从未出现过」，base 管子窗中枢并集。
    """
    if not band or not band.get("pctiles"):
        return []
    if band.get("thin_coverage"):
        # 覆盖不足（<250 天）：分布不是"该票的历史"，锚与中枢检查都失去意义——
        # 停用并只在 base 上留一条可见的黄旗（三情景重复三遍是噪声）
        ddiag[diag_key] = {"thin_coverage": True, "days": band["days"],
                           "years": band["years"], "basis": band["basis"]}
        if scenario_name == "base":
            return [["yellow", f"历史{label}带覆盖不足（{band['days']} 天/{band['years']} 年，"
                               "原始数据缺口）——锚与中枢检查停用，倍数假设无历史对照"]]
        return []
    pcts = band["pctiles"]
    rank = _pctile_rank(pcts, pe)
    # base 中枢检查用与判断层 PE 锚同一个窗（近 3 年子窗，见 valuation_service 的
    # band_meta 注入）：锚了子窗 P50 却按全窗 [P10,P90] 打旗会自相矛盾——全窗含
    # 2021 regime，子窗中位低于全窗 P10 的票（倍数已下台阶）恰好每次被冤枉。
    # min/max 的「从未出现过」检查仍看全窗（问题本来就是全历史范围的）。
    _rc = (band.get("recent") or {})
    _rp = _rc.get("pctiles") or {}
    cpcts = _rp if ("10" in _rp and "90" in _rp) else pcts
    cwin = f"近{_rc['years']}年子窗" if cpcts is _rp else f"近{band['years']}年全窗"
    ddiag[diag_key] = {
        "pctile": round(rank, 1), "median": round(pcts["50"], 1),
        "p10": round(pcts["10"], 1), "p90": round(pcts["90"], 1),
        "min": round(band["min"], 1), "max": round(band["max"], 1),
        "anchor_window": cwin,
        "anchor_p50": (round(cpcts["50"], 1) if "50" in cpcts else None),
        "years": band["years"], "days": band["days"], "basis": band["basis"]}
    ctx = (f"（近 {band['years']} 年已实现{label}：中位 {pcts['50']:.1f}x，"
           f"实际区间 {band['min']:.1f}~{band['max']:.1f}x，{band['days']} 个交易日）")
    if not band["min"] <= pe <= band["max"]:
        return [["yellow", f"{scenario_name} 目标 {label} {pe:.1f}x 落在该票近 {band['years']} 年"
                           f"从未出现过的区间之外{ctx}——不必然是错，"
                           "但请在 rationale 中给出依据"]]
    if scenario_name == "base":
        # 容许区间 = 子窗 [P10,P90] ∪ 锚 P50 的 ±ANCHOR_TOL——prompt 里写的锚纪律就是
        # 「偏离 P50 ±15% 以上要给证据」，若这里只按 [P10,P90] 打旗，倍数分布窄的票
        # （MSFT/AMZN 子窗半宽仅 ±12%）会出现"按纪律给了证据的偏离照样吃黄旗"，
        # 两层规则各说各话。取并集后：纪律内不打旗，纪律外必打旗，口径一致。
        ANCHOR_TOL = 0.15
        lo_ok, hi_ok = cpcts["10"], cpcts["90"]
        if "50" in cpcts:
            lo_ok = min(lo_ok, cpcts["50"] * (1 - ANCHOR_TOL))
            hi_ok = max(hi_ok, cpcts["50"] * (1 + ANCHOR_TOL))
        if not lo_ok <= pe <= hi_ok:
            return [["yellow", f"base 目标 {label} {pe:.1f}x 界外 {cwin} [P10,P90]="
                               f"[{cpcts['10']:.1f},{cpcts['90']:.1f}]x（并 P50±"
                               f"{ANCHOR_TOL:.0%} 后为 [{lo_ok:.1f},{hi_ok:.1f}]x，"
                               f"全窗第 {rank:.0f} 百分位）{ctx}——base 应为中枢情景"]]
    return []


ttm = facts["ttm"]
rev0 = ttm["revenue"]["value"] / 1e6

# XBRL 严重滞后时（外国发行人 6-K 无季度 XBRL；或业绩 8-K 已出而 10-Q 未交），
# 判断层可从财报原文提取真实 TTM 营收作为基准覆盖（与 net_cash/adj_ni 同属
# "LLM 提取事实并注明出处"，非算术）。必须在 MODE 分支之前生效：financials
# （银行/券商/fintech）恰好是 10-Q 滞后业绩公布 2-6 周的重灾区，此前这段落在
# 分支之后，financials 拿到 override 也会静默按上一季度基准估值。
rev0_note = ""
rev0_reported = rev0   # 覆盖前的 XBRL 口径营收——P/FCF 红旗按利润率换算用
if cfg.get("ttm_revenue_override"):
    rev0 = float(cfg["ttm_revenue_override"])
    rev0_note = cfg.get("ttm_revenue_note", "判断层按财报原文修正的 TTM 营收基准")

if MODE == "financials":
    # ================= 金融股：PE 法 + P/TBV 法 =================
    ttm_m = {k: round(ttm[k]["value"] / 1e6)
             for k in ("revenue", "pretax_income", "net_income")}
    ttm_m["revenue"] = round(rev0)   # 覆盖生效时与情景基准/Excel 公式区同源
    eq = facts["equity_instant"]
    gw = facts.get("goodwill_instant") or {}
    it = facts.get("intangibles_instant") or {}
    eq_end, eq_val = list(eq.items())[-1]
    tbv = round((eq_val - (list(gw.values())[-1] if gw else 0)
                 - (list(it.values())[-1] if it else 0)) / 1e6)
    # 每股 TBV 的分母用**时点流通股**，与 ptbv_band 的历史序列同源（PR #3 review）：
    # TBV 是时点存量，配加权平均稀释股数是流量均值配存量——增发季（SOFI 型）加权数
    # ≈期末股数的一半，当前每股 TBV 被高估近一倍，拿它去比一条按时点股数算出来的
    # 历史带，等于两个口径相减，锚给出的目标价系统性偏错。
    # 注意只换这一个分母：adj_eps 必须继续用加权稀释股数（GAAP EPS 口径），
    # 把 cfg["shares"] 全局换掉会把 EPS 一起算错。
    _sho = facts.get("shares_outstanding_instant") or {}
    if _sho:
        tbv_shares = round(list(_sho.values())[-1] / 1e6, 1)
        tbv_shares_basis = "instant"
    else:
        tbv_shares = cfg["shares"]
        tbv_shares_basis = "weighted_avg_fallback"
    tbv_ps = tbv / tbv_shares

    out = dict(
        ticker=cfg["ticker"], name=cfg["name"], date=cfg["date"], mode="financials",
        # financials 语义 v2（2026-08-14）：P/TBV 带锚（判断层注入 + ptbv_band_check）
        # 改变 base ptbv 的产生方式与目标价水平——与 standard v2→v3 的理由同构，
        # 锚前(v1)/锚后(v2) 金融股样本在 trend/compare 里必须按版本隔离
        semantics_version=2,
        blend_weights={m: BLEND_W[m] for m in ("pe", "ptbv")},
        meta=dict(price=cfg["price"], mcap=cfg["mcap"], shares=cfg["shares"],
                  fwd_shares=cfg["fwd_shares"], fwd_label=cfg["fwd_label"],
                  tbv=tbv, tbv_ps=round(tbv_ps, 2), tbv_asof=eq_end,
                  # 分母与口径都要可见：报告/verify 要用同一个数，读者也要能看出
                  # 这次是时点股数还是退回了加权稀释
                  tbv_shares=tbv_shares, tbv_shares_basis=tbv_shares_basis,
                  adr_multiple=cfg.get("adr_multiple", 1.0),
                  currency=cfg.get("currency", "USD"), vintage=VINTAGE,
                  # XBRL 结构化数据的期末——与 vintage.report_end（定期报告期末）
                  # 是两回事：外国发行人 6-K 无季度 XBRL 时二者能差一年以上
                  # （TSM 实测 report_end=2026-06-30 而 data_latest=2024-12-31），
                  # 报告里标注"哪些行还停在旧窗口"必须用这个。
                  data_latest=facts.get("data_latest")),
        ttm=ttm_m, adj_ni=cfg["adj_ni"], adj_eps=round(cfg["adj_ni"] / cfg["shares"], 2),
        rote_ttm=round(cfg["adj_ni"] / tbv, 4),
        notes=cfg["notes"], rationale=cfg["rationale"], adj_note=cfg["adj_note"],
        rev0_note=rev0_note, scenarios={}, manifest=manifest,
    )

    for name, s in cfg["scenarios"].items():
        rev1 = rev0 * (1 + s["g"])
        ni1 = rev1 * s["nm"]
        eps1 = ni1 / cfg["fwd_shares"]
        pe_ps = eps1 * s["pe"]
        ptbv_ps = tbv_ps * s["ptbv"]
        rote1 = ni1 / tbv
        justified = (rote1 - s["tg"]) / (s["wacc"] - s["tg"])
        _wsum = BLEND_W["pe"] + BLEND_W["ptbv"]
        blend = ((pe_ps * BLEND_W["pe"] + ptbv_ps * BLEND_W["ptbv"]) / _wsum
                 if _wsum > 0 else (pe_ps + ptbv_ps) / 2)
        # P/TBV 带检查（standard 的 pe_band_check 同一套逻辑，label 换 P/TBV）：
        # financials 此前无 warnings/diagnostics 通道——TODO 里挂了两轮的项，
        # 带子进来时必须一起开通，否则检查结果没地方去
        ddiag = {}
        warnings = pe_band_check(name, s["ptbv"], facts.get("ptbv_band"), ddiag,
                                 label="P/TBV", diag_key="ptbv_vs_history")
        out["scenarios"][name] = dict(
            assumptions=s, rev1=round(rev1), ni1=round(ni1), eps1=round(eps1, 2),
            pe_target=round(pe_ps, 1), ptbv_ps=round(ptbv_ps, 1),
            rote1=round(rote1, 4), justified_ptbv=round(justified, 2),
            justified_ps=round(tbv_ps * justified, 1),
            blend=round(blend, 1), upside=round(blend / cfg["price"] - 1, 4),
            fwd_pe=round(cfg["price"] / eps1, 1),
            method_spread=round(max(pe_ps, ptbv_ps) / min(pe_ps, ptbv_ps), 2)
            if min(pe_ps, ptbv_ps) > 0 else None,
            diagnostics=ddiag, warnings=warnings,
        )

    # 敏感性：base 口径 justified P/TBV 每股价值 = f(WACC, 永续g)
    s = cfg["scenarios"]["base"]
    rote_b = out["scenarios"]["base"]["rote1"]
    sens = {}
    for w in (s["wacc"] - 0.01, s["wacc"] - 0.005, s["wacc"], s["wacc"] + 0.005, s["wacc"] + 0.01):
        row = {}
        for g in (s["tg"] - 0.01, s["tg"] - 0.005, s["tg"], s["tg"] + 0.005, s["tg"] + 0.01):
            row[round(g, 4)] = round(tbv_ps * (rote_b - g) / (round(w, 4) - g), 1)
        sens[round(w, 4)] = row
    out["sensitivity"] = sens

    a_keys, a_rev = tail(facts["revenue_annual"], 8)
    hist_a = dict(labels=a_keys, rev=[round(v / 1e6) for v in a_rev])
    for m, key in (("op", "pretax_income_annual"), ("ni", "net_income_annual")):
        hist_a[m] = [round(facts[key].get(k, 0) / 1e6) for k in a_keys]
    hist_a["eps"] = [facts["eps_diluted_annual"].get(k) for k in a_keys]
    q_keys, q_rev = tail(facts["revenue_quarterly"], 8)
    hist_q = dict(labels=q_keys, rev=[round(v / 1e6) for v in q_rev])
    for m, key in (("op", "pretax_income_quarterly"), ("ni", "net_income_quarterly")):
        hist_q[m] = [round(facts[key].get(k, 0) / 1e6) for k in q_keys]
    out["history"] = dict(annual=hist_a, quarterly=hist_q)

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"===== {cfg['ticker']} @ ${cfg['price']} (financials) =====")
    print(f"TTM: rev {ttm_m['revenue']:,} pretax {ttm_m['pretax_income']:,} "
          f"adjNI {cfg['adj_ni']:,} adjEPS {out['adj_eps']} | TBV {tbv:,} "
          f"(${out['meta']['tbv_ps']}/股, ROTE {out['rote_ttm']:.1%})")
    for n, v in out["scenarios"].items():
        print(f"{n:5s}| EPS1 {v['eps1']:7.2f} | PE法 {v['pe_target']:8.1f} | "
              f"P/TBV法 {v['ptbv_ps']:8.1f} (justified {v['justified_ptbv']:.2f}x) | "
              f"综合 {v['blend']:8.1f} | {v['upside']:+.1%}")
        for lv, msg in v["warnings"]:
            print(f"      {'⛔' if lv == 'red' else '⚠️'} {msg}")
    sys.exit(0)

# ================= standard：PE + DCF + SOTP =================
ttm_m = {k: round(ttm[k]["value"] / 1e6) for k in ("revenue", "op_income", "net_income", "cfo", "capex")}
ttm_m["fcf"] = ttm_m["cfo"] - ttm_m["capex"]
ttm_m["revenue"] = round(rev0)   # 覆盖生效时 Excel 公式区与引擎共用同一基准

# P/FCF 红旗的分母：override 场景下 XBRL 的 fcf 绝对值是多年前的，直接除会对增长了
# 2-3 倍的公司产生每次必现的假红旗（还会因 reds 不清空而永久阻断连续性锚持久化）——
# 用"陈旧 FCF 利润率 × 当前营收基准"换算到同一量级
fcf_base = (ttm_m["fcf"] / rev0_reported * rev0) if rev0_reported else ttm_m["fcf"]

# v2（semantics_version=2, 2026-07-22）：主分部利润占比 >= 0.85 时 SOTP 与 PE 法
# 实为同一笔盈利乘两次倍数（无分部折价可解锁），退化为参考项不入综合——
# 综合 = PE 法与 DCF 各 50%。占比 < 0.85 时维持三法均值。
SOTP_SEG1_CAP = 0.85
sotp_in_blend = cfg["seg1_share"] < SOTP_SEG1_CAP

out = dict(
    ticker=cfg["ticker"], name=cfg["name"], date=cfg["date"], mode="standard",
    # 语义版本属于引擎实现而非 config 声明：本引擎无条件执行 v2 计算（SOTP 降级/
    # 诊断），重放 v1 老 config 时若透传声明值会得到"标 v1 却是两法综合"的自相矛盾
    # 输出，compare.py 的 1==1 还会静默掉它本该拦的口径跳变告警。
    # v3（2026-08-14）：判断层 PE 锚（历史 NTM 带子窗 P50）纳入语义——锚改变 base
    # PE 的产生方式与目标价水平，锚前(≤v2)/锚后(v3) 样本不可直接对比或混聚
    semantics_version=3,
    config_semantics_version=cfg.get("semantics_version", 1),
    # 只记录可能参与综合的腿：SOTP 降级为参考项时 W_SOTP 惰性——记进去会让
    # compare/trend 把两次数值完全相同的运行当成口径不同
    blend_weights={m: BLEND_W[m]
                   for m in (("pe", "dcf") + (("sotp",) if sotp_in_blend else ()))},
    meta=dict(price=cfg["price"], mcap=cfg["mcap"], shares=cfg["shares"],
              fwd_shares=cfg["fwd_shares"], net_cash=cfg["net_cash"], fwd_label=cfg["fwd_label"],
              seg1=cfg["seg1"], seg2=cfg["seg2"], seg1_share=cfg["seg1_share"],
              sotp_in_blend=sotp_in_blend,
              adr_multiple=cfg.get("adr_multiple", 1.0), currency=cfg.get("currency", "USD"),
              vintage=VINTAGE, data_latest=facts.get("data_latest")),
    ttm=ttm_m, adj_ni=cfg["adj_ni"], adj_eps=round(cfg["adj_ni"] / cfg["shares"], 2),
    notes=cfg["notes"], rationale=cfg["rationale"],
    net_cash_note=cfg["net_cash_note"], adj_note=cfg["adj_note"],
    other_income=cfg["other_income"], rev0_note=rev0_note, scenarios={}, manifest=manifest,
)

for name, s in cfg["scenarios"].items():
    rev1 = rev0 * (1 + s["g"])
    op1 = rev1 * s["opm"]
    ni1 = (op1 + cfg["other_income"]) * (1 - s["tax"])
    eps1 = ni1 / cfg["fwd_shares"]
    pe_target = eps1 * s["pe"]
    dcf_ps, dcf_eq, ddiag = dcf(rev0, s["g0"], s["gN"], s["margins"], s["wacc"], s["tg"],
                                cfg["net_cash"], cfg["shares"])
    sotp_eq = (op1 * cfg["seg1_share"] * s["m1"]
               + op1 * (1 - cfg["seg1_share"]) * s["m2"] + cfg["net_cash"])
    sotp_ps = sotp_eq / cfg["shares"]
    # 近零利润守卫：eps1<=0 或情景 opm<2% 时 PE 腿病态——盈利不是市场给这类票
    # 定价的基础，「近零盈利 × 正常倍数 ≈ 0」不是估值是除法事故（COIN 实测
    # g 全负 + opm≈0，PE 腿把 blend 拖出 60% 全距）。PE 腿标 n.m. 退出综合，
    # 综合退化为 DCF(+SOTP)；有 ps_band 时在红旗区给 P/S 参考价（不入综合）。
    # pe_target 仍照算并展示——退出综合 ≠ 隐藏。
    pe_nm = eps1 <= 0 or s["opm"] < 0.02
    # SOTP 腿同理：EV/EBIT 对负 EBIT 不成立。此前只守 PE 腿，未盈利标的（RKLB 型
    # 结构性营业亏损）的 sotp_eq = 负 EBIT×倍数 + 净现金 会照旧进综合——校验层要求
    # 亏损情景写 m1=m2=0，那样 sotp_ps 退化成"每股净现金"，一个纯现金数字冒充估值腿
    # 混进综合，同样不是估值。
    sotp_nm = op1 <= 0
    blend_methods = ((["pe"] if not pe_nm else []) + ["dcf"]
                     + (["sotp"] if sotp_in_blend and not sotp_nm else []))
    methods = ([] if pe_nm else [pe_target]) + [dcf_ps] \
        + ([sotp_ps] if sotp_in_blend and not sotp_nm else [])
    _vals = {"pe": pe_target, "dcf": dcf_ps, "sotp": sotp_ps}
    _wsum = sum(BLEND_W[m] for m in blend_methods)
    blend = (sum(_vals[m] * BLEND_W[m] for m in blend_methods) / _wsum
             if _wsum > 0 else sum(methods) / len(methods))
    spread = (round(max(methods) / min(methods), 2)
              if len(methods) > 1 and min(methods) > 0 else None)

    # ---- v2 经济合理性诊断（red=假设可修复，服务层可据此打回判断层一次；
    #      yellow=呈现层警示。全部随报告红旗区展示，不静默）----
    warnings = []
    if ddiag["yrN_rev_multiple"] and ddiag["yrN_rev_multiple"] > 8:
        warnings.append(["red", f"DCF 第10年营收为 TTM 的 {ddiag['yrN_rev_multiple']:.1f} 倍"
                                "（>8x，隐含 10 年 CAGR >23%）——增长路径过于激进"])
    if ddiag["tv_pv_share"] and ddiag["tv_pv_share"] > 0.75:
        warnings.append(["red", f"终值折现占 EV {ddiag['tv_pv_share']:.0%}（>75%）——"
                                "估值几乎全押在永续段，对 wacc-tg 极端敏感"])
    if fcf_base > 0.02 * rev0:
        p_fcf = dcf_eq / fcf_base
        ddiag["dcf_equity_over_ttm_fcf"] = round(p_fcf, 1)
        if not 5 <= p_fcf <= 90:
            warnings.append(["red", f"DCF 隐含股权价值为 TTM FCF 的 {p_fcf:.1f} 倍"
                                    "（界外 [5,90]）——路径假设与当前现金流量级脱节"])
    if cfg["adj_ni"] > 0:
        p_ni = blend * cfg["shares"] / cfg["adj_ni"]
        ddiag["blend_p_adjni"] = round(p_ni, 1)
        if not 6 <= p_ni <= 60:
            warnings.append(["yellow", f"综合目标价隐含 P/调整后净利 {p_ni:.1f}x（界外 [6,60]）"])
    if spread and spread > 2:
        warnings.append(["yellow", f"方法离散度 {spread}x（>2x）——各法分歧大，综合可信度降低"])
    if dcf_ps <= 0 and "dcf" in blend_methods:
        # margins 全程深负 + 净现金为负时 DCF 腿可以合法算出 <=0——PE 腿又被守卫
        # 退出时，负数会独腿成为"综合目标价"无声进产线。red：打回判断层复审
        warnings.append(["red", f"DCF 每股价值 {dcf_ps:.1f} <= 0（FCF 路径全程亏损或"
                                "净现金深负）——综合失去有效支撑腿，请修正 margins 路径"
                                "或声明永久受损"])
    if pe_nm:
        _ps_ref = ""
        _psb = facts.get("ps_band") or {}
        _psp = (_psb.get("recent") or {}).get("pctiles") or _psb.get("pctiles") or {}
        if "50" in _psp and cfg["fwd_shares"]:
            _rps1 = rev1 / cfg["fwd_shares"]
            ddiag["ps_ref"] = {
                "basis": _psb.get("basis"),
                "window": ("recent" if (_psb.get("recent") or {}).get("pctiles") else "full"),
                "ps_p50": round(float(_psp["50"]), 2), "rps1": round(_rps1, 2),
                "px": {q: round(float(_psp[q]) * _rps1, 1)
                       for q in ("25", "50", "75") if q in _psp}}
            _ps_ref = (f"；P/S 参考（不入综合）：历史 P/S P50 {float(_psp['50']):.2f}x × "
                       f"{name} 每股营收 {_rps1:.2f} ≈ {float(_psp['50']) * _rps1:.1f}")
        warnings.append(["yellow",
                         f"{name} PE 腿 n.m.（eps1 {eps1:.2f} / opm {s['opm']:.1%}——近零或"
                         "负利润下『盈利×倍数』无定价意义），综合退化为 "
                         + "+".join(m.upper() for m in blend_methods) + _ps_ref])
    if sotp_in_blend and sotp_nm:
        warnings.append(["yellow",
                         f"{name} SOTP 腿 n.m.（{cfg['fwd_label']} 营业利润 {op1:,.0f}M <= 0——"
                         "EV/EBIT 对负 EBIT 不适用），已剔出综合；综合仅取 "
                         + "+".join(m.upper() for m in blend_methods)])
    # 目标 PE 为 0（校验层对亏损情景的约定值）或 PE 腿已 n.m. 时不做历史带比对：
    # 它比的是"目标倍数在自身历史分布里的位置"，对一个已声明不适用的倍数比对，
    # 只会产出必然的「该票历史上从未出现过」黄旗
    if not pe_nm:
        warnings += pe_band_check(name, s["pe"], facts.get("pe_band"), ddiag)

    out["scenarios"][name] = dict(
        assumptions=s, rev1=round(rev1), op1=round(op1), ni1=round(ni1),
        eps1=round(eps1, 2), pe_target=round(pe_target, 1),
        dcf_ps=round(dcf_ps, 1), sotp_ps=round(sotp_ps, 1),
        blend=round(blend, 1), upside=round(blend / cfg["price"] - 1, 4),
        # eps1<=0 时 fwd_pe 是负数假读数，置 None（报告/归档按缺失处理）
        fwd_pe=round(cfg["price"] / eps1, 1) if eps1 > 0 else None,
        # blend 由哪几条腿构成——build_report 的综合公式必须与引擎同构，
        # 否则 verify_report 的交叉核对会在 PE 腿 n.m. 时假 FAIL
        blend_methods=blend_methods,
        method_spread=spread, diagnostics=ddiag, warnings=warnings,
    )

# base 锚检查：综合与市价偏离 >35% 本身不是错（估值可以偏离市价），
# 但必须显式看到并辩护，而不是三档整队随 base 静默平移
_dev = out["scenarios"]["base"]["blend"] / cfg["price"] - 1
if abs(_dev) > 0.35:
    out["scenarios"]["base"]["warnings"].append(
        ["yellow", f"base 综合较现价偏离 {_dev:+.0%}（>±35%）——请核对 base 假设"
                   "或在注记中显式说明为何与市场定价分歧"])

out["scenarios"]["base"]["warnings"] += vintage_warnings(
    cfg, VINTAGE, facts.get("dividends_quarterly"))

# ---- 价值交易区间：历史已实现 NTM PE 分位 × base 前瞻 EPS ----
# 与三情景互为对照：情景是「基本面情景各自的公允价」（bear=EPS↓×PE↓ 双压），
# 这行回答「按该票自己的历史倍数分布，base 盈利下会交易在哪个区间」——
# 参考表用手拍带子回答的问题，这里用分位数回答。锚窗优先近 3 年子窗
# （pe_band.recent，避开 2021 regime），回退全窗；facts.json round-trip 后
# recent.pctiles 的键是字符串（与 pctiles 同，见 _pctile_rank 注）。
_band = facts.get("pe_band") or {}
_rc = _band.get("recent") or {}
_use = _rc.get("pctiles") or _band.get("pctiles") or {}
_e1 = out["scenarios"]["base"]["eps1"]
# base PE 腿 n.m.（eps1<=0 或 opm<2%，与情景守卫同判据）时整块跳过：PE 分位 ×
# 近零/负 EPS 渲染出的"交易区间"是除法事故不是估值（opm 校验只保证 op1>0，
# other_income 无下界，op1+other_income 可以为负）。显式说原因，不静默。
_base_pe_nm = "pe" not in out["scenarios"]["base"]["blend_methods"]
if _base_pe_nm and _use:
    out["scenarios"]["base"]["warnings"].append(
        ["yellow", f"base PE 腿 n.m.（前瞻 EPS {_e1:.2f}）——交易区间（历史 PE 分位 × "
                   "base EPS）无意义，本次不出该块；参考红旗区的 P/S 参考价"])
if _band.get("thin_coverage") and _use:
    out["scenarios"]["base"]["warnings"].append(
        ["yellow", f"历史 PE 带覆盖不足（{_band.get('days')} 天）——交易区间不出"])
if (not _base_pe_nm and not _band.get("thin_coverage")
        and all(q in _use for q in ("10", "25", "50", "75", "90"))):
    _tr_pe = {q: round(float(_use[q]), 2) for q in ("10", "25", "50", "75", "90")}
    _sub = bool(_rc.get("pctiles"))
    # 现价隐含的前瞻倍数 = 价 ÷ base NTM EPS。它与本带子**同一个分母概念**
    # （都是"价 ÷ 未来 12 个月盈利"），因此可以直接比分位，不像 trailing 那样差着
    # 一个增长率。这个数引擎本来就算了（scenarios.base.fwd_pe），此前从未与带子
    # 连起来——于是"市场当前付多少倍"和"目标假设多少倍"各说各话，读者看不到
    # 目标价里有多少是倍数回归。唯一的口径差：带子分母是已实现 EPS，这里是估计值。
    # 建模 NTM EPS ÷ GAAP TTM EPS —— trailing→NTM 的换算因子（理由见下方 dict 注释）
    _gaap_ttm_eps = (ttm_m["net_income"] / cfg["shares"]) if cfg.get("shares") else None
    _ntm_eps_growth = (round(_e1 / _gaap_ttm_eps - 1, 4)
                       if _gaap_ttm_eps and _gaap_ttm_eps > 0 and _e1 else None)
    _now_pe = out["scenarios"]["base"]["fwd_pe"]
    _tgt_pe = cfg["scenarios"]["base"]["pe"]
    # 带外关系必须与百分位分开表达（PR #5 review）：_pctile_rank 对低于 P10 的值
    # 一律返回 10、高于 P90 一律返回 90（子窗只有 P10~P90 五档）。于是 20.0x 与
    # 25.7x 都会被报成"第 10 百分位"，与同时打出的"跌出下沿 P10"黄旗自相矛盾。
    # 规则：只有落在 [P10, P90] 内才给数值分位，带外只给关系。
    if not _now_pe:
        _now_rank, _now_rel = None, None
    elif _now_pe < _tr_pe["10"]:
        _now_rank, _now_rel = None, "below_p10"
    elif _now_pe > _tr_pe["90"]:
        _now_rank, _now_rel = None, "above_p90"
    else:
        _now_rank, _now_rel = round(_pctile_rank(_use, _now_pe), 1), "in_band"
    # 带内位置的可读文案，四个消费面共用一句，避免各写各的
    _now_pos = ({"below_p10": f"低于带子下沿 P10（{_tr_pe['10']:.1f}x）",
                 "above_p90": f"高于带子上沿 P90（{_tr_pe['90']:.1f}x）"}.get(_now_rel)
                or (f"带内第 {_now_rank:.0f} 百分位" if _now_rank is not None else None))
    out["trading_range"] = dict(
        basis=_band.get("basis"),
        window=(f"近{_rc['years']}年" if _sub else f"近{_band.get('years')}年"),
        days=(_rc.get("days") if _sub else _band.get("days")),
        # 窗口真实起止 + 滞后：ntm 口径结构性缺最近约一年，"近3年"不是区间跨度也
        # 不是"到今天为止的3年"——不报出来会被两头误读
        span=((_rc.get("span") if _sub else None) or _band.get("span")),
        eps_window=cfg["fwd_label"],
        eps1_base=_e1, pe=_tr_pe,
        px={q: round(v * _e1, 1) for q, v in _tr_pe.items()},
        full_window_p50=(round(float(_band["pctiles"]["50"]), 2)
                         if _band.get("pctiles") else None),
        fwd_pe_now=_now_pe, fwd_pe_now_pctile=_now_rank,
        fwd_pe_now_vs_band=_now_rel, fwd_pe_now_position=_now_pos,
        # px50/价 − 1 = (P50×eps1)/(fwd_pe_now×eps1) − 1 = P50/fwd_pe_now − 1 是恒等式。
        # 但它**不**意味着"与盈利预测无关"（PR #5 review）：fwd_pe_now 本身 = 价÷eps1，
        # eps1 变了中位价与本比值会同步变。它成立的前提是**给定同一个 base EPS**——
        # 此时现价与中位价用的是同一个分母，两者的差距只能是倍数差距，不能由盈利
        # 预测解释。措辞按此口径统一（Excel/stdout/前端/文档同源）。
        mult_reversion_to_p50=(round(_tr_pe["50"] / _now_pe - 1, 4) if _now_pe else None),
        mult_reversion_to_target=(round(_tgt_pe / _now_pe - 1, 4) if _now_pe else None),
        target_pe=_tgt_pe,
        drift=_band.get("drift"), trailing_nolag=_band.get("trailing_nolag"),
        # trailing → NTM 的换算因子必须是 **EPS 增速**，不是营收增速（PR #5 review）：
        #   trailing_PE = 价÷TTM_EPS，NTM_PE = 价÷NTM_EPS
        #   ⇒ NTM_PE = trailing_PE ÷ (NTM_EPS / TTM_EPS)
        # 此前用 cfg.scenarios.base.g，而 g 在 prompt 里被明确定义为**营收**增速
        # （"g 的定义 = 该窗口营收 ÷ TTM营收 − 1"）。利润率、税率、其他收益、股数
        # 变化都会让 EPS 增速与营收增速显著分叉（利润率扩张 + 回购的票尤其）。
        # 分母取 **GAAP** TTM EPS：带子的分子是价、分母是 NetIncomeLoss 口径的
        # 已实现 EPS，换算因子必须与它同源；用调整后 EPS 会引入第二重口径错配。
        # 拿不到正的 GAAP TTM EPS 时置 None，消费侧据此**不显示**换算值。
        trailing_ntm_eps_growth=_ntm_eps_growth)

    # 现价隐含倍数跌出/冲破带子时留痕：这是"目标 PE 的分位"之外的另一半信息——
    # 目标锚在 P50 不代表便宜，市场当前付的倍数在哪同样是事实。跌出 P10 尤其要说：
    # 此时目标价的涨幅几乎全部押在"倍数回到中枢"，而带子恰好看不见最近一年
    # 究竟发生了什么（滞后 span.lag_days 天）。
    _trw = out["trading_range"]
    out["scenarios"]["base"]["warnings"] += band_lag_warnings(_band, _trw.get("span"), _now_pe)
    if _now_pe and (_now_pe < _tr_pe["10"] or _now_pe > _tr_pe["90"]):
        _side = "跌出下沿 P10" if _now_pe < _tr_pe["10"] else "冲破上沿 P90"
        _sp = _trw.get("span") or {}
        out["scenarios"]["base"]["warnings"].append(["yellow",
            f"现价隐含前瞻倍数 {_now_pe:.1f}x {_side}（带子 P10~P90 = "
            f"{_tr_pe['10']:.1f}~{_tr_pe['90']:.1f}x）——目标 PE {_tgt_pe:g}x 相对现价隐含"
            f"{_trw['mult_reversion_to_target']:+.0%} 的纯倍数变动；"
            f"而带子止于 {_sp.get('end', '?')}（滞后 {_sp.get('lag_days', '?')} 天），"
            "最近一年的倍数不在分布内，请用下方 trailing 对照自行判断 regime 是否已变"])

# Rule of 40 透传（fetch_facts 计算，standard 模式）：营收增速+利润率的标尺，
# 与 pe_band 同属"倍数值不值得给"的判断参照，进报告与 prompt 元数据
if facts.get("rule_of_40"):
    out["rule_of_40"] = facts["rule_of_40"]

# 反向 DCF：base 口径下现价隐含的起始增速
s = cfg["scenarios"]["base"]
lo, hi = -0.20, 0.80
for _ in range(60):
    mid = (lo + hi) / 2
    _, eq, _d = dcf(rev0, mid, s["gN"], s["margins"], s["wacc"], s["tg"],
                    cfg["net_cash"], cfg["shares"])
    if eq < cfg["mcap"]:
        lo = mid
    else:
        hi = mid
out["reverse_dcf"] = round(mid, 4)

# base 敏感性 WACC × 永续g
sens = {}
for w in (s["wacc"] - 0.01, s["wacc"] - 0.005, s["wacc"], s["wacc"] + 0.005, s["wacc"] + 0.01):
    row = {}
    for g in (s["tg"] - 0.01, s["tg"] - 0.005, s["tg"], s["tg"] + 0.005, s["tg"] + 0.01):
        ps, _, _d = dcf(rev0, s["g0"], s["gN"], s["margins"], round(w, 4), round(g, 4),
                        cfg["net_cash"], cfg["shares"])
        row[round(g, 4)] = round(ps)
    sens[round(w, 4)] = row
out["sensitivity"] = sens

a_keys, a_rev = tail(facts["revenue_annual"], 8)
hist_a = dict(labels=a_keys, rev=[round(v / 1e6) for v in a_rev])
for m, key in (("op", "op_income_annual"), ("ni", "net_income_annual"),
               ("cfo", "cfo_annual"), ("capex", "capex_annual")):
    hist_a[m] = [round(facts.get(key, {}).get(k, 0) / 1e6) for k in a_keys]
hist_a["eps"] = [facts["eps_diluted_annual"].get(k) for k in a_keys]

q_keys, q_rev = tail(facts["revenue_quarterly"], 8)
hist_q = dict(labels=q_keys, rev=[round(v / 1e6) for v in q_rev])
for m, key in (("op", "op_income_quarterly"), ("ni", "net_income_quarterly")):
    hist_q[m] = [round(facts[key].get(k, 0) / 1e6) for k in q_keys]
out["history"] = dict(annual=hist_a, quarterly=hist_q)

json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

print(f"===== {cfg['ticker']} @ ${cfg['price']} (semantics v{out['semantics_version']}) =====")
print(f"TTM: rev {ttm_m['revenue']:,} op {ttm_m['op_income']:,} adjNI {cfg['adj_ni']:,} "
      f"adjEPS {out['adj_eps']} FCF {ttm_m['fcf']:,}")
print(f"反向DCF隐含起始增速: {out['reverse_dcf']:.1%}（base 利润率/WACC 条件下）")
if out.get("trading_range"):
    _tr = out["trading_range"]
    _sp = _tr.get("span") or {}
    # 标签写全三件事：①价格对应的是哪 12 个月的盈利 ②倍数取自哪段真实日期、多少天、
    # 滞后多久 ③中位相对现价的涨幅全部来自倍数回归。此前只写"近3年PE带×baseEPS"，
    # "近3年"会被读成区间的时间跨度，而滞后一年这件事完全不可见。
    print(f"价值交易区间 —— 盈利窗口 {_tr.get('eps_window') or '?'}（base EPS {_tr['eps1_base']}）")
    # span 缺失（本次改动之前生成的 facts.json）时整段不出，不打印 "?~?"
    print(f"  倍数取自 {_tr['window']}已实现 NTM PE"
          + (f": {_sp['start']}~{_sp['end']}" if _sp.get("start") else "")
          + f" 共 {_tr['days']} 个交易日"
          + (f"（止于 {_sp['lag_days']} 天前——ntm 口径需要该日之后满 4 个季度已披露，"
             "最近约一年结构性无值）" if _sp.get("lag_days") else ""))
    print(f"  P25~P75 {_tr['px']['25']}~{_tr['px']['75']}  中位 {_tr['px']['50']}"
          f"  宽区间 P10~P90 {_tr['px']['10']}~{_tr['px']['90']}")
    if _tr.get("fwd_pe_now"):
        print(f"  现价隐含前瞻倍数 {_tr['fwd_pe_now']:.1f}x"
              + (f"（{_tr['fwd_pe_now_position']}）" if _tr.get("fwd_pe_now_position") else "")
              + f" vs 目标 {_tr['target_pe']:g}x"
              f"  →  中位涨幅 {_tr['mult_reversion_to_p50']:+.1%} 是**纯倍数差距**"
              "（给定同一个 base EPS 时，现价与中位价用同一分母，两者之差只能是倍数之差；"
              "改 base EPS 会同比例移动中位价，故此非『与盈利预测无关』）")
    _df = _tr.get("drift")
    if _df:
        print(f"  窗口内漂移: 早段 {_df['early']['span']['start']}~{_df['early']['span']['end']} "
              f"P50 {_df['early']['p50']:.1f}x  →  近段 "
              f"{_df['late']['span']['start']}~{_df['late']['span']['end']} "
              f"P50 {_df['late']['p50']:.1f}x  ({_df['delta_pct']:+.1%})")
    _tn = _tr.get("trailing_nolag")
    if _tn:
        # 换算因子 = 建模 NTM EPS ÷ GAAP TTM EPS − 1（**EPS** 增速，不是营收增速）；
        # 拿不到就不显示换算值，宁可只给原始 trailing 数
        _g = _tr.get("trailing_ntm_eps_growth")
        # pctiles 的键经 facts.json 往返后是字符串（pe_band 里建的是 int）——两种都收，
        # 这正是 test_engine_band 记下的那个潜在坑，别在这里踩第二次
        _tnp = {str(k): v for k, v in (_tn.get("pctiles") or {}).items()}
        _tn50 = _tnp.get("50")
        _eq = _tn50 / (1 + _g) if (_g is not None and _tn50) else None
        print(f"  ⚠ 无滞后对照（trailing 口径，价÷过去12个月，**不可与上面直接相减**）: "
              f"{_tn['span']['start']}~{_tn['span']['end']}"
              + (f" P50 {_tn50:.1f}x，" if _tn50 else " ")
              + f"最新 {_tn['current']:.1f}x"
              + (f"；按 NTM EPS 增速 {_g:+.0%}（建模 NTM EPS ÷ GAAP TTM EPS）折成 "
                 f"NTM 可比口径约 {_eq:.1f}x" if _eq else "；无 GAAP TTM EPS，不做换算"))
        _gap = _tn.get("gap_since_main_band")
        if _gap:
            print(f"     主带盲区那段 {_gap['span']['start']}~{_gap['span']['end']}"
                  f"（{_gap['days']} 天）trailing P50 {_gap['p50']:.1f}x"
                  + (f" ≈ NTM 可比 {_gap['p50'] / (1 + _g):.1f}x" if _g is not None else ""))
if not sotp_in_blend:
    print(f"SOTP 降级为参考项（主分部利润占比 {cfg['seg1_share']:.0%} >= 85%），综合 = PE/DCF 均值")
for n, v in out["scenarios"].items():
    # 被守卫剔出综合的腿打 x（* 仍表示 seg1_share 降级），避免 stdout 看起来像
    # "三法都进了综合"——数字照显示，只是标出它没参与
    _bm = v["blend_methods"]
    print(f"{n:5s}| EPS1 {v['eps1']:8.2f} | "
          f"PE法 {v['pe_target']:9.1f}{' ' if 'pe' in _bm else 'x'}| "
          f"DCF {v['dcf_ps']:9.1f} | "
          f"SOTP {v['sotp_ps']:9.1f}"
          f"{'*' if not sotp_in_blend else (' ' if 'sotp' in _bm else 'x')}| "
          f"综合 {v['blend']:9.1f} | {v['upside']:+.1%}")
    for lv, msg in v["warnings"]:
        print(f"      {'⛔' if lv == 'red' else '⚠️'} {msg}")
