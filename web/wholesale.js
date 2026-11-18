/* =====================================================================
 * wholesale.js — 批发经营分析（阶段三）
 * 数据来自 /api/wholesale*，全部由后端按 P07 口径聚合；前端不重算指标。
 * 三类金额（批发采购 / 批发出库 / POS）永不在此相加。
 * ===================================================================== */
import { hideTooltip, fmtMoney, fmtCompact } from './charts.js';
import {
  donutCount, trendTwo, rankBar, fmtInt, fmtNum, emptyBox, loadingBox,
} from './boardcharts.js';

const $ = (id) => document.getElementById(id);

const state = {
  months: '', regions: '', customer: '', stall: '', sku: '',
  page: 1, limit: 10, sort: '', order: 'asc', q: '', kind: 'purchase',
  trendMetric: 'amount', custMode: 'purchase', stallMode: 'purchase', skuMode: 'sales',
  data: null, options: null,
};

const pct = (v, d = 1) => (v === null || v === undefined ? '—' : `${fmtNum(v, d)}%`);
const boxes = (v) => (v === null || v === undefined ? '—' : `${fmtInt(v)} 盒`);

function readUrl() {
  const u = new URLSearchParams(location.search);
  state.months = u.get('months') || '';
  state.regions = u.get('regions') || '';
  state.customer = u.get('customer') || '';
  state.stall = u.get('stall') || '';
  state.sku = u.get('sku') || '';
  state.kind = ['purchase', 'sales', 'inventory'].includes(u.get('kind')) ? u.get('kind') : 'purchase';
  state.page = Math.max(1, Number(u.get('page') || 1));
  state.sort = u.get('sort') || '';
  state.order = u.get('order') === 'desc' ? 'desc' : 'asc';
  state.q = u.get('q') || '';
}

function writeUrl() {
  const u = new URLSearchParams();
  [['months', state.months], ['regions', state.regions], ['customer', state.customer],
   ['stall', state.stall], ['sku', state.sku], ['q', state.q]].forEach(([k, v]) => {
    if (v) u.set(k, v);
  });
  if (state.kind !== 'purchase') u.set('kind', state.kind);
  if (state.page > 1) u.set('page', String(state.page));
  if (state.sort) { u.set('sort', state.sort); u.set('order', state.order); }
  const qs = u.toString();
  history.replaceState(null, '', qs ? `${location.pathname}?${qs}` : location.pathname);
}

function params(extra = {}) {
  const p = { ...extra };
  if (state.months) p.months = state.months;
  if (state.regions) p.regions = state.regions;
  if (state.customer) p.customer = state.customer;
  if (state.stall) p.stall = state.stall;
  if (state.sku) p.sku = state.sku;
  return p;
}

async function getJSON(path, query = {}) {
  const url = new URL(path, location.origin);
  Object.entries(query).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') url.searchParams.set(k, String(v));
  });
  const res = await fetch(url.toString(), { cache: 'no-store' });
  let body;
  try { body = await res.json(); } catch { throw new Error(`接口返回非 JSON（HTTP ${res.status}）`); }
  if (!res.ok || body.ok === false) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

let toastTimer = 0;
function toast(text) {
  const el = $('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

function setStatus(text, bad = false) {
  const el = $('status-badge');
  el.textContent = text;
  el.classList.toggle('off', bad);
}

/* ------------------------------------------------------------ 筛选器 */
function fillSelect(el, options, current, allLabel) {
  el.innerHTML = '';
  const first = document.createElement('option');
  first.value = '';
  first.textContent = allLabel;
  el.appendChild(first);
  options.forEach((opt) => {
    const o = document.createElement('option');
    o.value = opt.value;
    o.textContent = opt.label;
    el.appendChild(o);
  });
  el.value = options.some(o => o.value === current) ? current : '';
}

function renderFilterOptions() {
  const opt = state.options || {};
  fillSelect($('f-month'), (opt.months || []).map(m => ({ value: String(m), label: `${m} 月` })),
    state.months, '全部月份');
  fillSelect($('f-region'), (opt.regions || []).map(r => ({ value: r, label: r })),
    state.regions, '五大战区');
  fillSelect($('f-customer'), (opt.customers || []).map(c => ({
    value: c.customer_id, label: `${c.customer_name}（${c.customer_id}）`,
  })), state.customer, `全部批发客户（${(opt.customers || []).length}）`);
  const stalls = (opt.stalls || []).filter(s => !state.customer || s.customer_id === state.customer);
  fillSelect($('f-stall'), stalls.map(s => ({ value: s.stall_id, label: s.stall_name })),
    state.stall, `全部档口（${stalls.length}）`);
  fillSelect($('f-sku'), (opt.skus || []).map(s => ({
    value: s.sku_code, label: `${s.sku_name}`,
  })), state.sku, `全部 SKU（${(opt.skus || []).length}）`);
}

/* ------------------------------------------------------------ KPI */
function kpiCard({ label, value, sub, unit, accent, muted, text }) {
  return `<div class="kpi-card${accent ? ` accent-${accent}` : ''}${muted ? ' is-muted' : ''}">
    <div class="kpi-top"><span class="kpi-label">${label}</span>
      ${unit ? `<span class="kpi-unit">${unit}</span>` : ''}</div>
    <div class="kpi-main"><span class="kpi-value${text ? ' is-text' : ''}">${value}</span></div>
    ${sub ? `<div class="kpi-sub">${sub}</div>` : ''}
  </div>`;
}

function renderKpi(k) {
  $('kpi-row').innerHTML = [
    kpiCard({ label: '批发采购金额', value: fmtMoney(k.purchase_amount), unit: 'ERP 出货',
      sub: `${fmtInt(k.purchase_docs)} 张单据` }),
    kpiCard({ label: '批发出库金额', value: fmtMoney(k.sales_amount), unit: '对下游',
      accent: 'teal', sub: `${fmtInt(k.sales_docs)} 张单据 · 毛利 ${pct(k.gross_margin)}` }),
    kpiCard({ label: '采购数量', value: fmtInt(k.purchase_qty), unit: '盒',
      sub: `出库 ${fmtInt(k.sales_qty)} 盒 · 净流入 ${fmtInt(k.purchase_minus_sales_qty)} 盒` }),
    kpiCard({ label: '出库数量', value: fmtInt(k.sales_qty), unit: '盒', accent: 'purple',
      sub: `毛利额 ${fmtMoney(k.gross_profit)}` }),
    kpiCard({ label: '期末库存数量', value: fmtInt(k.ending_qty), unit: '盒',
      sub: `时点 ${k.anchor_date || '—'} · ${fmtInt(k.combinations)} 个组合` }),
    kpiCard({ label: '期末库存金额', value: fmtMoney(k.inventory_amount), unit: '结存×成本',
      accent: 'amber', sub: '估值口径已核对（结存数量×单位成本价）' }),
  ].join('');
}

/* ------------------------------------------------------------ 趋势 */
function renderTrend(rows) {
  const metric = state.trendMetric;
  const isAmount = metric === 'amount';
  const labels = rows.map(r => r.label);
  const series = [
    { name: '采购', color: '#2f6fe4',
      values: rows.map(r => (isAmount ? r.purchase_amount : r.purchase_qty)) },
    { name: '出库', color: '#0e9f8a',
      values: rows.map(r => (isAmount ? r.sales_amount : r.sales_qty)) },
  ];
  trendTwo($('chart-trend'), { labels, series, height: 210, padLeft: 78,
    axisFormat: (v) => (isAmount ? fmtCompact(v, '¥') : fmtInt(v)),
    valueFormat: (v) => (isAmount ? fmtMoney(v) : `${fmtInt(v)} 盒`),
    emptyText: '当前筛选条件下无月度数据' });
  $('legend-trend').innerHTML = series.map(s =>
    `<span><i style="background:${s.color}"></i>${s.name}${isAmount ? '金额' : '数量'}</span>`).join('');

  const p = rows.reduce((a, r) => a + (r.purchase_amount || 0), 0);
  const s = rows.reduce((a, r) => a + (r.sales_amount || 0), 0);
  $('foot-trend').textContent =
    `合计采购 ${fmtMoney(p)} · 出库 ${fmtMoney(s)} · 出库/采购 ${pct(s / (p || 1) * 100)}`
    + '；两类金额独立核算，不与 ERP 全渠道、POS 金额相加';
}

/* ------------------------------------------------------------ 客户 */
function renderCustomer(rows) {
  const host = $('chart-customer');
  const mode = state.custMode;
  if (!rows.length) return emptyBox(host, '当前筛选条件下无客户数据');
  const key = mode === 'purchase' ? 'purchase_amount' : 'sales_amount';
  const shareKey = mode === 'purchase' ? 'purchase_share' : 'sales_share';
  const sorted = [...rows].sort((a, b) => (b[key] || 0) - (a[key] || 0));
  const items = sorted.map((c, i) => ({
    label: c.customer_name,
    value: c[key] || 0,
    rate: null,
    rateText: pct(c[shareKey]),
    color: i === 0 ? '#2f6fe4' : '#5b9bf8',
    tip: `<div class="tt-row"><span>采购金额</span><b>${fmtMoney(c.purchase_amount)}</b></div>
          <div class="tt-row"><span>出库金额</span><b>${fmtMoney(c.sales_amount)}</b></div>
          <div class="tt-row"><span>采购−出库</span><b>${fmtMoney(c.gap_amount)}</b></div>
          <div class="tt-row"><span>毛利额</span><b>${fmtMoney(c.gross_profit)}</b></div>
          <div class="tt-row"><span>毛利率</span><b>${pct(c.gross_margin)}</b></div>`,
  }));
  rankBar(host, { items, valueFormat: (v) => fmtMoney(v), labelW: 82, valueW: 66, rateW: 46, rowH: 26 });
  const top = sorted[0];
  $('foot-customer').textContent = top
    ? `${mode === 'purchase' ? '采购' : '出库'}第一：${top.customer_name} ${fmtMoney(top[key])}`
      + `（${pct(top[shareKey])}）· 角标为贡献度 · 采购与出库独立核算`
    : '';
}

/* ------------------------------------------------------------ 档口 */
function renderStall(rows) {
  const host = $('chart-stall');
  const mode = state.stallMode;
  if (!rows.length) return emptyBox(host, '当前筛选条件下无档口数据');
  const key = mode === 'purchase' ? 'purchase_amount'
    : (mode === 'sales' ? 'sales_amount' : 'inventory_amount');
  const sorted = [...rows].sort((a, b) => (b[key] || 0) - (a[key] || 0)).slice(0, 12);
  const items = sorted.map(c => ({
    label: c.stall_name || c.stall_id,
    value: c[key] || 0,
    rateText: mode === 'stock' ? boxes(c.ending_qty) : null,
    color: mode === 'stock' ? '#7c63d6' : '#2f6fe4',
    tip: `<div class="tt-row"><span>采购金额</span><b>${fmtMoney(c.purchase_amount)}</b></div>
          <div class="tt-row"><span>出库金额</span><b>${fmtMoney(c.sales_amount)}</b></div>
          <div class="tt-row"><span>期末库存</span><b>${boxes(c.ending_qty)}</b></div>
          <div class="tt-row"><span>库存金额</span><b>${fmtMoney(c.inventory_amount)}</b></div>`,
  }));
  rankBar(host, {
    items, valueFormat: (v) => fmtMoney(v),
    labelW: 108, valueW: 64, rateW: 62, rowH: 24, maxLabel: 12,
  });
  $('foot-stall').textContent = mode === 'stock'
    ? '库存为时点值（所选期间最后一天），角标为结存盒数；不跨日期累加'
    : `按${mode === 'purchase' ? '采购' : '出库'}金额降序展示 TOP ${sorted.length} 个档口（共 ${rows.length} 个）`;
}

/* ------------------------------------------------------------ SKU */
function renderSku(rows) {
  const host = $('chart-sku');
  const mode = state.skuMode;
  if (!rows.length) return emptyBox(host, '当前筛选条件下无 SKU 数据');
  const key = mode === 'purchase' ? 'purchase_amount'
    : (mode === 'sales' ? 'sales_amount' : 'inventory_amount');
  const sorted = [...rows].sort((a, b) => (b[key] || 0) - (a[key] || 0)).slice(0, 10);
  const items = sorted.map((s, i) => ({
    label: s.sku_name,
    value: s[key] || 0,
    rateText: mode === 'stock' ? boxes(s.ending_qty) : null,
    color: i < 3 ? '#2f6fe4' : '#5b9bf8',
    tip: `<div class="tt-row"><span>采购</span><b>${fmtMoney(s.purchase_amount)}</b></div>
          <div class="tt-row"><span>出库</span><b>${fmtMoney(s.sales_amount)}</b></div>
          <div class="tt-row"><span>毛利率</span><b>${pct(s.gross_margin)}</b></div>
          <div class="tt-row"><span>采购−出库（盒）</span><b>${fmtInt(s.gap_qty)}</b></div>
          <div class="tt-row"><span>期末库存</span><b>${boxes(s.ending_qty)}</b></div>`,
  }));
  rankBar(host, {
    items, valueFormat: (v) => fmtMoney(v),
    labelW: 112, valueW: 64, rateW: 62, rowH: 24, maxLabel: 12,
  });
  $('foot-sku').textContent = mode === 'stock'
    ? '库存为时点值，角标为结存盒数；采购与出库差额反映期间净流入'
    : `按${mode === 'purchase' ? '采购' : '出库'}金额降序 TOP ${sorted.length}（共 ${rows.length} 个 SKU）`;
}

/* ------------------------------------------------------------ 库存 */
function renderInventory(inv) {
  const series = inv.series || [];
  const labels = series.map(r => r.date.slice(5));
  trendTwo($('chart-inventory'), {
    height: 200, padLeft: 78,
    labels,
    series: [{ name: '库存盒数', color: '#7c63d6',
               values: series.map(r => r.ending_qty) }],
    // 库存是时点水位，波动区间窄：从 0 起画会把曲线压平，改用数据区间作基线
    zeroBase: false,
    axisFormat: (v) => `${fmtNum(v / 10000, 1)}万`,
    valueFormat: (v) => `${fmtInt(v)} 盒`,
    emptyText: '当前筛选条件下无库存快照',
  });
  $('foot-inventory').textContent = series.length
    ? `${series[0].date} ~ ${series[series.length - 1].date} 共 ${series.length} 个自然日；`
      + '每天为跨组合合计的时点库存，绝不对时间维度求和'
    : '无库存记录';
}

function renderMonthEnd(inv) {
  const months = inv.months || [];
  const body = months.map(m => `<tr>
    <td>${m.label}</td><td>${m.date}</td>
    <td class="num">${fmtInt(m.ending_qty)}</td>
    <td class="num">${fmtMoney(m.inventory_amount)}</td>
    <td class="num">${fmtInt(m.real_zero)}</td>
    <td class="num">${fmtInt(m.not_yet_active)}</td>
  </tr>`).join('');
  $('month-end-table').innerHTML = `<table class="data"><thead><tr>
    <th>月份</th><th>取数日</th><th>月末库存（盒）</th><th>库存金额</th>
    <th>真实归零组合</th><th>启用前零组合</th></tr></thead>
    <tbody>${body || `<tr><td colspan="6" style="text-align:center;padding:14px">无月末快照</td></tr>`}</tbody></table>`;
  $('foot-monthend').textContent =
    '月末库存取该月最后一个自然日的真实记录；不补零、不前向填充、不做插值。';
}

function renderZero(inv) {
  const z = inv.zero_breakdown || {};
  $('fact-zero').innerHTML = [
    ['有效组合（档口×SKU）', fmtInt(z.combinations), ''],
    ['期间曾出现「经营后真实归零」', fmtInt(z.real_zero_combos), z.real_zero_combos ? 'warn' : ''],
    ['期间曾出现「启用前零库存」', fmtInt(z.not_active_combos), ''],
    ['锚定日仍有库存的组合', fmtInt(z.anchor_positive || z.positive_combos), ''],
    ['排除的不适用组合（N/A）', fmtInt(z.excluded_combinations), ''],
  ].map(([k, v, cls]) =>
    `<div class="fact-row"><span class="k">${k}</span><span class="v ${cls}">${v}</span></div>`).join('');

  const rows = (inv.by_stall || []).slice(0, 6);
  const body = rows.map(r => `<tr>
    <td>${r.label}</td><td class="num">${fmtInt(r.ending_qty)}</td>
    <td class="num">${fmtMoney(r.inventory_amount)}</td></tr>`).join('');
  $('stock-table').innerHTML = `<table class="data"><thead><tr>
    <th>档口（库存 TOP）</th><th>结存盒数</th><th>库存金额</th></tr></thead>
    <tbody>${body || '<tr><td colspan="3">无库存分布</td></tr>'}</tbody></table>`;
  $('foot-zero').textContent =
    'REAL_ZERO = 经营后真实卖到 0；NOT_YET_ACTIVE = 尚未启用，两者严格分开，不合并、不填零。';
}

/* ------------------------------------------------------------ 说明 */
function renderNotices(payload) {
  const k = payload.data.kpi;
  $('notice-main-text').innerHTML =
    `<b>金额边界</b>：批发采购 = 星禾 ERP 向批发客户出货（<b>${fmtMoney(k.purchase_amount)}</b>，`
    + `与全渠道 ERP 出货口径不同）；批发出库 = 批发客户对下游销售（<b>${fmtMoney(k.sales_amount)}</b>，`
    + `独立于 POS）。三类金额各自核算，<b>任何情况下不相加</b>。`
    + `库存为 <b>${k.anchor_date || '—'}</b> 的时点快照，共 ${fmtInt(k.combinations)} 个有效组合。`;
  $('chip-period').textContent = payload.meta.period || 'Y26';
  $('chip-anchor').textContent = `库存时点 ${payload.meta.inventory_anchor || '—'}`;
  const chips = [];
  if (state.regions) chips.push(`战区：${state.regions}`);
  if (state.customer) chips.push(`客户：${state.customer}`);
  if (state.stall) chips.push(`档口：${state.stall}`);
  if (state.sku) chips.push(`SKU：${state.sku}`);
  $('active-chips').innerHTML = chips.map(c => `<span class="chip">${c}</span>`).join('');
}

function renderCapability() {
  const items = [
    {
      title: 'P07：ending_qty 是时点指标',
      lines: [
        '日趋势里每个点是「同一天跨组合求和」，跨日期一律不累加。',
        '月末库存取该月最后一个自然日的真实记录；不补零、不插值、不前向填充。',
      ],
    },
    {
      title: '2,946 个有效组合 vs 1,014 个不适用组合',
      lines: [
        '55 个档口 × 72 个 SKU = 3,960 个理论组合，其中 2,946 个有业务发生。',
        '另外 1,014 个标记 N/A，在 ETL 层已排除，本页不补 0 也不展示为「零库存」。',
      ],
    },
    {
      title: 'NOT_YET_ACTIVE 与 REAL_ZERO 必须分开',
      lines: [
        'NOT_YET_ACTIVE = 该组合尚未开始经营，库存为 0 是「还没开始」。',
        'REAL_ZERO = 已经开始经营后卖到 0，是真实缺货信号。两者合并会误导补货决策。',
      ],
    },
    {
      title: '三类金额互不相加',
      lines: [
        '批发采购金额与 ERP 全渠道出货同源但口径不同（只含批发客户）。',
        '批发出库金额属于批发客户自己的销售，与 POS 终端销售无关。',
      ],
    },
    {
      title: '单据数按单据号去重',
      lines: [
        '明细表同时给出「单据数（COUNT DISTINCT 单据号）」与「SKU 行数」。',
        '本数据集每张单据只有 1 行，两个数字相同；口径仍按去重统计，换数据源也不会错。',
      ],
    },
  ];
  $('capability-count').textContent = `${items.length} 条`;
  $('capability-list').innerHTML = items.map(it => `
    <div class="cap-item"><h4>${it.title}</h4>
      ${it.lines.map(l => `<p>${l}</p>`).join('')}</div>`).join('');
}

/* ------------------------------------------------------------ 明细 */
const COLS = {
  purchase: [
    { key: 'full_date', label: '日期', sort: 'full_date', width: 84 },
    { key: 'purchase_id', label: '采购单号', sort: 'doc_id', width: 118 },
    { key: 'customer_id', label: '客户', sort: 'customer_name', width: 88 },
    { key: 'stall_id', label: '档口', sort: 'stall_name', width: 120 },
    { key: 'sku_name', label: 'SKU', sort: 'sku_name', width: 118 },
    { key: 'sku_code', label: 'SKU 编码', sort: 'sku_code', width: 74 },
    { key: 'category', label: '品类', sort: 'category', width: 50 },
    { key: 'purchase_qty', label: '数量(盒)', sort: 'purchase_qty', num: true, width: 72 },
    { key: 'purchase_unit_price', label: '单价', sort: null, num: true, width: 66 },
    { key: 'purchase_amount', label: '金额', sort: 'purchase_amount', num: true, width: 86 },
    { key: 'purchase_te', label: 'TE', sort: null, num: true, width: 60 },
    { key: 'region_name_std', label: '战区', sort: null, width: 50 },
  ],
  sales: [
    { key: 'full_date', label: '日期', sort: 'full_date', width: 84 },
    { key: 'outbound_id', label: '出库单号', sort: 'doc_id', width: 118 },
    { key: 'customer_id', label: '客户', sort: 'customer_name', width: 88 },
    { key: 'stall_id', label: '档口', sort: 'stall_name', width: 120 },
    { key: 'downstream_customer_name', label: '下游客户', sort: null, width: 110 },
    { key: 'sku_name', label: 'SKU', sort: 'sku_name', width: 118 },
    { key: 'sales_qty', label: '数量(盒)', sort: 'sales_qty', num: true, width: 72 },
    { key: 'sales_unit_price', label: '单价', sort: null, num: true, width: 66 },
    { key: 'sales_amount', label: '金额', sort: 'sales_amount', num: true, width: 86 },
    { key: 'cost_amount', label: '成本', sort: 'cost_amount', num: true, width: 80 },
    { key: 'gross_profit', label: '毛利', sort: 'gross_profit', num: true, width: 80 },
    { key: 'region_name_std', label: '战区', sort: null, width: 50 },
  ],
  inventory: [
    { key: 'snapshot_date', label: '快照日期', sort: 'full_date', width: 90 },
    { key: 'stall_key', label: '档口', sort: 'stall_name', width: 130 },
    { key: 'sku_key', label: 'SKU', sort: 'sku_name', width: 120 },
    { key: 'opening_qty', label: '期初', sort: null, num: true, width: 68 },
    { key: 'inbound_qty', label: '入库', sort: null, num: true, width: 68 },
    { key: 'outbound_qty', label: '出库', sort: null, num: true, width: 68 },
    { key: 'adjustment_qty', label: '调整', sort: null, num: true, width: 60 },
    { key: 'ending_qty', label: '结存', sort: 'ending_qty', num: true, width: 72 },
    { key: 'inventory_amount', label: '库存金额', sort: 'inventory_amount', num: true, width: 90 },
    { key: 'inventory_zero_type', label: '零库存类型', sort: null, width: 100 },
  ],
};

const MONEY_KEYS = new Set(['purchase_unit_price', 'purchase_amount', 'sales_unit_price',
  'sales_amount', 'cost_amount', 'gross_profit', 'inventory_amount']);

function renderTable(host, rows) {
  const cols = COLS[state.kind];
  const head = cols.map((c) => {
    const sorted = state.sort === c.sort && c.sort;
    const mark = c.sort ? `<em class="sort-mark">${sorted
      ? (state.order === 'desc' ? '▼' : '▲') : '⇅'}</em>` : '';
    return `<th class="${c.sort ? 'sortable' : ''}${sorted ? ' sorted' : ''}"
      data-sort="${c.sort || ''}" style="min-width:${c.width}px">${c.label}${mark}</th>`;
  }).join('');

  const body = rows.map((r) => cols.map((c) => {
    const v = r[c.key];
    let text;
    if (v === null || v === undefined || v === '') text = '—';
    else if (MONEY_KEYS.has(c.key)) text = `¥${fmtNum(v, 2)}`;
    else if (c.key === 'inventory_zero_type') {
      text = v === 'REAL_ZERO' ? '<span class="badge-cell badge-miss">真实归零</span>'
        : (v === 'NOT_YET_ACTIVE' ? '<span class="badge-cell badge-none">启用前零</span>' : '—');
    } else text = c.num ? fmtInt(v) : String(v);
    return `<td class="${c.num ? 'num' : ''}">${text}</td>`;
  }).join('')).map(tr => `<tr>${tr}</tr>`).join('');

  host.innerHTML = `<table class="data"><thead><tr>${head}</tr></thead>
    <tbody>${body || `<tr><td colspan="${cols.length}" style="text-align:center;padding:18px">
      当前筛选条件下没有明细</td></tr>`}</tbody></table>`;

  host.querySelectorAll('th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (!key) return;
      if (state.sort === key) state.order = state.order === 'asc' ? 'desc' : 'asc';
      else { state.sort = key; state.order = 'desc'; }
      state.page = 1;
      writeUrl();
      loadDetail();
    });
  });
}

/* ------------------------------------------------------------ 加载 */
let detailSeq = 0;

async function loadDetail() {
  const host = $('detail-table');
  const seq = ++detailSeq;          // 丢弃过期响应，避免快速切换时表格串数据
  if (!host.querySelector('table')) loadingBox(host, '正在查询明细…');
  try {
    const payload = await getJSON('/api/wholesale/detail', {
      ...params(), kind: state.kind, page: state.page, limit: state.limit,
      sort: state.sort, order: state.order, q: state.q,
    });
    if (seq !== detailSeq) return;
    renderTable(host, payload.data.rows);
    const d = payload.data;
    $('detail-count').textContent =
      `共 ${fmtInt(d.total)} 行 · 第 ${d.page} / ${d.pages} 页 · 每页 ${d.limit} 行`;
    $('detail-page').textContent = `${d.page} / ${d.pages}`;
    $('detail-prev').disabled = d.page <= 1;
    $('detail-next').disabled = d.page >= d.pages;
    $('detail-doc').innerHTML = d.doc_count === null
      ? `库存行 <b>${fmtInt(d.row_count)}</b> · 时点快照`
      : `单据 <b>${fmtInt(d.doc_count)}</b> 张 · SKU 行 ${fmtInt(d.row_count)} 行（按单据号去重）`;
  } catch (err) {
    if (seq !== detailSeq) return;
    host.innerHTML = `<div class="inline-empty"><b>明细加载失败</b><span>${err.message}</span></div>`;
  }
}

function markLoading() {
  hideTooltip();
  setStatus('查询中…');
  ['chart-trend', 'chart-customer', 'chart-stall', 'chart-sku', 'chart-inventory']
    .forEach((id) => loadingBox($(id), '正在查询数据…'));
}

let mainSeq = 0;

async function loadAll() {
  const seq = ++mainSeq;
  markLoading();
  $('alert').classList.add('hidden');
  try {
    state.options = (await getJSON('/api/wholesale/options', params())).data;
    if (seq !== mainSeq) return;
    renderFilterOptions();
    const payload = await getJSON('/api/wholesale', params());
    if (seq !== mainSeq) return;
    state.data = payload;
    renderKpi(payload.data.kpi);
    renderTrend(payload.data.trend);
    renderCustomer(payload.data.customers);
    renderStall(payload.data.stalls);
    renderSku(payload.data.skus);
    renderInventory(payload.data.inventory);
    renderMonthEnd(payload.data.inventory);
    renderZero(payload.data.inventory);
    renderNotices(payload);
    renderCapability();
    setStatus(`就绪 · ${(payload.meta.generated_at || '').slice(11, 19)}`);
    writeUrl();
    await loadDetail();
  } catch (err) {
    setStatus('加载失败', true);
    const el = $('alert');
    el.textContent = `数据加载失败：${err.message}`;
    el.classList.remove('hidden');
  }
}

/* ------------------------------------------------------------ 事件 */
function bind() {
  const reload = () => { state.page = 1; writeUrl(); loadAll(); };
  $('f-month').addEventListener('change', (e) => { state.months = e.target.value; reload(); });
  $('f-region').addEventListener('change', (e) => { state.regions = e.target.value; reload(); });
  $('f-customer').addEventListener('change', (e) => {
    state.customer = e.target.value; state.stall = ''; reload();
  });
  $('f-stall').addEventListener('change', (e) => { state.stall = e.target.value; reload(); });
  $('f-sku').addEventListener('change', (e) => { state.sku = e.target.value; reload(); });
  $('btn-reset').addEventListener('click', () => {
    state.months = ''; state.regions = ''; state.customer = ''; state.stall = '';
    state.sku = ''; state.page = 1; state.sort = ''; state.order = 'asc'; state.q = '';
    $('detail-q').value = '';
    writeUrl(); loadAll();
  });
  $('btn-refresh').addEventListener('click', loadAll);

  $('trend-metric').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-metric]');
    if (!btn) return;
    state.trendMetric = btn.dataset.metric;
    $('trend-metric').querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
    if (state.data) renderTrend(state.data.data.trend);
  });

  const bindMode = (id, key, render) => {
    $(id).addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-mode]');
      if (!btn) return;
      state[key] = btn.dataset.mode;
      $(id).querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
      if (state.data) render(state.data.data);
    });
  };
  bindMode('cust-mode', 'custMode', (d) => renderCustomer(d.customers));
  bindMode('stall-mode', 'stallMode', (d) => renderStall(d.stalls));
  bindMode('sku-mode', 'skuMode', (d) => renderSku(d.skus));

  $('detail-kind').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-kind]');
    if (!btn) return;
    state.kind = btn.dataset.kind;
    state.sort = ''; state.page = 1;
    $('detail-kind').querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
    $('detail-table').innerHTML = '';
    writeUrl(); loadDetail();
  });

  let timer = 0;
  $('detail-q').addEventListener('input', (e) => {
    clearTimeout(timer);
    const value = e.target.value.trim();
    timer = setTimeout(() => { state.q = value; state.page = 1; writeUrl(); loadDetail(); }, 260);
  });
  $('detail-prev').addEventListener('click', () => {
    if (state.page > 1) { state.page -= 1; writeUrl(); loadDetail(); }
  });
  $('detail-next').addEventListener('click', () => {
    state.page += 1; writeUrl(); loadDetail();
  });
}

readUrl();
$('detail-kind').querySelectorAll('button').forEach(b =>
  b.classList.toggle('on', b.dataset.kind === state.kind));
bind();
loadAll();
