/* =====================================================================
 * sfa-profile.js — SFA 门店画像分析（阶段二）
 * 数据全部来自 /api/sfa/profile*，前端不重算任何比率、不补造字段。
 * 关键边界：未采集 ≠ 未执行；热门 SKU ≠ 全部在售；POS 无记录 ≠ 零销售。
 * ===================================================================== */
import { hideTooltip } from './charts.js';
import {
  donutCount, rankBar, rangeBar, fmtInt, fmtNum, emptyBox, loadingBox,
} from './boardcharts.js';

const $ = (id) => document.getElementById(id);

const state = {
  months: '', regions: '', marketDepartment: '', employee: '', ka: '', sku: '',
  page: 1, limit: 10, sort: '', order: 'asc', q: '',
  kaMode: 'count', hotMode: 'feedback',
  data: null, options: null,
};

const pct = (v, d = 1) => (v === null || v === undefined ? '—' : `${fmtNum(v, d)}%`);

function readUrl() {
  const u = new URLSearchParams(location.search);
  state.months = u.get('months') || '';
  state.regions = u.get('regions') || '';
  state.marketDepartment = u.get('market_department') || '';
  state.employee = u.get('employee') || '';
  state.ka = u.get('ka') || '';
  state.sku = u.get('sku') || '';
  state.page = Math.max(1, Number(u.get('page') || 1));
  state.sort = u.get('sort') || '';
  state.order = u.get('order') === 'desc' ? 'desc' : 'asc';
  state.q = u.get('q') || '';
}

function writeUrl() {
  const u = new URLSearchParams();
  [['months', state.months], ['regions', state.regions],
   ['market_department', state.marketDepartment], ['employee', state.employee],
   ['ka', state.ka], ['sku', state.sku], ['q', state.q]].forEach(([k, v]) => {
    if (v) u.set(k, v);
  });
  if (state.page > 1) u.set('page', String(state.page));
  if (state.sort) { u.set('sort', state.sort); u.set('order', state.order); }
  const qs = u.toString();
  history.replaceState(null, '', qs ? `${location.pathname}?${qs}` : location.pathname);
}

function params(extra = {}) {
  const p = { ...extra };
  if (state.months) p.months = state.months;
  if (state.regions) p.regions = state.regions;
  if (state.marketDepartment) p.market_department = state.marketDepartment;
  if (state.employee) p.employee = state.employee;
  if (state.ka) p.ka = state.ka;
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
  fillSelect($('f-md'), (opt.market_departments || []).map(m => ({ value: m, label: m })),
    state.marketDepartment, '全部市场部');
  const employees = (opt.employees || []).filter(e => !state.regions || e.region_name === state.regions);
  fillSelect($('f-employee'), employees.map(e => ({
    value: e.employee_id,
    label: `${e.employee_name}（${e.managed_store_count}店）`,
  })), state.employee, `全部客户经理（${employees.length}）`);
  fillSelect($('f-ka'), (opt.ka_systems || []).map(k => ({ value: k, label: k })),
    state.ka, `全部 KA（${(opt.ka_systems || []).length}）`);
  fillSelect($('f-sku'), (opt.skus || []).map(s => ({
    value: s.sku_code, label: `${s.sku_name}（${s.category}）`,
  })), state.sku, `全部热门 SKU（${(opt.skus || []).length}）`);
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

function renderKpi(k, data) {
  const topKa = (data.ka_dist || []).slice(0, 3).map(x => x.label).join(' / ');
  $('kpi-row').innerHTML = [
    kpiCard({ label: '门店总数', value: fmtInt(k.store_total), unit: '家',
      sub: `拜访口径去重 · 花名册 ${fmtInt(k.store_roster)} 家` }),
    kpiCard({ label: '覆盖 KA 系统', value: fmtInt(k.ka_count), unit: '个',
      sub: topKa || '无' }),
    kpiCard({ label: '门店等级已采集', value: pct(k.level_rate), accent: 'teal',
      sub: `${fmtInt(k.level_known)} / ${fmtInt(k.store_total)} 家` }),
    kpiCard({ label: '热门 SKU 反馈', value: fmtInt(k.hot_sku_stores), unit: '家',
      muted: !k.hot_sku_stores,
      sub: `采集率 ${pct((100 * k.hot_sku_stores) / (k.store_total || 1))}（按门店）` }),
    kpiCard({ label: '零售价记录', value: fmtInt(k.price_records), unit: '条', accent: 'purple',
      sub: `占有效拜访 ${pct(k.price_coverage)}` }),
    kpiCard({ label: 'SFA×POS 可关联门店', value: fmtInt(k.joint_evaluated), unit: '家',
      accent: 'amber', muted: !k.joint_evaluated,
      sub: `一致 ${fmtInt(k.joint_consistent)} 家 · 需人工核实 ${fmtInt(k.joint_evaluated - k.joint_consistent)} 家` }),
  ].join('');
}

/* ------------------------------------------------------------ KA 分布 */
function renderKa(data) {
  const host = $('chart-ka');
  const items = (data.ka_dist || []).map((x, i) => ({
    label: x.label, value: x.value,
    color: ['#2f6fe4', '#0e9f8a', '#e8a33d', '#7c63d6', '#df4640', '#4aa3e8', '#8fbf5a', '#e07a9b'][i % 8],
  }));
  if (!items.length) return emptyBox(host, '当前筛选条件下无门店');

  donutCount(host, {
    items, unit: '家', unitShort: ' 家', size: 148,
    legendMode: state.kaMode === 'share' ? 'share' : 'count',
    // 环心合计只统计图上实际画出的切片，避免与「门店总数」对不上
    centerValue: state.kaMode === 'share'
      ? `${items.length}` : fmtInt(items.reduce((a, b) => a + b.value, 0)),
    centerLabel: state.kaMode === 'share' ? '个 KA' : 'TOP KA 门店',
  });
  $('foot-ka').textContent = data.ka_other
    ? `展示门店数 TOP ${items.length}，另有 ${data.ka_other} 个 KA 合计 ${fmtInt(
      (data.stores || []).length - items.reduce((a, b) => a + b.value, 0))} 家未单列`
    : '全部 KA 系统均已列出（按门店数降序）';
}

/* ------------------------------------------------------------ 等级与渠道 */
function renderLevel(data) {
  const host = $('chart-level');
  const rows = data.level_dist || [];
  if (!rows.length) return emptyBox(host, '当前筛选条件下无等级数据');
  const total = rows.reduce((a, b) => a + b.value, 0);
  const max = Math.max(...rows.map(r => r.value), 1);
  host.innerHTML = rows.map((r) => {
    const share = total ? (r.value / total) * 100 : 0;
    const unknown = r.label === '未采集' || r.label === '无';
    return `<div class="bullet-row">
      <span class="bullet-name">${r.label}</span>
      <span class="bullet-track"><i class="bullet-fill" style="width:${(r.value / max) * 100}%;
        ${unknown ? 'background:linear-gradient(90deg,#c3cedd,#dbe3ee)' : ''}"></i></span>
      <span class="bullet-value"><b>${fmtInt(r.value)}</b></span>
      <span class="bullet-rate ${unknown ? 'is-none' : ''}">${fmtNum(share, 1)}%</span>
    </div>`;
  }).join('');

  const channel = data.channel_dist || [];
  $('legend-channel').innerHTML = channel.map((c, i) =>
    `<span><i style="background:${['#2f6fe4', '#0e9f8a', '#e8a33d', '#7c63d6'][i % 4]}"></i>
      ${c.label} ${fmtInt(c.value)} 家</span>`).join('');

  const known = rows.filter(r => r.label !== '未采集' && r.label !== '无')
    .reduce((a, b) => a + b.value, 0);
  $('foot-level').textContent =
    `A–E 等级已采集 ${fmtInt(known)} 家（${pct((100 * known) / (total || 1))}）；`
    + `「未采集 / 无」${fmtInt(total - known)} 家不推断、不按平均补`;
}

/* ------------------------------------------------------------ 热门 SKU */
function renderHot(data) {
  const host = $('chart-hot');
  const hot = data.hot;
  const mode = state.hotMode;
  const rows = hot.top || [];
  if (!rows.length) {
    return emptyBox(host, '当前筛选条件下没有匹配到主数据的热门 SKU',
      '现场反馈名称未匹配到 72 SKU 主数据时标 PENDING，不猜测对应关系');
  }
  const items = rows.map((r, i) => ({
    label: r.sku_name || r.sku_code,
    value: mode === 'stores' ? r.store_count : r.feedback_count,
    color: i < 3 ? '#2f6fe4' : '#5b9bf8',
    tip: `<div class="tt-row"><span>SKU 编码</span><b>${r.sku_code}</b></div>
          <div class="tt-row"><span>品类</span><b>${r.category || '—'}</b></div>
          <div class="tt-row"><span>反馈次数</span><b>${r.feedback_count}</b></div>
          <div class="tt-row"><span>涉及门店</span><b>${r.store_count}</b></div>
          <div class="tt-row"><span>现场均价</span><b>${r.avg_price === null ? '—' : '¥' + fmtNum(r.avg_price, 1)}</b></div>`,
  }));
  rankBar(host, {
    items, valueFormat: (v) => `${fmtInt(v)} ${mode === 'stores' ? '家' : '次'}`,
    labelW: 132, valueW: 56, rateW: 8, rowH: 24,
  });
  const cov = hot.coverage;
  $('foot-hot').textContent =
    `有效拜访 ${fmtInt(cov.visits)} 次 · 命中主数据 ${fmtInt(cov.with_sku)} 次（${pct(cov.coverage_rate)}）`
    + ` · PENDING ${fmtInt(cov.pending)} 次 · 未采集 ${fmtInt(cov.not_collected)} 次；`
    + '现场反馈不等于门店在售 SKU，也不等于 POS 销量排名';
}

/* ------------------------------------------------------------ 价格 */
function renderPrice(data) {
  const host = $('chart-price');
  const items = (data.price.top || []).slice(0, 8).map((r) => ({
    label: r.name,
    min: r.min, max: r.max, p25: r.p25, p75: r.p75, median: r.median, n: r.n,
    extraTip: `<div class="tt-row"><span>异常价</span><b>${r.abnormal} 条</b></div>`,
  }));
  if (!items.length) return emptyBox(host, '当前筛选条件下没有零售价记录');
  rangeBar(host, { items, labelW: 122, rowH: 26, maxLabel: 13 });

  const cov = data.price.coverage;
  const worst = (data.price.top || []).filter(r => r.abnormal > 0)
    .sort((a, b) => b.abnormal - a.abnormal)[0];
  $('foot-price').textContent =
    `有效记录 ${fmtInt(cov.with_price)} 条 / ${fmtInt(cov.visits)} 次拜访（${pct(cov.coverage_rate)}）`
    + ` · ${cov.sku_groups} 个商品组 · 异常价 ${fmtInt(cov.abnormal_total)} 条待核实`
    + (worst ? `（最多：${worst.name} ${worst.abnormal} 条）` : '')
    + ' · 单位元/盒，不含满减/券/线上优惠';
}

/* ------------------------------------------------------------ 陈列采集 */
function renderDisplay(data) {
  const host = $('chart-display');
  const fields = data.display.fields || [];
  if (!fields.length) return emptyBox(host, '当前筛选条件下无执行数据');
  const total = fields[0].total || 0;
  const base = Math.max(total, 1);
  host.innerHTML = fields.map((f) => {
    const filled = f.filled || 0;
    const empty = filled === 0;
    return `<div class="bullet-row">
      <span class="bullet-name">${f.label}</span>
      <span class="bullet-track"><i class="bullet-fill" style="width:${(filled / base) * 100}%;
        ${empty ? 'background:#dfe6f0' : ''}"></i></span>
      <span class="bullet-value"><b>${fmtInt(filled)}</b> / ${fmtInt(f.total)}</span>
      <span class="bullet-rate ${empty ? 'is-none' : ''}">${pct(f.rate)}</span>
    </div>`;
  }).join('');

  const st = data.display.status || {};
  const arrival = data.display.arrival || {};
  const locTop = (data.display.location || []).map(x => `${x.label} ${x.value}`).join(' · ');
  $('fact-display').innerHTML = [
    ['拜访完成情况', `已完成 ${fmtInt(st.completed)} / 未完成 ${fmtInt(st.incomplete)} / 未标注 ${fmtInt(st.unknown)}`, ''],
    ['离店时间异常', `${fmtInt(arrival.bad_departure)} 条`, arrival.bad_departure ? 'bad' : ''],
    ['陈列位置 TOP', locTop || '未采集', ''],
  ].map(([k, v, cls]) =>
    `<div class="fact-row"><span class="k">${k}</span><span class="v ${cls}">${v}</span></div>`).join('');

  $('foot-display').textContent = data.display.photo_note;
}

/* ------------------------------------------------------------ 联合分析 */
function renderJoint(data) {
  const s = data.joint.summary || {};
  $('fact-joint').innerHTML = [
    ['有热门 SKU 反馈的门店', `${fmtInt(s.sfa_feedback_stores)} 家`, ''],
    ['其中 POS 已对齐', `${fmtInt(s.aligned_stores)} 家`, ''],
    ['可联合评估（V1 join）', `${fmtInt(s.join_eligible_stores)} 家`, ''],
    ['反馈 SKU = POS 销量第一', `${fmtInt(s.same_top)} 家`, s.same_top ? '' : 'warn'],
    ['已售但非第一', `${fmtInt(s.sold_not_top)} 家`, ''],
    ['POS 无该 SKU 记录（待核实）', `${fmtInt(s.not_sold)} 家`, s.not_sold ? 'warn' : ''],
    ['未对齐 / PENDING', `${fmtInt(s.pending_stores)} 家`, ''],
  ].map(([k, v, cls]) =>
    `<div class="fact-row"><span class="k">${k}</span><span class="v ${cls}">${v}</span></div>`).join('');

  const rows = data.joint.rows || [];
  const body = rows.map(r => `<tr>
    <td>${r.store_id}</td><td>${r.ka_system || '—'}</td>
    <td>${r.sfa_name || r.sfa_sku || '—'}</td>
    <td>${r.pos_top_name || '—'}</td>
    <td class="num">${r.sfa_sku_rank ? `第 ${r.sfa_sku_rank}/${r.pos_sku_count}` : '无记录'}</td>
    <td>${r.status === 'SAME_TOP'
      ? '<span class="badge-cell badge-ok">一致</span>'
      : (r.status === 'SOLD_NOT_TOP'
        ? '<span class="badge-cell badge-warn">有销非第一</span>'
        : '<span class="badge-cell badge-miss">待核实</span>')}</td>
  </tr>`).join('');
  $('joint-table').innerHTML = `<table class="data"><thead><tr>
    <th>门店</th><th>KA 系统</th><th>SFA 现场反馈 SKU</th><th>POS 销量第一 SKU</th>
    <th>反馈 SKU 在 POS 排名</th><th>结论</th></tr></thead>
    <tbody>${body || '<tr><td colspan="6" style="text-align:center;padding:14px">没有可联合评估的门店</td></tr>'}</tbody></table>`;

  $('foot-joint').textContent = data.joint.note;
}

/* ------------------------------------------------------------ 提示条 / 口径 */
function renderNotices(payload) {
  const d = payload.data;
  const k = d.kpi;
  $('notice-main-text').innerHTML =
    `<b>门店口径</b>：本页只统计「当前筛选下有效拜访实际到过的门店」，共 <b>${fmtInt(k.store_total)}</b> 家`
    + `（花名册 ${fmtInt(k.store_roster)} 家）。SFA 只采集一组「门店重点分销产品」，`
    + `不代表门店全部在售 SKU；零售价为门店前端标价（元/盒），不含满减、券与线上优惠价。`;
  $('chip-period').textContent = payload.meta.period || 'Y26';
  const window = payload.meta.coverage_window;
  if (window && window.first_date) {
    $('chip-window').textContent = `数据窗口 ${window.first_date} ~ ${window.last_date}`;
  }
  const chips = [];
  if (state.regions) chips.push(`战区：${state.regions}`);
  if (state.marketDepartment) chips.push(`市场部：${state.marketDepartment}`);
  if (state.ka) chips.push(`KA：${state.ka}`);
  if (state.sku) chips.push(`SKU：${state.sku}`);
  $('active-chips').innerHTML = chips.map(c => `<span class="chip">${c}</span>`).join('');
}

function renderCapability(payload) {
  const d = payload.data;
  const items = [
    {
      title: '热门 SKU ≠ 门店在售 SKU',
      lines: [
        `有效拜访 ${fmtInt(d.hot.coverage.visits)} 次中，仅 ${fmtInt(d.hot.coverage.with_sku)} 次能精确映射到 72 SKU 主数据。`,
        `PENDING ${fmtInt(d.hot.coverage.pending)} 次、未采集 ${fmtInt(d.hot.coverage.not_collected)} 次单独列示；`
        + '不猜测名称、不把反馈次数当成销量。',
      ],
    },
    {
      title: '零售价是门店前端标价，不是成交价',
      lines: [
        '单位为元/盒，取自底表「门店重点分销产品零售价」列，不含满减、优惠券、补贴与线上 APP 价。',
        `低于同商品 P25 一半的记录计为异常价（共 ${fmtInt(d.price.coverage.abnormal_total)} 条），`
        + '只标记待核实，不删除、不改写。',
      ],
    },
    {
      title: '未采集 ≠ 未执行',
      lines: [
        '陈列、POSM、缺货等字段为空一律记为未采集，页面同时给出采集覆盖率。',
        '执行照片字段在宽表中未落地，本页不展示图片，也不编造执行凭证。',
      ],
    },
    {
      title: 'SFA × POS 的样本边界',
      lines: [
        `花名册 ${fmtInt(d.kpi.store_roster)} 家中仅 27 家 POS 对齐（V1 join eligible），`
        + `其中只有 ${fmtInt(d.joint.summary.join_eligible_stores)} 家有可比的现场反馈。`,
        'POS 无该 SKU 记录标记「待核实」，不等于零销售；未对齐门店保留 PENDING，不强行关联。',
      ],
    },
  ];
  $('capability-count').textContent = `${items.length} 条`;
  $('capability-list').innerHTML = items.map(it => `
    <div class="cap-item"><h4>${it.title}</h4>
      ${it.lines.map(l => `<p>${l}</p>`).join('')}</div>`).join('');
}

/* ------------------------------------------------------------ 明细 */
const COLS = [
  { key: 'store_id', label: '门店编码', sort: 'store_id', width: 104 },
  { key: 'store_name', label: '门店名称', sort: 'store_name', width: 150 },
  { key: 'ka_system', label: 'KA 系统', sort: 'ka_system', width: 96 },
  { key: 'region_name_std', label: '战区', sort: 'war_zone', width: 50 },
  { key: 'market_department', label: '市场部', sort: 'market_department', width: 96 },
  { key: 'store_level', label: '等级', sort: 'store_level', width: 52 },
  { key: 'store_type', label: '门店类型', sort: null, width: 82 },
  { key: 'visit_count', label: '拜访次数', sort: 'visit_count', num: true, width: 66 },
  { key: 'last_visit_date', label: '最近拜访', sort: null, width: 84 },
  { key: 'hot_sku_name', label: '现场反馈 SKU', sort: 'focus_sku_name', width: 150 },
  { key: 'hot_price', label: '前端零售价', sort: 'focus_retail_price', num: true, width: 78 },
  { key: 'display_location', label: '陈列位置', sort: null, width: 90 },
  { key: 'pos_link_status', label: 'POS 对齐', sort: null, width: 78 },
];

function renderTable(host, rows) {
  const head = COLS.map((c) => {
    const sorted = state.sort === c.sort && c.sort;
    const mark = c.sort ? `<em class="sort-mark">${sorted
      ? (state.order === 'desc' ? '▼' : '▲') : '⇅'}</em>` : '';
    return `<th class="${c.sort ? 'sortable' : ''}${sorted ? ' sorted' : ''}"
      data-sort="${c.sort || ''}" style="min-width:${c.width}px">${c.label}${mark}</th>`;
  }).join('');

  const body = rows.map((r) => COLS.map((c) => {
    const v = r[c.key];
    let text;
    if (c.key === 'hot_price') text = v === null || v === undefined ? '未采集' : `¥${fmtNum(v, 1)}`;
    else if (c.key === 'hot_sku_name') text = v ? v : '<span class="badge-cell badge-none">未采集</span>';
    else if (c.key === 'display_location') text = v ? v : '<span class="badge-cell badge-none">未采集</span>';
    else if (c.key === 'store_level') text = (v && v !== '未采集') ? v : '未采集';
    else if (c.key === 'pos_link_status') {
      text = v === 'POS_ALIGNED'
        ? '<span class="badge-cell badge-ok">POS 已对齐</span>'
        : '<span class="badge-cell badge-none">SFA_ONLY</span>';
    } else if (v === null || v === undefined || v === '') text = '—';
    else text = c.num ? fmtInt(v) : String(v);
    const cls = [c.num ? 'num' : '', (c.key === 'store_level' && (!v || v === '未采集')) ? 'muted' : '']
      .filter(Boolean).join(' ');
    return `<td class="${cls}">${text}</td>`;
  }).join('')).map(tr => `<tr>${tr}</tr>`).join('');

  host.innerHTML = `<table class="data"><thead><tr>${head}</tr></thead>
    <tbody>${body || `<tr><td colspan="${COLS.length}" style="text-align:center;padding:18px">
      当前筛选条件下没有门店</td></tr>`}</tbody></table>`;

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
async function loadDetail() {
  const host = $('detail-table');
  if (!host.querySelector('table')) loadingBox(host, '正在查询明细…');
  try {
    const payload = await getJSON('/api/sfa/profile/detail', {
      ...params(), page: state.page, limit: state.limit,
      sort: state.sort, order: state.order, q: state.q,
    });
    renderTable(host, payload.data.rows);
    const d = payload.data;
    $('detail-total').textContent = fmtInt(d.total);
    $('detail-count').textContent =
      `共 ${fmtInt(d.total)} 家门店 · 第 ${d.page} / ${d.pages} 页 · 每页 ${d.limit} 行`;
    $('detail-page').textContent = `${d.page} / ${d.pages}`;
    $('detail-prev').disabled = d.page <= 1;
    $('detail-next').disabled = d.page >= d.pages;
  } catch (err) {
    host.innerHTML = `<div class="inline-empty"><b>明细加载失败</b><span>${err.message}</span></div>`;
  }
}

function markLoading() {
  hideTooltip();
  setStatus('查询中…');
  ['chart-ka', 'chart-hot', 'chart-price'].forEach((id) => {
    const el = $(id);
    if (el) loadingBox(el, '正在查询数据…');
  });
  $('chart-level').innerHTML = '<div class="inline-empty">正在查询数据…</div>';
  $('chart-display').innerHTML = '<div class="inline-empty">正在查询数据…</div>';
}

async function loadAll() {
  markLoading();
  $('alert').classList.add('hidden');
  try {
    state.options = (await getJSON('/api/sfa/profile/options', params())).data;
    renderFilterOptions();
    const payload = await getJSON('/api/sfa/profile', params());
    state.data = payload;
    renderKpi(payload.data.kpi, payload.data);
    renderKa(payload.data);
    renderLevel(payload.data);
    renderHot(payload.data);
    renderPrice(payload.data);
    renderDisplay(payload.data);
    renderJoint(payload.data);
    renderNotices(payload);
    renderCapability(payload);
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
  $('f-region').addEventListener('change', (e) => {
    state.regions = e.target.value; state.employee = ''; reload();
  });
  $('f-md').addEventListener('change', (e) => {
    state.marketDepartment = e.target.value; state.employee = ''; reload();
  });
  $('f-employee').addEventListener('change', (e) => { state.employee = e.target.value; reload(); });
  $('f-ka').addEventListener('change', (e) => { state.ka = e.target.value; reload(); });
  $('f-sku').addEventListener('change', (e) => { state.sku = e.target.value; reload(); });
  $('btn-reset').addEventListener('click', () => {
    state.months = ''; state.regions = ''; state.marketDepartment = '';
    state.employee = ''; state.ka = ''; state.sku = ''; state.page = 1;
    state.sort = ''; state.order = 'asc'; state.q = '';
    $('detail-q').value = '';
    writeUrl(); loadAll();
  });
  $('btn-refresh').addEventListener('click', loadAll);

  $('ka-mode').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-mode]');
    if (!btn) return;
    state.kaMode = btn.dataset.mode;
    $('ka-mode').querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
    if (state.data) renderKa(state.data.data);
  });
  $('hot-mode').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-mode]');
    if (!btn) return;
    state.hotMode = btn.dataset.mode;
    $('hot-mode').querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
    if (state.data) renderHot(state.data.data);
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
bind();
loadAll();
