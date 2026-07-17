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
  "scenarios": {
    "bear|base|bull": {
      "g": 0.32,          // 下一财年营收增速 vs TTM
      "opm": 0.64, "tax": 0.16, "pe": 30,
      "m1": 24, "m2": 15, // 分部 EV/EBIT 倍数
      "wacc": 0.095, "tg": 0.03,
      "g0": 0.32, "gN": 0.05,             // DCF 十年线性衰减起止增速
      "margins": [0.46, ...]              // 十年 FCF 利润率路径（10 个值）
    }
  },
  "rationale": { "g|opm|pe|m1|rl|wacc|dcf_margin": "每条假设的依据" },
  "notes": ["写进报告摘要的判断层注记（一次性项目、风险、出处）"]
}
```

## 判断层检查清单（写 config 前必做）

1. **一次性项目**：逐季对比净利 vs 营业利润，异常季度去财报里找原因（税务法案、投资重估、
   减值、出口管制费用），调整后再算 TTM EPS
2. **分部数据**：10-K/10-Q 的 Segment 附注（XBRL companyfacts 没有分部数据）
3. **净现金**：XBRL 的 instant 标签常滞后，以最新 10-Q 的流动性章节原文为准
4. **周期股**（内存/油气等）：bear 必须建模完整下行段；PE 用峰值利润低倍数
5. **前瞻信息**：资本开支指引、客户预付款、产能投放时点——都在 10-Q MD&A 里
