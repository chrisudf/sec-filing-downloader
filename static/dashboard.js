const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const C = { s1: css("--s1"), s2: css("--s2"), s3: css("--s3"), s4: css("--s4"),
            s5: css("--s5"), s6: css("--s6"), s7: css("--s7"), s8: css("--s8"),
            text: css("--text"), muted: css("--muted"), border: css("--border"),
            card2: css("--card2") };

const state = { freq: "quarterly", data: null, charts: {}, seq: 0, segSeq: 0,
                segMode: "abs", segData: null };
const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- 控件与 URL 同步 ----
const qs = new URLSearchParams(location.search);
if (qs.get("ticker")) $("ticker").value = qs.get("ticker").toUpperCase();
if (qs.get("compare")) $("compare").value = qs.get("compare").toUpperCase();
if (qs.get("freq") === "annual") setFreq("annual");
if (["3", "5", "10"].includes(qs.get("years"))) $("years").value = qs.get("years");

function setFreq(v) {
  state.freq = v;
  for (const b of $("freqSeg").querySelectorAll("button"))
    b.classList.toggle("on", b.dataset.v === v);
}
$("freqSeg").addEventListener("click", (e) => {
  // 守卫用输入框而不是 state.data：首次加载在途时切换也要生效（seq 会作废旧请求）
  if (e.target.dataset.v) { setFreq(e.target.dataset.v); if ($("ticker").value.trim()) load(); }
});
$("years").addEventListener("change", () => { if ($("ticker").value.trim()) load(); });
$("go").addEventListener("click", load);
$("ticker").addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
$("compare").addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
$("compare").addEventListener("change", () => { if ($("ticker").value.trim()) load(); });

function setStatus(cls, msg) {
  const el = $("status");
  el.className = "status " + cls;
  el.textContent = msg;
}

// ---- 数值格式 ----
function fmtUSD(v) {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
  if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
  return (v / 1e6).toFixed(0) + "M";
}
const fmtPct = (v) => v == null ? "—" : (v * 100).toFixed(1) + "%";
// 同比：季度用 i-4 避开季节性，年度用 i-1
const yoy = (arr, i, step) =>
  arr && arr[i] != null && arr[i - step] != null && arr[i - step] !== 0
    ? arr[i] / arr[i - step] - 1 : null;
// 对比票按日历季对齐：NVDA(1月年结) vs AAPL(9月年结) 的期末就近匹配
function alignCompare(mainPeriods, cmpPeriods, cmpArr) {
  const cmpDates = cmpPeriods.map(p => new Date(p.end).getTime());
  return mainPeriods.map(p => {
    const t = new Date(p.end).getTime();
    let best = -1, bestGap = 45 * 864e5;
    cmpDates.forEach((ct, j) => {
      const gap = Math.abs(ct - t);
      if (gap < bestGap) { best = j; bestGap = gap; }
    });
    return best >= 0 ? cmpArr[best] : null;
  });
}
const fmtYoY = (v) => v == null ? "" :
  `（YoY ${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%）`;

// ---- ECharts 公共外观 ----
function baseOpt() {
  return {
    textStyle: { color: C.muted, fontFamily: '"Segoe UI","Microsoft YaHei",system-ui,sans-serif' },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      backgroundColor: C.card2, borderColor: C.border,
      textStyle: { color: C.text, fontSize: 12, fontFamily: "inherit" },
      valueFormatter: fmtUSD,
    },
    legend: { bottom: 0, left: 10, right: 10, textStyle: { color: C.muted, fontSize: 12 }, itemWidth: 14, itemHeight: 10 },
    grid: { left: 56, right: 20, top: 24, bottom: 56 },
  };
}
const catAxis = (labels) => ({
  type: "category", data: labels,
  axisLine: { lineStyle: { color: C.border } },
  axisTick: { show: false },
  axisLabel: { color: C.muted, fontSize: 11 },
});
const usdAxis = () => ({
  type: "value",
  splitLine: { lineStyle: { color: C.border, opacity: .6 } },
  axisLabel: { color: C.muted, fontSize: 11, formatter: fmtUSD },
});
function mount(id, opt) {
  if (opt.toolbox === undefined) {
    opt.toolbox = { right: 4, top: 0, iconStyle: { borderColor: C.muted },
      feature: { saveAsImage: {
        name: `${state.data ? state.data.ticker : "chart"}_${id}`,
        backgroundColor: css("--card"), title: "存图" } } };
  }
  if (!state.charts[id]) state.charts[id] = echarts.init($(id));
  state.charts[id].setOption(opt, true);
}
window.addEventListener("resize", () => Object.values(state.charts).forEach(c => c.resize()));

const bar = (name, data, color, extra = {}) => Object.assign({
  name, type: "bar", data,
  itemStyle: { color, borderRadius: [3, 3, 0, 0] },
  barMaxWidth: 26, emphasis: { focus: "series" },
}, extra);

// ---- TTM 汇总 tiles：估值锚定的分母（P/E、EV/FCF 都用 TTM）----
function renderTtm(d) {
  const box = $("ttmTiles");
  box.textContent = "";
  const t = d.ttm || {};
  const fcf = (t.cfo && t.cfo.value != null && t.capex && t.capex.value != null)
    ? t.cfo.value - t.capex.value : null;
  const niv = t.net_income && t.net_income.value;
  const rev = t.revenue && t.revenue.value;
  const defs = [
    ["TTM 营收", t.revenue, fmtUSD],
    ["TTM 营业利润", t.op_income, fmtUSD],
    ["TTM 净利", t.net_income, fmtUSD],
    ["TTM OCF", t.cfo, fmtUSD],
    ["TTM FCF", { value: fcf, note: fcf != null ? "OCF−资本开支" : null }, fmtUSD],
    ["TTM EPS", t.eps_diluted, (v) => "$" + v.toFixed(2)],
    ["TTM 净利率", { value: (niv != null && rev) ? niv / rev : null }, fmtPct],
  ];
  let shown = 0;
  for (const [k, item, fmt] of defs) {
    if (!item) continue;
    const tile = document.createElement("div");
    tile.className = "tile";
    const kd = document.createElement("div"); kd.className = "k"; kd.textContent = k;
    const vd = document.createElement("div"); vd.className = "v";
    vd.textContent = item.value != null ? fmt(item.value) : "—";
    tile.appendChild(kd); tile.appendChild(vd);
    const note = item.error || item.note;
    if (note) {
      const nd = document.createElement("div"); nd.className = "n";
      nd.textContent = note;                     // 宁缺勿错：口径注记原样展示
      tile.appendChild(nd);
    }
    box.appendChild(tile);
    if (item.value != null) shown++;
  }
  box.style.display = shown ? "grid" : "none";
}

// ---- 图 1：损益摘要（上金额 / 下利润率，共用时间轴）----
function renderIncome(d, labels) {
  const inc = d.income, m = inc.margins;
  const opt = baseOpt();
  opt.axisPointer = { link: [{ xAxisIndex: "all" }] };
  const step = d.freq === "quarterly" ? 4 : 1;
  const seriesData = { "营收": inc.revenue, "营业成本": inc.cogs,
                       "运营费用": inc.opex, "净利润": inc.net_income };
  opt.tooltip.formatter = (ps) => {
    let html = `<b>${ps[0].axisValue}</b>`;
    for (const p of ps) {
      const pct = p.axisIndex === 1 || p.seriesName.includes("率") || p.seriesName.includes("YoY");
      const g = yoy(seriesData[p.seriesName], p.dataIndex, step);
      html += `<br>${p.marker} ${p.seriesName}: ${pct ? fmtPct(p.value) : fmtUSD(p.value)}${fmtYoY(g)}`;
    }
    // 一次性/市值波动项污染净利 ≥20% 的期，hover 直接给明细
    const i = ps[0].dataIndex;
    const ni = d.income.net_income[i];
    const impact = oneoffImpact(d, i);
    if (ni && Math.abs(impact) >= Math.abs(ni) * 0.2) {
      html += `<br><span style="color:${C.s7}">⚡ 本期含大额一次性/投资项：</span>`;
      for (const cx of oneoffAt(d, i))
        if (!["interest_income", "interest_expense_nonop", "fx_gain", "other_nonop"].includes(cx.key))
          html += `<br>&nbsp;&nbsp;${esc(cx.label)}: ${cx.val >= 0 ? "+" : "−"}${fmtUSD(Math.abs(cx.val))}`;
    }
    return html;
  };
  // 利润率面板给足高度：太矮时 0-100% 的刻度会把 20-50% 的正常波段挤成一团
  opt.grid = [
    { left: 56, right: 20, top: 24, height: "42%" },
    { left: 56, right: 20, top: "58%", height: "28%" },
  ];
  opt.xAxis = [
    Object.assign(catAxis(labels), { gridIndex: 0, axisLabel: { show: false } }),
    Object.assign(catAxis(labels), { gridIndex: 1 }),
  ];
  opt.yAxis = [
    Object.assign(usdAxis(), { gridIndex: 0 }),
    // scale:true 让利润率轴贴合数据范围：GOOG 净利率被投资收益推到 94% 时，
    // 0-100% 固定刻度会把正常的 20-40% 区间压成一条线
    { type: "value", gridIndex: 1, scale: true,
      splitLine: { lineStyle: { color: C.border, opacity: .6 } },
      axisLabel: { color: C.muted, fontSize: 11, formatter: (v) => (v * 100).toFixed(0) + "%" } },
  ];
  const line = (name, data, color) => ({
    name, type: "line", data, xAxisIndex: 1, yAxisIndex: 1,
    lineStyle: { width: 2, color }, itemStyle: { color },
    symbol: "circle", symbolSize: 5, connectNulls: false,
  });
  // 调整后净利率：净利剔除一次性/投资项（按当期有效税率税后化，
  // eff = 税额/税前，越界回退 21% 法定税率）——GOOG 报表净利率被
  // +99B 浮盈推到 94% 时，这条虚线还原经营内核（~27%）
  const adjNet = inc.net_income.map((ni, i) => {
    const rev = inc.revenue[i];
    if (ni == null || !rev) return null;
    const imp = oneoffImpact(d, i);
    if (!imp) return null;
    const pretax = inc.pretax_income[i], tax = inc.income_tax[i];
    let eff = 0.21;
    if (pretax && tax != null && pretax > 0)
      eff = Math.min(Math.max(tax / pretax, 0), 0.5);
    return (ni - imp * (1 - eff)) / rev;
  });
  const hasAdj = adjNet.some((v, i) =>
    v != null && m.net[i] != null && Math.abs(v - m.net[i]) > 0.01);

  // 整列为空的系列不画也不进图例（银行没有营业成本/毛利率，硬挂着会误导）
  // ⚡ 标记：一次性/投资项污染净利 ≥20% 的期，净利润柱顶加标（hover 有明细）
  const flags = inc.net_income.map((ni, i) =>
    ni && Math.abs(oneoffImpact(d, i)) >= Math.abs(ni) * 0.2 ? i : null)
    .filter(v => v !== null);
  const niBar = bar("净利润", inc.net_income, C.s4, {
    markPoint: {
      symbol: "circle", symbolSize: 1, silent: true,
      label: { show: true, formatter: "⚡", fontSize: 13, color: C.s7 },
      data: flags.map(i => ({ coord: [i, inc.net_income[i]],
                              symbolOffset: [0, -12] })),
    },
  });
  opt.series = [
    bar("营收", inc.revenue, C.s1),
    bar("营业成本", inc.cogs, C.s2),
    bar("运营费用", inc.opex, C.s3),
    niBar,
    line("毛利率", m.gross, C.s5),
    line("营业利润率", m.operating, C.s6),
    line("净利润率", m.net, C.s7),
    { name: "净利率(剔一次性)", type: "line", data: hasAdj ? adjNet : [],
      xAxisIndex: 1, yAxisIndex: 1, connectNulls: false,
      lineStyle: { width: 2, type: "dashed", color: C.s8 },
      itemStyle: { color: C.s8 }, symbol: "circle", symbolSize: 4 },
    // 营收 YoY 线默认藏在图例里（点图例开），财报前第一问是增速拐点
    { name: "营收 YoY%", type: "line",
      data: inc.revenue.map((_, i) => yoy(inc.revenue, i, step)),
      xAxisIndex: 1, yAxisIndex: 1, connectNulls: false,
      lineStyle: { width: 2, color: C.s1 }, itemStyle: { color: C.s1 },
      symbol: "circle", symbolSize: 4 },
  ].filter(s => s.data.some(v => v != null));
  opt.legend.selected = { "营收 YoY%": false };
  // 对比票 overlay：只叠无量纲的比率线（利润率/YoY），绝对金额不混叠
  if (state.cmp && state.cmp.data) {
    const cd = state.cmp.data, ct = state.cmp.ticker;
    const al = (arr) => alignCompare(d.periods, cd.periods, arr);
    const cmpLine = (name, arr, color, off) => ({
      name: `${name}(${ct})`, type: "line", data: al(arr),
      xAxisIndex: 1, yAxisIndex: 1, connectNulls: false,
      lineStyle: { width: 1.5, type: "dashed", color, opacity: .55 },
      itemStyle: { color, opacity: .55 }, symbol: "diamond", symbolSize: 4,
    });
    const cm = cd.income.margins;
    const cmpYoY = cd.income.revenue.map((_, i) =>
      yoy(cd.income.revenue, i, cd.freq === "quarterly" ? 4 : 1));
    const cmpSeries = [
      cmpLine("毛利率", cm.gross, C.s5),
      cmpLine("营业利润率", cm.operating, C.s6),
      cmpLine("净利润率", cm.net, C.s7),
      cmpLine("营收 YoY%", cmpYoY, C.s1),
    ].filter(x => x.data.some(v => v != null));
    opt.series.push(...cmpSeries);
    opt.legend.selected[`营收 YoY%(${ct})`] = false;
  }
  mount("cIncome", opt);
}

// ---- 图 1b：每股与股本（上 EPS / 下稀释股本，共用时间轴）----
function renderEps(d, labels) {
  const inc = d.income;
  const card = $("epsCard");
  if (!inc.eps_diluted.some(v => v != null)) {
    card.style.display = "none";
    if (state.charts.cEps) { state.charts.cEps.dispose(); delete state.charts.cEps; }
    return;
  }
  card.style.display = "block";
  const step = d.freq === "quarterly" ? 4 : 1;
  const opt = baseOpt();
  opt.axisPointer = { link: [{ xAxisIndex: "all" }] };
  opt.tooltip.formatter = (ps) => {
    let html = `<b>${ps[0].axisValue}</b>`;
    for (const p of ps) {
      if (p.seriesName === "稀释 EPS") {
        const g = yoy(inc.eps_diluted, p.dataIndex, step);
        html += `<br>${p.marker} 稀释 EPS: ${p.value == null ? "—" : "$" + p.value.toFixed(2)}${fmtYoY(g)}`;
      } else {
        html += `<br>${p.marker} 稀释股本: ${p.value == null ? "—" : (p.value / 1e9).toFixed(2) + "B 股"}`;
      }
    }
    return html;
  };
  opt.grid = [
    { left: 56, right: 20, top: 24, height: "44%" },
    { left: 56, right: 20, top: "62%", height: "26%" },
  ];
  opt.xAxis = [
    Object.assign(catAxis(labels), { gridIndex: 0, axisLabel: { show: false } }),
    Object.assign(catAxis(labels), { gridIndex: 1 }),
  ];
  opt.yAxis = [
    { type: "value", gridIndex: 0, scale: true,
      splitLine: { lineStyle: { color: C.border, opacity: .6 } },
      axisLabel: { color: C.muted, fontSize: 11, formatter: (v) => "$" + v } },
    { type: "value", gridIndex: 1, scale: true,
      splitLine: { lineStyle: { color: C.border, opacity: .6 } },
      axisLabel: { color: C.muted, fontSize: 11,
                   formatter: (v) => (v / 1e9).toFixed(1) + "B" } },
  ];
  opt.series = [
    bar("稀释 EPS", inc.eps_diluted, C.s1),
    { name: "稀释股本", type: "line", data: inc.shares_diluted,
      xAxisIndex: 1, yAxisIndex: 1, connectNulls: false,
      lineStyle: { width: 2, color: C.s2 }, itemStyle: { color: C.s2 },
      symbol: "circle", symbolSize: 5 },
  ];
  mount("cEps", opt);
}

// ---- 一次性/营业外组件（SEC XBRL 有标准标签，罚款类覆盖较杂）----
const ONEOFF_LABELS = {
  equity_inv_gain: "股权投资公允价值变动", interest_income: "利息收入",
  interest_expense_nonop: "利息支出", fx_gain: "汇兑损益",
  other_nonop: "其他营业外", restructuring: "重组费用",
  impairment: "资产/商誉减值", litigation: "诉讼和解/罚金",
  disposal_gain: "业务处置损益",
};
// 支出性质的组件按报表口径取负号
const ONEOFF_SIGN = { interest_expense_nonop: -1, restructuring: -1,
                      impairment: -1, litigation: -1 };

function oneoffAt(d, i) {
  const out = [];
  for (const [k, arr] of Object.entries(d.oneoff || {})) {
    const v = arr[i];
    if (v == null || v === 0) continue;
    out.push({ key: k, label: ONEOFF_LABELS[k], val: (ONEOFF_SIGN[k] || 1) * v });
  }
  return out;
}

// 一次性/市值波动类（利息收支是经常性的，不算）对净利的污染程度
function oneoffImpact(d, i) {
  return oneoffAt(d, i)
    .filter(x => !["interest_income", "interest_expense_nonop", "fx_gain",
                   "other_nonop"].includes(x.key))
    .reduce((s, x) => s + x.val, 0);
}

// ---- 图 2：利润瀑布 ----
function wfItems(d, i) {
  const inc = d.income;
  const v = (arr) => (arr && arr[i] != null) ? arr[i] : null;
  const rev = v(inc.revenue), cogs = v(inc.cogs), rnd = v(inc.rnd), sga = v(inc.sga),
        opex = v(inc.opex), op = v(inc.op_income), pretax = v(inc.pretax_income),
        tax = v(inc.income_tax), ni = v(inc.net_income);
  if (rev == null || ni == null) return null;
  const items = [{ name: "营收", val: rev, total: true }];
  let run = rev;
  const push = (name, delta) => { items.push({ name, val: delta, total: false }); run += delta; };
  if (cogs != null) push("营业成本", -cogs);
  if (rnd != null) push("研发", -rnd);
  if (sga != null) push("销售及管理", -sga);
  // 运营费用里未拆出的部分（有 opex 披露时对账出「其他运营费用」）
  if (opex != null) {
    const other = opex - (rnd || 0) - (sga || 0);
    // 银行没有「其他运营费用」概念：整块是非利息支出（薪酬/经纪/技术）
    const lbl = d.bank_format ? ((rnd || sga) ? "非利息支出(其他)" : "非利息支出")
                              : "其他运营费用";
    if (Math.abs(other) > Math.abs(rev) * 0.001) push(lbl, -other);
  }
  if (op != null) {
    const drift = op - run;
    if (Math.abs(drift) > Math.abs(rev) * 0.001) push("其他(经营)", drift);
    items.push({ name: "营业利润", val: op, total: true });
    run = op;
  }
  if (pretax != null) {
    const nonop = pretax - run;
    // 银行的税前桥接项主体是信贷损失拨备，不是「营业外」
    if (Math.abs(nonop) > Math.abs(rev) * 0.001)
      push(d.bank_format ? "拨备及其他" : "营业外损益", nonop);
    run = pretax;
  }
  if (tax != null) push("所得税", -tax);
  const drift = ni - run;
  if (Math.abs(drift) > Math.abs(rev) * 0.001) push("其他(税后)", drift);
  items.push({ name: "净利润", val: ni, total: true });
  return items;
}

function renderWaterfall(d, i) {
  const items = wfItems(d, i);
  // 营业外损益按 XBRL 组件拆解：投资损益主导时改紫色并在 hover 里列明细
  const nonoffComp = oneoffAt(d, i).filter(x =>
    ["equity_inv_gain", "interest_income", "interest_expense_nonop",
     "fx_gain", "other_nonop"].includes(x.key));
  const eqv = nonoffComp.find(x => x.key === "equity_inv_gain");
  if (items) {
    for (const it of items) {
      if (it.name !== "营业外损益") continue;
      it.components = nonoffComp;
      it.unexplained = it.val - nonoffComp.reduce((s, x) => s + x.val, 0);
      it.investmentDriven = !!(eqv && Math.abs(eqv.val) > Math.abs(it.val) * 0.5);
    }
  }
  const el = $("cWaterfall");
  if (!items) {
    if (state.charts.cWaterfall) { state.charts.cWaterfall.dispose(); delete state.charts.cWaterfall; }
    el.innerHTML = '<div class="empty">该期损益科目不完整，无法绘制瀑布图</div>';
    return;
  }
  // 只在离开空态时清容器：图表活着时清 innerHTML 会把 ECharts 的画布拆下 DOM
  if (!state.charts.cWaterfall) el.innerHTML = "";
  const base = [], vis = [];
  let run = 0;
  for (const it of items) {
    if (it.total) { base.push(0); vis.push({ value: it.val, total: true }); run = it.val; }
    else {
      const lo = Math.min(run, run + it.val);
      base.push(lo); vis.push({ value: Math.abs(it.val), delta: it.val }); run += it.val;
    }
  }
  const opt = baseOpt();
  opt.tooltip.formatter = (ps) => {
    const p = ps.find(x => x.seriesIndex === 1);
    if (!p) return "";
    const it = items[p.dataIndex];
    let html = `<b>${it.name}</b><br>${it.total ? fmtUSD(it.val) : (it.val >= 0 ? "+" : "−") + fmtUSD(Math.abs(it.val))}`;
    if (it.components && it.components.length) {
      html += `<br><span style="color:${C.muted}">按 XBRL 组件拆解：</span>`;
      for (const cx of it.components)
        html += `<br>&nbsp;&nbsp;${esc(cx.label)}: ${cx.val >= 0 ? "+" : "−"}${fmtUSD(Math.abs(cx.val))}`;
      if (Math.abs(it.unexplained) > Math.abs(it.val) * 0.02)
        html += `<br>&nbsp;&nbsp;未拆解余项: ${it.unexplained >= 0 ? "+" : "−"}${fmtUSD(Math.abs(it.unexplained))}`;
      if (it.investmentDriven)
        html += `<br><span style="color:${C.s7}">◆ 投资损益主导（市值波动，非经营所得）</span>`;
    }
    return html;
  };
  opt.legend = { show: false };
  opt.grid.bottom = 40;
  opt.xAxis = catAxis(items.map(x => x.name));
  opt.xAxis.axisLabel.interval = 0;
  opt.xAxis.axisLabel.rotate = items.length > 7 ? 30 : 0;
  opt.yAxis = usdAxis();
  // stackStrategy "all"：默认 samesign 会让负基座+正可见段从 0 起画，
  // 亏损期（运行小计跨零）的悬空条会整个错位到零轴上方
  opt.series = [
    { type: "bar", stack: "wf", stackStrategy: "all", data: base,
      itemStyle: { color: "transparent" },
      emphasis: { itemStyle: { color: "transparent" } }, tooltip: { show: false }, barMaxWidth: 44 },
    { type: "bar", stack: "wf", stackStrategy: "all",
      data: vis.map((v, idx) => ({
        value: v.value,
        // 蓝=小计，橙=扣减，绿=增加，紫=投资损益主导的营业外项（市值波动）
        itemStyle: { color: v.total ? C.s1
                     : items[idx].investmentDriven ? C.s7
                     : (items[idx].val >= 0 ? C.s3 : C.s2),
                     borderRadius: 3 },
        label: {
          show: true, position: v.total ? "top" : (items[idx].val >= 0 && !v.total ? "top" : "bottom"),
          color: C.muted, fontSize: 11,
          formatter: () => v.total ? fmtUSD(items[idx].val)
                                   : (items[idx].val >= 0 ? "+" : "−") + fmtUSD(Math.abs(items[idx].val)),
        },
      })),
      barMaxWidth: 44 },
  ];
  mount("cWaterfall", opt);
}

// ---- 图 2b：各细分业务营收（独立异步加载，冷取数要逐份解析申报文件）----
const SEG_COLORS = [C.s1, C.s2, C.s3, C.s4, C.s5, C.s6, C.s7];
const OTHER_COLOR = "#7a7b88";

function renderSegments(axis) {
  const el = $("cSegments");
  if (state.charts.cSegments) { state.charts.cSegments.dispose(); delete state.charts.cSegments; }
  el.innerHTML = "";
  const labels = axis.periods.map(p => p.label);
  const pctMode = state.segMode === "pct";
  // 占比模式：各成员除以当期成员合计（含其他），回答 mix-shift；
  // 总营收线在占比下无意义，隐藏
  const rowSum = labels.map((_, i) => {
    let t = 0;
    for (const arr of axis.series) t += arr[i] || 0;
    if (axis.other) t += axis.other[i] || 0;
    return t || null;
  });
  const toPct = (arr) => arr.map((v, i) =>
    v != null && rowSum[i] ? v / rowSum[i] : null);
  const opt = baseOpt();
  const step = (state.data && state.data.freq === "annual") ? 1 : 4;
  const rawByName = {};
  axis.members.forEach((m, j) => { rawByName[m.label] = axis.series[j]; });
  if (axis.other) rawByName["其他"] = axis.other;
  opt.tooltip.formatter = (ps) => {
    let html = `<b>${esc(ps[0].axisValue)}</b>`;
    const i = ps[0].dataIndex;
    for (const p of [...ps].reverse()) {
      if (p.value == null) continue;
      if (p.seriesName === "总营收") {
        const mark = axis.derived[i] ? "（Q4=年度-前三季推导）" : "";
        html += `<br>${p.marker} 总营收: ${fmtUSD(p.value)}${mark}`;
        continue;
      }
      // 成员名源自申报文件（第三方数据），拼 HTML 前必须转义
      const raw = rawByName[p.seriesName] ? rawByName[p.seriesName][i] : null;
      const share = raw != null && rowSum[i] ? ` · 占 ${(raw / rowSum[i] * 100).toFixed(1)}%` : "";
      const g = yoy(rawByName[p.seriesName], i, step);
      html += `<br>${p.marker} ${esc(p.seriesName)}: ${fmtUSD(raw)}${share}${fmtYoY(g)}`;
    }
    if (axis.reconciled[i] === false) html += `<br>⚠ 该期分部与合并总额未对账`;
    return html;
  };
  // 图例一键隔离：点亮目标后按「反选」即单看一个分部
  opt.legend.selector = [{ type: "all", title: "全选" },
                         { type: "inverse", title: "反选" }];
  opt.xAxis = catAxis(labels);
  opt.yAxis = pctMode
    ? { type: "value", max: 1,
        splitLine: { lineStyle: { color: C.border, opacity: .6 } },
        axisLabel: { color: C.muted, fontSize: 11,
                     formatter: (v) => (v * 100).toFixed(0) + "%" } }
    : usdAxis();
  opt.series = axis.members.map((m, i) => ({
    name: m.label, type: "bar", stack: "seg",
    data: pctMode ? toPct(axis.series[i]) : axis.series[i],
    itemStyle: { color: SEG_COLORS[i % SEG_COLORS.length] },
    barMaxWidth: 30, emphasis: { focus: "series" },
  }));
  if (axis.other) {
    opt.series.push({ name: "其他", type: "bar", stack: "seg",
      data: pctMode ? toPct(axis.other) : axis.other,
      itemStyle: { color: OTHER_COLOR }, barMaxWidth: 30 });
  }
  if (!pctMode) {
    opt.series.push({ name: "总营收", type: "line", data: axis.total,
      lineStyle: { width: 2, type: "dashed", color: C.text },
      itemStyle: { color: C.text }, symbol: "circle", symbolSize: 5,
      connectNulls: false });
  }
  mount("cSegments", opt);
}

$("segMode").addEventListener("click", (e) => {
  const m = e.target.dataset.m;
  if (!m || m === state.segMode) return;
  state.segMode = m;
  for (const b of $("segMode").querySelectorAll("button"))
    b.classList.toggle("on", b.dataset.m === m);
  if (state.segData) {
    const cur = state.segData.axes.find(x => x.key === state.segAxis)
      || state.segData.axes[0];
    if (cur) renderSegments(cur);
  }
});

function renderSegAxisToggle(axes, active) {
  const seg = $("segAxis");
  seg.innerHTML = "";
  seg.style.display = axes.length > 1 ? "flex" : "none";
  for (const a of axes) {
    const b = document.createElement("button");
    b.textContent = a.label;
    b.classList.toggle("on", a.key === active);
    b.onclick = () => { state.segAxis = a.key; renderSegAxisToggle(axes, a.key); renderSegments(axes.find(x => x.key === a.key)); };
    seg.appendChild(b);
  }
}

function segEmpty(msg) {
  if (state.charts.cSegments) { state.charts.cSegments.dispose(); delete state.charts.cSegments; }
  $("segAxis").style.display = "none";
  $("cSegments").innerHTML = `<div class="empty">${""}</div>`;
  $("cSegments").firstChild.textContent = msg;
}

async function loadSegments(ticker, freq, years) {
  // 分部卡片用独立序号：主请求失败的新一轮不该作废仍然有效的在途分部
  // 响应，否则卡片会永久停在「加载中…」
  const seq = ++state.segSeq;
  segEmpty("分部数据加载中…（冷启动要逐份解析 10-K/10-Q，约 10-60 秒）");
  concReset("集中度数据加载中…");  // 别让上一只股票的风险横幅挂在加载窗口里
  try {
    const res = await fetch(`/api/segments/${encodeURIComponent(ticker)}?freq=${freq}&years=${years}`);
    if (seq !== state.segSeq) return;
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `请求失败（HTTP ${res.status}）`);
    }
    const d = await res.json();
    if (seq !== state.segSeq) return;
    state.segData = d;
    if (d.axes.length) {
      // 记住用户上次选的轴；没有同名轴再回默认（业务线>经营分部>地区）
      const pick = d.axes.find(x => x.key === state.segAxis) || d.axes[0];
      renderSegAxisToggle(d.axes, pick.key);
      renderSegments(pick);
    } else {
      segEmpty("该公司没有可用的分部营收数据");
    }
    renderConcentration(d.concentration);
  } catch (e) {
    if (seq === state.segSeq) {
      segEmpty(e.message);
      // 瞬态失败不能说成「未披露集中度」——那是错误的肯定信号
      concReset("集中度数据加载失败：" + e.message);
    }
  }
}

// ---- 图 2c：客户与供应商集中度 ----
function concReset(msg) {
  if (state.charts.cConcTrend) { state.charts.cConcTrend.dispose(); delete state.charts.cConcTrend; }
  $("concBanner").style.display = "none";
  $("cConcTrend").style.display = "none";
  const tableEl = $("concTable");
  tableEl.textContent = "";
  if (msg) {
    const d = document.createElement("div");
    d.className = "conc-empty";
    d.textContent = msg;
    tableEl.appendChild(d);
  }
}

function renderConcentration(c) {
  const banner = $("concBanner"), trendEl = $("cConcTrend"), tableEl = $("concTable");
  concReset(null);
  if (!c || !c.latest || !c.latest.length) {
    concReset("该公司未在 XBRL 中披露集中度风险数据（通常意味着没有单一客户占营收 ≥10%）。");
    return;
  }
  // 风险横幅：只看「占营收」基准的客户集中度，别的基准不冒充营收风险；
  // 群体口径（前N大合计/某地区客户）用「合计」措辞，不冒充单一客户
  banner.style.display = "block";
  const agg = c.risk.aggregate ? "（合计口径）" : "";
  banner.className = "conc-banner " +
    (c.risk.level === "stale" ? "low" : c.risk.level);
  banner.textContent = c.risk.level === "stale"
    ? `最近一次集中度披露在 ${c.risk.last_end}，此后的申报未再披露（通常意味着不再有 ≥10% 的集中）；下表为历史披露。`
    : c.risk.level === "high"
      ? `高度集中风险：${c.risk.top_party}${agg} 占营收 ${c.risk.top_pct}%，失去该客户将构成重大事件。`
      : c.risk.level === "medium"
        ? `中度集中：${c.risk.top_party}${agg} 占营收 ${c.risk.top_pct}%。`
        : "未披露占营收 ≥10% 的单一客户；下表为应收款/供应商等其他基准的集中度披露。";
  // 趋势：营收基准的单一客户合计（年度），≥2 期才画；披露空窗年份
  // 补 null 点断线呈现，不把跨年空档压成相邻点
  if (c.trend.length >= 2) {
    const tr = [];
    for (let i = 0; i < c.trend.length; i++) {
      if (i > 0) {
        const prevY = +c.trend[i - 1].end.slice(0, 4);
        const curY = +c.trend[i].end.slice(0, 4);
        for (let y = prevY + 1; y < curY; y++)
          tr.push({ label: "'" + String(y).slice(2), sum: null, count: null });
      }
      tr.push(c.trend[i]);
    }
    trendEl.style.display = "block";
    const opt = baseOpt();
    opt.legend = { show: false };
    opt.grid = { left: 56, right: 20, top: 28, bottom: 28 };
    opt.tooltip.formatter = (ps) => {
      const p = ps[0]; const t = tr[p.dataIndex];
      if (t.sum == null) return `<b>${esc(t.label)}</b><br>该年度未披露单一客户集中度`;
      return `<b>${esc(t.label)}</b><br>${p.marker} 重大单一客户合计占营收: ${t.sum}%（${t.count} 家）`;
    };
    opt.xAxis = catAxis(tr.map(t => t.label));
    opt.yAxis = { type: "value",
      splitLine: { lineStyle: { color: C.border, opacity: .6 } },
      axisLabel: { color: C.muted, fontSize: 11, formatter: (v) => v + "%" } };
    opt.series = [{
      type: "line", data: tr.map(t => t.sum), connectNulls: false,
      lineStyle: { width: 2, color: C.s1 }, itemStyle: { color: C.s1 },
      symbol: "circle", symbolSize: 6,
      areaStyle: { color: C.s1, opacity: .12 },
      label: { show: true, position: "top", color: C.muted, fontSize: 11,
               formatter: (p) => tr[p.dataIndex].count != null ? `${tr[p.dataIndex].count} 家` : "" },
    }];
    mount("cConcTrend", opt);
  }
  // 明细表透视（textContent 构建，成员名来自申报文件）：
  // 同一对手方的多基准并成一行，客户/供应商优先，地域/资产折叠
  const PRIMARY = ["客户", "供应商", "信用", "贷款人", "再保险"];
  const groups = new Map();
  for (const r of c.latest) {
    const k = r.party + "|" + r.type;
    if (!groups.has(k)) groups.set(k, { party: r.party, type: r.type,
                                        aggregate: r.aggregate, cells: [] });
    groups.get(k).cells.push(r);
  }
  const revPct = (g) => Math.max(0, ...g.cells.filter(x => x.benchmark === "营收")
                                              .map(x => x.pct));
  const allPct = (g) => Math.max(...g.cells.map(x => x.pct));
  const rows = [...groups.values()].sort((a, b) => {
    const pa = PRIMARY.indexOf(a.type), pb = PRIMARY.indexOf(b.type);
    const oa = pa < 0 ? 99 : pa, ob = pb < 0 ? 99 : pb;
    if (oa !== ob) return oa - ob;
    return (revPct(b) - revPct(a)) || (allPct(b) - allPct(a));
  });
  const primary = rows.filter(g => PRIMARY.includes(g.type));
  const secondary = rows.filter(g => !PRIMARY.includes(g.type));

  function buildTable(list) {
    const tbl = document.createElement("table");
    tbl.className = "conc";
    const head = tbl.createTHead().insertRow();
    for (const h of ["交易对手", "类型", "占比（按基准）", "期间"]) {
      const th = document.createElement("th");
      th.textContent = h;
      head.appendChild(th);
    }
    const body = tbl.createTBody();
    for (const g of list) {
      const tr = body.insertRow();
      tr.insertCell().textContent = g.party + (g.aggregate ? "（合计）" : "");
      const tdType = tr.insertCell();
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = g.type;
      tdType.appendChild(tag);
      const tdP = tr.insertCell();
      g.cells.sort((a, b) => (b.benchmark === "营收") - (a.benchmark === "营收")
                             || b.pct - a.pct);
      g.cells.forEach((r, i) => {
        if (i) tdP.appendChild(document.createTextNode("　"));
        const sp = document.createElement("span");
        sp.className = "pct";
        const range = r.pct_lo != null ? `${r.pct_lo}–${r.pct}` : `${r.pct}`;
        sp.textContent = `占${r.benchmark} ${range}%`;
        const risky = r.type === "客户" && r.benchmark === "营收";
        sp.style.color = risky && r.pct >= 30 ? css("--err")
          : risky && r.pct >= 10 ? "#e0a63f" : "";
        tdP.appendChild(sp);
      });
      const latestCell = g.cells.reduce((x, y) => x.end >= y.end ? x : y);
      const sp = latestCell.annual ? "年度" : latestCell.days <= 100 ? "单季" : "YTD";
      tr.insertCell().textContent = `截至 ${latestCell.end}（${sp}）`;
    }
    return tbl;
  }

  if (primary.length) tableEl.appendChild(buildTable(primary));
  if (secondary.length) {
    const det = document.createElement("details");
    det.className = "fold";
    const sum = document.createElement("summary");
    sum.textContent = `地域与其他分布（${secondary.length} 项）`;
    det.appendChild(sum);
    det.appendChild(buildTable(secondary));
    tableEl.appendChild(det);
  }
}

// ---- 图 3：现金与债务 ----
function renderBalance(d, labels) {
  const b = d.balance;
  const opt = baseOpt();
  opt.xAxis = catAxis(labels);
  opt.yAxis = usdAxis();
  opt.series = [
    bar("现金及等价物", b.cash, C.s1),
    bar("有价证券", b.securities, C.s2),
    bar("总债务", b.total_debt, C.s3),
  ];
  mount("cBalance", opt);
}

// ---- 图 4：现金流·股东回报·股权激励（流出取负画到零轴下方）----
function renderCashflow(d, labels) {
  const cf = d.cashflow;
  const OUT = ["资本开支", "回购", "分红"];
  const opt = baseOpt();
  opt.tooltip.formatter = (ps) => {
    let html = `<b>${ps[0].axisValue}</b>`;
    for (const p of ps) {
      const raw = OUT.includes(p.seriesName) && p.value != null ? -p.value : p.value;
      html += `<br>${p.marker} ${p.seriesName}: ${fmtUSD(raw)}`;
    }
    // 股东回报与 SBC 相对 FCF 的比例——回购是不是 FCF 付得起、激励有多重
    const i = ps[0].dataIndex;
    const fcf = cf.fcf[i], ret = (cf.buyback[i] || 0) + (cf.dividends[i] || 0);
    if (fcf && ret) html += `<br><span style="color:${C.muted}">股东回报(回购+分红) ${fmtUSD(ret)} = FCF 的 ${(ret / fcf * 100).toFixed(0)}%</span>`;
    if (fcf && cf.sbc[i]) html += `<br><span style="color:${C.muted}">SBC = FCF 的 ${(cf.sbc[i] / fcf * 100).toFixed(0)}%</span>`;
    return html;
  };
  opt.xAxis = catAxis(labels);
  opt.yAxis = usdAxis();
  const negBar = (name, arr, color) =>
    bar(name, arr.map(v => v == null ? null : -v), color,
        { itemStyle: { color, borderRadius: [0, 0, 3, 3] } });
  opt.series = [
    bar("经营现金流", cf.ocf, C.s1),
    bar("自由现金流", cf.fcf, C.s2),
    negBar("资本开支", cf.capex, C.s3),
    negBar("回购", cf.buyback, C.s4),
    negBar("分红", cf.dividends, C.s5),
    { name: "SBC(股权激励)", type: "line", data: cf.sbc,
      lineStyle: { width: 2, type: "dashed", color: C.s6 },
      itemStyle: { color: C.s6 }, symbol: "circle", symbolSize: 5,
      connectNulls: false },
  ].filter(s => s.data.some(v => v != null));
  mount("cCashflow", opt);
}

// ---- 加载与渲染 ----
async function load() {
  const ticker = $("ticker").value.trim().toUpperCase();
  if (!ticker) return setStatus("err", "请输入股票代码");
  // 序号防竞态：慢票的旧响应回来时若已有新请求，直接丢弃
  const seq = ++state.seq;
  const freq = state.freq;
  const years = $("years").value;
  $("go").disabled = true;
  setStatus("", "从 SEC EDGAR 取数中…");
  try {
    const cmpT = $("compare").value.trim().toUpperCase();
    const url = `/api/financials/${encodeURIComponent(ticker)}?freq=${freq}&years=${years}`;
    const cmpP = (cmpT && cmpT !== ticker)
      ? fetch(`/api/financials/${encodeURIComponent(cmpT)}?freq=${freq}&years=${years}`)
          .then(r => r.ok ? r.json() : null).catch(() => null)
      : Promise.resolve(null);
    const res = await fetch(url);
    if (seq !== state.seq) return;
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `请求失败（HTTP ${res.status}）`);
    }
    const d = await res.json();
    const cmpD = await cmpP;
    if (seq !== state.seq) return;
    state.data = d;
    state.cmp = cmpD ? { ticker: cmpD.ticker, data: cmpD } : null;
    if (cmpT && !cmpD) setStatus("err", `对比票 ${cmpT} 加载失败，已按单票渲染`);
    history.replaceState(null, "",
      `?ticker=${d.ticker}&freq=${freq}&years=${years}` +
      (state.cmp ? `&compare=${state.cmp.ticker}` : ""));
    document.title = `${d.ticker} 财务图表 · EDGAR 财报下载器`;
    // 公司名来自 EDGAR 第三方数据，必须走 textContent 而不是 innerHTML
    const co = $("coname");
    co.textContent = "";
    const b = document.createElement("b");
    b.textContent = d.name;
    co.appendChild(b);
    co.appendChild(document.createTextNode(`（CIK ${d.cik}）`));
    $("welcome").style.display = "none";
    $("charts").style.display = "grid";

    const labels = d.periods.map(p => p.label);
    renderTtm(d);
    renderIncome(d, labels);
    renderEps(d, labels);
    renderBalance(d, labels);
    renderCashflow(d, labels);
    // 点损益柱直接驱动利润瀑布（下拉保留作回退）
    state.charts.cIncome.off("click");
    state.charts.cIncome.on("click", (p) => {
      if (p.dataIndex == null) return;
      $("wfPeriod").value = p.dataIndex;
      renderWaterfall(state.data, p.dataIndex);
    });
    // 同 labels 的四张卡十字线联动（分部/集中度期间轴不同，不入组）
    for (const id of ["cIncome", "cEps", "cBalance", "cCashflow"])
      if (state.charts[id]) state.charts[id].group = "sync";
    echarts.connect("sync");
    loadSegments(d.ticker, freq, years);  // 独立异步，不阻塞主图状态
    // 瀑布图期数下拉：默认最新一期
    const sel = $("wfPeriod");
    sel.innerHTML = "";
    d.periods.forEach((p, i) => sel.add(new Option(p.label, i)));
    sel.value = d.periods.length - 1;
    sel.onchange = () => renderWaterfall(d, +sel.value);
    renderWaterfall(d, d.periods.length - 1);
    // 卡片刚显示时容器才有宽度，让 ECharts 重算一次
    requestAnimationFrame(() => Object.values(state.charts).forEach(c => c.resize()));
    if (d.warning) setStatus("err", `${d.periods.length} 期 · ⚠ ${d.warning}`);
    else setStatus("ok", `${d.periods.length} 期 · ${freq === "quarterly" ? "季度" : "年度"}`);
  } catch (e) {
    if (seq === state.seq) setStatus("err", e.message);
  } finally {
    if (seq === state.seq) $("go").disabled = false;
  }
}

// ---- CSV 复制：把当前数据拼成 期间×科目 表，直接进剪贴板 ----
function csvIncome(d) {
  const L = [["期间", "营收", "营业成本", "运营费用", "净利润", "毛利率",
              "营业利润率", "净利润率", "稀释EPS"]];
  d.periods.forEach((p, i) => L.push([p.label,
    d.income.revenue[i], d.income.cogs[i], d.income.opex[i],
    d.income.net_income[i], d.income.margins.gross[i],
    d.income.margins.operating[i], d.income.margins.net[i],
    d.income.eps_diluted[i]]));
  return L;
}
function csvCashflow(d) {
  const L = [["期间", "经营现金流", "自由现金流", "资本开支", "回购", "分红", "SBC"]];
  d.periods.forEach((p, i) => L.push([p.label,
    d.cashflow.ocf[i], d.cashflow.fcf[i], d.cashflow.capex[i],
    d.cashflow.buyback[i], d.cashflow.dividends[i], d.cashflow.sbc[i]]));
  return L;
}
function csvConc() {
  const c = state.segData && state.segData.concentration;
  if (!c) return null;
  const L = [["交易对手", "类型", "基准", "占比%", "占比下限%", "合计口径", "期末"]];
  for (const r of c.latest)
    L.push([r.party, r.type, r.benchmark, r.pct, r.pct_lo, r.aggregate, r.end]);
  return L;
}
document.addEventListener("click", async (e) => {
  const kind = e.target.dataset && e.target.dataset.csv;
  if (!kind) return;
  const d = state.data;
  const table = kind === "income" ? (d && csvIncome(d))
    : kind === "cashflow" ? (d && csvCashflow(d)) : csvConc();
  if (!table) return;
  const text = table.map(row => row.map(v =>
    v == null ? "" : String(v).includes(",") ? `"${v}"` : v).join(",")).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    const old = e.target.textContent;
    e.target.textContent = "已复制 ✓";
    setTimeout(() => { e.target.textContent = old; }, 1500);
  } catch { e.target.textContent = "复制失败"; }
});

if ($("ticker").value) load();
