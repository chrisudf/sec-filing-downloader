// Watchlist PE 分位页：读 /api/pe_rank（valuation/pe_rank.py 的最近一份 JSON），
// 刷新走 /api/pe_rank/refresh + /api/pe_rank/status 轮询。口径说明见 pe_rank.py 文件头。
const $ = (id) => document.getElementById(id);
const STALE_DAYS = 8;          // 每周定时 + 1 天余量；超过说明定时任务没跑
const POLL_MS = 3000;
let data = null;

// ---- 格式 ----
const fmtPE = (v) => v == null ? "—" : v >= 1000 ? ">999x" : `${v.toFixed(1)}x`;
const fmtNum = (v, d = 1) => v == null ? "—" : v >= 1000 ? ">999" : v.toFixed(d);
const pctPct = (v) => `${v >= 0 ? "+" : ""}${Math.round(v * 100)}%`;
// 分位只用明度表达，不用红绿——红绿会被读成买卖信号
const shade = (p) => `rgba(232, 232, 238, ${(0.03 + 0.25 * p / 100).toFixed(3)})`;

function usMarketOpen(d = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "2-digit",
    minute: "2-digit", hourCycle: "h23",
  }).formatToParts(d).map((x) => [x.type, x.value]));
  if (parts.weekday === "Sat" || parts.weekday === "Sun") return false;
  const m = Number(parts.hour) * 60 + Number(parts.minute);
  return m >= 9 * 60 + 30 && m < 16 * 60;
}

// ---- DOM 小工具（服务端文本一律走 textContent）----
function el(tag, { text, cls, title, attrs } = {}, ...kids) {
  const e = document.createElement(tag);
  if (text != null) e.textContent = text;
  if (cls) e.className = cls;
  if (title) e.title = title;
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  for (const k of kids) if (k) e.append(k);
  return e;
}
const mark = (sym, title) => el("span", { text: sym, cls: "mark", title });

function setStatus(msg, kind = "") {
  $("status").textContent = msg;
  $("status").className = "status" + (kind ? " " + kind : "");
}

// ---- 表格 ----
const GROUPS = [
  ["TTM（GAAP）", 5], ["营业利润口径", 5], ["前瞻（一致预期）", 4], ["Yahoo", 2],
];
const SUBS = ["PE", "10y", "5y", "3y", "近3年 P10/50/90",
              "PE", "10y", "5y", "3y", "近3年 P10/50/90",
              "本财年", "下财年", "下财年区间", "下财年止", "tPE", "PEG"];
const GSTART = new Set([0, 5, 10, 14]);     // 每组第一列画左边线
const NCOLS = 2 + SUBS.length;

function head() {
  const r1 = el("tr", {}, el("th", { text: "票", cls: "tk", attrs: { rowspan: 2 } }),
                el("th", { text: "收盘", attrs: { rowspan: 2 } }));
  for (const [name, n] of GROUPS)
    r1.append(el("th", { text: name, cls: "grp gstart", attrs: { colspan: n } }));
  const r2 = el("tr");
  SUBS.forEach((s, i) => r2.append(el("th", { text: s, cls: GSTART.has(i) ? "gstart" : "" })));
  return el("thead", {}, r1, r2);
}

function bandCells(m) {
  if (m.err) {
    return [el("td", { text: "n/a", cls: "na", title: m.err }),
            ...[0, 1, 2, 3].map(() => el("td", { text: "—", cls: "na" }))];
  }
  const pe = el("td", { text: fmtPE(m.pe) });
  if (!m.fresh) {
    pe.append(mark("†", `当前 TTM 窗口被剔（一次性畸变/亏损/近零），显示的是 ${m.date} 的末个有效点`));
  }
  const why = m.thin ? `有效样本仅 ${m.days10} 天，不给分位` : "该窗口有效样本不足 250 天";
  const pct = (v) => v == null
    ? el("td", { text: "—", cls: "na", title: why })
    : el("td", { text: `P${Math.round(v)}`, cls: "pct",
                 attrs: { style: `background:${shade(v)}` } });
  const p3 = m.p3;
  const band = el("td", p3
    ? { text: `${Math.round(p3["10"])} / ${Math.round(p3["50"])} / ${Math.round(p3["90"])}` }
    : { text: "—", cls: "na" });
  return [pe, pct(m.r10), pct(m.r5), pct(m.r3), band];
}

function fwdCells(fw, labels) {
  fw = fw || {};
  const eps = (k) => fw[`${k}_eps`] != null
    ? `${fw[`${k}_n`]} 位分析师，一致预期 EPS ${fw[`${k}_eps`].toFixed(2)}` : null;
  const fy1 = el("td", { text: fmtPE(fw.fy1_pe), title: eps("fy1") });
  if (fw.fy1_suspect) {
    const f = fw.fy1_suspect;
    fy1.append(mark("⚠", `本财年一致预期 90 天内 ${pctPct(f.j0)}、下财年仅 ${pctPct(f.j1)}——`
                          + "疑似一次性收益进了预期，本财年 PE 偏低不可信，看下财年"));
  }
  let rng = "—";
  if (fw.fy2_pe_lo !== undefined) {
    rng = `${fmtNum(fw.fy2_pe_lo)}–${fw.fy2_low_nonpos ? "含亏损" : fmtNum(fw.fy2_pe_hi)}`;
  }
  return [fy1, el("td", { text: fmtPE(fw.fy2_pe), title: eps("fy2") }),
          el("td", { text: rng, title: "收盘 ÷ 最高预期 … 收盘 ÷ 最低预期" }),
          el("td", { text: (labels && labels[1]) || "—" })];
}

function bodyRow(r) {
  if (r.skip) {
    return el("tr", { cls: "skip" }, el("td", { text: r.ticker, cls: "tk" }),
              el("td", { text: r.skip, cls: "skip", attrs: { colspan: NCOLS - 1 } }));
  }
  const tk = el("td", { cls: "tk" }, el("a", {
    text: r.ticker + " ", title: "在新标签页打开财务图表",
    attrs: { href: `/dashboard.html?ticker=${encodeURIComponent(r.ticker)}`,
             target: "_blank", rel: "noopener" } }, el("span", { text: "↗" })));
  const yh = r.yh || {};
  const cells = [...bandCells(r.gaap), ...bandCells(r.op), ...fwdCells(r.fwd, r.fy_labels),
                 el("td", { text: fmtNum(yh.tpe) }), el("td", { text: fmtNum(yh.peg, 2) })];
  cells.forEach((c, i) => GSTART.has(i) && c.classList.add("gstart"));
  return el("tr", {}, tk, el("td", { text: r.close.toFixed(2) }), ...cells);
}

// 排序档位：有值(0) < 缺值(1) < 跳过行(2)，同档再按数值——不能用 Infinity 当哨兵，
// Infinity − Infinity = NaN 会让 sort 的比较结果不自洽
function sortKey(r, path) {
  if (r.skip) return [2, 0];
  const [grp, k] = path.split(".");
  const v = (r[grp] || {})[k];
  return v == null ? [1, 0] : [0, v];
}
const byKey = (path) => (a, b) => {
  const [ta, va] = sortKey(a, path), [tb, vb] = sortKey(b, path);
  return ta - tb || va - vb;
};

function render() {
  const path = $("sort").value;
  const rows = [...data.rows];
  if (path) rows.sort(byKey(path));
  $("tbl").replaceChildren(head(), el("tbody", {}, ...rows.map(bodyRow)));
  $("notes").replaceChildren(...data.notes.map((n) => el("li", { text: n })));

  const gen = new Date(data.generated_at);
  const ageDays = (Date.now() - gen) / 864e5;
  $("meta").replaceChildren(
    "数据截至 ", el("b", { text: data.px_date || "—" }),
    data.intraday ? " 盘中价" : " 收盘",
    ` · 生成于 ${gen.toLocaleString("zh-CN", { hour12: false })}`);
  const warns = [];
  if (data.intraday) warns.push("这份结果是美股盘中跑的：「收盘」列是盘中价，分位与前瞻 PE 会随之浮动。");
  if (ageDays > STALE_DAYS) {
    warns.push(`这份结果已是 ${Math.floor(ageDays)} 天前的——每周六早上的定时任务可能没跑（电脑没开？），`
               + "点「刷新」手动更新。");
  }
  $("banner").textContent = warns.join(" ");
  $("banner").style.display = warns.length ? "" : "none";
}

// ---- 数据 ----
async function load() {
  const r = await fetch("/api/pe_rank");
  if (r.status === 404) {
    $("main").style.display = "none";
    $("empty").style.display = "";
    $("meta").textContent = "";
    return;
  }
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d.detail || `HTTP ${r.status}`);
  }
  data = await r.json();
  $("empty").style.display = "none";
  $("main").style.display = "";
  render();
}

let timer = null;
async function poll() {
  clearTimeout(timer);
  $("refresh").disabled = true;
  let st;
  try {
    st = await (await fetch("/api/pe_rank/status")).json();
  } catch {
    setStatus("连不上服务", "err");
    $("refresh").disabled = false;
    return;
  }
  if (st.status === "running") {
    setStatus(st.total ? `刷新中 ${st.done + 1}/${st.total} · ${st.current}`
                       : "刷新中…（全表约 5 分钟）");
    timer = setTimeout(poll, POLL_MS);
    return;
  }
  $("refresh").disabled = false;
  if (st.status === "done") {
    setStatus("已刷新", "ok");
    await load().catch((e) => setStatus(e.message, "err"));
  } else if (st.status === "failed") {
    setStatus("刷新失败：" + (st.error || "未知错误"), "err");
  }
}

async function refresh() {
  if (usMarketOpen() && !confirm(
      "美股正在交易：现在刷新拿到的是盘中价，分位和前瞻 PE 会随盘中价浮动。\n仍然刷新？")) return;
  $("refresh").disabled = true;
  setStatus("启动中…");
  try {
    const r = await fetch("/api/pe_rank/refresh", { method: "POST" });
    if (!r.ok && r.status !== 409) {           // 409 = 已经在跑，直接接上轮询
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${r.status}`);
    }
  } catch (e) {
    setStatus(e.message, "err");
    $("refresh").disabled = false;
    return;
  }
  poll();
}

// ---- 启动 ----
for (const p of [0, 25, 50, 75, 100]) {
  $("legend").append(el("i", { title: `P${p}`, attrs: { style: `background:${shade(p)}` } }));
}
try { $("sort").value = localStorage.getItem("wl.sort") || ""; } catch { /* 隐私模式 */ }
$("sort").addEventListener("change", () => {
  try { localStorage.setItem("wl.sort", $("sort").value); } catch { /* 忽略 */ }
  if (data) render();
});
$("refresh").addEventListener("click", refresh);
load().catch((e) => setStatus(e.message, "err"));
fetch("/api/pe_rank/status").then((r) => r.json())
  .then((st) => { if (st.status === "running") poll(); }).catch(() => {});
