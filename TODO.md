# 路线图 / TODO

## ✅ v0.1 — 搜索 · 下载 · 重命名（已完成）

- [x] ticker → CIK 查询（SEC company_tickers.json，24h 缓存）
- [x] 公司信息卡：名称 / 交易所 / 官网 / 投资者网站 / 最新财报日期
- [x] 10-K / 10-Q / 20-F / 6-K 按季度区间批量下载（支持"到最新"）
- [x] 超出最近 1000 条提交时自动拉取历史分页
- [x] 统一重命名 `代码_表单_报告期.htm` + zip 打包 + manifest.csv 溯源
- [x] SEC 合规：User-Agent 带邮箱、0.12s 请求间隔、单次 60 文件上限
- [x] 深色单页前端（中文，无构建步骤）
- [x] 快捷模式「最新一份 / 最新两份」（默认最新一份，每种类型各取最新 N 份）

## 📦 v0.2 — 文件体验增强

- [x] 6-K 附件自动下载（业绩正文常在 EX-99 里，主文档只是封面；2026-07-17 实装）
- [x] 6-K 智能挑选 v1：同报告期取最大文件 + 精确季度末优先（TSM 类 reportDate=季度末的发行人已可用）
- [ ] 6-K 智能挑选 v2：BABA 类发行人 reportDate=发布日、垃圾公告又大又多，需接 EDGAR 全文搜索
      API（efts.sec.gov）按 "Results Announcement" 等关键词识别业绩 6-K
- [ ] HTML 财报转 PDF（参考站的卖点；可用 headless Chromium / weasyprint）
- [ ] XBRL 原始文件下载选项
- [ ] 包含修正版（10-K/A、10-Q/A）的开关
- [ ] 下载进度条（服务端 SSE 或 websocket 推送逐文件进度）
- [ ] 批量输入多个 ticker，一次打包
- [ ] 公司官网 / 投资者网站为空时的兜底（SEC 字段经常空缺，可从 10-K 封面页解析）

## 📊 v0.3 — 财务数据提取（估值的地基）

- [x] 接 SEC XBRL companyfacts API：`valuation/fetch_facts.py`（2026-07-17 实装）
      —— 多标签合并（公司会换标签）、Q4 自动推导、TTM（损益=四季加总；现金流=年度+YTD差额）
- [x] 核心科目时间序列：营收/营业利润/净利/EPS/CFO/CapEx/现金/债务/股本
- [ ] 数据缓存（SQLite），避免重复请求 SEC
- [ ] 分部数据自动提取（目前由判断层从 10-K/10-Q Segment 附注人工提取）

## 💰 v0.4 — 估值分析

- [x] 估值引擎 `valuation/engine.py`：PE 法 + 十年两段式 FCFF DCF + SOTP/倍数法 +
      反向 DCF + WACC×永续g 敏感性；bear/base/bull 三情景（2026-07-17 实装）
- [x] 六表 Excel 报告 `valuation/build_report.py`（摘要/情景假设/DCF/SOTP/历史数据/出处，
      假设全部为可调黄色格、公式联动）+ `verify_report.py` 独立复算验证
- [x] 用户级 Claude Code skill `/valuation TICKER` 串起全流程（LLM 只做判断层：定假设+注出处）
- [x] 已产出样例：META / NVDA / MU（reports/，不入库）
- [ ] 相对估值：PE / PS / EV-EBITDA 历史分位 + 同行对比
- [ ] 报告 PDF 导出

## 🚀 v0.5 — 部署与产品化

- [ ] 部署到 Railway / Fly.io（参考站即 Railway）
- [ ] 简单访问控制（密码或邀请码）
- [ ] 请求日志与失败重试
- [ ] 移动端适配微调
- [ ] （可选）Stripe 付费墙，复刻参考站商业模式
