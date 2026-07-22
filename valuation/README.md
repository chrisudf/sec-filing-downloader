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

# 5. 独立验证（formulas 包重算 16 个关键单元格 vs 引擎）
python valuation\verify_report.py valuation.json reports\NVDA_valuation_2026-07-17.xlsx
```

依赖：`pip install httpx openpyxl formulas yfinance`（yfinance 用于取现价）。

## config.json schema

```jsonc
{
  "ticker": "NVDA", "name": "NVIDIA (英伟达)", "date": "2026-07-17",
  "price": 207.40, "mcap": 5023435,        // $M，yfinance
  "shares": 24391, "fwd_shares": 24300,    // 百万股：最新稀释 / 下一财年估计
  "net_cash": 44000, "net_cash_note": "现金+短期证券-债务，出处…",
  "adj_ni": 137831, "adj_note": "TTM 调整口径说明（还原/剔除了哪些一次性项）",
  "other_income": 2400,                    // 年化其他收益 $M
  "fwd_label": "FY2027E（至2027-01）",
  "seg1": "主分部名", "seg2": "次分部名", "seg1_share": 0.95,
  "semantics_version": 2,                  // v2（2026-07-22）：一致性规则/诊断/SOTP 降级口径
  "scenarios": {
    "bear|base|bull": {
      "g": 0.32,          // 下一财年营收增速 vs TTM
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

## 判断层检查清单（写 config 前必做）

1. **一次性项目**：逐季对比净利 vs 营业利润，异常季度去财报里找原因（税务法案、投资重估、
   减值、出口管制费用），调整后再算 TTM EPS
2. **分部数据**：10-K/10-Q 的 Segment 附注（XBRL companyfacts 没有分部数据）
3. **净现金**：XBRL 的 instant 标签常滞后，以最新 10-Q 的流动性章节原文为准
4. **周期股**（内存/油气等）：bear 必须建模完整下行段；PE 用峰值利润低倍数
5. **前瞻信息**：资本开支指引、客户预付款、产能投放时点——都在 10-Q MD&A 里
