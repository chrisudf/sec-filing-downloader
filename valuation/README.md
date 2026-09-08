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
  "semantics_version": 4,                  // v4（2026-09-06）：v3 之上服务器前置注入 FCF 锚表/OI&E 残差/期后 filing 索引
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

- **一致性校验**（valuation_service）：单参数边界、跨情景排序（g/opm/pe/m1/m2 须
  bear<=base<=bull）、反双重计数（盈利收缩>20% 时 pe/m1/m2 >= 0.6×base；扩张>25% 时
  <= 1.4×base；m2 仅真双分部参与，被亏损协议强制 0 的 m1/m2 不参与）、
  margins 谷底 >= 0.4×TTM FCF 利润率；`permanent_impairment=true`
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
     **按构造恒等于**倍数回归幅度（`px50/价 − 1 = P50/fwd_pe_now − 1`）。注意这句话
     成立的前提是**给定同一个 base EPS**——此时现价与中位价用同一个分母，两者之差
     只能是倍数之差、不能由盈利预测解释；但这**不等于**"与盈利预测无关"，改 base EPS
     会同比例移动中位价（`fwd_pe_now` 本身也是 价÷base EPS）。
     现价跌出 P10 / 冲破 P90 时打一条 🟡
  4. **窗口内漂移**（`.drift`）：同一条带子内"近一年观测"vs"更早观测"的 P50 分开给，
     期间 re-rating 过的票不会被平均成一个看不出分歧的中枢
- **无滞后 trailing 对照**（`pe_band.trailing_nolag`）：补上主带结构性看不见的最近一年，
  含"主带盲区那一段"的单独分位。**分母口径不同**（trailing=价÷过去12个月），成长股
  系统性高出约一个增长率——MSFT 实测同一天 trailing 39.7 / ntm 30.2 = 1.316，恰好等于
  FY2026/FY2025 EPS 之比，是算术不是巧合。因此数据层只出原始数，engine 用
  **EPS 增速**（建模 NTM EPS ÷ **GAAP** TTM EPS，字段 `trailing_ntm_eps_growth`）折成
  NTM 可比口径并标注"仅量级对照"，**任何情况下不可与主带直接相减**。
  换算因子**不能用营收增速 g**：利润率、税率、其他收益、股数变化都会让 EPS 增速与
  营收增速显著分叉；分母也必须取 GAAP 口径（带子的分母是 `NetIncomeLoss`），
  用调整后 EPS 会引入第二重错配。拿不到正的 GAAP TTM EPS 时不做换算
- **blend 权重政策**：`VALUATION_BLEND_W_PE/_DCF/_SOTP/_PTBV`（默认等权，行为不变）；
  权重随 valuation.json 进 Excel 公式与 compare/trend——改权重=改口径
- **Rule of 40 注入**：营收 TTM 增速 + 营业利润率/FCF 口径（剔 SBC 变体）进 prompt
  元数据与摘要——高倍数值不值得给的对照标尺，不执法
- **financials v2（2026-08-14）**：base P/TBV 默认锚历史带（时点流通股分母）锚窗 P50
  ±15% 证据纪律；financials 情景开通 warnings 通道（P/TBV 带检查）

## v4 语义（semantics_version=4，2026-09-06）

- **数据前置注入**（valuation_service，只改注入不改引擎计算）：此前 prompt 许诺的
  数据服务器有却不给，判断层徒手拼数、引擎事后打旗。v4 起 FACTS 摘要注入：
  1. **年度 FCF 利润率表**（与 engine.hist_fcf_margins 同口径，含峰值/中位与
     实际覆盖财年、缺口逐年点名）——终值 margins 的锚
  2. **OI&E 组件矩阵 + 逐季「税前−营业利润」残差行**（只列实际存在的序列，
     缺失的显式点名）——other_income 的推导基
  3. **期后 filing 索引**（报告期后提交的 424B*/S-*/8-K/SC 13*/10-*，服务器实查
     EDGAR submissions、不下载文档）——post_period_capital_events 核对的线索；
     取数失败降级为显式"索引不可用"，绝不连累估值任务
- 锚的来源改变 = 语义改变（与 v2→v3 同构）：注入前(≤v3)/注入后(v4) 样本在
  trend/compare 里按版本隔离，连续性锚跨版本自动失效重建一次（预期行为）。
  financials 模式不随 v4 动（其补课单独走 fin v3，见下节）。

### 分部对照（0023，2026-09-08，**不占语义号**）

管道此前从不取分部数据（facts.json 里一条分部字段都没有），判断层凭 SECTIONS
正文自拍 `seg1_share`，而它 `>= 0.85` 会让引擎把 SOTP 腿降级为参考项——
「本公司是单一业务」一句自我声明就能悄悄关掉一整条估值腿，校验层还只查 `0<=x<=1`。
MSFT 2026-09-08 实测就是这个形态（判断层 `seg1_share=1.0` 零理由，发行人按三分部
申报 44%/42%/14%）。

- **注入**：`build_segments` 取发行人自己申报的分部营收结构，作为「# 分部（发行人
  XBRL 申报）」块进 prompt（standard 模式）。取数失败/超时降级为显式的"无可用分部
  申报"，绝不连累估值任务——与期后 filing 索引同规格。
- **举证闸**（`_check_seg1_share`）：发行人多分部 + 最大分部营收占比 < 85% + 判断层
  给 `seg1_share >= 85%` + `rationale.sotp` 为空 → 拒收。**不做数值等式检查**：
  seg1_share 是营业利润占比、对照物是营收占比，两者合法地不同；要的是理由不是对齐。
- **呈现**（`engine.seg_share_crosscheck`）：两个口径差 >25pp 时进 `warnings_global`
  黄旗，并点明 SOTP 腿在不在综合里。

**为什么不升语义号**：v3/v4 改的是**每一个**标的的锚来源，而这条只在判断层原本
就与发行人申报矛盾时才改变输出（NVDA 94% vs 91.8%、AMZN 58% vs 57.9% 逐位不变）。
形态与 0018 幽灵 ADR 同类——修的是缺陷而非口径，同样不占语义号。真改变了综合腿集合
的那些运行，`compare.py` 的逐情景 `blend_methods` 警告本就会报。

## financials v3 语义（fin semantics_version=3，2026-09-06）

SOFI 实测一次 financials 运行零警告发货——standard 在 v2 就补的护栏 financials
一条都没有。fin v3 补课：

- **跨情景排序**（_validate_judgment_financials）：g / nm / pe / ptbv 须
  bear<=base<=bull——此前 bear.pe > bull.pe、倒挂的 g 全部放行（正是 v2 给
  standard 修掉的漂移放大器）
- **亏损协议**：nm 下界从 0 放宽到 -0.5；nm<=0 的情景必须 pe=0。旧下界让
  「刚扭亏 fintech 的 P20 bear 现实上是亏损」无法表达，prompt 还推着模型
  『压到微利』——假微利 × 15-30x PE 静默进综合（standard 的 COIN 微利除法
  事故同型）。引擎把 nm<=0 / eps1<=0 的 PE 腿标 n.m. 剔出综合（blend_methods
  记录），综合退化为 P/TBV 单腿；**微利守卫**：0<nm<1% 同样 n.m.（黄旗）
- Excel 综合公式与引擎 blend_methods 同构（PE 腿 n.m. 时 verify_report 不假 FAIL）
- **基线护栏补齐**（engine fin 分支此前在所有 base 级护栏之前 return）：
  时效检查（vintage_warnings——银行/券商/fintech 恰是 10-Q 滞后重灾区，fin 无
  net_cash，锚退到 TBV 口径）、base 综合偏离现价 ±35% 黄旗、方法离散度 >2x 黄旗、
  目标 PE 的历史带比对（pe_band 对 fin facts 本就生成，此前只查了 P/TBV 腿）
- **连续性锚持久化**：写路径去掉 mode=="standard" 门禁（load 路径早就支持 fin
  语义，写路径不写=fin 自动连续性结构性 no-op），gate-clean 判据不变
- 综合目标价的产生方式结构性改变：fin v2 样本与 v3 在 trend/compare 里按版本
  隔离，连续性锚跨版本自动失效重建一次（预期行为）

## 判断层检查清单（写 config 前必做）

1. **一次性项目**：逐季对比净利 vs 营业利润，异常季度去财报里找原因（税务法案、投资重估、
   减值、出口管制费用），调整后再算 TTM EPS
2. **分部数据**：10-K/10-Q 的 Segment 附注（XBRL companyfacts 没有分部数据）
3. **净现金**：XBRL 的 instant 标签常滞后，以最新 10-Q 的流动性章节原文为准
4. **周期股**（内存/油气等）：bear 必须建模完整下行段；PE 用峰值利润低倍数
5. **前瞻信息**：资本开支指引、客户预付款、产能投放时点——都在 10-Q MD&A 里
