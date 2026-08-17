# valuation/ — 估值引擎

三层架构：**数据层（XBRL，零 LLM）→ 计算层（本目录，确定性 Python）→ 判断层（LLM/人写 config 定假设，逐条注明出处）**。

## 流水线

```powershell
# 1. 取数（XBRL companyfacts -> facts.json，含 TTM）
python valuation\fetch_facts.py NVDA facts.json you@email.com

# 2. 判断层写 config.json（假设 + 出处），schema 见下

# 3. 引擎计算（PE 法 / 十年 FCFF DCF / SOTP + 反向 DCF + 敏感性）
python valuation\engine.py config.json facts.json valuation.json [manifest.csv]

# 4. 生成六表 Excel（默认 reports/{T}_valuation_{date}.xlsx）
python valuation\build_report.py valuation.json

# 5. 独立验证（formulas 包重算 16~17 个关键单元格 vs 引擎，交易区间块在时多一项）
python valuation\verify_report.py valuation.json reports\NVDA_valuation_2026-07-17.xlsx
```

依赖：`pip install httpx openpyxl formulas yfinance`（yfinance 用于取现价）。

## config.json schema

```jsonc
{
  "ticker": "NVDA", "name": "NVIDIA (英伟达)", "date": "2026-07-17",
  "price": 207.40, "mcap": 5023435,        // $M，yfinance
  "shares": 24391, "fwd_shares": 24300,    // 百万股：最新稀释 / 前瞻期(NTM)估计
  "net_cash": 44000, "net_cash_note": "现金+短期证券-债务，出处…",
  "adj_ni": 137831, "adj_note": "TTM 调整口径说明（还原/剔除了哪些一次性项）",
  "other_income": 2400,                    // 年化其他收益 $M
  "fwd_label": "NTM 2026-08~2027-07（横跨 FY2027 后段 + FY2028 前段）",
                                           // 服务器从 report_end 派生并覆盖——判断层不要输出；
                                           // 前瞻期恒为 NTM 而非财年（见 valuation_service.fwd_window）
                                           // 手写 config 时照此格式给一个说明性标签即可
  "seg1": "主分部名", "seg2": "次分部名", "seg1_share": 0.95,
  "semantics_version": 3,                  // v3（2026-08-14）：v2 之上 base PE 锚历史 NTM 带子窗 P50
  "scenarios": {
    "bear|base|bull": {
      "g": 0.32,          // 前瞻期(NTM)营收增速 vs TTM
      "opm": 0.64, "tax": 0.16, "pe": 30,
      "m1": 24, "m2": 15, // 分部 EV/EBIT 倍数
      "wacc": 0.095, "tg": 0.03,
      "g0": 0.32, "gN": 0.05,             // DCF 十年线性衰减起止增速；gN∈(0,0.12]——
                                          // 终年增速必须为正，衰退终态建模进 margins 而非负 gN
      "margins": [0.46, ...],             // 十年 FCF 利润率路径（10 个值，上限 0.65 或 1.2×当前FCF率）
      "permanent_impairment": true,        // 可选：判断为永久受损（解锁低盈利×低倍数组合）
      "impairment_note": "原文出处"        // permanent_impairment 时必填
    }
  },
  "rationale": { "g|opm|pe|m1|rl|wacc|dcf_margin": "每条假设的依据" },
  "notes": ["写进报告摘要的判断层注记（一次性项目、风险、出处）"]
}
```

## v2 语义（semantics_version=2，2026-07-22）

三次 NVDA 实测运行 bear 综合在 \$29-\$77 间漂移（同财报同 prompt）、10 次受控采样
bear \$37-\$87，根因是判断层对连续参数的独立采样叠加"情景倍数×同情景盈利"的
周期双重计数。v2 改动：

- **一致性校验**（valuation_service）：单参数边界、跨情景排序（g/opm/pe/m1 须
  bear<=base<=bull）、反双重计数（盈利收缩>20% 时 pe/m1 >= 0.6×base；扩张>25% 时
  <= 1.4×base）、margins 谷底 >= 0.4×TTM FCF 利润率；`permanent_impairment=true`
  + `impairment_note` 可豁免（永久受损判断下低盈利×低倍数自洽）
- **SOTP 降级**（engine）：seg1_share >= 0.85 时 SOTP 只作参考不入综合
  （与 PE 法同一笔盈利计两次），综合 = PE/DCF 均值
- **诊断红旗**（engine 输出 diagnostics/warnings）：DCF 第10年营收 >8×TTM、
  终值占 EV >75%、隐含 P/FCF 界外 [5,90] 为 red（服务层打回判断层一次）；
  隐含 P/adj_ni 界外 [6,60]、方法离散 >2x、base 偏离现价 >±35% 为 yellow（报告红旗区展示）
- 引擎输出增加顶层 `semantics_version` 与 meta.sotp_in_blend；v1/v2 config 的
  倍数假设不可直接对比（v1 的 pe 语义相同但无联动约束）

## v3 语义（semantics_version=3，2026-08-14）

- **判断层 PE 锚**（valuation_service 注入）：base 目标 PE 默认锚历史已实现 NTM PE 带
  近 3 年子窗 P50，偏离 ±15% 须在 rationale.pe 给财报证据；bear/bull 参照 P25/P75 量级。
  engine.pe_band_check 的 base 中枢检查 = 子窗 [P10,P90] ∪ P50±15% 并集，与纪律同口径
- **pe 下限带证据自适应**：锚窗 P50×1.15<8 的低倍数票，校验下限自动放宽至
  max(4, 0.6×锚窗P10)（带子即证据）；无带子回退静态 8
- 锚改变 base PE 的产生方式与目标价水平：锚前(≤v2)/锚后(v3) 样本在 trend/compare 里
  按版本隔离，连续性锚跨版本自动失效重建
- **近零利润守卫**：情景 eps1<=0 或 opm<2% 时 PE 腿 n.m. 退出综合（blend_methods 记录），
  综合退化为 DCF(+SOTP)，红旗区给 P/S 参考价（facts.ps_band，不入综合）；
  `op1<=0` 时 SOTP 腿同样剔出（EV/EBIT 对负 EBIT 不成立）
- **未盈利标的**（2026-08-11）：opm 允许为负（下限 -1.0）；NTM 盈利为负的情景须给
  `tax=0`、`pe=0`、`m1=m2=0`（倍数法对负分子不成立），校验层强制、prompt 已写明。
  此前 `0 < opm` 与从负 FCF 率算出的空 margins 区间让这类标的必然两次 retry 后硬失败。
  引擎按上面的守卫剔腿，综合退化为 DCF；亏损情景跳过历史 PE 带比对（对已声明不适用
  的倍数比对只会产出必然的黄旗）。详见 `TUNING.md` 的「未盈利标的」
- **交易区间的口径标注**（2026-08-17）：这块最容易被误读，四件事现在都写在输出里
  （engine stdout / Excel 摘要 / 前端一行摘要三处同源）：
  1. **区间对应的是盈利窗口那 12 个月**（`trading_range.eps_window`，如
     `NTM 2026-07~2027-06`），不是"近3年"——后者是**倍数历史**的长度
  2. **倍数窗口的真实起止与滞后**（`.span`）：ntm 口径要求"该日之后满 4 个季度已披露"，
     所以**最近约一年结构性无值**。MSFT 实测标称"近3年"、实际覆盖 2023-08~2025-10、
     止于 293 天前——最近一年的倍数变化不在分布内
  3. **现价当前位置**（`.fwd_pe_now` = 价÷base 前瞻 EPS，与本带同一分母概念可直接比分位）
     与 **倍数回归归因**（`.mult_reversion_to_p50`）：区间中位相对现价的涨幅
     **按构造恒等于**倍数回归幅度（EPS 在分子分母里消掉），与盈利预测无关。
     现价跌出 P10 / 冲破 P90 时打一条 🟡
  4. **窗口内漂移**（`.drift`）：同一条带子内"近一年观测"vs"更早观测"的 P50 分开给，
     期间 re-rating 过的票不会被平均成一个看不出分歧的中枢
- **无滞后 trailing 对照**（`pe_band.trailing_nolag`）：补上主带结构性看不见的最近一年，
  含"主带盲区那一段"的单独分位。**分母口径不同**（trailing=价÷过去12个月），成长股
  系统性高出约一个增长率——MSFT 实测同一天 trailing 39.7 / ntm 30.2 = 1.316，恰好等于
  FY2026/FY2025 EPS 之比，是算术不是巧合。因此数据层只出原始数，engine 用 base g
  折成 NTM 可比口径并标注"仅量级对照"，**任何情况下不可与主带直接相减**
- **blend 权重政策**：`VALUATION_BLEND_W_PE/_DCF/_SOTP/_PTBV`（默认等权，行为不变）；
  权重随 valuation.json 进 Excel 公式与 compare/trend——改权重=改口径
- **Rule of 40 注入**：营收 TTM 增速 + 营业利润率/FCF 口径（剔 SBC 变体）进 prompt
  元数据与摘要——高倍数值不值得给的对照标尺，不执法
- **financials v2（2026-08-14）**：base P/TBV 默认锚历史带（时点流通股分母）锚窗 P50
  ±15% 证据纪律；financials 情景开通 warnings 通道（P/TBV 带检查）

## 判断层检查清单（写 config 前必做）

1. **一次性项目**：逐季对比净利 vs 营业利润，异常季度去财报里找原因（税务法案、投资重估、
   减值、出口管制费用），调整后再算 TTM EPS
2. **分部数据**：10-K/10-Q 的 Segment 附注（XBRL companyfacts 没有分部数据）
3. **净现金**：XBRL 的 instant 标签常滞后，以最新 10-Q 的流动性章节原文为准
4. **周期股**（内存/油气等）：bear 必须建模完整下行段；PE 用峰值利润低倍数
5. **前瞻信息**：资本开支指引、客户预付款、产能投放时点——都在 10-Q MD&A 里
