/* =====================================================================
 * region.js — 战区销售分析 V1（独立页面）
 *
 * 复用 styles.css / charts.js / api.js（meta、detail）与
 * /api/region/analysis（复用 server/filters.py 的参数化 WHERE + ETL V1 已验收视图）。
 * 前端只做展示与联动，不重算指标、不生成数据、不把 ERP 与 POS 金额相加。
 * 全国经营驾驶舱 V1.1 的 index.html / app.js / charts.js / styles.css / api.js 零改动。
 * ===================================================================== */
import { api } from './api.js';
import * as CH from './charts.js';

const $ = (id) => document.getElementById(id);
const DEFAULTS = { regions: '', months: '', channel: '', ka: '', customer: '', category: '', sku: '' };
const state = {
  ...DEFAULTS,
  trendErpMetric: 'amount', trendPosMetric: 'amount',
  rankAmountSide: 'pos', rankBoxesSide: 'pos',
  categorySide: 'pos', skuSide: 'pos', kaDistSide: 'pos', kaDrillSide: 'pos',
  detailSource: 'pos', detailPage: 1, drawerPage: 1,
  meta: null, data: null, busy: false, pending: false, detailToken: 0,
};

/* ------------------------------------------------------------ 取数 */
async function getJSON(path, params = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') url.searchParams.set(k, String(v));
  });
  const res = await fetch(url.toString(), { cache: 'no-store' });
  let payload;
  try { payload = await res.json(); }
  catch (e) { throw new Error(`接口返回非 JSON（HTTP ${res.status}）`); }
  if (!res.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${res.status}`);
  return payload;
}

function params() {
  const p = {};
  for (const k of Object.keys(DEFAULTS)) if (state[k] !== '') p[k] = state[k];
  return p;
}

/* ------------------------------------------------------------ 小工具 */
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast._timer); toast._timer = setTimeout(() => t.classList.remove('show'), 2200);
}
function alertBox(msg) {
  const a = $('alert');
  if (!msg) { a.classList.add('hidden'); return; }
  a.textContent = msg; a.classList.remove('hidden');
}
function status(text, ok = true) {
  const b = $('status-badge'); b.textContent = text;
  b.className = 'status' + (ok ? '' : ' off');
}
function opt(value, label) {
  const o = document.createElement('option'); o.value = value; o.textContent = label; return o;
}
function hasValues(rows, key = 'value') {
  return Array.isArray(rows) && rows.some(r => r && r[key] !== null && r[key] !== undefined);
}
function pct(v) { return v === null || v === undefined ? '—' : `${CH.fmtNum(v, 1)}%`; }
function escapeText(v) {
  return String(v ?? '—').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
}
function legend(hostId, series) {
  $(hostId).innerHTML = series.map(s =>
    `<span><i style="background:${s.color}"></i>${escapeText(s.name)}</span>`).join('');
}

/* ------------------------------------------------------------ 筛选器 */
function fillAll(key, build) {
  document.querySelectorAll(`select[data-filter="${key}"]`).forEach(sel => {
    const old = sel.value;
    sel.innerHTML = '';
    build(sel);
    if (old && [...sel.options].some(o => o.value === old)) sel.value = old;
  });
}
function fillSelects(m) {
  fillAll('regions', s => {
    s.appendChild(opt('', m.scope_label));
    m.regions.forEach(x => s.appendChild(opt(x, x)));
  });
  fillAll('months', s => {
    s.appendChild(opt('', '全部月份（1–10 月）'));
    (state.meta?._months || []).forEach(x => s.appendChild(opt(String(x), `${x} 月`)));
  });
  fillAll('channel', s => {
    s.appendChild(opt('', '全部渠道'));
    m.channels.forEach(x => s.appendChild(opt(x, x)));
  });
  fillAll('ka', s => {
    s.appendChild(opt('', '全部 KA 系统'));
    m.kas.forEach(x => s.appendChild(opt(x, x)));
  });
  fillAll('customer', s => {
    s.appendChild(opt('', '全部客户'));
    m.customers.forEach(x => s.appendChild(opt(x.customer_id, `${x.customer_name} · ${x.customer_id}`)));
  });
  fillAll('category', s => {
    s.appendChild(opt('', '全部品类'));
    m.categories.forEach(x => s.appendChild(opt(x, x)));
  });
  fillAll('sku', s => {
    s.appendChild(opt('', `全部 SKU（${m.skus.length}）`));
    m.categories.forEach(cg => {
      const g = document.createElement('optgroup'); g.label = cg;
      m.skus.filter(x => x.category === cg).forEach(x =>
        g.appendChild(opt(x.sku_code, `${x.sku_name} · ${x.sku_code}`)));
      s.appendChild(g);
    });
  });
  $('foot-views').textContent = `已验收视图 ${m.delivered_views.length} 个`;
  $('scope-badge').textContent = m.scope_label;
}

function fillMonths(months) {
  state.meta._months = months;
  fillAll('months', s => {
    s.appendChild(opt('', '全部月份（1–10 月）'));
    months.forEach(x => s.appendChild(opt(String(x), `${x} 月`)));
  });
  syncControls();
}

function syncControls() {
  document.querySelectorAll('select[data-filter]').forEach(s => {
    const k = s.dataset.filter;
    if (k in state && [...s.options].some(o => o.value === state[k])) s.value = state[k];
  });
  const tabs = {
    'trend-erp-metric': ['trendErpMetric', 'metric'],
    'trend-pos-metric': ['trendPosMetric', 'metric'],
    'rank-amount-side': ['rankAmountSide', 'side'],
    'rank-boxes-side': ['rankBoxesSide', 'side'],
    'category-side': ['categorySide', 'side'],
    'sku-side': ['skuSide', 'side'],
    'ka-dist-side': ['kaDistSide', 'side'],
    'ka-drill-side': ['kaDrillSide', 'side'],
    'detail-source': ['detailSource', 'source'],
  };
  Object.entries(tabs).forEach(([id, [key, attr]]) => {
    document.querySelectorAll(`#${id} button`).forEach(b =>
      b.classList.toggle('on', b.dataset[attr] === state[key]));
  });
  const labels = [];
  if (state.regions) labels.push(`战区：${state.regions}`);
  if (state.months) labels.push(`${state.months} 月`);
  if (state.channel) labels.push(`渠道：${state.channel}`);
  if (state.ka) labels.push(`KA：${state.ka}`);
  if (state.customer) labels.push(`客户：${state.customer}`);
  if (state.category) labels.push(`品类：${state.category}`);
  if (state.sku) labels.push(`SKU：${state.sku}`);
  $('active-chips').innerHTML = labels.map(x => `<span class="chip">${escapeText(x)}</span>`).join('');
  $('chip-month').textContent = state.months ? `所选月份：${state.months} 月` : '全部月份（1–10 月）';
}

/* ------------------------------------------------------------ 加载 */
const HOSTS = ['chart-trend-erp', 'chart-trend-pos', 'chart-rank-amount', 'chart-rank-boxes',
               'chart-category', 'chart-sku', 'chart-ka-dist', 'chart-ka-drill',
               'compare-table', 'detail-table'];
const FEET = ['foot-trend-erp', 'foot-trend-pos', 'foot-rank-amount', 'foot-rank-boxes',
              'foot-category', 'foot-sku', 'foot-ka-dist', 'foot-ka-drill', 'foot-compare'];

function markLoading() {
  HOSTS.forEach(id => CH.loading($(id)));
  FEET.forEach(id => { $(id).textContent = ''; });
  if (!state.data) $('region-tiles').innerHTML = '<div class="state-box">正在读取五大战区…</div>';
  status('查询中…', false);
}

async function loadAll() {
  if (state.busy) { state.pending = true; return; }
  state.busy = true; state.pending = false;
  markLoading(); alertBox(null);
  try {
    const res = await getJSON('/api/region/analysis', params());
    state.data = res.data;
    fillMonths(res.data.capabilities.months_all);
    renderAll(res.meta);
    status('数据已更新');
  } catch (e) {
    alertBox(`数据查询失败：${e.message}。请确认后端服务已启动、ETL 数据库可读。`);
    HOSTS.forEach(id => CH.failed($(id), e.message));
    status('查询失败', false);
  } finally {
    state.busy = false;
    if (state.pending) {
      state.pending = false;
      setTimeout(() => loadAll(), 0);
      return;
    }
    loadDetail();
  }
}

function renderAll(meta) {
  renderTiles(); renderTrend('erp'); renderTrend('pos');
  renderRank('amount'); renderRank('boxes');
  renderCategory(); renderSKU(); renderKADist(); renderKADrill();
  renderCompare(); renderCapabilities();
  $('chip-period').textContent = 'Y26 · 2026 年 1–10 月';
  $('chip-period').title = meta.period;
}

/* ------------------------------------------------------------ 第一行：五大战区总览 */
function renderTiles() {
  const items = state.data.overview.items;
  $('region-tiles').innerHTML = items.map(r => `
    <div class="region-tile ${state.regions === r.name ? 'on' : ''}" data-region="${escapeText(r.name)}"
         title="点击下钻 ${escapeText(r.name)}，全部图表与明细同步联动">
      <div class="tile-top">
        <span class="tile-name">${escapeText(r.name)}</span>
        <span class="tile-rank">POS #${r.pos_amount_rank ?? '—'}</span>
        <span class="tile-sub">ERP #${r.erp_amount_rank ?? '—'}</span>
      </div>
      <div class="tile-grid">
        <div class="tile-cell tile-erp">
          <div class="tile-sys">ERP Sell-in</div>
          <div class="tile-val">${CH.fmtMoney(r.erp_amount)}</div>
          <div class="tile-share">占五区 ${pct(r.erp_share_pct)} · ${CH.fmtCompact(r.erp_boxes, '盒')}</div>
        </div>
        <div class="tile-cell tile-pos">
          <div class="tile-sys">POS Sell-out</div>
          <div class="tile-val">${CH.fmtMoney(r.pos_amount)}</div>
          <div class="tile-share">占五区 ${pct(r.pos_share_pct)} · ${CH.fmtCompact(r.pos_boxes, '盒')}</div>
        </div>
      </div>
    </div>`).join('');
}

function setRegion(name) {
  state.regions = state.regions === name ? '' : name;
  state.detailPage = 1;
  syncControls(); loadAll();
  toast(state.regions ? `已下钻战区：${name}` : '已恢复五大战区合计');
}

/* ------------------------------------------------------------ 第二行：月度趋势 */
function renderTrend(side) {
  const host = $(side === 'erp' ? 'chart-trend-erp' : 'chart-trend-pos');
  const foot = $(side === 'erp' ? 'foot-trend-erp' : 'foot-trend-pos');
  const m = state.data.monthly;
  const metric = side === 'erp' ? state.trendErpMetric : state.trendPosMetric;
  const months = m.months;
  const chosen = state.months ? Number(state.months) : null;
  const regions = Object.keys(m[side]);
  const fmt = metric === 'amount' ? CH.fmtMoney : (v) => CH.fmtCompact(v, '盒');
  const series = regions.map((rg, i) => ({
    name: rg, color: CH.PALETTE[i % CH.PALETTE.length],
    values: (m[side][rg][metric] || []).map((v, j) =>
      (chosen !== null && months[j] !== chosen) ? null : v),
  }));
  if (!series.some(s => hasValues(s.values.map(v => ({ value: v }))))) {
    CH.empty(host, side === 'pos'
      ? '该筛选条件下 POS 无终端销售数据（不按 0 计）'
      : '该筛选条件下 ERP 无出货数据');
    foot.textContent = '无记录月份保持断点，不补零、不连线';
    return;
  }
  CH.lineChart(host, {
    labels: months.map(x => `${x}月`), height: 140, area: false,
    tipPrefix: `${side === 'erp' ? 'ERP 出货' : 'POS 销售'} · `,
    axisFormat: metric === 'amount' ? (v) => CH.fmtCompact(v, '¥') : (v) => CH.fmtCompact(v),
    tipFormat: fmt, series,
  });
  const scope = state.data.capabilities.month_scope;
  foot.textContent = `${side === 'erp' ? 'ERP Sell-in' : 'POS Sell-out'} · ${metric === 'amount' ? '金额' : '盒数'} · `
    + `${scope} · 五区同图，点击战区卡下钻`;
  foot.title = '趋势保留完整 Y26 1–10 月真实序列；所选月份之外在图上断开、不补零。'
    + '主导战区金额占比较高时，其余战区曲线贴近横轴属真实数据形态，可点击战区卡单独查看。';
}

/* ------------------------------------------------------------ 第二行：金额 / 数量排名 */
function renderRank(metric) {
  const side = metric === 'amount' ? state.rankAmountSide : state.rankBoxesSide;
  const host = $(metric === 'amount' ? 'chart-rank-amount' : 'chart-rank-boxes');
  const foot = $(metric === 'amount' ? 'foot-rank-amount' : 'foot-rank-boxes');
  const rows = state.data.ranking.ranks[`${side}_${metric}`] || [];
  if (!hasValues(rows)) {
    CH.empty(host, '当前筛选条件下无排名数据');
    foot.textContent = '';
    return;
  }
  const fmt = metric === 'amount' ? CH.fmtMoney : (v) => CH.fmtCompact(v, '盒');
  CH.hbarChart(host, {
    items: rows.map((r, i) => ({
      label: `${r.rank}. ${r.name}`, value: r.value, badge: r.share_pct, raw: r,
      color: CH.PALETTE[i % CH.PALETTE.length],
      tipExtra: `<div class="tt-row"><span>战区内份额</span><b>${pct(r.share_pct)}</b></div>`,
    })),
    rowH: 20, labelW: 54, valueW: 66, badgeW: 40, maxLabel: 8, clickable: true,
    badgeFormat: (v) => `${CH.fmtNum(v, 1)}%`,
    valueFormat: fmt, tipFormat: fmt,
    tipLabel: `${side === 'erp' ? 'ERP 出货' : 'POS 销售'}${metric === 'amount' ? '金额' : '盒数'}`,
    onClick: (it) => setRegion(it.raw.name),
  });
  foot.textContent = `${side === 'erp' ? 'ERP Sell-in' : 'POS Sell-out'} · ${metric === 'amount' ? '金额' : '盒数'}`
    + ` · ${state.data.capabilities.month_scope} · 点击条形下钻战区`;
}

/* ------------------------------------------------------------ 第三行：品类结构 */
function renderCategory() {
  const side = state.categorySide;
  const c = state.data.category;
  const colors = ['#2f6fe4', '#5b9bf8', '#9dc2fb'];
  const series = c.categories.map((cat, i) => ({
    name: cat, color: colors[i % colors.length], values: c[side][cat],
  }));
  if (!series.some(s => s.values.some(v => v !== null && v !== undefined))) {
    CH.empty($('chart-category'), '当前筛选条件下无品类销售数据');
    $('legend-category').innerHTML = '';
    $('foot-category').textContent = '';
    return;
  }
  CH.groupedBar($('chart-category'), { labels: c.regions, series, height: 140 });
  legend('legend-category', series);
  const top = c.regions[0];
  $('foot-category').textContent = `${side === 'erp' ? 'ERP 出货' : 'POS 销售'} · 品类A/品类B/品类C · `
    + `${top} 占比 ${c.categories.map(cat =>
      `${cat} ${pct(c.shares[side][top]?.[cat])}`).join(' / ')}`;
}

/* ------------------------------------------------------------ 第三行：SKU TOP */
function renderSKU() {
  const side = state.skuSide;
  const rows = state.data.sku[side] || [];
  if (!hasValues(rows, 'amount')) {
    CH.empty($('chart-sku'), '当前筛选条件下无 SKU 销售数据');
    $('foot-sku').textContent = '';
    return;
  }
  CH.hbarChart($('chart-sku'), {
    items: rows.map((r, i) => ({
      label: `${r.rank}. ${r.name}`, value: r.amount, raw: r,
      color: CH.PALETTE[i % CH.PALETTE.length],
      tipExtra: `<div class="tt-row"><span>名称</span><b>${escapeText(r.sku_name)}</b></div>
                 <div class="tt-row"><span>品类</span><b>${escapeText(r.category)}</b></div>
                 <div class="tt-row"><span>盒数</span><b>${CH.fmtNum(r.boxes)}</b></div>`,
    })),
    rowH: 17, labelW: 78, valueW: 60, maxLabel: 12, clickable: true,
    valueFormat: CH.fmtMoney, tipFormat: CH.fmtMoney, tipLabel: '销售额',
    onClick: (it) => {
      state.sku = state.sku === it.raw.name ? '' : it.raw.name;
      state.detailPage = 1; syncControls(); loadAll();
      toast(state.sku ? `已下钻 SKU：${it.raw.name}` : '已清除 SKU 下钻');
    },
  });
  $('foot-sku').textContent = `${side === 'erp' ? 'ERP 出货' : 'POS 销售'} · 命中 ${state.data.sku.total[side]} 个 SKU`
    + ` · 展示 TOP ${rows.length} · 点击条形联动全局 SKU`;
}

/* ------------------------------------------------------------ 第三行：各战区 KA 分布 */
function renderKADist() {
  const side = state.kaDistSide;
  const k = state.data.ka;
  const series = (k.series[side] || []).map((s, i) => ({
    name: s.name, color: CH.PALETTE[i % CH.PALETTE.length], values: s.values,
  }));
  if (!series.length || !series.some(s => s.values.some(v => v !== null && v !== undefined))) {
    CH.empty($('chart-ka-dist'), side === 'pos'
      ? '该筛选条件下 POS 无 KA 终端销售数据' : '该筛选条件下 ERP 无客户出货数据');
    $('legend-ka-dist').innerHTML = '';
    $('foot-ka-dist').textContent = '';
    return;
  }
  CH.groupedBar($('chart-ka-dist'), { labels: k.regions, series, height: 140 });
  legend('legend-ka-dist', series);
  $('foot-ka-dist').textContent = `${side === 'pos' ? 'POS ka_system' : 'ERP customer_id'} · `
    + `五战区 × TOP ${series.length} · ${state.data.capabilities.month_scope} · 映射 PENDING，两套名单不合并`;
}

/* ------------------------------------------------------------ 第三行：KA / 客户排名下钻 */
function renderKADrill() {
  const side = state.kaDrillSide;
  const rows = state.data.ka[side].top || [];
  if (!hasValues(rows, 'amount')) {
    CH.empty($('chart-ka-drill'), '当前筛选条件下无 KA / 客户数据');
    $('foot-ka-drill').textContent = '';
    return;
  }
  CH.hbarChart($('chart-ka-drill'), {
    items: rows.map((r, i) => ({
      label: r.label || r.name, value: r.amount, badge: r.share_pct, raw: r,
      color: CH.PALETTE[i % CH.PALETTE.length],
      tipExtra: `<div class="tt-row"><span>编码</span><b>${escapeText(r.name)}</b></div>
                 <div class="tt-row"><span>盒数</span><b>${CH.fmtNum(r.boxes)}</b></div>`,
    })),
    rowH: 17, labelW: 76, valueW: 58, badgeW: 40, maxLabel: 9, clickable: true,
    badgeFormat: (v) => `${CH.fmtNum(v, 1)}%`,
    valueFormat: CH.fmtMoney, tipFormat: CH.fmtMoney,
    tipLabel: side === 'pos' ? 'POS KA 终端销售' : 'ERP 客户出货',
    onClick: (it) => {
      const key = side === 'pos' ? 'ka' : 'customer';
      state[key] = state[key] === it.raw.name ? '' : it.raw.name;
      state.detailPage = 1; syncControls(); loadAll();
      toast(state[key] ? `已联动${side === 'pos' ? ' KA 系统' : '客户'}：${it.raw.name}` : '已清除下钻');
    },
  });
  $('foot-ka-drill').textContent = `${side === 'pos' ? 'POS KA 系统' : 'ERP 客户'} · `
    + `共 ${state.data.ka[side].total_members} 个 · 展示 TOP ${rows.length} · 点击联动全局筛选`;
}

/* ------------------------------------------------------------ 第四行：ERP / POS 对照 */
function renderCompare() {
  const rows = state.data.compare.rows;
  $('compare-table').innerHTML = `
    <table class="compare"><thead><tr>
      <th>战区</th>
      <th class="th-erp">ERP 出货金额</th><th class="th-erp">ERP 五区占比</th>
      <th class="th-erp">ERP 出货盒数</th><th class="th-erp">ERP 毛利率</th><th class="th-erp">ERP 排名</th>
      <th class="th-pos">POS 销售金额</th><th class="th-pos">POS 五区占比</th>
      <th class="th-pos">POS 销售盒数</th><th class="th-pos">POS 排名</th>
    </tr></thead><tbody>${rows.map(r => `
      <tr><td><b>${escapeText(r.name)}</b></td>
        <td>${CH.fmtMoney(r.erp_amount)}</td><td>${pct(r.erp_share_pct)}</td>
        <td>${CH.fmtCompact(r.erp_boxes, '盒')}</td>
        <td>${r.erp_margin_pct === null || r.erp_margin_pct === undefined ? '—' : `${CH.fmtNum(r.erp_margin_pct, 2)}%`}</td>
        <td class="rank-cell">#${r.erp_amount_rank ?? '—'}</td>
        <td class="pos-col">${CH.fmtMoney(r.pos_amount)}</td>
        <td class="pos-col">${pct(r.pos_share_pct)}</td>
        <td class="pos-col">${CH.fmtCompact(r.pos_boxes, '盒')}</td>
        <td class="pos-col rank-cell">#${r.pos_amount_rank ?? '—'}</td>
      </tr>`).join('')}</tbody></table>`;
  // 静态结构 + 来自后端的文本（转义后插入），不拼接任何业务数值
  $('foot-compare').innerHTML =
    `<span class="compare-sys-tag erp">ERP Sell-in</span><span class="compare-sys-tag pos">POS Sell-out</span>`
    + ` · ${escapeText(state.data.compare.note)} · ${escapeText(state.data.capabilities.month_scope)}`
    + ` · 不支持：${escapeText(state.data.compare.not_supported.join(' / '))}`;
}

/* ------------------------------------------------------------ 能力边界 */
function renderCapabilities() {
  const caps = state.data.capabilities;
  $('capability-count').textContent = `明确列出 ${caps.missing.length} 项未支持分析`;
  $('capability-list').innerHTML = caps.missing.map(m => `
    <div class="cap-item">
      <h4>${escapeText(m.title)}</h4>
      <p><b>原因：</b>${escapeText(m.reason)}</p>
      <p><b>本页处理：</b>${escapeText(m.handling)}</p>
    </div>`).join('') + `
    <div class="cap-item cap-notes">
      <h4>本页冻结口径</h4>
      <ul>${caps.notes.map(n => `<li>${escapeText(n)}</li>`).join('')}</ul>
    </div>`;
}

/* ------------------------------------------------------------ 经营明细（复用 /api/detail） */
const COL_LABELS = {
  year: '年', month: '月', full_date: '日期', region_name_std: '战区',
  sku_code: 'SKU 编码', sku_name: 'SKU 名称', category: '品类', channel_type: '渠道',
  sales_boxes: '销售盒数', sales_amount: '销售金额', customer_id: '客户编码',
  customer_name: '客户名称', shipment_te: '出货TE', cost_amount: '成本',
  gross_profit: '毛利额', ka_system: 'KA 系统', store_name: '门店', sales_te: '销售TE',
};
const NUMERIC = new Set(['sales_boxes', 'sales_amount', 'shipment_te', 'cost_amount',
                         'gross_profit', 'sales_te', 'year', 'month']);
const DETAIL_COLS = {
  pos: ['full_date', 'region_name_std', 'channel_type', 'ka_system',
        'category', 'sku_code', 'sales_boxes', 'sales_amount'],
  erp: ['full_date', 'region_name_std', 'channel_type', 'customer_name',
        'category', 'sku_code', 'sales_boxes', 'sales_amount', 'gross_profit'],
};
const DRAWER_COLS = {
  pos: ['full_date', 'region_name_std', 'channel_type', 'ka_system', 'store_name',
        'sku_code', 'sku_name', 'category', 'sales_boxes', 'sales_amount', 'sales_te'],
  erp: ['full_date', 'region_name_std', 'channel_type', 'customer_id', 'customer_name',
        'sku_code', 'sku_name', 'category', 'sales_boxes', 'sales_amount',
        'shipment_te', 'cost_amount', 'gross_profit'],
};
function tableHTML(rows, cols) {
  if (!rows.length) return '';
  return `<table class="data"><thead><tr>` +
    cols.map(c => `<th>${COL_LABELS[c] || c}</th>`).join('') + `</tr></thead><tbody>` +
    rows.map(r => `<tr>` + cols.map(c => {
      const v = r[c];
      const txt = NUMERIC.has(c) && v !== null && v !== undefined
        ? CH.fmtNum(v, Number.isInteger(v) ? 0 : 2) : escapeText(v);
      return `<td class="${NUMERIC.has(c) ? 'num' : ''}">${txt}</td>`;
    }).join('') + `</tr>`).join('') + `</tbody></table>`;
}

async function loadDetail() {
  const token = ++state.detailToken;
  CH.loading($('detail-table'), '正在读取明细…');
  const p = { ...params(), source: state.detailSource, page: state.detailPage, limit: 10 };
  try {
    const res = await api.detail(p);
    if (token !== state.detailToken) return;
    const d = res.data;
    if (!d.rows.length) CH.empty($('detail-table'), '当前筛选条件下无明细记录');
    else $('detail-table').innerHTML = tableHTML(d.rows, DETAIL_COLS[state.detailSource]);
    const pages = Math.max(1, Math.ceil(d.total / d.page_size));
    $('detail-count').textContent =
      `共 ${CH.fmtNum(d.total)} 行 · 每页 ${d.page_size} 行 · ${state.data?.capabilities.month_scope || 'Y26 1–10 月'}`;
    $('detail-page').textContent = `第 ${d.page} / ${pages} 页`;
    $('detail-prev').disabled = d.page <= 1;
    $('detail-next').disabled = d.page >= pages;
  } catch (e) {
    if (token !== state.detailToken) return;
    CH.failed($('detail-table'), e.message);
    $('detail-count').textContent = '明细读取失败';
  }
}

async function loadDrawer() {
  CH.loading($('drawer-table'), '正在读取明细…');
  const p = { ...params(), source: state.detailSource, page: state.drawerPage, limit: 20 };
  try {
    const res = await api.detail(p);
    const d = res.data;
    $('drawer-sub').textContent =
      `${state.detailSource === 'erp' ? 'ERP 出货明细' : 'POS 终端销售明细'} · ${res.meta.region_label}`
      + ` · ${state.data?.capabilities.month_scope || ''}`;
    $('drawer-count').textContent = `共 ${CH.fmtNum(d.total)} 行 · Y26`;
    $('page-label').textContent = `第 ${d.page} 页`;
    if (!d.rows.length) { CH.empty($('drawer-table'), '当前筛选条件下无明细记录'); return; }
    $('drawer-table').innerHTML = tableHTML(d.rows, DRAWER_COLS[state.detailSource]);
  } catch (e) {
    CH.failed($('drawer-table'), e.message);
  }
}
function openDrawer() { $('modal').classList.remove('hidden'); state.drawerPage = 1; loadDrawer(); }

/* ------------------------------------------------------------ 事件 */
function bind() {
  document.addEventListener('change', e => {
    const sel = e.target.closest('select[data-filter]');
    if (!sel) return;
    state[sel.dataset.filter] = sel.value;
    state.detailPage = 1;
    syncControls(); loadAll();
  });
  $('region-tiles').addEventListener('click', e => {
    const t = e.target.closest('.region-tile');
    if (t) setRegion(t.dataset.region);
  });

  const tabs = [
    ['trend-erp-metric', 'metric', 'trendErpMetric', () => renderTrend('erp')],
    ['trend-pos-metric', 'metric', 'trendPosMetric', () => renderTrend('pos')],
    ['rank-amount-side', 'side', 'rankAmountSide', () => renderRank('amount')],
    ['rank-boxes-side', 'side', 'rankBoxesSide', () => renderRank('boxes')],
    ['category-side', 'side', 'categorySide', () => renderCategory()],
    ['sku-side', 'side', 'skuSide', () => renderSKU()],
    ['ka-dist-side', 'side', 'kaDistSide', () => renderKADist()],
    ['ka-drill-side', 'side', 'kaDrillSide', () => renderKADrill()],
    ['detail-source', 'source', 'detailSource', () => { state.detailPage = 1; loadDetail(); }],
  ];
  tabs.forEach(([id, attr, key, render]) => {
    $(id).addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      state[key] = b.dataset[attr];
      syncControls();
      if (state.data) render();
    });
  });

  $('btn-reset').addEventListener('click', () => {
    Object.assign(state, DEFAULTS);
    Object.assign(state, {
      trendErpMetric: 'amount', trendPosMetric: 'amount',
      rankAmountSide: 'pos', rankBoxesSide: 'pos', categorySide: 'pos',
      skuSide: 'pos', kaDistSide: 'pos', kaDrillSide: 'pos',
      detailSource: 'pos', detailPage: 1,
    });
    syncControls(); loadAll(); toast('已重置为五大战区合计 · 全部月份');
  });
  $('btn-refresh').addEventListener('click', () => { loadAll(); toast('已重新查询 ETL 视图'); });
  $('btn-detail').addEventListener('click', openDrawer);
  $('btn-drawer').addEventListener('click', openDrawer);
  $('detail-prev').addEventListener('click', () => {
    if (state.detailPage > 1) { state.detailPage--; loadDetail(); }
  });
  $('detail-next').addEventListener('click', () => { state.detailPage++; loadDetail(); });
  $('drawer-close').addEventListener('click', () => $('modal').classList.add('hidden'));
  $('modal').addEventListener('click', e => { if (e.target.id === 'modal') $('modal').classList.add('hidden'); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') $('modal').classList.add('hidden');
  });
  $('page-prev').addEventListener('click', () => {
    state.drawerPage = Math.max(1, state.drawerPage - 1); loadDrawer();
  });
  $('page-next').addEventListener('click', () => { state.drawerPage++; loadDrawer(); });
  $('source-toggle').addEventListener('click', () => {
    state.detailSource = state.detailSource === 'pos' ? 'erp' : 'pos';
    state.drawerPage = 1; state.detailPage = 1;
    syncControls(); loadDrawer(); loadDetail();
  });
  let rt;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => {
      if (!state.data) return;
      renderTrend('erp'); renderTrend('pos'); renderRank('amount'); renderRank('boxes');
      renderCategory(); renderSKU(); renderKADist(); renderKADrill();
    }, 180);
  });
}

/* ------------------------------------------------------------ 启动 */
async function boot() {
  bind();
  try {
    const m = await api.meta();
    state.meta = m.data;
    fillSelects(m.data);
    syncControls();
    if (m.data.health.delivered_views_missing.length) {
      alertBox(`缺少已验收视图：${m.data.health.delivered_views_missing.join('、')}`);
    }
  } catch (e) {
    alertBox(`元数据加载失败：${e.message}`);
    status('接口异常', false);
    return;
  }
  await loadAll();
}
boot();
