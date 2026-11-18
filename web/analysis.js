/* =====================================================================
 * analysis.js — 综合经营分析 V2（减法版）
 *
 * 页面只有四个区域：
 *   A 顶部筛选（7 个全局筛选，作用于全部区块与明细）
 *   B 核心 KPI（ERP / POS 两张卡，主指标可切换 金额 / 盒数 / TE）
 *   C 经营趋势 + 维度分析（趋势 1 张；维度分析按 6 个 Tab 动态换内容）
 *   D 经营明细（随维度换字段，支持筛选、排序、搜索、分页、下钻）
 *
 * V2 删除的重复内容（不再请求、不再渲染、不留空白卡）：
 *   维度成员卡 / 独立金额占比环图 / 数量排名独立图 / ERP-POS 份额双条图 /
 *   ERP-POS 对照表 / 战区维度里的 KA·客户分析 / 品类维度的品类结构自映射。
 *
 * 前端只做展示：不重算指标、不生成数据、不把 ERP 与 POS 金额相加。
 * ===================================================================== */
import { api } from './api.js';
import * as CH from './charts.js';

const $ = (id) => document.getElementById(id);
const DIMS = ['region', 'channel', 'ka', 'customer', 'category', 'sku'];
const DIM_STATE_KEY = {
  region: 'regions', channel: 'channel', ka: 'ka',
  customer: 'customer', category: 'category', sku: 'sku',
};
const DEFAULTS = { regions: '', months: '', channel: '', ka: '', customer: '', category: '', sku: '' };

/**
 * 每个维度只声明「真正需要」的分析块；没有声明的块直接隐藏，不占位、不画空图。
 *   rank   : 销售排名（金额 / 盒数通过指标切换复用同一张图）
 *   struct : 结构 / 贡献度，kind 决定用哪套渲染
 */
const LAYOUT = {
  region: {
    rank: { title: '战区销售排名' },
    struct: { kind: 'category', title: '战区销售结构 · 品类构成' },
    sub: '排名看规模，结构看品类构成；点条形可下钻为战区筛选',
  },
  channel: {
    rank: { title: '渠道销售排名' },
    struct: { kind: 'donut', title: '渠道贡献度' },
    // POS 只覆盖「商超/终端」一个渠道，默认落在 ERP 才能同时看到三个渠道；
    // 切到 POS 仍然可用（并在脚注说明其余渠道无终端记录）。
    defaultSide: 'erp',
    sub: '排名看规模，贡献度看该渠道在系统内的份额；POS 仅覆盖商超/终端',
  },
  ka: {
    rank: { title: 'KA 系统销售排名' },
    struct: { kind: 'donut', title: 'KA 系统贡献度' },
    sub: 'POS 单侧口径：ERP 无 KA 字段，不做映射猜测',
  },
  customer: {
    rank: { title: '客户销售排名' },
    struct: { kind: 'donut', title: '客户贡献' },
    sub: 'ERP 单侧口径：POS 事实表无客户字段，不跨系统推断',
  },
  category: {
    rank: null,
    struct: { kind: 'donut', title: '品类销售结构', full: true },
    sub: '品类A / 品类B / 品类C的占比见结构图，逐月走势见上方趋势',
  },
  sku: {
    rank: { title: 'SKU 销售 TOP' },
    struct: { kind: 'sku-attr', title: '粒数与规格分析' },
    sub: 'TOP 看单品贡献，粒数 / 规格看包装结构',
  },
};

const state = {
  ...DEFAULTS,
  dimension: 'region',
  kpiMetric: 'amount',
  trendSide: 'pos', trendMetric: 'amount',
  rankSide: 'pos', rankMetric: 'amount',
  structSide: 'pos', skuAttr: 'pieces',
  detailSource: 'pos', detailPage: 1,
  detailSort: '', detailOrder: 'asc', detailQ: '',
  drawerPage: 1,
  meta: null, data: null, busy: false, pending: false,
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
  const p = { dimension: state.dimension };
  for (const k of Object.keys(DEFAULTS)) if (state[k] !== '') p[k] = state[k];
  return p;
}

/** 维度 + 筛选写回地址栏：刷新 / 分享 / 旧 region.html 跳转都不丢条件。 */
function syncUrl() {
  const url = new URL(location.href);
  url.searchParams.set('dimension', state.dimension);
  for (const k of Object.keys(DEFAULTS)) {
    if (state[k] === '') url.searchParams.delete(k);
    else url.searchParams.set(k, state[k]);
  }
  history.replaceState(null, '', url);
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
function pct(v) { return v === null || v === undefined ? '—' : `${CH.fmtNum(v, 1)}%`; }
function escapeText(v) {
  return String(v ?? '—').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
}
function legend(hostId, series) {
  $(hostId).innerHTML = series.map(s =>
    `<span><i style="background:${s.color}"></i>${escapeText(s.name)}</span>`).join('');
}
/* 趋势多序列用色：5 条线叠在一起要能分辨，但只允许冷色系——
   绿 / 橙 / 红按设计规范属于状态色（涨跌、预警），拿来区分战区会被误读成好坏。 */
const SERIES_COLORS = ['#1a6ff0', '#4aa3e8', '#7c63d6', '#6f7c95', '#a9b8cc']
/* 排名条形用蓝色梯度：长度已经表达大小，颜色不再承担第二层含义 */
const RANK_SHADES = ['#1a6ff0', '#3c85f3', '#5797f5', '#71a9f7', '#8bbaf9',
                     '#a3c9fb', '#b9d7fc', '#cbe2fd', '#dcedfe', '#eaf4ff'];;
/** 环图分片用色：相邻扇区不同色即可，同样避开状态色。 */
const DONUT_COLORS = ['#1a6ff0', '#4aa3e8', '#7c63d6', '#6f7c95', '#a9b8cc', '#9cc2fb', '#8d9bb3'];
function currentDim() { return state.data?.dimension || { label: '维度', has_erp: true, has_pos: true, note: '' }; }
function sideOk(side) {
  const d = currentDim();
  return side === 'erp' ? d.has_erp !== false : d.has_pos !== false;
}
function sideName(side) { return side === 'erp' ? 'ERP' : 'POS'; }

/** 单侧维度（KA 只有 POS、客户只有 ERP）自动落到可用侧，避免整片空图。 */
const SIDE_KEYS = ['trendSide', 'rankSide', 'structSide', 'detailSource'];
function alignSides() {
  SIDE_KEYS.forEach(k => {
    if (state[k] === 'erp' && currentDim().has_erp === false) state[k] = 'pos';
    else if (state[k] === 'pos' && currentDim().has_pos === false) state[k] = 'erp';
  });
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
    s.appendChild(opt('', '五大战区合计'));   // 完整范围写在标题徽章与页脚，下拉里不截断
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

const TABS = {
  'kpi-metric': ['kpiMetric', 'metric'],
  'trend-side': ['trendSide', 'side'],
  'trend-metric': ['trendMetric', 'metric'],
  'rank-side': ['rankSide', 'side'],
  'rank-metric': ['rankMetric', 'metric'],
  'struct-side': ['structSide', 'side'],
  'struct-mode': ['skuAttr', 'attr'],
  'detail-source': ['detailSource', 'source'],
};

function syncControls() {
  document.querySelectorAll('select[data-filter]').forEach(s => {
    const k = s.dataset.filter;
    if (k in state && [...s.options].some(o => o.value === state[k])) s.value = state[k];
  });
  const d = currentDim();
  const ok = { erp: d.has_erp !== false, pos: d.has_pos !== false };
  Object.entries(TABS).forEach(([id, [key, attr]]) => {
    document.querySelectorAll(`#${id} button`).forEach(b => {
      const v = b.dataset[attr];
      const dimSwitch = attr === 'side' || attr === 'source';
      const unavailable = dimSwitch && v && v in ok && !ok[v];
      b.disabled = unavailable;
      b.title = unavailable ? `${d.label}维度在 ${v.toUpperCase()} 侧无对应字段` : '';
      b.classList.toggle('on', v === state[key] && !unavailable);
    });
  });
  document.querySelectorAll('#dim-tabs button').forEach(b =>
    b.classList.toggle('on', b.dataset.dim === state.dimension));

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
  $('chip-dim').textContent = `分析维度：${currentDim().label}`;
}

/* ------------------------------------------------------------ 加载 */
const HOSTS = ['chart-trend', 'chart-rank', 'chart-struct', 'detail-table'];
const FEET = ['foot-trend', 'foot-rank', 'foot-struct'];

function markLoading() {
  CH.hideTooltip();   // 重新取数时清掉上一次悬停的浮层，避免它跟着新数据留在屏幕上
  HOSTS.forEach(id => CH.loading($(id)));
  FEET.forEach(id => { $(id).textContent = ''; });
  status('查询中…', false);
}

async function loadAll() {
  if (state.busy) { state.pending = true; return; }
  state.busy = true; state.pending = false;
  markLoading(); alertBox(null);
  try {
    const res = await getJSON('/api/analysis', params());
    state.data = res.data;
    if (state._alignedDim !== state.dimension) {
      // 维度自带的默认系统（目前只有渠道，POS 只有商超/终端一个渠道）
      const pref = LAYOUT[state.dimension]?.defaultSide;
      if (pref) {
        state.trendSide = pref; state.rankSide = pref;
        state.structSide = pref; state.detailSource = pref;
      }
      alignSides();
      state._alignedDim = state.dimension;
      syncControls();
    }
    fillMonths(res.data.capabilities.months_all);
    renderAll(res.meta);
    status('数据已更新');
  } catch (e) {
    alertBox(`数据查询失败：${e.message}。请确认后端服务已启动、ETL 数据库可读。`);
    HOSTS.forEach(id => CH.failed($(id), e.message));
    status('查询失败', false);
  } finally {
    state.busy = false;
    if (state.pending) { state.pending = false; setTimeout(() => loadAll(), 0); return; }
    loadDetail();
  }
}

function renderAll(meta) {
  renderDimensionUI();
  renderKpi();
  renderTrend();
  renderRank();
  renderStructure();
  renderCapabilities();
  $('chip-period').textContent = 'Y26 · 2026 年 1–10 月';
  $('chip-period').title = meta.period;
}

/* ------------------------------------------------------------ 维度说明 */
function renderDimensionUI() {
  const d = currentDim();
  const lay = LAYOUT[state.dimension] || LAYOUT.region;
  $('chip-dim').textContent = `分析维度：${d.label}`;
  const sides = [d.has_erp ? 'ERP' : null, d.has_pos ? 'POS' : null].filter(Boolean).join(' + ');
  const warn = !d.has_erp || !d.has_pos ? ` <span class="warn">仅 ${sides}</span>` : '';
  $('dim-hint').innerHTML = `<b>${escapeText(d.label)}</b> · ${escapeText(d.note)}${warn}`;
  $('dim-area-title').textContent = `维度分析 · ${d.label}`;
  $('dim-area-sub').textContent = lay.sub;
  $('h-trend').textContent = `${d.label}月度趋势`;
  $('trend-sub').textContent = `${d.label}金额 TOP 成员 · 完整 Y26 1–10 月 · 所选月份之外断开`;

  // 排名块：没有就整块隐藏，不占据版面
  $('card-rank').classList.toggle('hidden', !lay.rank);
  $('dim-area').classList.toggle('single', !lay.rank);
  $('h-rank').textContent = lay.rank ? lay.rank.title : '';

  $('h-struct').textContent = lay.struct.title;
  $('struct-mode').classList.toggle('hidden', lay.struct.kind !== 'sku-attr');
}

/* ------------------------------------------------------------ B 核心 KPI */
const KPI_LABEL = { amount: '金额', boxes: '盒数', te: 'TE' };

function kpiMainValue(side, o, metric) {
  if (metric === 'amount') return CH.fmtMoney(o.sales_amount);
  if (metric === 'boxes') return CH.fmtCompact(o.sales_boxes, '盒');
  return CH.fmtCompact(side === 'erp' ? o.shipment_te : o.sales_te, 'TE');
}

function kpiCard(side, k) {
  const o = k[side], isErp = side === 'erp', metric = state.kpiMetric;
  const mom = o.mom ? o.mom[metric] : null;
  const cls = CH.growthClass(mom === null || mom === undefined ? null : mom);
  const momTxt = mom === null || mom === undefined ? '—' : CH.fmtPct(mom, 1);
  const aux = [];
  if (metric !== 'amount') aux.push(['金额', CH.fmtMoney(o.sales_amount)]);
  if (metric !== 'boxes') aux.push(['盒数', CH.fmtCompact(o.sales_boxes, '盒')]);
  if (metric !== 'te') aux.push(['TE', CH.fmtCompact(isErp ? o.shipment_te : o.sales_te, 'TE')]);
  if (isErp) {
    aux.push(['毛利额', CH.fmtMoney(o.gross_profit)]);
    aux.push(['毛利率', `${CH.fmtNum(o.gross_margin_pct, 2)}%`]);
  } else {
    aux.push(['记录数', `${CH.fmtNum(o.rows_count)} 行`]);
  }
  return `<article class="kpi-card has-icon sys-${side}">
    <span class="kpi-icon${isErp ? '' : ' tone-green'}">${isErp ? '出' : '销'}</span>
    <div class="kpi-body">
      <div class="kpi-top">
        <span class="kpi-sys">当前维度 · ${isErp ? 'ERP 出货' : 'POS 终端'}</span>
        <span class="kpi-mom ${cls}" style="margin-left:auto">${momTxt}</span>
      </div>
      <div class="kpi-main">
        <span class="kpi-value">${kpiMainValue(side, o, metric)}</span>
        <span class="kpi-caption">${KPI_LABEL[metric]} · 环比 ${k.prev_month} 月</span>
      </div>
      <div class="kpi-aux">
        ${aux.map(([label, v]) => `<span><em>${label}</em><b>${v}</b></span>`).join('')}
      </div>
    </div>
  </article>`;
}

function renderKpi() {
  const k = state.data.kpi;
  $('kpi-row').innerHTML = kpiCard('erp', k) + kpiCard('pos', k);
  // 维度上下文：这页看的是「维度内谁贡献」，集团总量在驾驶舱看，避免两页读数重复。
  // 成员数 / 合计一律取 overview（全量成员 + 该侧合计），不能用排名数组——
  // 排名是 TOP10，用它当分母会把「共 N 个成员」「合计」两处同时说错。
  const d = currentDim();
  const side = d.has_pos ? 'pos' : 'erp';
  const ov = state.data.overview || {};
  const key = side === 'pos' ? 'pos_amount' : 'erp_amount';
  const total = ov[`${side}_total_amount`];
  const members = (ov.items || []);
  const ranked = members
    .filter(x => x[key] !== null && x[key] !== undefined)
    .sort((a, b) => (b[key] || 0) - (a[key] || 0));
  if (!ranked.length || !total) { $('kpi-sub').textContent = `${d.label}维度在${side === 'pos' ? 'POS' : 'ERP'} 侧无可排名成员`; return; }
  const top = ranked[0];
  const share = total ? (top[key] * 100) / total : null;
  const silent = members.length - ranked.length;
  const missing = silent > 0
    ? ` · 其中 ${silent} 个成员无${side === 'pos' ? ' POS ' : ' ERP '}记录（不按 0 计）`
    : '';
  /* 中英文之间留一个空格，避免出现「SKU维度」这种黏连 */
  const dimName = /[A-Za-z]$/.test(d.label) ? `${d.label} ` : d.label;
  $('kpi-sub').textContent =
    `${dimName}维度共 ${ov.member_count || members.length} 个成员 · ` +
    `TOP1 ${top.label || top.name} 占 ${CH.fmtNum(share || 0, 1)}%` +
    `（${CH.fmtCompact(top[key], '¥')}）${missing}` +
    ` · 合计 ${CH.fmtCompact(total, '¥')}（${side === 'pos' ? 'POS' : 'ERP'} 口径）· ERP 与 POS 不相加`;
}

/* ------------------------------------------------------------ C-1 趋势 */
function renderTrend() {
  const host = $('chart-trend'), foot = $('foot-trend');
  const d = currentDim();
  const side = state.trendSide, metric = state.trendMetric;
  if (!sideOk(side)) {
    CH.empty(host, `${d.label}维度在 ${sideName(side)} 侧无对应字段`);
    $('legend-trend').innerHTML = '';
    foot.textContent = `${d.label}维度不适用于 ${sideName(side)}`;
    return;
  }
  const m = state.data.monthly;
  const months = m.months;
  const matrix = m[side] || {};
  const chosen = state.months ? Number(state.months) : null;
  const series = m.members.map((name, i) => ({
    name: m.labels[i] || name,
    color: SERIES_COLORS[i % SERIES_COLORS.length],
    values: (matrix[name]?.[metric] || []).map((v, j) =>
      (chosen !== null && months[j] !== chosen) ? null : v),
  }));
  if (!series.some(s => s.values.some(v => v !== null && v !== undefined))) {
    CH.empty(host, side === 'pos'
      ? '该筛选条件下 POS 无终端销售数据（不按 0 计）'
      : '该筛选条件下 ERP 无出货数据');
    $('legend-trend').innerHTML = '';
    foot.textContent = '无记录月份保持断点，不补零、不连线';
    return;
  }
  CH.lineChart(host, {
    labels: months.map(x => `${x}月`), height: 196, area: false,
    tipPrefix: `${side === 'erp' ? 'ERP 出货' : 'POS 销售'} · `,
    axisFormat: metric === 'amount' ? (v) => CH.fmtCompact(v, '¥') : (v) => CH.fmtCompact(v),
    tipFormat: metric === 'amount' ? CH.fmtMoney : (v) => CH.fmtCompact(v, '盒'),
    series,
  });
  // 图例只列真正画出线的成员：该侧无记录的成员留在排名/明细里查，不制造「有这条线」的错觉
  const drawn = series.filter(s => s.values.some(v => v !== null && v !== undefined));
  const idle = series.filter(s => !drawn.includes(s));
  legend('legend-trend', drawn);
  foot.textContent = `${side === 'erp' ? 'ERP Sell-in' : 'POS Sell-out'} · ${metric === 'amount' ? '金额' : '盒数'}`
    + ` · 只画金额 TOP ${m.members.length} 成员`
    + (idle.length ? `（${idle.map(x => x.name).join('、')} 在该侧无记录，未画线）` : '')
    + ` · ${state.data.capabilities.month_scope} · 未选月份断开`;
}

/* ------------------------------------------------------------ C-2 排名（金额/盒数 合并为指标切换） */
function renderRank() {
  const lay = LAYOUT[state.dimension];
  if (!lay || !lay.rank) {
    // 该维度不需要排名块：整块隐藏，同时把 loading 占位清空，
    // 避免隐藏卡里残留「正在查询数据」状态（既占内存也会误导排查）
    $('chart-rank').innerHTML = '';
    $('foot-rank').textContent = '';
    return;
  }
  const d = currentDim(), side = state.rankSide, metric = state.rankMetric;
  const host = $('chart-rank'), foot = $('foot-rank');
  if (!sideOk(side)) {
    CH.empty(host, `${d.label}维度在 ${sideName(side)} 侧无对应字段`);
    foot.textContent = `${d.label}维度不适用于 ${sideName(side)}`;
    return;
  }
  const all = state.data.ranking.ranks[`${side}_${metric}`] || [];
  // 该侧无记录的成员不画「0 长条」：直接从图里去掉，在脚注里点名，避免看起来像数值为 0
  const rows = all.filter(r => r.value !== null && r.value !== undefined);
  const idle = all.filter(r => r.value === null || r.value === undefined);
  if (!rows.length) {
    CH.empty(host, `${d.label}维度在 ${sideName(side)} 侧无可排名记录（不按 0 计）`);
    foot.textContent = '';
    return;
  }
  const fmt = metric === 'amount' ? CH.fmtMoney : (v) => CH.fmtCompact(v, '盒');
  CH.hbarChart(host, {
    items: rows.map((r, i) => ({
      label: `${r.rank}. ${r.label || r.name}`, value: r.value, badge: r.share_pct, raw: r,
      color: RANK_SHADES[i % RANK_SHADES.length],
      tipExtra: `<div class="tt-row"><span>系统内份额</span><b>${pct(r.share_pct)}</b></div>`,
    })),
    rowH: 20, labelW: 104, valueW: 70, badgeW: 42, maxLabel: 14, clickable: true,
    badgeFormat: (v) => `${CH.fmtNum(v, 1)}%`,
    valueFormat: fmt, tipFormat: fmt,
    tipLabel: `${sideName(side)}${metric === 'amount' ? '金额' : '盒数'}`,
    onClick: (it) => setMember(it.raw.name),
  });
  foot.textContent = `${sideName(side)} ${metric === 'amount' ? '金额' : '盒数'}`
    + ` · 共 ${state.data.overview.member_count} 个${d.label}成员`
    + (idle.length ? `（${idle.map(x => x.label || x.name).join('、')} 在该侧无记录，未参与排名）` : '')
    + ' · 点击条形下钻';
}

function setMember(name) {
  const key = DIM_STATE_KEY[state.dimension];
  state[key] = state[key] === name ? '' : name;
  state.detailPage = 1;
  syncUrl(); syncControls(); loadAll();
  toast(state[key] ? `已下钻：${name}` : '已取消下钻');
}

/* ------------------------------------------------------------ C-2 结构 / 贡献度 */
function renderStructure() {
  const kind = LAYOUT[state.dimension]?.struct.kind;
  if (kind === 'category') return renderStructCategory();
  if (kind === 'sku-attr') return renderStructSkuAttr();
  return renderStructDonut();
}

/** 维度 × 品类构成（仅战区使用）：一眼看清每个战区的品类A/品类B/品类C配比。 */
function renderStructCategory() {
  const d = currentDim(), side = state.structSide;
  const host = $('chart-struct'), foot = $('foot-struct');
  if (!sideOk(side)) {
    CH.empty(host, `${d.label}维度在 ${sideName(side)} 侧无对应字段`);
    $('legend-struct').innerHTML = '';
    foot.textContent = `${d.label}维度不适用于 ${sideName(side)}`;
    return;
  }
  const s = state.data.structure;
  const series = s.categories.map((cat, i) => ({
    name: cat, color: CH.PALETTE[i % CH.PALETTE.length], values: s[side][cat] || [],
  }));
  if (!series.some(se => se.values.some(v => v !== null && v !== undefined))) {
    CH.empty(host, '当前筛选条件下无品类销售数据');
    $('legend-struct').innerHTML = '';
    foot.textContent = '';
    return;
  }
  CH.groupedBar(host, { labels: s.labels.map(x => String(x).slice(0, 6)), series, height: 168 });
  legend('legend-struct', series);
  foot.textContent = `${sideName(side)} · 金额 TOP ${s.labels.length} 成员的品类构成`
    + ' · 只展示当前筛选内成员，不合并其他战区';
}

/** 维度贡献度（渠道 / KA / 客户 / 品类）：单个系统的份额环图。 */
function renderStructDonut() {
  const d = currentDim(), side = state.structSide;
  const host = $('chart-struct'), foot = $('foot-struct');
  $('legend-struct').innerHTML = '';
  if (!sideOk(side)) {
    CH.empty(host, `${d.label}维度在 ${sideName(side)} 侧无对应字段`);
    foot.textContent = `${d.label}维度不适用于 ${sideName(side)}`;
    return;
  }
  const key = `${side}_amount`;
  const all = state.data.overview.items
    .filter(x => x[key] !== null && x[key] !== undefined && x[key] > 0)
    .sort((a, b) => b[key] - a[key]);
  if (!all.length) {
    CH.empty(host, '当前筛选条件下无贡献度数据（不按 0 计）');
    foot.textContent = '';
    return;
  }
  const TOP = 7;
  const top = all.slice(0, TOP);
  const items = top.map((x, i) => ({
    label: x.label, value: x[key], color: DONUT_COLORS[i % DONUT_COLORS.length],
  }));
  const rest = all.slice(TOP);
  if (rest.length) {
    items.push({ label: `其余 ${rest.length} 个`, value: rest.reduce((a, b) => a + b[key], 0),
                 color: '#c3d3e8' });
  }
  const total = all.reduce((a, b) => a + b[key], 0);
  const full = LAYOUT[state.dimension]?.struct.full === true;
  CH.donutChart(host, {
    items, size: full ? 196 : 148, legendMode: 'amount',
    centerValue: CH.fmtMoney(total), centerLabel: `${sideName(side)} 合计`,
  });
  if (full) {
    // 整行独享时把图例换成「金额 / 占比 / 盒数」小表：同一视角的读法，不新增图表
    host.querySelector('.donut-legend')?.remove();
    const tbl = document.createElement('table');
    tbl.className = 'struct-tbl';
    tbl.innerHTML = `<thead><tr><th>${escapeText(d.label)}</th><th>金额</th><th>占比</th><th>盒数</th></tr></thead>
      <tbody>${all.map(x => `<tr><td>${escapeText(x.label)}</td>
        <td>${CH.fmtMoney(x[key])}</td>
        <td>${pct(x[`${side}_share_pct`])}</td>
        <td>${CH.fmtCompact(x[`${side}_boxes`], '盒')}</td></tr>`).join('')}</tbody>`;
    host.querySelector('.donut-wrap')?.appendChild(tbl);
  }
  foot.textContent = `${sideName(side)} · 分母为当前筛选内该侧合计`
    + (rest.length ? ` · 「其余 ${rest.length} 个」为同系统内剩余成员合计，占比明细见排名与明细` : '')
    + ' · 点击排名条形可下钻';
}

/** 粒数与规格（仅 SKU 维度）：一张卡用指标切换看两种包装结构。 */
function renderStructSkuAttr() {
  const d = currentDim(), side = state.structSide, attr = state.skuAttr;
  const host = $('chart-struct'), foot = $('foot-struct');
  $('legend-struct').innerHTML = '';
  if (!sideOk(side)) {
    CH.empty(host, `SKU 维度在 ${sideName(side)} 侧无对应字段`);
    foot.textContent = `SKU 维度不适用于 ${sideName(side)}`;
    return;
  }
  const rows = (state.data.sku[attr] || {})[side] || [];
  if (!rows.length) {
    CH.empty(host, `当前筛选条件下无${attr === 'pieces' ? '粒数' : '规格'}数据`);
    foot.textContent = '';
    return;
  }
  const fmt = CH.fmtMoney;
  CH.hbarChart(host, {
    items: rows.map((r, i) => ({
      label: r.label, value: r.amount, badge: r.share_pct,
      color: CH.PALETTE[i % CH.PALETTE.length],
      tipExtra: `<div class="tt-row"><span>盒数</span><b>${CH.fmtCompact(r.boxes, '盒')}</b></div>`
        + `<div class="tt-row"><span>SKU 数</span><b>${CH.fmtNum(r.skus)}</b></div>`,
    })),
    rowH: 17, labelW: 74, valueW: 68, badgeW: 44, maxLabel: 9,
    badgeFormat: (v) => `${CH.fmtNum(v, 1)}%`,
    valueFormat: fmt, tipFormat: fmt,
    tipLabel: `${attr === 'pieces' ? '粒数' : '规格'}金额`,
  });
  foot.textContent = `${sideName(side)} · 按${attr === 'pieces' ? '每盒粒数（pieces_per_box）' : '果径规格（specification）'}分组`
    + ` · 共 ${rows.length} 组 · 分子分母同为该侧合计`;
}

/* ------------------------------------------------------------ 能力边界 */
function renderCapabilities() {
  const cap = state.data.capabilities;
  const missing = cap.missing || [];
  $('capability-count').textContent = `不支持 ${missing.length} 项`;
  const matrix = cap.dimension_matrix || [];
  const matrixHtml = `<div class="cap-item cap-matrix"><h4>六维度可用性</h4>
    <table class="matrix"><thead><tr><th>维度</th><th>ERP</th><th>POS</th></tr></thead><tbody>
    ${matrix.map(m => `<tr><td>${escapeText(m.label)}</td>
      <td class="${m.erp ? '' : 'no'}">${m.erp ? '可用' : '无字段'}</td>
      <td class="${m.pos ? '' : 'no'}">${m.pos ? '可用' : '无字段'}</td></tr>`).join('')}
    </tbody></table></div>`;
  const items = missing.map(m => `<div class="cap-item">
      <h4>${escapeText(m.title)}</h4>
      <p><b>原因</b> ${escapeText(m.reason)}</p>
      <p><b>处理</b> ${escapeText(m.handling)}</p>
    </div>`).join('');
  const notes = `<div class="cap-item cap-notes"><h4>口径说明</h4>
    <ul>${(cap.notes || []).map(n => `<li>${escapeText(n)}</li>`).join('')}</ul></div>`;
  $('capability-list').innerHTML = matrixHtml + items + notes;
}

/* ------------------------------------------------------------ D 经营明细 */
const COL_LABEL = {
  year: '年', month: '月', full_date: '日期', region_name_std: '战区',
  sku_code: 'SKU 编码', sku_name: 'SKU 名称', category: '品类', channel_type: '渠道',
  customer_id: '客户编码', customer_name: '客户', ka_system: 'KA 系统', store_name: '门店',
  sales_boxes: '盒数', sales_amount: '金额', shipment_te: 'TE', sales_te: 'TE',
  cost_amount: '成本', gross_profit: '毛利',
};
const MONEY_COLS = new Set(['sales_amount', 'cost_amount', 'gross_profit']);

function orderColumns(cols) {
  const order = state.data?.detail_columns?.order || [];
  const usable = cols.filter(c => c !== 'year');
  const head = order.filter(c => usable.includes(c));
  const rest = usable.filter(c => !head.includes(c));
  const dateCols = rest.filter(c => c === 'full_date');
  const tail = rest.filter(c => c !== 'full_date');
  return [...dateCols, ...head, ...tail];
}

function cell(col, v) {
  if (v === null || v === undefined || v === '') return '—';
  if (MONEY_COLS.has(col)) return CH.fmtMoney(v);
  if (col === 'sales_boxes') return CH.fmtNum(v);
  if (col === 'shipment_te' || col === 'sales_te') return CH.fmtCompact(v);
  if (col === 'month') return `${v} 月`;
  return escapeText(v);
}

function renderDetail(hostId, payload, sortable) {
  const host = $(hostId);
  const rows = payload.rows || [];
  if (!rows.length) {
    host.innerHTML = '<div class="state-box">当前筛选条件下无明细记录（不补零、不生成数据）</div>';
    return;
  }
  const cols = orderColumns(payload.columns || Object.keys(rows[0]));
  const hl = payload.detail_columns?.highlight || state.data?.detail_columns?.highlight;
  const sortableSet = new Set(sortable ? (payload.sortable || []) : []);
  const head = cols.map(c => {
    const can = sortableSet.has(c);
    const dir = state.detailSort === c ? state.detailOrder : '';
    return `<th data-col="${c}" class="${c === hl ? 'hl' : ''}${can ? ' sortable' : ''}${dir ? ' sorted' : ''}"
      ${can ? `title="点击按${COL_LABEL[c] || c}排序"` : ''}>${escapeText(COL_LABEL[c] || c)}`
      + (can ? `<i class="sort-mark">${dir === 'asc' ? '▲' : dir === 'desc' ? '▼' : '⇅'}</i>` : '')
      + `</th>`;
  }).join('');
  host.innerHTML = `<table class="data"><thead><tr>${head}</tr></thead><tbody>
    ${rows.map(r => `<tr>${cols.map(c =>
      `<td class="${c === hl ? 'hl' : ''}">${cell(c, r[c])}</td>`).join('')}</tr>`).join('')}
  </tbody></table>`;
}

function detailParams(limit, page) {
  const p = { ...params(), source: state.detailSource, page, limit };
  if (state.detailSort) { p.sort = state.detailSort; p.order = state.detailOrder; }
  if (state.detailQ) p.q = state.detailQ;
  return p;
}

async function loadDetail() {
  try {
    const res = await getJSON('/api/analysis/detail', detailParams(10, state.detailPage));
    const d = res.data;
    d.detail_columns = state.data?.detail_columns;
    renderDetail('detail-table', d, true);
    const pages = Math.max(1, Math.ceil((d.total || 0) / (d.page_size || 10)));
    const parts = [
      state.detailSource === 'pos' ? 'POS 终端销售' : 'ERP 出货',
      `共 ${CH.fmtNum(d.total)} 行`,
      `第 ${d.page} / ${pages} 页`,
    ];
    if (state.detailQ) parts.push(`搜索「${state.detailQ}」`);
    if (state.detailSort) parts.push(`按${COL_LABEL[state.detailSort] || state.detailSort}${state.detailOrder === 'asc' ? '升序' : '降序'}`);
    parts.push('高亮列为当前维度');
    $('detail-count').textContent = parts.join(' · ');
    $('detail-page').textContent = `${d.page} / ${pages}`;
    $('detail-sort-hint').textContent = state.detailQ
      ? `关键词已生效 · 点击表头可排序` : '点击表头可排序';
    $('detail-q-clear').classList.toggle('hidden', !state.detailQ);
    state._detailPages = pages;
    $('detail-sub').textContent = `字段按「${currentDim().label}」维度排列`
      + ' · 沿用顶部全局筛选 · 支持排序 / 搜索 / 分页 / 下钻';
  } catch (e) {
    $('detail-table').innerHTML = `<div class="state-box">明细加载失败：${escapeText(e.message)}</div>`;
    $('detail-count').textContent = '';
  }
}

async function loadDrawer() {
  try {
    const res = await getJSON('/api/analysis/detail', detailParams(50, state.drawerPage));
    const d = res.data;
    d.detail_columns = state.data?.detail_columns;
    renderDetail('drawer-table', d, false);
    const pages = Math.max(1, Math.ceil((d.total || 0) / (d.page_size || 50)));
    $('drawer-count').textContent = `共 ${CH.fmtNum(d.total)} 行`
      + (state.detailQ ? ` · 搜索「${state.detailQ}」` : '');
    $('page-label').textContent = `${d.page} / ${pages}`;
    $('drawer-sub').textContent = `${state.detailSource === 'pos' ? 'POS 终端销售' : 'ERP 出货'}`
      + ` · 维度：${currentDim().label}`;
    state._drawerPages = pages;
  } catch (e) {
    $('drawer-table').innerHTML = `<div class="status">明细加载失败：${escapeText(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------ 事件 */
function rerenderCharts() {
  if (!state.data) return;
  renderDimensionUI();
  renderKpi();
  renderTrend(); renderRank(); renderStructure();
}

function bind() {
  $('dim-tabs').addEventListener('click', e => {
    const b = e.target.closest('button[data-dim]');
    if (!b || b.dataset.dim === state.dimension) return;
    state.dimension = b.dataset.dim;
    state.detailPage = 1;
    syncUrl(); syncControls(); loadAll();
    toast(`已切换到「${b.textContent.replace(/(POS|ERP)$/, '')}」维度`);
  });

  document.querySelectorAll('select[data-filter]').forEach(sel => {
    sel.addEventListener('change', () => {
      state[sel.dataset.filter] = sel.value;
      state.detailPage = 1;
      syncUrl(); syncControls(); loadAll();
    });
  });

  Object.entries(TABS).forEach(([id, [key, attr]]) => {
    $(id).addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b || !b.dataset[attr] || b.disabled) return;
      state[key] = b.dataset[attr];
      syncControls();
      if (key === 'detailSource') loadDetail();
      else rerenderCharts();
    });
  });

  $('btn-reset').addEventListener('click', () => {
    Object.assign(state, DEFAULTS);
    state.detailPage = 1; state.detailQ = ''; state.detailSort = ''; state.detailOrder = 'asc';
    $('detail-q').value = '';
    syncUrl(); syncControls(); loadAll();
    toast('已重置筛选（维度保持不变）');
  });
  $('btn-refresh').addEventListener('click', () => loadAll());

  // 明细：搜索（防抖）+ 排序 + 分页
  let qTimer = null;
  $('detail-q').addEventListener('input', e => {
    const v = e.target.value.trim();
    clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      state.detailQ = v; state.detailPage = 1; loadDetail();
    }, 320);
  });
  $('detail-q-clear').addEventListener('click', () => {
    state.detailQ = ''; state.detailPage = 1; $('detail-q').value = ''; loadDetail();
  });
  $('detail-table').addEventListener('click', e => {
    const th = e.target.closest('th.sortable');
    if (!th) return;
    const col = th.dataset.col;
    if (state.detailSort === col) state.detailOrder = state.detailOrder === 'asc' ? 'desc' : 'asc';
    else { state.detailSort = col; state.detailOrder = 'desc'; }
    state.detailPage = 1;
    loadDetail();
  });
  $('detail-prev').addEventListener('click', () => {
    if (state.detailPage > 1) { state.detailPage--; loadDetail(); }
  });
  $('detail-next').addEventListener('click', () => {
    if (state.detailPage < (state._detailPages || 1)) { state.detailPage++; loadDetail(); }
  });

  // 抽屉
  $('btn-drawer').addEventListener('click', () => {
    $('modal').classList.remove('hidden'); state.drawerPage = 1; loadDrawer();
  });
  $('drawer-close').addEventListener('click', () => $('modal').classList.add('hidden'));
  $('modal').addEventListener('click', e => {
    if (e.target === $('modal')) $('modal').classList.add('hidden');
  });
  $('page-prev').addEventListener('click', () => {
    if (state.drawerPage > 1) { state.drawerPage--; loadDrawer(); }
  });
  $('page-next').addEventListener('click', () => {
    if (state.drawerPage < (state._drawerPages || 1)) { state.drawerPage++; loadDrawer(); }
  });
  $('source-toggle').addEventListener('click', () => {
    state.detailSource = state.detailSource === 'pos' ? 'erp' : 'pos';
    syncControls(); state.drawerPage = 1; state.detailPage = 1; loadDrawer(); loadDetail();
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') $('modal').classList.add('hidden');
  });
}

/* ------------------------------------------------------------ 启动 */
async function boot() {
  const q = new URL(location.href).searchParams;
  const initDim = q.get('dimension');
  if (DIMS.includes(initDim)) state.dimension = initDim;
  Object.keys(DEFAULTS).forEach(k => { if (q.get(k)) state[k] = q.get(k); });
  bind();
  status('初始化…', false);
  try {
    const m = await api.meta();
    state.meta = m.data;
    fillSelects(m.data);
    syncControls();
    await loadAll();
    syncUrl();
  } catch (e) {
    alertBox(`初始化失败：${e.message}。请确认 python3 run.py 已启动。`);
    status('初始化失败', false);
  }
}

boot();
