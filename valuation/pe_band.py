# -*- coding: utf-8 -*-
"""历史 PE 带：XBRL TTM EPS × yfinance 日收盘 -> 逐日 PE -> 分位数/σ 带 + 逆向匹配。

用法:
  python pe_band.py TICKER EMAIL [--years 5] [--match 23.5,31.5] [--out OUT.json]

  --years N       回看年数（默认 5）
  --match lo,hi   逆向工程：给定一组 PE 带，反查它对应的分位数/σ 倍数/覆盖率
  --out PATH      同时写出 JSON

产出三块：
  1. PE 分布统计 + 若干候选带（min/max、各分位、均值±kσ）及各自的历史覆盖率
  2. 逐财年实现 PE（低/中/高）——用来核对某条带子是否真的框住了每一年
  3. --match 的逆向匹配——回答「这组 PE 带是按什么规则定出来的」

四个会静默出错的地方，都已处理：
- **防前视**：某交易日的 PE 用「当天已经公告的」TTM EPS，按 XBRL 的 filed 日切换，
  不是按报告期末。用期末会让 PE 带假性收窄（把还没公布的业绩提前算进去）。
- **TTM EPS = TTM 净利 ÷ 四季平均稀释股数**：EPS 和加权平均股数都不可跨季加总
  （见 fetch_facts.py:244），净利可以——所以在分子上加总，分母取四季均值。
- **拆股归一**：yfinance 的 Close 已按拆股调整，XBRL 的股数未必（companyfacts 只在
  后续财报把该期做比较期时才追溯重述——重述是**逐条**的，不是整段的）。按每条记录
  自己的最新 filed 日判定口径：filed 早于拆股生效日的记录必为拆前口径，逐条回补；
  否则 AAPL 4:1 / AMZN 20:1 这类会把拆股前整段 PE 算错一个数量级。
- **一次性畸变双侧剔除**：near-zero 地板只挡分母塌缩，挡不住分母膨胀——巨额一次性
  收益（AMZN 2026Q2 $53.4B Anthropic 重估）会把实现 EPS 吹大、PE 假性变低，带子的
  min/P10 被拉低后 engine 的越界诊断恰好在最该响的票上失灵。按「单季净利偏离同窗
  中位季 > ANOM_K 倍」双侧判定并整窗剔除，照 near_zero 惯例留痕。
"""
import argparse
import bisect
import json
import statistics
import sys
from datetime import date, timedelta

import httpx

# 口径 -> 序列字段名。必须是唯一定义处：compute_band 与 main 各写一份三元表达式时，
# 加 ntm 档只改了前者，CLI 在 --basis ntm 下把 trailing 的 PE 当成 NTM 打印出来
# （MSFT 末点显示 39.7x，实际应为 30.2x）——分布统计走 compute_band 未受影响，
# 但展示层的数直接是错的。
BASIS_KEY = {"forward": "pe_forward", "trailing": "pe_trailing", "ntm": "pe_ntm"}

# 指标维度（metric）正交于口径维度（basis）：eps -> pe_*（分母=每股净利），
# rps -> ps_*（分母=每股营收，P/S 带）。整套机械（filed 可知日/拆股归一/Q4 推导/
# 分位带/子窗/畸变剔除）对两个指标同构——P/S 带给近零利润票（COIN/微利期周期股）
# 提供 PE 失效时的参照，engine 的近零利润守卫消费它。

# 一次性畸变窗口判定：单季净利偏离同窗中位季超过 ANOM_K 倍（双侧）。校准点
# （AMZN 真实数据实测）：2026-06-30 窗口（2025Q3~2026Q2，末季含 ~$42B 税后
# Anthropic 重估）偏离 1.40 倍——1.5 恰好漏掉动机案例，故取 1.25；正常零售
# 季节性（Q4 高 ~40%）偏离 <0.5 倍，2023-2025 干净窗口实测 ≤0.4 倍，余量充足。
# 已知代价：4 季内利润 >3 倍的超高速 ramp（NVDA 2023 型）窗口可能被误标——
# 那类窗口本身 PE 离散度极大，剔除且留痕好过静默混入。判定只用净利自身的
# 截面形状，不引入营业利润等第二科目，保持模块零额外取数。
# 第二个已知代价：判定是「相对同窗中位季」的，窗口自身中位季趋零时（周期底/
# 微利期）任一正常季都能越过 1.25 倍门槛，整段谷底窗口会被剔。方向上与
# near-zero 地板一致（都认为分母不代表盈利能力），且全量留痕可核对，故不额外
# 加绝对量级门槛——真要放宽，改这里而不是调 K（K 是按畸变幅度校准的）。
ANOM_K = 1.25

NI_TAGS = ["NetIncomeLoss", "ProfitLossAttributableToOwnersOfParent", "ProfitLoss"]
# 营收 tag 与 fetch_facts.TAGS_STANDARD/TAGS_IFRS 的 revenue 候选一致；总营收 tag 与
# 分项 tag 同期并存时取大（CRCL：Revenues $2,747M vs 合同收入 $110M，取小静默错 25 倍）
REV_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
            "SalesRevenueNet", "Revenue", "RevenueFromContractsWithCustomers"]
# TBV 组件 tag 与 fetch_facts.TAGS_FINANCIALS 一致（严格同源：engine 的
# tbv = equity − goodwill − intangibles 用的正是这套 tag 的时点值）
EQ_TAGS = ["StockholdersEquity",
           "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
SHOUT_TAGS = ["CommonStockSharesOutstanding"]          # us-gaap 时点流通股
SHOUT_DEI = ["EntityCommonStockSharesOutstanding"]     # dei 封面页兜底
GW_TAGS = ["Goodwill"]
IT_TAGS = ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"]
SH_TAGS = ["WeightedAverageNumberOfDilutedSharesOutstanding",
           "AdjustedWeightedAverageShares", "WeightedAverageShares"]
# 稀释 EPS（股数兜底反推用）：US GAAP + IFRS
EPS_TAGS = ["EarningsPerShareDiluted", "DilutedEarningsLossPerShare"]


def resolve_cik(ticker, headers):
    m = httpx.get("https://www.sec.gov/files/company_tickers.json",
                  headers=headers, timeout=60).json()
    for v in m.values():
        if v["ticker"].upper() == ticker:
            return int(v["cik_str"])
    raise SystemExit(f"SEC EDGAR 中未找到 {ticker}")


def pick(facts, tags, kind, units, prefer_max=False):
    """{期末: {"val","filed","first_filed"}}。

    val 取 filed **最新**（重述/换标签后的口径更准）；可知日取 filed **最早**。
    prefer_max（营收用）：同期同 filed 打平取较大者——总营收与分项 tag 并存时取小
    是静默错误（fetch_facts.pick 的 CRCL 教训，此处同规则）。
    两者必须分开：同一期会在后续财报里作为比较期反复出现，若拿最新 filed 当可知日，
    2024 年的季度会被标成 2026 年才可知，整条 PE 序列退化成「远古 EPS 除当期价格」。
    """
    rows = {}
    for tag in tags:
        if tag not in facts:
            continue
        for unit, entries in facts[tag]["units"].items():
            if unit not in units:
                continue
            for f in entries:
                end, start = f.get("end"), f.get("start")
                if kind == "instant":
                    # 时点科目（权益/商誉/无形）：无 start，只有期末快照
                    if start is not None or end is None:
                        continue
                else:
                    if start is None or end is None:
                        continue
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                    if kind == "annual":
                        if not 350 <= days <= 380:
                            continue
                        # AMZN 这类公司每个季报都申报一条 twelve-months-ended，长度同样落在
                        # 350~380 天里（见 fetch_facts.py:221）。只按天数过滤会把 4 个季末
                        # 都当成财年末，财年边界被切成四段、每段只剩 60 多个交易日。
                        if f.get("fp", "FY") != "FY":
                            continue
                    if kind == "quarterly" and not 80 <= days <= 100:
                        continue
                filed, val = f.get("filed", ""), float(f["val"])
                r = rows.get(end)
                if r is None:
                    rows[end] = {"val": val, "filed": filed, "first_filed": filed}
                    continue
                if filed > r["filed"] or (prefer_max and filed == r["filed"]
                                          and val > r["val"]):
                    r["val"], r["filed"] = val, filed
                if filed and (not r["first_filed"] or filed < r["first_filed"]):
                    r["first_filed"] = filed
    return dict(sorted(rows.items()))


def derive_q4(quarterly, annual):
    """Q4 = 年度 − 前三季，只对可加总的流量科目成立（净利可以，股数/EPS 不行）。"""
    for a_end, a in annual.items():
        ae = date.fromisoformat(a_end)
        in_year = {k: v for k, v in quarterly.items()
                   if 0 < (ae - date.fromisoformat(k)).days < 340}
        if len(in_year) == 3 and a_end not in quarterly:
            quarterly[a_end] = {"val": a["val"] - sum(v["val"] for v in in_year.values()),
                                "filed": a["filed"], "first_filed": a["first_filed"]}
    return dict(sorted(quarterly.items()))


def derive_q4_avg(quarterly, annual):
    """加权平均股数的 Q4 = 4×FY均值 − 前三季之和。

    fetch_facts.py:244 说「加权股数不可加总」，指的是 年度−前三季 那个**和式**
    （会得到大负数）；股数是均值不是和，均值式 4×FY−Σ3 才成立。公司不报 Q4 10-Q，
    不补这一期的话每个滚动四季窗口都缺一角，TTM 序列会直接为空。

    调用前提：输入序列已过 normalize_splits——均值式要求 FY 与三个季度同口径，
    混用拆前/拆后口径会推出垃圾 Q4。gate 是口径归一失效时的兜底，×÷1.5 对数对称
    （旧值 0.5/2 宽到连 20:1 混口径都放行过）：单季 50% 级别的增发/回购跳变仍在
    容忍内，2:1 以上的残留口径混用会被拒掉；5:4 这类小比例混用 gate 拦不住，
    第一道防线是 normalize_splits 的逐条 filed 判定。
    """
    for a_end, a in annual.items():
        ae = date.fromisoformat(a_end)
        in_year = {k: v for k, v in quarterly.items()
                   if 0 < (ae - date.fromisoformat(k)).days < 340}
        if len(in_year) != 3 or a_end in quarterly:
            continue
        vals = [v["val"] for v in in_year.values()]
        q4 = 4 * a["val"] - sum(vals)
        if 2 / 3 * min(vals) < q4 < 1.5 * max(vals):  # 越界=口径不一致或数据异常，宁缺勿错
            quarterly[a_end] = {"val": q4, "filed": a["filed"],
                                "first_filed": a["first_filed"]}
    return dict(sorted(quarterly.items()))


def normalize_splits(shares, splits):
    """XBRL 股数序列的拆股口径归一：filed 早于拆股生效日的记录必为拆前口径，逐条回补。

    判据是确定性的：会计准则（ASC 260）要求拆股后发出的报告对**所有列报期间**追溯
    重述加权股数，而一份在拆股生效前 filed 的文件不可能反映尚未发生的拆股——
    所以「该期最新 filed 日 vs 拆股日」逐条决定口径，不需要猜。

    旧实现按「拆股日前后各 2 期均值的跳变」整段判断，两个实测会咬人的洞：
    ① XBRL 重述是逐条的——拆股后的新报告只重述其比较期（±4 个季度左右），更老的
      期间永远停留在拆前口径。探测点（紧邻拆股日的期间）恰恰是最先被重述的，
      obs≈1 → 判「已重述」→ 更老的拆前期间整段漏掉（NVDA 2024-06 10:1 实测：
      ntm band min 5.1 vs 修复后 17.9，带子被静默拉宽）。
    ② 反向拆股（ratio<1）时「obs > ratio*0.75」恒真：已重述（obs≈1）同样满足
      0.1×0.75 的门槛，会把重述好的序列再乘 0.1，凭空错一个数量级。
    逐条判定天然覆盖两个象限与多次拆股（filed 早于两次拆股的记录依次吃到两个系数）。
    缺 filed 日的记录无法判定，保守不动并留痕。
    """
    notes = []
    for sd, ratio in sorted(splits.items(), reverse=True):
        sd_s = sd.isoformat()
        # yfinance 的拆股日是 ex-date（首个已调整交易日），ASC 260 的重述触发点是
        # 分配/生效日，可早 1-3 个日历日（NVDA 2024：分配周五 06-07，yfinance 06-10）。
        # 落在缝里 filed 的文件已重述却会被判成拆前——留 4 天缓冲：缝内口径不可判，
        # 保守不动并留痕（与缺 filed 同一处置），残留跳变哨兵兜底
        sd_cut = (sd - timedelta(days=4)).isoformat()
        fixed = skipped = ambiguous = 0
        for v in shares.values():
            filed = v.get("filed")
            if not filed:
                skipped += 1
                continue
            if sd_cut <= filed < sd_s:
                ambiguous += 1
                continue
            if filed < sd_cut:
                v["val"] *= ratio
                fixed += 1
        _amb = (f"；{ambiguous} 期 filed 落在拆股日前 4 天缓冲窗内（分配日/ex-date 缝隙，"
                "口径不可判）未动" if ambiguous else "")
        _skp = f"；{skipped} 期缺 filed 日未动" if skipped else ""
        if fixed:
            notes.append(f"{sd_s} {ratio:g}:1 拆股 — {fixed} 期最新 filed 早于拆股日"
                         "（拆前口径），已按比例回补" + _amb + _skp)
        else:
            notes.append(f"{sd_s} {ratio:g}:1 拆股 — 全部期间 filed 晚于拆股日"
                         "（XBRL 已追溯重述），无需调整" + _amb + _skp)
    return notes


def backfill_shares_from_eps(sh, ni, eps_rows):
    """稀释股数缺季用 净利÷稀释EPS 反推。

    双类股（GOOG 的 Class A/C）把加权稀释股数按股份类别打了 XBRL 维度，
    companyfacts 只保留无维度的合并口径——GOOG 实测 2019-2023 十八个季度
    有净利没股数，TTM 点只长出 7 个、NTM 配对仅 3 对，整条带子瘦成 61 天。
    净利与稀释 EPS 都有合并口径值，相除即隐含加权稀释股数。

    精度：EPS 两位小数舍入，误差 ≈ 0.005/|EPS|（重述后季度 EPS ~0.3-3 →
    0.2%~1.6%），对分位带足够；|EPS|<0.05 时舍入误差 >10%，不反推。
    口径：EPS 是每股科目，重述文件里已按拆股调整——隐含股数的拆股基准
    跟随 **EPS 条目的 filed 日**，normalize_splits 的逐条 filed 规则照常归一。
    """
    added = 0
    for k, nv in ni.items():
        if k in sh:
            continue
        ev = eps_rows.get(k)
        if not ev or not ev.get("val") or abs(ev["val"]) < 0.05:
            continue
        val = nv["val"] / ev["val"]
        if val <= 0:
            continue
        sh[k] = {"val": val, "filed": ev["filed"],
                 "first_filed": max(nv.get("first_filed") or "",
                                    ev.get("first_filed") or "")}
        added += 1
    return added


def build_ttm_eps(ni_q, sh_q, anom_k=ANOM_K):
    """滚动四季 TTM EPS，附「最早可知日」= 四个季度里最晚的 filed 日。

    附一次性畸变标记（anomalous）：单季净利偏离同窗中位季 > ANOM_K 倍即整窗标记。
    双侧判定——巨额一次性收益把 EPS 吹大（PE 假低）和巨额减值把 EPS 打穿（PE 假高）
    是同一种"分母不代表盈利能力"，near-zero 地板只挡得住后者的极端形态。标记不删点
    （删点会让 ntm 配对的 i+4 错位），由消费侧跳过并计数。
    """
    pts, qs = [], sorted(ni_q.items())
    for i in range(3, len(qs)):
        window = qs[i - 3:i + 1]
        ends = [date.fromisoformat(k) for k, _ in window]
        if not all(80 <= (ends[j + 1] - ends[j]).days <= 100 for j in range(3)):
            continue
        sh = [sh_q.get(k) for k, _ in window]
        if not all(sh):
            continue
        avg_sh = sum(s["val"] for s in sh) / 4
        if avg_sh <= 0:
            continue
        known = max(max(v["first_filed"] for _, v in window),
                    max(s["first_filed"] for s in sh))
        if not known:
            continue
        vals = {k: v["val"] for k, v in window}
        # leave-one-out 判据（0061）：逐季与"其余三季的中位"比。旧判据拿全窗四季
        # 中位当基准——窗内两季同向畸变时中位被拖走、偏离缩水到门槛之下
        # （GOOG 2026H1 连续两季股权重估实测漏网：Q1'26 那扇窗 dev/median<1.25）。
        # 拿掉自己再取中位，三季里至多一季脏、中位必落在干净季上，单季/双季畸变
        # 同判据覆盖。代价：对超高速 ramp（NVDA 2023 型）更敏感，会多剔几扇窗——
        # 该类窗口 PE 离散度本就极大，剔除且留痕好过静默混入（与 ANOM_K 注释同）。
        items = list(vals.items())
        anom_q, worst = None, 0.0
        for j, (k, x) in enumerate(items):
            others = [y for i2, (_, y) in enumerate(items) if i2 != j]
            med_o = statistics.median(others)
            if not med_o:
                continue
            r = abs(x - med_o) / abs(med_o)
            if r > worst:
                worst, anom_q = r, k
        anomalous = bool(anom_k) and worst > anom_k
        pts.append({"period_end": window[-1][0], "known_from": known,
                    "ttm_ni": sum(v["val"] for _, v in window),
                    "avg_diluted_shares": avg_sh,
                    "ttm_eps": sum(v["val"] for _, v in window) / avg_sh,
                    "anomalous": anomalous,
                    "anom_q": anom_q if anomalous else None})
    return sorted(pts, key=lambda p: p["known_from"])


def years_ago(n):
    """date.today() 往回 n 年。不用 replace(year=)——闰日运行时目标年 2/29 多半
    不存在，ValueError 会让整条带子在每个 2 月 29 日全天缺席。"""
    return date.today() - timedelta(days=round(365.25 * n))


def pctile(sv, p):
    k = (len(sv) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sv) - 1)
    return sv[f] + (sv[c] - sv[f]) * (k - f)


def rank_of(sv, x):
    return 100.0 * bisect.bisect_left(sv, x) / len(sv)


def coverage(sv, lo, hi):
    return 100.0 * (bisect.bisect_right(sv, hi) - bisect.bisect_left(sv, lo)) / len(sv)


def load_inputs(ticker, email, years=5):
    """一次下载，供多份带子（pe/ps 指标）共用：companyfacts + yfinance 历史价 + 拆股表。

    compute_band 每次自行下载是已知病灶（2 个 SEC 请求 + 1 次 yfinance 历史价），
    多指标时代价翻倍还容易撞限速——fetch_facts 先 load 一次再喂给各 compute_band。
    hist 回看 years+1 年：inputs 只能喂给 years 不大于它的 compute_band。
    """
    ticker = ticker.upper()
    H = {"User-Agent": f"sec-filing-downloader pe_band ({email})"}
    cik = resolve_cik(ticker, H)
    allf = httpx.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                     headers=H, timeout=90).json()["facts"]
    facts = allf.get("us-gaap") or allf.get("ifrs-full")
    if not facts:
        raise RuntimeError(f"{ticker}（CIK {cik}）没有 us-gaap/ifrs-full 数据")
    import yfinance as yf
    tk = yf.Ticker(ticker)
    hist = tk.history(period=f"{years + 1}y", auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"yfinance 取不到 {ticker} 价格")
    splits = {d.date(): float(r) for d, r in tk.splits.items() if r and float(r) != 1.0}
    return {"cik": cik, "facts": facts, "dei": allf.get("dei") or {},
            "hist": hist, "splits": splits, "years": years}


def compute_band(ticker, email, years=5, basis="forward", include_series=False,
                 metric="eps", inputs=None):
    """算出历史 PE/P.S 分布，返回纯数据 dict（不打印）。

    供 CLI 与 fetch_facts.py 共用——engine.py 是零联网的确定性计算层，
    带子必须由数据层算好塞进 facts.json，不能让引擎自己去取。
    metric="eps"（PE 带，默认）或 "rps"（P/S 带，分母=每股营收）；
    inputs=load_inputs(...) 可复用已下载数据，None 则自行下载。
    """
    ticker = ticker.upper()
    if inputs is None:
        inputs = load_inputs(ticker, email, years)
    elif inputs.get("years", years) < years:
        raise RuntimeError("inputs 的价格窗口比请求的 years 短，请重新 load_inputs")
    facts, hist, splits = inputs["facts"], inputs["hist"], inputs["splits"]
    cik = inputs["cik"]
    NUM_TAGS, P = (NI_TAGS, "pe") if metric == "eps" else (REV_TAGS, "ps")
    NUM_WORD = "净利" if metric == "eps" else "营收"

    ni_a = pick(facts, NUM_TAGS, "annual", {"USD"}, prefer_max=(metric == "rps"))
    ni_q = derive_q4(pick(facts, NUM_TAGS, "quarterly", {"USD"},
                          prefer_max=(metric == "rps")), ni_a)
    if len(ni_q) < 4:
        raise RuntimeError(f"{ticker} 季度 XBRL 不足（{NUM_WORD} {len(ni_q)} 期）"
                         "——外国发行人常无季度 XBRL，本工具需要季度序列")
    sh_a = pick(facts, SH_TAGS, "annual", {"shares"})
    sh_q = pick(facts, SH_TAGS, "quarterly", {"shares"})
    # 股数兜底（0060）：反推的分子必须是净利（与 EPS 同分子），与本带 metric 无关。
    # 兜底在归一之前——隐含条目带 EPS 侧的 filed 日，逐条拆股规则照常适用
    eps_q = pick(facts, EPS_TAGS, "quarterly", {"USD/shares"})
    eps_a = pick(facts, EPS_TAGS, "annual", {"USD/shares"})
    if metric == "eps":
        ni4sh_a, ni4sh_q = ni_a, ni_q
    else:
        ni4sh_a = pick(facts, NI_TAGS, "annual", {"USD"})
        ni4sh_q = derive_q4(pick(facts, NI_TAGS, "quarterly", {"USD"}), dict(ni4sh_a))
    imp_q = backfill_shares_from_eps(sh_q, ni4sh_q, eps_q)
    imp_a = backfill_shares_from_eps(sh_a, ni4sh_a, eps_a)
    # 归一必须在 derive_q4_avg **之前**：Q4 = 4×FY − Σ3Q 的均值式要求 FY 与三个
    # 季度同口径。旧顺序（先推导后归一）在拆股年会先用混口径推出垃圾 Q4、
    # 再对垃圾整段乘系数——两步各错一次
    split_notes = normalize_splits(sh_q, splits)
    normalize_splits(sh_a, splits)
    if imp_q or imp_a:
        split_notes.append(f"股数兜底: {imp_q} 个季度 + {imp_a} 个财年由 净利÷稀释EPS 反推"
                           "（股数 XBRL 仅按股份类别维度申报，companyfacts 无合并口径；"
                           "EPS 舍入误差 ~0.2-1.6%）")
    sh_q = derive_q4_avg(sh_q, sh_a)
    if len(sh_q) < 4:
        raise RuntimeError(f"{ticker} 季度 XBRL 不足（{NUM_WORD} {len(ni_q)} 期/股数 {len(sh_q)} 期）"
                         "——外国发行人常无季度 XBRL，本工具需要季度序列")
    # 残留口径跳变哨兵：逐条 filed 规则的兜底（yfinance 拆股日偏差、罕见的不重述
    # 再申报都会在相邻期股数上留下台阶）。只留痕不修数——修数需要能定位口径边界，
    # 启发式做不到，上面的教训就是这么来的
    _sq = sorted(sh_q.items())
    for (k1, v1), (k2, v2) in zip(_sq, _sq[1:]):
        r = v2["val"] / v1["val"] if v1["val"] else 0
        if r and not 2 / 3 < r < 1.5:
            split_notes.append(f"⚠ {k1}→{k2} 相邻期股数跳变 {r:.2f}x——若非大额增发/"
                               "回购/并购，拆股口径归一可能有残留，请核对该期 filed 与拆股日")

    # 畸变守卫只对 eps 生效：ANOM_K=1.25 按净利一次性损益校准（AMZN 1.40x），
    # 营收没有"一次性膨胀"的常见类似物，极端季节性票（游戏/税务软件 Q4 >2.25×
    # 中位是常态结构）会被整段误剔——P/S 带宁可保留全部窗口
    pts = build_ttm_eps(ni_q, sh_q, anom_k=(ANOM_K if metric == "eps" else None))
    if not pts:
        raise RuntimeError(f"{ticker} 无法构造连续四季 TTM EPS")

    # forward 口径：用「该财年最终实现的」EPS 反算当年每一天的 PE。
    # 这是**刻意**用后见之明——它不是交易信号，而是在回答「这只票在某财年里，
    # 按该财年真实 EPS 算，实际交易在什么倍数区间」。要复刻「财年交易区间」这类
    # 预测，需要的历史分布正是它；用滚动 TTM 口径去比会系统性差一个增长率。
    eps_fy = {e: ni_a[e]["val"] / sh_a[e]["val"] for e in ni_a
              if e in sh_a and sh_a[e]["val"] > 0 and ni_a[e]["val"] > 0}
    fy_ends = sorted(ni_a)
    # 亏损年 PE 无意义，整年从分布里剔除——不说出来的话会被当成"数据缺了一段"
    dropped = [e for e in fy_ends if e not in eps_fy and e >= str(date.today().year - years)]
    # 财年层面的一次性畸变（forward 口径的对应物）：FY 含畸变季则该年实现 EPS 同样
    # 不代表盈利能力，与亏损年同等处置——整年剔除并单独留痕
    anom_fys = []
    for e in (list(eps_fy) if metric == "eps" else []):
        ae = date.fromisoformat(e)
        qv = [v["val"] for k, v in ni_q.items()
              if 0 <= (ae - date.fromisoformat(k)).days < 340]
        if len(qv) != 4:
            continue
        # 与 build_ttm_eps 同一 leave-one-out 判据（财年 = 固定四季窗）
        hit = False
        for j, x in enumerate(qv):
            others = [y for i2, y in enumerate(qv) if i2 != j]
            med_o = statistics.median(others)
            if med_o and abs(x - med_o) / abs(med_o) > ANOM_K:
                hit = True
                break
        if hit:
            del eps_fy[e]
            anom_fys.append(e)

    def fy_of(d):
        i = bisect.bisect_left(fy_ends, d)
        return fy_ends[i] if i < len(fy_ends) else None

    # ntm 口径：某期末之后 12 个月**实际实现**的 EPS = 4 个季度后那一点的 TTM EPS。
    # 这是与 engine 严格同源的口径——engine 算 rev1 = TTM×(1+g)，前瞻期恒为 NTM
    # 而非财年（见 valuation_service.fwd_window 的 docstring）。forward 口径按财年
    # 切，只在 report_end = 财年末时才与 engine 重合（MSFT 是，AMZN 的 Q2 报告期
    # 不是）。要让 engine 的目标 PE 与历史分位严格可比，就得用这一档。
    by_end = sorted(pts, key=lambda p: p["period_end"])
    ntm_after, ntm_anom = {}, set()
    for i in range(len(by_end) - 4):
        a, b = by_end[i], by_end[i + 4]
        da = date.fromisoformat(a["period_end"])
        db = date.fromisoformat(b["period_end"])
        if 350 <= (db - da).days <= 380 and b["ttm_eps"] > 0:
            # ntm 的分母是未来窗口 b：b 含一次性畸变季（如 AMZN 2026Q2 的重估收益）
            # 时该分母不代表盈利能力，整段映射剔除并在消费侧按天计数
            if b.get("anomalous"):
                ntm_anom.add(a["period_end"])
            else:
                ntm_after[a["period_end"]] = b["ttm_eps"]

    start = years_ago(years).isoformat()
    knowns = [p["known_from"] for p in pts]
    series = []
    anom_days = {"trailing": 0, "ntm": 0}
    stale_days = 0
    # 陈旧点守卫：Q4 被 sanity gate 拒掉时 build_ttm_eps 会跳过含缺口的 4 个窗口，
    # bisect 回退拿到的可能是 5 个季度前的点——「宁缺勿错」的缺必须真缺，
    # 拿陈旧 EPS 除当期价格混进分布比缺一段更糟。>210 天（约缺一个季度以上）跳过留痕
    STALE_DAYS = 210
    for ts, row in hist.iterrows():
        d = ts.date().isoformat()
        if d < start:
            continue
        px = float(row["Close"])
        rec = {"date": d, "close": px}
        i = bisect.bisect_right(knowns, d) - 1
        if i >= 0 and (date.fromisoformat(d)
                       - date.fromisoformat(pts[i]["period_end"])).days > STALE_DAYS:
            stale_days += 1
            i = -1
        if i >= 0 and pts[i]["ttm_eps"] > 0:
            if pts[i].get("anomalous"):
                anom_days["trailing"] += 1
            else:
                rec[f"{P}_trailing"] = px / pts[i]["ttm_eps"]
                rec["ttm_eps"] = pts[i]["ttm_eps"]
                rec["ttm_period"] = pts[i]["period_end"]
        if i >= 0:
            # ntm 不再嵌套在 ttm_eps>0 之下：分母是未来窗口的 EPS，与当前已知 TTM
            # 是否为正无关（负 TTM 期恰是周期底，realized-NTM 视角本应有值）
            if pts[i]["period_end"] in ntm_anom:
                anom_days["ntm"] += 1
            else:
                ntm_eps = ntm_after.get(pts[i]["period_end"])
                if ntm_eps:
                    rec[f"{P}_ntm"] = px / ntm_eps
                    rec["ntm_eps"] = ntm_eps
        fy = fy_of(d)
        if fy and fy in eps_fy:
            rec[f"{P}_forward"] = px / eps_fy[fy]
            rec["fy_eps"] = eps_fy[fy]
            rec["fy"] = fy
        if any(k in rec for k in (f"{P}_trailing", f"{P}_forward", f"{P}_ntm")):
            series.append(rec)

    key = f"{P}_{basis}"
    # 对照档固定选 trailing（唯一一档不依赖后见之明，任何标的都必然有值）
    other = f"{P}_trailing" if basis != "trailing" else f"{P}_forward"
    series = [s for s in series if key in s]
    if len(series) < 60:
        raise RuntimeError(
            f"{ticker} {basis} 口径有效交易日仅 {len(series)} 天，样本不足"
            + ("（forward 口径需要该财年已披露年报，本财年至今必然缺）"
               if basis == "forward" else
               "（ntm 口径需要该日之后满 4 个季度已披露，最近一年必然缺）"
               if basis == "ntm" else ""))

    # 分母趋零时 PE 无信息量：AMZN 2022 年滚动 EPS 逼近零，ntm 口径下算出 300x
    # 这类数——不是"贵"，是除以了一个接近 0 的数。均值/σ/max 会被单点带飞，而
    # engine.pe_band_check 正是用 min/max 判"该票从未出现过的倍数"，被污染就失效。
    # 规则：剔除 EPS < 窗口内 EPS 中位数 25% 的交易日（此时 PE 已是中位倍数的 4 倍
    # 以上，纯由分母塌缩造成）。forward 口径按亏损财年整年剔除是同一思路的离散版。
    EPS_KEY = {f"{P}_forward": "fy_eps", f"{P}_trailing": "ttm_eps",
               f"{P}_ntm": "ntm_eps"}[key]
    eps_all = sorted(s[EPS_KEY] for s in series if s.get(EPS_KEY))
    near_zero = []
    if eps_all:
        floor = 0.25 * pctile(eps_all, 50)
        near_zero = [s["date"] for s in series if (s.get(EPS_KEY) or 0) < floor]
        if near_zero:
            series = [s for s in series if (s.get(EPS_KEY) or 0) >= floor]
    if len(series) < 60:
        raise RuntimeError(f"{ticker} {basis} 口径剔除盈利塌缩期后仅剩 {len(series)} 天，样本不足")

    pes = [s[key] for s in series]
    other_pes = sorted(s[other] for s in series if other in s)
    sv = sorted(pes)
    mean, sd_ = statistics.mean(pes), statistics.pstdev(pes)
    cur = series[-1]

    pcts = {p: pctile(sv, p) for p in (1, 5, 10, 25, 50, 75, 90, 95, 99)}

    # 近 3 年子窗分位：5 年全窗把 2021 零利率 regime 的倍数原样计入（AMZN 全窗
    # P50≈31x 比手工参考带的中位还高），直接拿全窗 P50 当锚会把泡沫期抬进锚里。
    # 子窗按日历日切——ntm 口径最近一年天然无实现值，子窗实际覆盖会更短，days
    # 如实报告；样本 <60 天不出，宁缺勿错。供 engine 交易区间/判断层 PE 锚优先取用。
    RECENT_YEARS = 3
    recent = None
    if years > RECENT_YEARS:
        rstart = years_ago(RECENT_YEARS).isoformat()
        rsv = sorted(s[key] for s in series if s["date"] >= rstart)
        # 子窗与全窗同一覆盖门槛（250 天）：GOOG 实测全窗兜底修回 490 天后，
        # 子窗仍只剩 ~60 天被污染的尾巴——60 天的 P50 拿去当锚比没有锚更糟。
        # 子窗不够厚就回退全窗（消费侧的 recent→pctiles 回退链天然接住）
        if len(rsv) >= 250:
            recent = {"years": RECENT_YEARS, "days": len(rsv),
                      "min": rsv[0], "max": rsv[-1],
                      "pctiles": {p: pctile(rsv, p) for p in (10, 25, 50, 75, 90)}}

    bounds = [(fy_ends[i - 1] if i else "0000-00-00", fy_ends[i]) for i in range(len(fy_ends))]
    bounds.append((fy_ends[-1], "9999-99-99"))
    fy_rows = []
    for lo_d, hi_d in bounds:
        seg = sorted(s[key] for s in series if lo_d < s["date"] <= hi_d)
        if len(seg) < 20:
            continue
        fy_rows.append({"fy_end": hi_d if hi_d != "9999-99-99" else "本财年至今",
                        "low": seg[0], "median": pctile(seg, 50),
                        "high": seg[-1], "days": len(seg)})

    # 覆盖率门槛：有效天数不足一个财年（<250 交易日）时，分布不代表"该票的历史"，
    # 锚/交易区间/中枢检查在消费侧全部停用（看 thin_coverage 字段）。双类股股数
    # 维度化导致的原料缺口（GOOG 实测 5 年仅 61 天）、新上市、长停牌都会踩到——
    # 薄样本照出不误（排查用），但不给它锚的话语权。
    out = {
        "ticker": ticker, "cik": cik, "basis": basis, "metric": metric,
        "years": years, "days": len(series), "thin_coverage": len(series) < 250,
        "mean": mean, "median": pcts[50], "stdev": sd_, "min": sv[0], "max": sv[-1],
        "pctiles": pcts, "recent": recent, "fiscal_years": fy_rows, "current": cur,
        "other_basis": other.split("_")[1],
        "other_basis_median": (pctile(other_pes, 50) if other_pes else None),
        "split_notes": split_notes, "dropped_fys": dropped,
        "anom_windows": [{"period_end": p["period_end"], "quarter": p["anom_q"]}
                         for p in by_end if p.get("anomalous")],
        "anom_days": anom_days, "anom_fys": anom_fys, "stale_days": stale_days,
        "near_zero_days": len(near_zero),
        "near_zero_range": ([near_zero[0], near_zero[-1]] if near_zero else None),
        "candidates": [{"name": n, "lo": lo, "hi": hi, "mid": (lo + hi) / 2,
                        "coverage_pct": coverage(sv, lo, hi)}
                       for n, lo, hi in (("最低 / 最高", sv[0], sv[-1]),
                                         ("P5 / P95", pcts[5], pcts[95]),
                                         ("P10 / P90", pcts[10], pcts[90]),
                                         ("P25 / P75", pcts[25], pcts[75]),
                                         ("均值 ±1.0σ", mean - sd_, mean + sd_),
                                         ("均值 ±1.5σ", mean - 1.5 * sd_, mean + 1.5 * sd_),
                                         ("均值 ±2.0σ", mean - 2 * sd_, mean + 2 * sd_))],
    }
    if include_series:
        out["series"] = series
    out["_sorted"] = sv
    return out


def compute_ptbv_band(ticker, email, years=5, inputs=None):
    """金融股历史 P/TBV 带（trailing 口径：当日已知最新每股有形账面价值）。

    与 compute_band 输出 schema 兼容（pctiles/recent/min/max/days/basis/metric），
    供 engine 金融股分支的带检查与判断层 P/TBV 锚注入。只有 trailing 口径——
    TBV 是存量不是流量，"已实现 NTM"对它没有 PE/PS 那样的意义；银行估值惯例
    也是当前 P/TBV vs 自身历史。时点科目免 TTM 加总与 Q4 推导，比 PE 带简单：
    每股 TBV = (权益 − 商誉 − 无形)/加权稀释股数（与 engine.tbv_ps 同构），
    可知日 = 各组件 first_filed 最大值。近零/负权益期照 near-zero 惯例剔除留痕。
    """
    ticker = ticker.upper()
    if inputs is None:
        inputs = load_inputs(ticker, email, years)
    elif inputs.get("years", years) < years:
        raise RuntimeError("inputs 的价格窗口比请求的 years 短，请重新 load_inputs")
    facts, hist, splits = inputs["facts"], inputs["hist"], inputs["splits"]

    eq = pick(facts, EQ_TAGS, "instant", {"USD"})
    gw = pick(facts, GW_TAGS, "instant", {"USD"})
    it = pick(facts, IT_TAGS, "instant", {"USD"})
    # 分母 = **时点流通股**（us-gaap CommonStockSharesOutstanding，dei 封面页兜底）：
    # TBV 是时点存量，除以当季加权平均是拿流量均值配存量——增发季（SOFI 型
    # 一次性摊薄）加权数≈新股数的一半，每股 TBV 会被高估近一倍，带子整段失真。
    # 时点股数同样按逐条 filed 规则做拆股归一。加权稀释只作最后兜底并留痕
    #（engine 当前点 tbv_ps 用最新加权稀释是既有口径，差异在无增发季可忽略）。
    sho = pick(facts, SHOUT_TAGS, "instant", {"shares"})
    if len(sho) < 4 and inputs.get("dei"):
        sho = pick(inputs["dei"], SHOUT_DEI, "instant", {"shares"})
    shares_basis = "instant"
    if len(sho) >= 4:
        split_notes = normalize_splits(sho, splits)
        sh_items = sorted(sho.items())
    else:
        shares_basis = "weighted_avg_fallback"
        sh_a = pick(facts, SH_TAGS, "annual", {"shares"})
        sh_q = pick(facts, SH_TAGS, "quarterly", {"shares"})
        split_notes = normalize_splits(sh_q, splits)
        normalize_splits(sh_a, splits)
        sho = derive_q4_avg(sh_q, sh_a)
        sh_items = sorted(sho.items())
        split_notes.append("⚠ 无时点流通股 XBRL，分母退回加权稀释股数——增发季的"
                           "每股 TBV 会被高估，带子仅供参考")
    if len(eq) < 4 or len(sho) < 4:
        raise RuntimeError(f"{ticker} XBRL 不足（权益 {len(eq)} 期/股数 {len(sho)} 期），"
                           "P/TBV 带需要季度级时点序列")

    # 商誉/无形 carry-forward：银行的余额表科目通常每季都报，但个别标的只在 10-K
    # 报无形——缺季按 0 处理会让 TBV 逐季跳台阶，且与 engine「取最新时点值」的
    # 口径相悖。取 ≤ 该期末的最近一期，其 first_filed 计入可知日；真无历史才是 0
    _gw_items = sorted(gw.items())
    _it_items = sorted(it.items())

    def _carry(items, e):
        prev = None
        for k, x in items:
            if k <= e:
                prev = x
            else:
                break
        return prev or {"val": 0.0, "first_filed": ""}

    pts = []
    for e, v in sorted(eq.items()):
        gwv = _carry(_gw_items, e)
        itv = _carry(_it_items, e)
        tbv = v["val"] - gwv["val"] - itv["val"]
        # 股数配对：期末精确匹配优先，否则取期末前 100 天内最近一期时点值
        s_ = sho.get(e)
        if s_ is None:
            cand = [x for k, x in sh_items
                    if 0 <= (date.fromisoformat(e) - date.fromisoformat(k)).days <= 100]
            s_ = cand[-1] if cand else None
        if not s_ or s_["val"] <= 0:
            continue
        known = max(x for x in (v["first_filed"], gwv.get("first_filed") or "",
                                itv.get("first_filed") or "", s_["first_filed"]) if x)
        pts.append({"period_end": e, "known_from": known, "tbv_ps": tbv / s_["val"]})
    pts.sort(key=lambda p: p["known_from"])
    if not pts:
        raise RuntimeError(f"{ticker} 无法构造每股 TBV 序列")

    start = years_ago(years).isoformat()
    knowns = [p["known_from"] for p in pts]
    series = []
    stale_days = 0
    neg_days = []
    for ts, row in hist.iterrows():
        d = ts.date().isoformat()
        if d < start:
            continue
        i = bisect.bisect_right(knowns, d) - 1
        if i >= 0 and (date.fromisoformat(d)
                       - date.fromisoformat(pts[i]["period_end"])).days > 210:
            stale_days += 1     # 数据缺口时 bisect 会回退到跨季旧点，陈旧分母不入分布
            continue
        if i >= 0 and pts[i]["tbv_ps"] <= 0:
            neg_days.append(d)  # 负/零 TBV 期照 near-zero 惯例留痕，不静默消失
            continue
        if i >= 0:
            series.append({"date": d, "close": float(row["Close"]),
                           "ptbv": float(row["Close"]) / pts[i]["tbv_ps"],
                           "tbv_ps": pts[i]["tbv_ps"], "period_end": pts[i]["period_end"]})
    if len(series) < 60:
        raise RuntimeError(f"{ticker} P/TBV 有效交易日仅 {len(series)} 天，样本不足")

    tps = sorted(x["tbv_ps"] for x in series)
    floor = 0.25 * pctile(tps, 50)
    near_zero = [x["date"] for x in series if x["tbv_ps"] < floor]
    if near_zero:
        series = [x for x in series if x["tbv_ps"] >= floor]
    if len(series) < 60:
        raise RuntimeError(f"{ticker} 剔除权益趋零期后仅剩 {len(series)} 天，样本不足")

    vals = [x["ptbv"] for x in series]
    sv = sorted(vals)
    pcts = {p: pctile(sv, p) for p in (1, 5, 10, 25, 50, 75, 90, 95, 99)}
    recent = None
    RECENT_YEARS = 3
    if years > RECENT_YEARS:
        rstart = years_ago(RECENT_YEARS).isoformat()
        rsv = sorted(x["ptbv"] for x in series if x["date"] >= rstart)
        if len(rsv) >= 250:   # 子窗与全窗同一覆盖门槛，理由见 compute_band
            recent = {"years": RECENT_YEARS, "days": len(rsv),
                      "min": rsv[0], "max": rsv[-1],
                      "pctiles": {p: pctile(rsv, p) for p in (10, 25, 50, 75, 90)}}
    cur = series[-1]
    return {
        "ticker": ticker, "cik": inputs["cik"], "basis": "trailing", "metric": "tbvps",
        "years": years, "days": len(series), "thin_coverage": len(series) < 250,
        "mean": statistics.mean(vals), "median": pcts[50], "stdev": statistics.pstdev(vals),
        "min": sv[0], "max": sv[-1], "pctiles": pcts, "recent": recent, "current": cur,
        "split_notes": split_notes, "shares_basis": shares_basis,
        "stale_days": stale_days,
        "neg_equity_days": len(neg_days),
        "neg_equity_range": ([neg_days[0], neg_days[-1]] if neg_days else None),
        "near_zero_days": len(near_zero),
        "near_zero_range": ([near_zero[0], near_zero[-1]] if near_zero else None),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("email")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--basis", choices=("forward", "trailing", "ntm"), default="forward",
                    help="forward=按该财年已实现EPS（复刻「财年交易区间」用这个）；"
                         "trailing=按当日已公告的滚动TTM EPS；"
                         "ntm=按该日已知最新TTM期末之后12个月实现的EPS"
                         "（与 engine 的前瞻期严格同源；较日历日起算最多滞后一季）")
    ap.add_argument("--metric", choices=("eps", "rps"), default="eps",
                    help="eps=PE 带（默认）；rps=P/S 带（分母=每股营收，"
                         "近零利润票的参照——PE 失效的域换 PS 是教科书答案）")
    ap.add_argument("--match")
    ap.add_argument("--out")
    a = ap.parse_args()

    try:
        b = compute_band(a.ticker, a.email, a.years, a.basis,
                         include_series=bool(a.out), metric=a.metric)
    except RuntimeError as e:
        raise SystemExit(str(e))
    sv = b.pop("_sorted")
    P = "pe" if a.metric == "eps" else "ps"
    LBL = "PE" if a.metric == "eps" else "P/S"
    EW = "EPS" if a.metric == "eps" else "每股营收"
    NUMW = "净利" if a.metric == "eps" else "营收"
    mean, sd_, cur, key = b["mean"], b["stdev"], b["current"], f"{P}_{a.basis}"

    basis_desc = {"forward": f"forward（分母 = 该财年最终实现的{EW}）",
                  "trailing": f"trailing（分母 = 当日已公告的滚动四季{EW}）",
                  "ntm": f"ntm（分母 = 该日已知最新 TTM 期末之后 12 个月实现的{EW}，"
                         "与 engine 前瞻期同源）"}[a.basis]
    print(f"\n=== {b['ticker']} 历史 {LBL} 带 · {basis_desc} ===")
    print(f"近 {a.years} 年，{b['days']} 个交易日 | XBRL CIK {b['cik']} | "
          "价格 yfinance 日收盘（拆股调整）")
    for n in b["split_notes"]:
        print(f"  拆股: {n}")
    if a.basis == "forward" and b["dropped_fys"]:
        print(f"  剔除: 财年 {', '.join(b['dropped_fys'])} {NUMW}为负或缺股数，"
              f"{LBL} 无意义，整年不计入分布")
    if a.basis == "forward" and b["anom_fys"]:
        print(f"  剔除: 财年 {', '.join(b['anom_fys'])} 含单季一次性畸变"
              f"（偏离同年中位季 >{ANOM_K}x），实现{EW}不代表盈利能力，整年不计入")
    if b["anom_windows"] and a.basis in ("trailing", "ntm"):
        _tail = "、".join(f"{w['period_end']}(畸变季 {w['quarter']})"
                          for w in b["anom_windows"][-3:])
        # 窗口数是全 XBRL 历史口径（META 的 3 个全在 2012-13），天数才是本次
        # --years 窗口内的实际影响——两个数不同源，写在一起必须说清，否则
        # "剔除 26 个窗口却只影响 0 天"会被当成 bug 排查
        print(f"  剔除: 全历史 {len(b['anom_windows'])} 个 TTM 窗口含单季一次性畸变"
              f"（末几个: {_tail}）——影响本窗 trailing {b['anom_days']['trailing']} 天 / "
              f"ntm {b['anom_days']['ntm']} 天；巨额一次性损益会把实现{EW}吹大、"
              f"{LBL} 假性变低，双侧剔除")
    if b.get("stale_days"):
        print(f"  剔除: {b['stale_days']} 个交易日的最近已知点距该日 >210 天（数据缺口/"
              "Q4 推导被拒）——陈旧分母不入分布")
    if b["near_zero_days"]:
        r = b["near_zero_range"]
        print(f"  剔除: {b['near_zero_days']} 个交易日的{EW}低于窗口中位数的 25%"
              f"（{r[0]}~{r[1]}）——分母趋零，{LBL} 无信息量")
    if a.basis == "ntm":
        print(f"  末点: {cur['date']} 收盘 {cur['close']:.2f} / 其后12个月实现{EW} "
              f"{cur['ntm_eps']:.2f} = {LBL} {cur[key]:.1f}x，处第 {rank_of(sv, cur[key]):.0f} 百分位")
        print("       （ntm 序列止于「最近一个满 4 季度已披露」的日子；这是刻意的后见之明，"
              "用途是给 engine 的目标 PE 提供同口径历史分布，不是交易信号）")
    elif a.basis == "forward":
        print(f"  末点: {cur['date']} 收盘 {cur['close']:.2f} / FY{cur['fy']} 实现{EW} "
              f"{cur['fy_eps']:.2f} = {LBL} {cur[key]:.1f}x，处第 {rank_of(sv, cur[key]):.0f} 百分位")
        print(f"       （forward 序列止于最近一个已披露年报的财年末；本财年至今无实现{EW}，"
              "要看当下位置请加 --basis trailing）")
    else:
        print(f"  最新: {cur['date']} 收盘 {cur['close']:.2f} / TTM {EW} {cur['ttm_eps']:.2f} "
              f"（{cur['ttm_period']} 期末）= {LBL} {cur[key]:.1f}x，"
              f"处第 {rank_of(sv, cur[key]):.0f} 百分位")
    print(f"\n分布: 均值 {mean:.2f}  中位数 {b['median']:.2f}  σ {sd_:.2f}  "
          f"最低 {b['min']:.2f}  最高 {b['max']:.2f}")
    if b.get("recent"):
        rc, rp = b["recent"], b["recent"]["pctiles"]
        print(f"  近{rc['years']}年子窗({rc['days']}天): "
              f"P10 {rp[10]:.2f}  P25 {rp[25]:.2f}  P50 {rp[50]:.2f}  "
              f"P75 {rp[75]:.2f}  P90 {rp[90]:.2f}"
              "  ← 锚定/交易区间建议用这组（全窗把 2021 regime 原样计入）")
    if b["other_basis_median"]:
        print(f"  对照 {b['other_basis']} 口径中位数 {b['other_basis_median']:.2f}"
              f"（两者之比 {b['other_basis_median'] / b['median']:.2f}x ≈ 期间 EPS 增速，"
              "两个口径的带子不可混用）")
    print(f"  注意：均值 ≠ 中位数 ≠ (min+max)/2。{LBL} 序列右偏时 (min+max)/2 会系统性偏高。")
    print(f"  (min+max)/2 = {(b['min'] + b['max']) / 2:.2f}   vs   真中位数 {b['median']:.2f}")

    print(f"\n{'候选带':<14}{'下沿':>8}{'上沿':>8}{'中点':>8}{'半宽%':>8}{'覆盖率':>9}")
    for c in b["candidates"]:
        print(f"{c['name']:<14}{c['lo']:>8.2f}{c['hi']:>8.2f}{c['mid']:>8.2f}"
              f"{100 * (c['hi'] - c['mid']) / c['mid']:>7.1f}%{c['coverage_pct']:>8.1f}%")

    # 逐财年实现 PE：某条带子是否真框住了每一年，只能这样核对
    print(f"\n{'财年(期末)':<14}{'最低' + LBL:>9}{'中位' + LBL:>9}{'最高' + LBL:>9}{'天数':>7}")
    for f in b["fiscal_years"]:
        print(f"{f['fy_end']:<14}{f['low']:>9.2f}{f['median']:>9.2f}"
              f"{f['high']:>9.2f}{f['days']:>7}")

    if a.match:
        lo, hi = (float(x) for x in a.match.split(","))
        mid = (lo + hi) / 2
        k_lo = (mean - lo) / sd_ if sd_ else 0
        k_hi = (hi - mean) / sd_ if sd_ else 0
        cov = coverage(sv, lo, hi)
        fy_in = sum(1 for f in b["fiscal_years"] if f["low"] >= lo and f["high"] <= hi)
        print(f"\n=== 逆向匹配 [{lo}, {hi}] ===")
        print(f"  下沿 {lo} → 第 {rank_of(sv, lo):.1f} 百分位 = 均值 − {k_lo:.2f}σ")
        print(f"  上沿 {hi} → 第 {rank_of(sv, hi):.1f} 百分位 = 均值 + {k_hi:.2f}σ")
        print(f"  中点 {mid:.2f}（真中位数 {b['median']:.2f}，均值 {mean:.2f}），"
              f"半宽 ±{100 * (hi - mid) / mid:.1f}%")
        print(f"  覆盖率 {cov:.1f}% 的交易日 | 完整框住 {fy_in}/{len(b['fiscal_years'])} 个财年")
        print(f"  对称性: {'σ 对称' if abs(k_lo - k_hi) < 0.15 else 'σ 不对称——不是均值±kσ 规则'}")
        b["match"] = {"lo": lo, "hi": hi, "mid": mid, "k_lo": k_lo, "k_hi": k_hi,
                      "pct_lo": rank_of(sv, lo), "pct_hi": rank_of(sv, hi),
                      "coverage_pct": cov, "fy_contained": fy_in,
                      "fy_total": len(b["fiscal_years"])}

    if a.out:
        json.dump(b, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n已写出 {a.out}")


if __name__ == "__main__":
    sys.exit(main())
