/* =====================================================================
 * pos.js — POS 门店动销（阶段五）
 * 数据来自 /api/pos*，所有聚合与比率都由后端按 P01/P05/P12/P13 口径算好；
 * 前端只做展示与交互，不重算任何指标。
 *
 * 三条绝不越界的红线：
 *   1. POS 金额不与 ERP 出货、批发金额相加；
 *   2. POS 无记录不显示为 0，也不说「零销售」；
 *   3. 单价只做结构指标（金额÷盒数），不做价格执行 / 折扣分析。
 * ===================================================================== */
import { hideTooltip, fmtMoney, fmtCompact } from './charts.js';
import {
  donutCount, trendTwo, rankBar, fmtInt, fmtNum, emptyBox, loadingBox,
} from './boardcharts.js';

const $ = (id) => document.getElementById(id);

const state = {
  months: '', regions: '', ka: '', category: '', sku: '', store: '',
  page: 1, limit: 12, sort: '', order: 'desc', q: '', kind: 'store',
  trendMetric: 'amount', storeMetric: 'amount', skuMetric: 'amount',
  data: null, options: null,
};

const pct = (v, d = 1) => (v === null || v === undefined ? '—' : `${fmtNum(v, d)}%`);
const money = (v) => (v === null || v === undefined ? '—' : `¥${fmtNum(v, 2)}`);
const signed = (v, d = 1) => {
  if (v === null || v === undefined) return '—';
  const s = v > 0 ? '+' : '';
  return `${s}${fmtNum(v, d)}%`;
};
const esc = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/* ------------------------------------------------------------ URL / 请求 */
function readUrl() {
  const u = new URLSearchParams(location.search);
  state.months = u.get('months') || '';
  state.regions = u.get('regions') || '';
  state.ka = u.get('ka') || '';
  state.category = u.get('category') || '';
  state.sku = u.get('sku') || '';
  state.store = u.get('store') || '';
  state.kind = u.get('kind') === 'sku' ? 'sku' : 'store';
  state.page = Math.max(1, Number(u.get('page') || 1));
  state.sort = u.get('sort') || '';
  state.order = u.get('order') === 'asc' ? 'asc' : 'desc';
  state.q = u.get('q') || '';
}

function writeUrl() {
  const u = new URLSearchParams();
  [['months', state.months], ['regions', state.regions], ['ka', state.ka],
   ['category', state.category], ['sku', state.sku], ['store', state.store],
   ['q', state.q]].forEach(([k, v]) => { if (v) u.set(k, v); });
  if (state.kind !== 'store') u.set('kind', state.kind);
  if (state.page > 1) u.set('page', String(state.page));
  if (state.sort) { u.set('sort', state.sort); u.set('order', state.order); }
  const qs = u.toString();
  history.replaceState(null, '', qs ? `${location.pathname}?${qs}` : location.pathname);
}

function params(extra = {}) {
  const p = { ...extra };
  ['months', 'regions', 'ka', 'category', 'sku', 'store'].forEach((k) => {
    if (state[k]) p[k] = state[k];
  });
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
  fillSelect($('f-ka'), (opt.ka || []).map(k => ({
    value: k.ka_system, label: `${k.ka_system}（${k.store_count} 店）`,
  })), state.ka, `全部 KA（${(opt.ka || []).length}）`);
  fillSelect($('f-category'), (opt.categories || []).map(c => ({ value: c, label: c })),
    state.category, '全部品类');
  fillSelect($('f-sku'), (opt.skus || []).map(s => ({
    value: s.sku_code, label: `${s.sku_name}`,
  })), state.sku, `全部 SKU（${(opt.skus || []).length}）`);
  const stores = (opt.stores || []).filter(s => !state.ka || s.ka_system === state.ka);
  fillSelect($('f-store'), stores.map(s => ({
    value: s.store_id, label: `${s.store_name}`,
  })), state.store, `全部门店（${stores.length}）`);
}

function renderChips() {
  const chips = [];
  if (state.months) chips.push(`月份 ${state.months.split(',').map(m => `${m} 月`).join('、')}`);
  if (state.regions) chips.push(`战区 ${state.regions.split(',').join('、')}`);
  if (state.ka) chips.push(`KA ${state.ka}`);
  if (state.category) chips.push(`品类 ${state.category}`);
  if (state.sku) chips.push(`SKU ${state.sku}`);
  if (state.store) chips.push(`门店 ${state.store}`);
  $('active-chips').innerHTML = chips.map(c => `<span class="chip plain">${esc(c)}</span>`).join('');
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

function renderKpi(k, d) {
  const cov = d.coverage || {};
  const lastLabel = d.trend.length ? d.trend[d.trend.length - 1].label : '最近月';
  $('kpi-row').innerHTML = [
    kpiCard({
      label: '终端销售额', value: fmtMoney(k.amount), unit: 'Sell-out',
      sub: `${fmtInt(k.store_count)} 家门店 · ${fmtInt(k.month_count)} 个月`,
    }),
    kpiCard({
      label: '终端销量', value: fmtInt(k.boxes), unit: '盒', accent: 'teal',
      sub: `TE ${fmtInt(k.te)} · 单店日均 ${fmtInt(k.boxes_per_store_day)} 盒`,
    }),
    kpiCard({
      label: '动销门店数', value: fmtInt(k.store_count), unit: '家',
      sub: `另有 ${fmtInt(cov.sfa_only_stores)} 家仅 SFA、无 POS 数据`,
    }),
    kpiCard({
      label: '单店月均销售额', value: fmtMoney(k.amount_per_store_month),
      sub: `按 ${fmtInt(k.store_month_count)} 个门店×月 组合平均`,
    }),
    kpiCard({
      label: '单盒均价', value: `¥${fmtNum(k.unit_price, 2)}`, unit: '元/盒', accent: 'purple',
      sub: '加权 = 金额 ÷ 盒数 · 只反映规格结构',
    }),
    kpiCard({
      label: `${lastLabel}环比`, value: signed(k.last_month_mom),
      accent: 'amber', muted: k.last_month_mom === null,
      sub: '相邻自然月环比 · 不做同比',
    }),
  ].join('');
}

/* ------------------------------------------------------------ 趋势 */
function renderTrend(rows) {
  const metric = state.trendMetric;
  const isAmount = metric === 'amount';
  trendTwo($('chart-trend'), {
    labels: rows.map(r => r.label),
    series: [{
      name: isAmount ? '销售额' : '销量', color: '#2f6fe4',
      values: rows.map(r => (isAmount ? r.amount : r.boxes)),
    }],
    height: 214, padLeft: 78, zeroBase: false,
    axisFormat: (v) => (isAmount ? fmtCompact(v, '¥') : fmtInt(v)),
    valueFormat: (v) => (isAmount ? fmtMoney(v) : `${fmtInt(v)} 盒`),
    emptyText: '当前筛选条件下无月度数据',
  });
  $('legend-trend').innerHTML =
    `<span><i style="background:#2f6fe4"></i>${isAmount ? '终端销售额' : '终端销量'}</span>`;

  const total = rows.reduce((a, r) => a + (isAmount ? r.amount : r.boxes), 0);
  const peak = rows.reduce((a, r) => ((r.amount || 0) > (a.amount || 0) ? r : a), rows[0] || {});
  $('foot-trend').textContent = rows.length
    ? `合计 ${isAmount ? fmtMoney(total) : `${fmtInt(total)} 盒`} · 峰值 ${peak.label}`
      + `${isAmount ? fmtMoney(peak.amount) : `${fmtInt(peak.boxes)} 盒`}`
      + ' · 纵轴按数据区间取基线（非 0 起）以便看清月度波动，数值照实标注'
    : '';
}

/* ------------------------------------------------------------ 门店排名 */
function renderStores(rows) {
  const host = $('chart-store');
  if (!rows.length) return emptyBox(host, '当前筛选条件下无门店销售记录');
  const byAmount = state.storeMetric === 'amount';
  const sorted = [...rows].sort((a, b) => (byAmount ? b.amount - a.amount : b.boxes - a.boxes));
  const items = sorted.slice(0, 12).map((s, i) => ({
    label: s.store_name,
    value: byAmount ? s.amount : s.boxes,
    rateText: pct(s.share),
    color: i === 0 ? '#2f6fe4' : '#5b9bf8',
    tip: `<div class="tt-row"><span>销售额</span><b>${fmtMoney(s.amount)}</b></div>
          <div class="tt-row"><span>销量</span><b>${fmtInt(s.boxes)} 盒</b></div>
          <div class="tt-row"><span>占大盘</span><b>${pct(s.share)}</b></div>
          <div class="tt-row"><span>动销 SKU</span><b>${fmtInt(s.sku_count)} 个</b></div>
          <div class="tt-row"><span>单盒均价</span><b>¥${fmtNum(s.unit_price, 2)}</b></div>
          <div class="tt-row"><span>动能</span><b>${signed(s.momentum)}</b></div>`,
    store_id: s.store_id,
  }));
  rankBar(host, {
    items, labelW: 118, valueW: 76, rateW: 44, rowH: 26, maxLabel: 9,
    valueFormat: (v) => (byAmount ? fmtCompact(v, '¥') : `${fmtInt(v)} 盒`),
    onClick: (it) => openDrill(it.store_id),
  });
  const top = sorted[0];
  $('foot-store').textContent = `共 ${rows.length} 家门店 · 第一 ${top.store_name}`
    + `（占 ${pct(top.share)}）· 角标为占大盘比重 · 点门店条可下钻`;
}

/* ------------------------------------------------------------ KA 结构 */
function renderKa(rows) {
  const host = $('chart-ka');
  if (!rows.length) return emptyBox(host, '当前筛选条件下无 KA 数据');
  const palette = ['#2f6fe4', '#5b9bf8', '#0e9f8a', '#e8a33d', '#7c63d6', '#4cc2ae',
    '#9dc2fb', '#df4640', '#a8b3c2'];
  donutCount(host, {
    items: rows.map((k, i) => ({
      label: k.ka_system, value: k.amount, color: palette[i % palette.length],
    })),
    size: 148, unit: '销售额',
    centerValue: pct(rows[0].share, 1), centerLabel: `${rows[0].ka_system}占比`,
  });
  const top = rows[0];
  $('foot-ka').textContent = `${rows.length} 个 KA 系统 · ${top.ka_system} 最大`
    + `（${pct(top.share)}，${top.store_count} 家店，单店 ${fmtCompact(top.amount_per_store, '¥')}）`;
}

/* ------------------------------------------------------------ 品类 / SKU */
function renderCategory(rows) {
  const host = $('chart-category');
  if (!rows.length) return emptyBox(host, '当前筛选条件下无品类数据');
  donutCount(host, {
    items: rows.map(r => ({
      label: r.category, value: r.amount, color: r.color || '#9dc2fb',
    })),
    size: 148, unit: '销售额',
    centerValue: pct(rows[0].share, 1), centerLabel: `${rows[0].category}占比`,
  });
  const top = rows[0];
  $('foot-category').textContent = `${top.category} 占 ${pct(top.share)}`
    + `（单盒均价 ¥${fmtNum(top.unit_price, 2)}）`
    + ' · 结构差异来自规格组合，不是价格差异';
}

function renderSku(rows) {
  const host = $('chart-sku');
  if (!rows.length) return emptyBox(host, '当前筛选条件下无 SKU 数据');
  const byAmount = state.skuMetric === 'amount';
  const sorted = [...rows].sort((a, b) => (byAmount ? b.amount - a.amount : b.boxes - a.boxes));
  const catColor = { 品类A: '#e8a33d', 品类B: '#0e9f8a', 品类C: '#df4640' };
  rankBar(host, {
    items: sorted.slice(0, 10).map(s => ({
      label: s.sku_name, value: byAmount ? s.amount : s.boxes,
      rateText: pct(s.share),
      color: catColor[s.category] || '#5b9bf8',
      tip: `<div class="tt-row"><span>品类</span><b>${esc(s.category)}</b></div>
            <div class="tt-row"><span>销售额</span><b>${fmtMoney(s.amount)}</b></div>
            <div class="tt-row"><span>销量</span><b>${fmtInt(s.boxes)} 盒</b></div>
            <div class="tt-row"><span>覆盖门店</span><b>${fmtInt(s.store_count)} 家</b></div>
            <div class="tt-row"><span>单盒均价</span><b>¥${fmtNum(s.unit_price, 2)}</b></div>`,
    })),
    labelW: 126, valueW: 74, rateW: 42, rowH: 24, maxLabel: 11,
    valueFormat: (v) => (byAmount ? fmtCompact(v, '¥') : `${fmtInt(v)} 盒`),
  });
  const top = sorted[0];
  $('foot-sku').textContent = `${sorted.length} 个 SKU 有动销 · 第一 ${top.sku_name}`
    + `（占 ${pct(top.share)}，覆盖 ${top.store_count} 家门店）· 颜色为品类`;
}

/* ------------------------------------------------------------ 动能榜 */
function momRow(row, maxAbs) {
  const v = row.momentum;
  const up = v >= 0;
  const width = Math.min(50, (Math.abs(v) / (maxAbs || 1)) * 50);
  const style = up ? `left:50%;width:${width}%` : `right:50%;width:${width}%`;
  return `<div class="mom-row" data-store="${esc(row.store_id)}"
      title="点击查看 ${esc(row.store_name)} 详情">
    <span class="mom-name">${esc(row.store_name)}</span>
    <span class="mom-phase">${fmtCompact(row.amount_first, '¥')} → ${fmtCompact(row.amount_second, '¥')}</span>
    <span class="mom-track"><i class="mom-axis"></i>
      <i class="mom-fill ${up ? 'up' : 'down'}" style="${style}"></i></span>
    <span class="mom-val ${up ? 'up' : 'down'}">${signed(v)}</span>
  </div>`;
}

function renderMomentum(m) {
  const split = m.split || {};
  const rows = m.rows || [];
  $('momentum-split').textContent = split.note || '—';
  if (!rows.length) {
    $('momentum-up').innerHTML = '';
    $('momentum-down').innerHTML =
      '<div class="inline-empty"><b>动能暂不可比</b><span>窗口只有 1 个月，无法前后半对比</span></div>';
    $('momentum-up-count').textContent = '—';
    $('momentum-down-count').textContent = '—';
    $('foot-momentum').textContent = '动能需要至少 2 个月的窗口；单月请直接看趋势与排名。';
    return;
  }
  const maxAbs = Math.max(...rows.map(r => Math.abs(r.momentum)), 1);
  const upRows = rows.filter(r => r.momentum > 0).slice(0, 5);
  const downRows = rows.filter(r => r.momentum < 0).slice(-5).reverse();
  $('momentum-up').innerHTML = upRows.length
    ? upRows.map(r => momRow(r, maxAbs)).join('')
    : '<div class="inline-empty"><b>没有增长门店</b></div>';
  $('momentum-down').innerHTML = downRows.length
    ? downRows.map(r => momRow(r, maxAbs)).join('')
    : '<div class="inline-empty"><b>没有下滑门店</b></div>';
  $('momentum-up-count').textContent = `${m.up} 家增长`;
  $('momentum-down-count').textContent = `${m.down} 家下滑`;
  $('foot-momentum').textContent =
    `${m.comparable} 家可比门店中 ${m.up} 家后半段更强、${m.down} 家走弱`
    + ` · 绿=后半段高于前半段，红=下滑 · 这是窗口内结构变化，不是同比`
    + (m.flat ? ` · ${m.flat} 家持平` : '');
}

/* ------------------------------------------------------------ 集中度与覆盖 */
function renderConcentration(c) {
  donutCount($('chart-concentration'), {
    items: [
      { label: 'Top 3 门店', value: c.top3_amount, color: '#2f6fe4' },
      { label: '其余门店', value: c.others_amount, color: '#dbe6f6' },
    ],
    size: 132, unit: '销售额', unitShort: '',
    centerValue: pct(c.cr3), centerLabel: 'CR3',
  });
  const rows = [
    ['门店数（有 POS 数据）', `${fmtInt(c.store_count)} 家`],
    ['CR3（前 3 家占比）', pct(c.cr3)],
    ['CR5（前 5 家占比）', pct(c.cr5)],
    ['CR10（前 10 家占比）', pct(c.cr10)],
  ];
  $('fact-concentration').innerHTML = rows.map(([k, v], i) =>
    `<div class="fact-row"><span class="k">${k}</span>
      <span class="v${i === 1 && c.cr3 >= 50 ? ' warn' : ''}">${v}</span></div>`).join('');
  $('foot-concentration').textContent =
    `最大单店 ${c.top_store || '—'} 占 ${pct(c.top_share)} · CR3 ${pct(c.cr3)} 说明大盘对少数门店依赖很重；`
    + `这 ${fmtInt(c.store_count)} 家是对齐样本，不是全国门店。`;
}

function renderCoverage(cov) {
  donutCount($('chart-coverage'), {
    items: [
      { label: '有 POS 数据', value: cov.stores_scope, color: '#2f6fe4' },
      { label: '仅 SFA 拜访', value: cov.sfa_only_stores, color: '#dbe6f6' },
    ],
    size: 132, unit: '门店', unitShort: ' 家', legendMode: 'count',
    centerValue: fmtInt(cov.sfa_total_stores), centerLabel: '五大战区门店',
  });
  const rows = [
    ['五大战区门店合计', `${fmtInt(cov.sfa_total_stores)} 家`],
    ['有 POS 销售记录', `${fmtInt(cov.stores_scope)} 家`],
    ['仅 SFA、无 POS', `${fmtInt(cov.sfa_only_stores)} 家（不按零销售计）`],
    ['POS 行覆盖（Y26）', pct(cov.row_share)],
    ['POS 金额覆盖（Y26）', pct(cov.amount_share)],
    ['对齐版本门店数', `${fmtInt(cov.stores_all)} 家（含战区F/战区G ${fmtInt(cov.stores_all - cov.stores_scope)} 家）`],
  ];
  $('fact-coverage').innerHTML = rows.map(([k, v]) =>
    `<div class="fact-row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
  $('foot-coverage').textContent =
    `覆盖率为 Y26 实测：分母是本年 ${fmtInt(cov.rows_all)} 行 POS 记录；`
    + `P01 文档的 84.63% / 80.93% 分母是两个自然年全量 85,000 行，两者都对，只差分母。`;
  $('notice-main-text').innerHTML =
    `<b>POS 无记录 ≠ 零销售</b>：五大战区 ${fmtInt(cov.sfa_total_stores)} 家门店中，`
    + `只有 <b>${fmtInt(cov.stores_scope)} 家</b>有 POS 销售数据，`
    + `其余 ${fmtInt(cov.sfa_only_stores)} 家仅 SFA 拜访、没有 POS 数据源——`
    + `它们不进任何 POS 分母，也不按零销售计。金额口径为门店收银端成交（Sell-out），`
    + `与 ERP 出货（Sell-in）<b>永不相加</b>。`;
}

/* ------------------------------------------------------------ 口径声明 */
function renderCapability(meta) {
  const notes = meta.notes || [];
  const cov = state.data ? state.data.data.coverage : null;
  const extra = cov ? [
    `本页实测：Y26 对齐版 POS ${fmtInt(cov.rows_all)} 行 / ${fmtInt(cov.stores_all)} 家；`
    + `五大战区内 ${fmtInt(cov.rows_scope)} 行 / ${fmtInt(cov.stores_scope)} 家。`,
    `数据刷新：${meta.etl && meta.etl.finished_at ? meta.etl.finished_at.replace('T', ' ') : '—'}`,
  ] : [];
  const all = [...notes, ...extra];
  $('capability-list').innerHTML = all.map(t =>
    `<div class="capability-item"><span class="dot"></span><span>${esc(t)}</span></div>`).join('');
  $('capability-count').textContent = `（${all.length} 条）`;
}

/* ------------------------------------------------------------ 明细 */
const COLS = {
  store: [
    { key: 'store_name', label: '门店', sort: 'store_name', width: 150 },
    { key: 'ka_system', label: 'KA', sort: 'ka_system', width: 66 },
    { key: 'war_zone', label: '战区', sort: 'war_zone', width: 46 },
    { key: 'month', label: '月份', sort: 'month', width: 46 },
    { key: 'sales_amount', label: '销售额', sort: 'sales_amount', num: true, money: true, width: 92 },
    { key: 'sales_boxes', label: '销量(盒)', sort: 'sales_boxes', num: true, width: 72 },
    { key: 'sku_count', label: '动销SKU', sort: 'sku_count', num: true, width: 68 },
    { key: 'unit_price', label: '单盒均价', sort: 'unit_price', num: true, money: true, width: 76 },
    { key: 'share', label: '占大盘', sort: 'share', pct: true, width: 64 },
    { key: 'mom', label: '环比', sort: 'mom', signed: true, width: 64 },
  ],
  sku: [
    { key: 'store_name', label: '门店', sort: 'store_name', width: 150 },
    { key: 'sku_code', label: 'SKU 编码', sort: 'sku_code', width: 74 },
    { key: 'sku_name', label: 'SKU 名称', sort: 'sku_name', width: 118 },
    { key: 'category', label: '品类', sort: 'category', width: 50 },
    { key: 'sales_amount', label: '销售额', sort: 'sales_amount', num: true, money: true, width: 92 },
    { key: 'sales_boxes', label: '销量(盒)', sort: 'sales_boxes', num: true, width: 72 },
    { key: 'unit_price', label: '单盒均价', sort: 'unit_price', num: true, money: true, width: 76 },
    { key: 'share', label: '占大盘', sort: 'share', pct: true, width: 64 },
  ],
};

function renderTable(host, rows) {
  const cols = COLS[state.kind];
  const head = cols.map((c) => {
    const sorted = state.sort === c.sort && c.sort;
    const mark = c.sort ? `<em class="sort-mark">${sorted
      ? (state.order === 'desc' ? '▼' : '▲') : '⇅'}</em>` : '';
    return `<th class="${c.sort ? 'sortable' : ''}${sorted ? ' sorted' : ''}"
      data-sort="${c.sort || ''}" style="min-width:${c.width}px">${c.label}${mark}</th>`;
  }).join('');

  const cells = (r) => cols.map((c) => {
    const v = r[c.key];
    let text;
    if (v === null || v === undefined || v === '') text = '—';
    else if (c.money) text = `¥${fmtNum(v, 2)}`;
    else if (c.pct) text = pct(v);
    else if (c.signed) {
      const cls = v > 0 ? 'up' : (v < 0 ? 'down' : '');
      text = `<span class="mom-val ${cls}" style="display:inline">${signed(v)}</span>`;
    } else text = c.num ? fmtInt(v) : esc(v);
    return `<td class="${c.num ? 'num' : ''}">${text}</td>`;
  }).join('');

  const body = rows.map((r) => `<tr class="row-drill" data-store="${esc(r.store_id)}"
    title="点击查看门店详情">${cells(r)}</tr>`).join('');

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
  host.querySelectorAll('tr.row-drill').forEach((tr) => {
    tr.addEventListener('click', () => openDrill(tr.dataset.store));
  });
}

/* ------------------------------------------------------------ 加载 */
let detailSeq = 0;

async function loadDetail() {
  const host = $('detail-table');
  const seq = ++detailSeq;
  if (!host.querySelector('table')) loadingBox(host, '正在查询明细…');
  try {
    const payload = await getJSON('/api/pos/detail', {
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
    $('detail-grain').textContent = `粒度：${d.grain}`;
  } catch (err) {
    if (seq !== detailSeq) return;
    host.innerHTML = `<div class="inline-empty"><b>明细加载失败</b><span>${err.message}</span></div>`;
  }
}

function markLoading() {
  hideTooltip();
  setStatus('查询中…');
  ['chart-trend', 'chart-store', 'chart-ka', 'chart-category', 'chart-sku', 'chart-coverage']
    .forEach((id) => loadingBox($(id), '正在查询数据…'));
}

let mainSeq = 0;

async function loadAll() {
  const seq = ++mainSeq;
  markLoading();
  $('alert').classList.add('hidden');
  renderChips();
  try {
    state.options = (await getJSON('/api/pos/options', params())).data;
    if (seq !== mainSeq) return;
    renderFilterOptions();
    const payload = await getJSON('/api/pos', params());
    if (seq !== mainSeq) return;
    state.data = payload;
    const d = payload.data;
    renderKpi(d.kpi, d);
    renderTrend(d.trend);
    renderStores(d.stores);
    renderKa(d.ka);
    renderCategory(d.categories);
    renderSku(d.skus);
    renderMomentum(d.momentum);
    renderConcentration(d.concentration);
    renderCoverage(d.coverage);
    renderCapability(payload.meta);
    $('chip-period').textContent = payload.meta.period || 'Y26';
    $('chip-coverage').textContent =
      `POS 覆盖 ${fmtInt(d.coverage.stores_scope)} / ${fmtInt(d.coverage.sfa_total_stores)} 家`;
    const etl = payload.meta.etl || {};
    const fresh = etl.finished_at ? etl.finished_at.replace('T', ' ').slice(0, 16) : '—';
    $('chip-fresh').textContent = `数据刷新 ${fresh}`;
    $('foot-etl').textContent = `${etl.run_id || '—'} · ${fresh}`;
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

/* ------------------------------------------------------------ 门店下钻 */
let drillSeq = 0;

async function openDrill(storeId) {
  if (!storeId) return;
  const seq = ++drillSeq;
  $('drill-mask').classList.remove('hidden');
  $('drill-title').textContent = '门店动销下钻';
  $('drill-sub').textContent = storeId;
  $('drill-meta').textContent = '正在读取…';
  $('drill-trend').innerHTML = '';
  loadingBox($('drill-trend'), '正在查询门店数据…');
  try {
    const payload = await getJSON('/api/pos/drill', { ...params(), store: storeId });
    if (seq !== drillSeq) return;
    const d = payload.data;
    const s = d.store;
    $('drill-title').textContent = s.store_name;
    $('drill-sub').textContent = `${s.store_id} · ${s.ka_system || '—'} · ${s.war_zone || '—'}`;
    $('drill-meta').innerHTML =
      `市场部 <b>${esc(s.market_department || '未采集')}</b> · `
      + `门店等级 <b>${esc(s.store_level || '未采集')}</b> · `
      + `门店类型 <b>${esc(s.store_type || '未采集')}</b><br>`
      + `该店占所选口径大盘 <b>${pct(d.summary.share)}</b>（共 ${fmtInt(d.summary.store_count_in_scope)} 家门店）`;

    const k = d.summary;
    $('drill-kpi').innerHTML = [
      kpiCard({ label: '销售额', value: fmtMoney(k.amount), sub: `${fmtInt(k.month_count)} 个月` }),
      kpiCard({ label: '销量', value: fmtInt(k.boxes), unit: '盒',
        sub: `单盒均价 ¥${fmtNum(k.unit_price, 2)}` }),
      kpiCard({ label: '动销 SKU', value: fmtInt(k.sku_count), unit: '个',
        sub: `${fmtInt(k.day_count)} 天有销售记录` }),
    ].join('');

    trendTwo($('drill-trend'), {
      labels: d.monthly.map(m => `${m.month} 月`),
      series: [{ name: '销售额', color: '#2f6fe4', values: d.monthly.map(m => m.amount) }],
      height: 150, padLeft: 70, zeroBase: false,
      axisFormat: (v) => fmtCompact(v, '¥'),
      valueFormat: (v) => fmtMoney(v),
      emptyText: '该门店在所选窗口内无销售记录',
    });

    donutCount($('drill-cat'), {
      items: d.categories.map(c => ({ label: c.category, value: c.amount, color: c.color })),
      size: 132, unit: '销售额',
      centerValue: pct(d.categories.length ? d.categories[0].share : null, 1),
      centerLabel: `${d.categories.length ? d.categories[0].category : '—'}占比`,
    });

    const rows = d.skus.map(x => `<tr><td>${esc(x.sku_name)}</td>
      <td class="num">${fmtCompact(x.amount, '¥')}</td>
      <td class="num">${pct(x.share)}</td></tr>`).join('');
    $('drill-sku').innerHTML = `<table class="data"><thead><tr><th>SKU</th>
      <th class="num">销售额</th><th class="num">占比</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="3" style="text-align:center;padding:12px">无</td></tr>'}</tbody></table>`;
  } catch (err) {
    if (seq !== drillSeq) return;
    $('drill-meta').textContent = `加载失败：${err.message}`;
    $('drill-trend').innerHTML =
      `<div class="inline-empty"><b>无法读取该门店</b><span>${esc(err.message)}</span></div>`;
  }
}

function closeDrill() {
  drillSeq += 1;
  $('drill-mask').classList.add('hidden');
}

/* ------------------------------------------------------------ 事件 */
function bind() {
  const reload = () => { state.page = 1; writeUrl(); loadAll(); };
  $('f-month').addEventListener('change', (e) => { state.months = e.target.value; reload(); });
  $('f-region').addEventListener('change', (e) => { state.regions = e.target.value; reload(); });
  $('f-ka').addEventListener('change', (e) => {
    state.ka = e.target.value; state.store = ''; reload();
  });
  $('f-category').addEventListener('change', (e) => { state.category = e.target.value; reload(); });
  $('f-sku').addEventListener('change', (e) => { state.sku = e.target.value; reload(); });
  $('f-store').addEventListener('change', (e) => { state.store = e.target.value; reload(); });
  $('btn-reset').addEventListener('click', () => {
    Object.assign(state, {
      months: '', regions: '', ka: '', category: '', sku: '', store: '',
      page: 1, sort: '', order: 'desc', q: '',
    });
    $('detail-q').value = '';
    writeUrl(); loadAll();
  });
  $('btn-refresh').addEventListener('click', loadAll);

  const bindMetric = (id, key, render) => {
    $(id).addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-metric]');
      if (!btn) return;
      state[key] = btn.dataset.metric;
      $(id).querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
      if (state.data) render(state.data.data);
    });
  };
  bindMetric('trend-metric', 'trendMetric', (d) => renderTrend(d.trend));
  bindMetric('store-metric', 'storeMetric', (d) => renderStores(d.stores));
  bindMetric('sku-metric', 'skuMetric', (d) => renderSku(d.skus));

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

  // 动能榜点击同样可以下钻
  ['momentum-up', 'momentum-down'].forEach((id) => {
    $(id).addEventListener('click', (e) => {
      const row = e.target.closest('[data-store]');
      if (row) openDrill(row.dataset.store);
    });
  });

  $('drill-close').addEventListener('click', closeDrill);
  $('drill-mask').addEventListener('click', (e) => {
    if (e.target === $('drill-mask')) closeDrill();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDrill();
  });
}

readUrl();
$('detail-kind').querySelectorAll('button').forEach(b =>
  b.classList.toggle('on', b.dataset.kind === state.kind));
bind();
loadAll();
