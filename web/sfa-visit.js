/* =====================================================================
 * sfa-visit.js — SFA 销售拜访分析（阶段一）
 * 所有指标由后端 /api/sfa/visit* 计算，前端只做展示与交互，不重算任何比率。
 * 目标配置走 /api/sfa/targets*（写独立配置库 data/app_config.db）。
 * ===================================================================== */
import { hideTooltip } from './charts.js';
import {
  donutCount, trendTwo, rankBar, fmtInt, fmtNum, emptyBox, loadingBox,
} from './boardcharts.js';

const $ = (id) => document.getElementById(id);
const PAGE = 'sfa-visit';

const state = {
  months: '',           // '' = 全部（1–10 月）
  regions: '',
  marketDepartment: '',
  employee: '',
  page: 1,
  limit: 10,
  sort: '',
  order: 'asc',
  q: '',
  rankMode: 'rate',     // rate | stores
  rankPicked: false,    // 用户是否手动切换过排名口径
  trendMetric: 'stores',
  data: null,
  options: null,
};

const pct = (v, d = 1) => (v === null || v === undefined ? '—' : `${fmtNum(v, d)}%`);
const rateClass = (v) => (v === null || v === undefined ? 'rate-none'
  : (v >= 100 ? 'rate-ok' : 'rate-miss'));

/* ------------------------------------------------------------ URL 同步 */
function readUrl() {
  const u = new URLSearchParams(location.search);
  state.months = u.get('months') || '';
  state.regions = u.get('regions') || '';
  state.marketDepartment = u.get('market_department') || '';
  state.employee = u.get('employee') || '';
  state.page = Math.max(1, Number(u.get('page') || 1));
  state.sort = u.get('sort') || '';
  state.order = u.get('order') === 'desc' ? 'desc' : 'asc';
  state.q = u.get('q') || '';
}

function writeUrl() {
  const u = new URLSearchParams();
  if (state.months) u.set('months', state.months);
  if (state.regions) u.set('regions', state.regions);
  if (state.marketDepartment) u.set('market_department', state.marketDepartment);
  if (state.employee) u.set('employee', state.employee);
  if (state.page > 1) u.set('page', String(state.page));
  if (state.sort) { u.set('sort', state.sort); u.set('order', state.order); }
  if (state.q) u.set('q', state.q);
  const qs = u.toString();
  history.replaceState(null, '', qs ? `${location.pathname}?${qs}` : location.pathname);
}

function params(extra = {}) {
  const p = { ...extra };
  if (state.months) p.months = state.months;
  if (state.regions) p.regions = state.regions;
  if (state.marketDepartment) p.market_department = state.marketDepartment;
  if (state.employee) p.employee = state.employee;
  return p;
}

/* ------------------------------------------------------------ 通用 */
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

async function postJSON(path, payload) {
  const res = await fetch(path, {
    method: 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    body: JSON.stringify(payload),
  });
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

function showAlert(text) {
  const el = $('alert');
  if (!text) { el.classList.add('hidden'); return; }
  el.textContent = text;
  el.classList.remove('hidden');
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
  const months = opt.months || [];
  fillSelect($('f-month'), months.map(m => ({ value: String(m), label: `${m} 月` })),
    state.months, '全部月份');
  fillSelect($('f-region'), (opt.regions || []).map(r => ({ value: r, label: r })),
    state.regions, '五大战区');
  fillSelect($('f-md'), (opt.market_departments || []).map(m => ({ value: m, label: m })),
    state.marketDepartment, '全部市场部');

  const employees = (opt.employees || []).filter(e =>
    (!state.regions || e.region_name === state.regions)
    && (!state.marketDepartment || e.market_department === state.marketDepartment));
  fillSelect($('f-employee'), employees.map(e => ({
    value: e.employee_id,
    label: `${e.employee_name}（${e.employee_id}·${e.managed_store_count}店）`,
  })), state.employee, `全部客户经理（${employees.length}）`);

  const dl = $('employee-options');
  dl.innerHTML = '';
  (opt.target_registry || []).forEach((e) => {
    const o = document.createElement('option');
    o.value = e.employee_id;
    o.label = `${e.employee_name} · ${e.war_zone} · ${e.managed_store_count} 店`;
    dl.appendChild(o);
  });
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
  const cfgTotal = k.employee_configured;
  const unset = k.employee_unconfigured;
  const demo = !!k.target_is_demo;
  $('kpi-row').innerHTML = [
    kpiCard({
      label: '客户经理总人数', value: fmtInt(k.employee_total), unit: '人',
      sub: `已配置目标 ${cfgTotal} 人 · 未配置 ${unset} 人`
        + (demo ? ` · 其中示例 ${k.employee_demo_target} 人` : ''),
    }),
    kpiCard({
      label: '应拜访门店目标', value: k.target_store_total ? fmtInt(k.target_store_total) : '未配置',
      unit: '家', muted: !k.target_store_total, text: !k.target_store_total,
      sub: !k.target_store_total ? '库中无目标，不按 0 计'
        : (demo ? '示例目标（DEMO）· 仅演示，可一键清除' : '仅已配置目标的客户经理合计'),
    }),
    kpiCard({
      label: '实际拜访门店数', value: fmtInt(k.actual_store_total), unit: '家',
      sub: `按人×月去重 · 其中已配置人员 ${fmtInt(k.actual_store_configured)} 家`,
    }),
    kpiCard({
      label: '整体目标完成率', value: pct(k.completion_rate),
      muted: k.completion_rate === null, accent: 'teal',
      sub: k.completion_rate === null ? '无可计算目标'
        : (demo ? '示例目标下的完成率（DEMO）' : '已配置人员实际 / 目标合计'),
    }),
    kpiCard({
      label: '已达标客户经理', value: fmtInt(k.employee_reached), unit: '人', accent: 'purple',
      sub: '完成率 ≥ 100%',
    }),
    kpiCard({
      label: '未达标客户经理', value: fmtInt(k.employee_missed), unit: '人', accent: 'amber',
      sub: '含一次都没拜访的客户经理',
    }),
  ].join('');

  $('kpi-sub').textContent = k.target_total_note;
}

/* ------------------------------------------------------------ 战区完成情况 */
function renderRegion(rows) {
  const host = $('chart-region');
  if (!rows.length) return emptyBox(host, '当前筛选条件下无战区数据');
  const valueOf = (r) => (r.employee_configured
    ? (r.actual_configured || 0) : (r.actual_store_count || 0));
  const base = Math.max(...rows.map(r => Math.max(r.target_store_count || 0, valueOf(r))), 1);
  host.innerHTML = rows.map((r) => {
    const target = r.target_store_count || 0;
    const actual = valueOf(r);
    const rate = r.completion_rate;
    const fill = Math.max(1.5, (actual / base) * 100);
    const marker = target ? Math.min(99.4, (target / base) * 100) : null;
    const cls = rate === null ? '' : (rate >= 100 ? ' is-over' : ' is-miss');
    return `<div class="bullet-row">
      <span class="bullet-name">${r.region_name}</span>
      <span class="bullet-track" title="${r.region_name}：已完成 ${actual} 家 / 目标 ${target || '未配置'} 家（已配置 ${r.employee_configured}/${r.employee_total} 人）">
        <i class="bullet-fill${cls}" style="width:${fill}%"></i>
        ${marker === null ? '' : `<i class="bullet-target" style="left:${marker}%"></i>`}
      </span>
      <span class="bullet-value"><b>${actual}</b> / ${target || '—'}</span>
      <span class="bullet-rate ${rate === null ? 'is-none' : (rate >= 100 ? 'is-ok' : 'is-miss')}">
        ${rate === null ? '未配置' : pct(rate)}
      </span>
    </div>`;
  }).join('');

  const withTarget = rows.filter(r => r.target_store_count);
  $('foot-region').textContent = withTarget.length
    ? `已配置目标的战区 ${withTarget.length} / ${rows.length} 个；条形长度按「实际/目标」较大值归一，竖线为目标位置`
    : '五大战区均未配置月度目标，因此不显示竖线、不计算完成率（不按 0 计）';

  // 补充小表：把人员数与覆盖情况摊开，避免左卡留白，同时给出逐战区口径
  const body = rows.map(r => `<tr>
    <td>${r.region_name}</td>
    <td class="num">${fmtInt(r.employee_total)}</td>
    <td class="num">${r.employee_configured || '—'}</td>
    <td class="num">${fmtInt(r.managed_store_count)}</td>
    <td class="num">${fmtInt(r.actual_configured || 0)} / ${fmtInt(r.actual_store_count)}</td>
    <td class="num">${pct(r.coverage_rate)}</td>
    <td class="num">${fmtInt(r.repeat_visit_count)}</td>
  </tr>`).join('');
  $('region-tbl').innerHTML = `<table class="data"><thead><tr>
    <th>战区</th><th>经理</th><th>已配目标</th><th>负责门店</th>
    <th>完成 / 拜访</th><th>覆盖</th><th>重复</th></tr></thead><tbody>${body}</tbody></table>`;
}

/* ------------------------------------------------------------ 客户经理排名 */
function renderRank(rows) {
  const host = $('chart-rank');
  if (!rows.length) return emptyBox(host, '当前筛选条件下无客户经理数据');

  const rateMode = state.rankMode === 'rate';
  // 全员未配置时，逐行重复「未配置」角标只是噪声：整张卡在脚注说明一次即可
  const anyConfigured = rows.some(e => e.completion_rate !== null);
  const items = rows.filter(e => !rateMode || e.completion_rate !== null)
    .map((e) => ({
      label: `${e.employee_name}`,
      value: rateMode ? e.completion_rate : e.visited_store_count,
      rate: (!rateMode && anyConfigured) ? e.completion_rate : null,
      rateText: (!rateMode && !anyConfigured) ? null
        : (!rateMode && e.completion_rate === null ? '未配置' : null),
      color: rateMode ? (e.completion_rate >= 100 ? '#0e9f8a' : '#2f6fe4') : '#2f6fe4',
      tip: `<div class="tt-row"><span>战区</span><b>${e.region_name || '—'}</b></div>
            <div class="tt-row"><span>市场部</span><b>${e.market_department || '—'}</b></div>
            <div class="tt-row"><span>目标</span><b>${e.target_store_count ?? '未配置'}${
              e.target_source === 'DEMO' ? ' <i class="demo-flag">示例</i>'
              : e.target_source === 'MIXED' ? ' <i class="demo-flag">含示例</i>' : ''}</b></div>
            <div class="tt-row"><span>实际门店</span><b>${e.visited_store_count}</b></div>
            <div class="tt-row"><span>拜访记录</span><b>${e.visit_count}</b></div>
            <div class="tt-row"><span>完成率</span><b>${pct(e.completion_rate)}</b></div>`,
      employee: e,
    }));

  if (!items.length) {
    return emptyBox(host, '尚无客户经理配置月度目标',
      '完成率排名需要目标；可切换到「拜访门店数」查看实际执行，或先在弹窗中配置目标');
  }

  rankBar(host, {
    items,
    valueFormat: (v) => (rateMode ? pct(v) : `${fmtInt(v)} 店`),
    tipLabel: rateMode ? '完成率' : '拜访门店数',
    labelW: 66, valueW: 54, rateW: 52, rowH: 24,
    onClick: (it) => openDrill(it.employee.employee_id),
  });
  $('foot-rank').textContent = rateMode
    ? `按完成率降序 · 仅列出已配置目标的客户经理（${items.length} 人）· 点击可下钻`
    : `按拜访门店数降序 · 未配置目标的人也会出现（完成率显示「未配置」）· 点击可下钻`;
}

/* ------------------------------------------------------------ 月度趋势 */
function renderTrend(rows, coverage) {
  const host = $('chart-trend');
  const legend = $('legend-trend');
  const metric = state.trendMetric;
  const valueKey = metric === 'visits' ? 'visit_count' : 'visited_store_count';
  const name = metric === 'visits' ? '拜访记录数' : '拜访门店数';

  const labels = rows.map(r => r.label);
  const series = [{
    name, color: '#2f6fe4',
    values: rows.map(r => (r[valueKey] === null ? null : r[valueKey])),
  }];
  if (rows.some(r => r.target_store_count !== null && r.target_store_count !== undefined)) {
    series.push({
      name: '月度目标', color: '#e8a33d', dashed: true,
      values: rows.map(r => (r.target_store_count ?? null)),
    });
  }

  trendTwo(host, { labels, series, height: 196,
    emptyText: '当前筛选条件下无月度数据' });

  legend.innerHTML = series.map(s =>
    `<span><i style="background:${s.color}"></i>${s.name}</span>`).join('');

  const window = (state.data && state.data.meta && state.data.meta.coverage_window) || {};
  const span = window.first_date ? `${window.first_date} ~ ${window.last_date}（${window.days} 天）` : '—';
  $('foot-trend').textContent =
    `SFA 底表实际覆盖 ${span}；按月归集，无拜访的月份不出现在横轴（不补零）`;
}

/* ------------------------------------------------------------ 覆盖与重复 */
function renderCoverage(cov) {
  donutCount($('chart-coverage'), {
    items: [
      { label: '已覆盖门店', value: cov.covered_stores, color: '#2f6fe4' },
      { label: '未拜访门店', value: cov.unvisited_stores, color: '#e8a33d' },
    ],
    unit: '家', unitShort: ' 家', size: 132,
    centerValue: pct(cov.coverage_rate), centerLabel: '门店覆盖率',
  });

  const rows = [
    ['应拜访门店（花名册）', fmtInt(cov.universe_stores), '家', ''],
    ['已覆盖门店', fmtInt(cov.covered_stores), '家', ''],
    ['未拜访门店', fmtInt(cov.unvisited_stores), '家', cov.unvisited_stores > 0 ? 'warn' : ''],
    ['拜访记录数', fmtInt(cov.visit_count), '次', ''],
    ['重复拜访次数', fmtInt(cov.repeat_visit_count), '次', cov.repeat_visit_count > 0 ? 'warn' : ''],
    ['去重后有效到店', fmtInt(cov.dedup_visit_count), '次', ''],
  ];
  $('fact-coverage').innerHTML = rows.map(([k, v, unit, cls]) =>
    `<div class="fact-row"><span class="k">${k}</span>
      <span class="v ${cls}">${v}<span class="hint"> ${unit}</span></span></div>`).join('');

  $('foot-coverage').textContent =
    '重复拜访 = 同客户经理、同门店、同自然月内第 2 次及以后；只统计、不增加完成量';
}

/* ------------------------------------------------------------ 辅助提示条 */
function renderNotices(payload) {
  const window = (payload.meta && payload.meta.coverage_window) || {};
  const k = payload.data.kpi;
  const span = window.first_date ? `${window.first_date} ~ ${window.last_date}` : '未知';
  $('notice-window-text').innerHTML =
    `<b>SFA 数据窗口</b>：底表共 ${fmtInt(k.visit_count)} 条有效拜访，仅覆盖 <b>${span}</b>`
    + `（${window.days || 0} 个拜访日）。月度趋势按自然月归集，`
    + `不代表整月完整执行；未覆盖的月份不会出现在图上，也不补 0。`;

  const targetNotice = $('notice-target');
  const demoCount = k.employee_demo_target || 0;
  if (demoCount > 0) {
    // 示例目标：用 info 风格（不是告警）+ 明确按钮，避免被当成真实目标
    targetNotice.classList.remove('hidden');
    targetNotice.classList.remove('notice-warn');
    targetNotice.classList.add('notice-info');
    $('notice-target-text').innerHTML =
      `<b>当前目标是示例数据（DEMO）</b>：${fmtInt(demoCount)} 名客户经理的目标由`
      + `「当月实际完成门店数 × 0.80–1.30 确定性系数」生成，<b>不是真实业务目标</b>，`
      + `只用于让完成率、排名、战区完成情况这几张图看得清楚。可随时清除，恢复「未配置」。`;
  } else if (k.employee_unconfigured > 0) {
    targetNotice.classList.remove('hidden');
    targetNotice.classList.remove('notice-info');
    targetNotice.classList.add('notice-warn');
    $('notice-target-text').innerHTML =
      `<b>月度目标未配置</b>：当前筛选下 ${k.employee_total} 名客户经理中，`
      + `<b>${k.employee_unconfigured}</b> 名没有月度目标。`
      + `按业务规则，未配置的人不参与完成率计算（既不按 0 计、也不按平均补）。`
      + `系统不会静默补默认值；如需先看效果，可一键生成<b>示例目标（DEMO）</b>，页面会全程标注。`;
  } else {
    targetNotice.classList.add('hidden');
  }

  $('chip-period').textContent = payload.meta.period || 'Y26';
  $('chip-window').textContent = `数据窗口 ${span}`;
  $('chip-target').textContent =
    `目标已配置 ${k.employee_configured} / ${k.employee_total} 人`
    + (demoCount ? ` · 示例 ${k.employee_demo_target}` : '');
  const demoChip = $('chip-demo');
  if (demoChip) demoChip.classList.toggle('hidden', !demoCount);

  const chips = [];
  if (state.regions) chips.push(`战区：${state.regions}`);
  if (state.marketDepartment) chips.push(`市场部：${state.marketDepartment}`);
  if (state.employee) chips.push(`客户经理：${state.employee}`);
  $('active-chips').innerHTML = chips.map(c => `<span class="chip">${c}</span>`).join('');
}

/* ------------------------------------------------------------ 口径说明 */
function renderCapability(payload) {
  const k = payload.data.kpi;
  const items = [
    {
      title: '成交口径：完成量 ≠ 拜访记录数',
      lines: [
        `本项目前拜访记录 ${fmtInt(k.visit_count)} 次，按「人×月」去重后的完成门店 `
        + `${fmtInt(k.actual_store_total)} 家，全公司再对门店去重后覆盖 ${fmtInt(k.covered_stores)} 家。`,
        '三个数字刻意分列：记录数含重复拜访，完成量剔除重复，覆盖数是公司口径。',
      ],
    },
    {
      title: '目标来源：真实录入优先，示例数据必须标明',
      lines: [
        `目标库当前 ${k.employee_configured} 条生效记录；未配置 ${k.employee_unconfigured} 人。`,
        k.employee_demo_target
          ? `其中 ${k.employee_demo_target} 人的目标是示例数据（DEMO）：按实际完成数 × 0.80–1.30 系数生成，`
            + '明细表「目标来源」列带「示例」角标，可一键清除；手工录入或导入的真实目标永不被覆盖。'
          : '当前没有示例目标：库中目标全部来自手工录入或 CSV 导入。',
        '目标为空时完成率返回空值（页面显示「—」），不按 0 计、不按平均值补。',
      ],
    },
    {
      title: '被排除的记录',
      lines: [
        '163 条拜访日期缺失、门店编码为空的记录不计入完成量（无法归属自然月）。',
        '战区F、战区G与 UNKNOWN 门店属 OUT_OF_SCOPE，不在五大战区口径内。',
      ],
    },
    {
      title: 'SFA 底表的时间局限',
      lines: [
        '真实底表只覆盖 2026-09-28 ~ 2026-10-03 六个拜访日，因此「月度趋势」只有 9、10 两个月。',
        '不是数据缺失，也不是按 0 补的；如需完整年度趋势必须补采底表，不能用推算值。',
      ],
    },
  ];
  $('capability-count').textContent = `${items.length} 条`;
  $('capability-list').innerHTML = items.map(it => `
    <div class="cap-item"><h4>${it.title}</h4>
      ${it.lines.map(l => `<p>${l}</p>`).join('')}</div>`).join('');
}

/* ------------------------------------------------------------ 明细表 */
const COLS = [
  { key: 'employee_name', label: '客户经理', sort: 'employee_name', width: 92 },
  { key: 'employee_id', label: 'ID', sort: 'employee_id', width: 66 },
  { key: 'region_name', label: '战区', sort: 'war_zone', width: 52 },
  { key: 'market_department', label: '市场部', sort: 'market_department', width: 92 },
  { key: 'managed_store_count', label: '负责门店', sort: null, num: true, width: 68 },
  { key: 'target_store_count', label: '月度目标', sort: 'target_store_count', num: true, width: 68 },
  { key: 'target_source', label: '目标来源', sort: null, width: 62 },
  { key: 'visited_store_count', label: '实际门店', sort: 'visited_store_count', num: true, width: 68 },
  { key: 'completion_rate', label: '完成率', sort: 'completion_rate', num: true, width: 64 },
  { key: 'gap', label: '缺口', sort: 'gap', num: true, width: 70 },
  { key: 'repeat_visit_count', label: '重复拜访', sort: 'repeat_visit_count', num: true, width: 68 },
  { key: 'coverage_rate', label: '负责门店覆盖率', sort: 'coverage_rate', num: true, width: 94 },
  { key: 'status', label: '状态', sort: null, width: 62 },
];

function cellText(column, row) {
  const v = row[column.key];
  if (column.key === 'target_source') {
    // 目标来源必须在明细里可见：示例目标绝不允许看起来像真实目标
    if (v === 'DEMO') return '<span class="badge-cell badge-demo">示例</span>';
    if (v === 'MIXED') return '<span class="badge-cell badge-demo">含示例</span>';
    if (v === 'MANUAL') return '<span class="badge-cell badge-ok">手工/导入</span>';
    return '<span class="badge-cell badge-none">无</span>';
  }
  if (column.key === 'target_store_count') {
    return v === null || v === undefined
      ? '<span class="badge-cell badge-none">未配置</span>' : fmtInt(v);
  }
  if (column.key === 'completion_rate') return pct(v);
  if (column.key === 'coverage_rate') return pct(v);
  if (column.key === 'gap') {
    if (v === null || v === undefined) return '—';
    if (v < 0) return `<span class="badge-cell badge-ok">超额 ${Math.abs(v)}</span>`;
    return fmtInt(v);
  }
  if (column.key === 'status') {
    const cls = v === '已达标' ? 'badge-ok' : (v === '未达标' ? 'badge-miss' : 'badge-none');
    return `<span class="badge-cell ${cls}">${v}</span>`;
  }
  if (v === null || v === undefined || v === '') return '—';
  return column.num ? fmtInt(v) : String(v);
}

function renderTable(host, rows, opts = {}) {
  const head = COLS.map((c) => {
    const sorted = state.sort === c.sort && c.sort;
    const mark = c.sort ? `<em class="sort-mark">${sorted
      ? (state.order === 'desc' ? '▼' : '▲') : '⇅'}</em>` : '';
    return `<th class="${c.sort ? 'sortable' : ''}${sorted ? ' sorted' : ''}"
      data-sort="${c.sort || ''}" style="min-width:${c.width}px">${c.label}${mark}</th>`;
  }).join('');

  const body = rows.map((r) => COLS.map((c) => {
    if (c.key === 'employee_name') {
      return `<td class="drill" data-emp="${r.employee_id}">${r.employee_name || '—'}</td>`;
    }
    const cls = [c.num ? 'num' : '', c.key === 'completion_rate' ? rateClass(r[c.key]) : '']
      .filter(Boolean).join(' ');
    return `<td class="${cls}">${cellText(c, r)}</td>`;
  }).join('')).map(tr => `<tr>${tr}</tr>`).join('');

  host.innerHTML = `<table class="data"><thead><tr>${head}</tr></thead>
    <tbody>${body || `<tr><td colspan="${COLS.length}" class="muted"
      style="text-align:center;padding:18px">当前筛选条件下没有客户经理</td></tr>`}</tbody></table>`;

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
  host.querySelectorAll('td.drill').forEach((td) => {
    td.addEventListener('click', () => openDrill(td.dataset.emp));
  });
}

function renderDetailFooter(payload) {
  const d = payload.data;
  $('detail-count').textContent =
    `共 ${fmtInt(d.total)} 名客户经理 · 第 ${d.page} / ${d.pages} 页 · 每页 ${d.limit} 行`;
  $('detail-page').textContent = `${d.page} / ${d.pages}`;
  $('detail-prev').disabled = d.page <= 1;
  $('detail-next').disabled = d.page >= d.pages;
}

/* ------------------------------------------------------------ 下钻 */
async function openDrill(employeeId) {
  if (!employeeId) return;
  $('drill-title').textContent = '客户经理拜访明细';
  $('drill-sub').textContent = employeeId;
  $('drill-stores').innerHTML = '<div class="inline-empty">加载中…</div>';
  $('drill-visits').innerHTML = '';
  $('drill-kpi').innerHTML = '';
  $('modal').classList.remove('hidden');
  try {
    const payload = await getJSON('/api/sfa/visit/drill', {
      ...params(), employee: employeeId, limit: 50,
    });
    const s = payload.data.summary;
    const emp = (state.data?.data.employees || []).find(e => e.employee_id === employeeId) || {};
    $('drill-title').textContent = `${emp.employee_name || employeeId} · 拜访明细`;
    $('drill-sub').textContent =
      `${emp.region_name || ''} ${emp.market_department || ''} `.trim();

    $('drill-kpi').innerHTML = [
      kpiCard({ label: '拜访记录', value: fmtInt(s.visit_count), unit: '次' }),
      kpiCard({ label: '去重门店', value: fmtInt(s.store_count), unit: '家',
        sub: `负责门店 ${s.managed_store_count} 家` }),
      kpiCard({ label: '完成率', value: pct(s.completion_rate),
        muted: s.completion_rate === null,
        sub: s.target_store_count === null
          ? '该客户经理未配置目标' : `目标 ${s.target_store_count} 家` }),
    ].join('');

    const storeRows = payload.data.stores.map(st => `<tr>
      <td>${st.store_id}</td><td>${st.store_name || '—'}</td><td>${st.ka_system || '—'}</td>
      <td>${st.store_level || '未采集'}</td><td>${st.store_type || '—'}</td>
      <td>${st.city || '—'}</td><td>${st.pos_link_status === 'POS_ALIGNED'
        ? '<span class="badge-cell badge-ok">POS 已对齐</span>'
        : '<span class="badge-cell badge-none">SFA_ONLY</span>'}</td></tr>`).join('');
    $('drill-stores').innerHTML = `<table class="data">
      <thead><tr><th>门店编码</th><th>门店名称</th><th>KA 系统</th><th>门店等级</th>
        <th>门店类型</th><th>城市</th><th>POS 对齐</th></tr></thead>
      <tbody>${storeRows || '<tr><td colspan="7">该客户经理名下无门店</td></tr>'}</tbody></table>`;
    $('drill-store-sub').textContent =
      `${payload.data.stores.length} 家（门店维度，含未拜访）`;

    const visitRows = payload.data.visits.map(v => `<tr>
      <td>${v.visit_date || '—'}</td><td>${v.actual_arrival_time || '—'}</td>
      <td>${v.store_id}</td><td>${v.store_name || '—'}</td><td>${v.ka_system || '—'}</td>
      <td>${v.focus_product_name || '未采集'}</td>
      <td class="num">${v.focus_retail_price === null ? '—' : fmtNum(v.focus_retail_price, 1)}</td>
      <td>${v.display_location && v.display_location.trim()
        ? v.display_location : '<span class="badge-cell badge-none">未采集</span>'}</td>
      <td>${v.visit_complete || '—'}</td></tr>`).join('');
    $('drill-visits').innerHTML = `<table class="data">
      <thead><tr><th>拜访日期</th><th>到店时间</th><th>门店编码</th><th>门店名称</th>
        <th>KA 系统</th><th>重点商品反馈</th><th>前端零售价（元/盒）</th>
        <th>陈列位置</th><th>完成情况</th></tr></thead>
      <tbody>${visitRows || '<tr><td colspan="9">该客户经理在所选条件下无有效拜访</td></tr>'}</tbody></table>`;
    $('drill-visit-sub').textContent =
      `${payload.data.visits.length} 条有效拜访（${s.first_date || '—'} ~ ${s.last_date || '—'}）`;
    $('drill-note').textContent =
      '门店来自 dim_store（该经理名下），拜访记录来自 fact_sfa_visit（有效拜访口径），两者不 JOIN 事实表。';
  } catch (err) {
    $('drill-stores').innerHTML = `<div class="inline-empty"><b>加载失败</b><span>${err.message}</span></div>`;
  }
}

/* ------------------------------------------------------------ 目标配置弹窗 */
function targetTable(rows) {
  const sourceBadge = (r) => (r.data_source === 'DEMO'
    ? '<span class="badge-cell badge-demo">示例</span>'
    : r.data_source === 'IMPORT'
      ? '<span class="badge-cell badge-ok">导入</span>'
      : '<span class="badge-cell badge-none">手工</span>');
  const body = rows.map(r => `<tr>
    <td>${r.employee_id}</td><td>${r.employee_name || '—'}</td>
    <td>${r.war_zone || '未在花名册'}</td>
    <td>${r.year}</td><td>${r.month} 月</td>
    <td class="num">${fmtInt(r.target_store_count)}</td>
    <td>${sourceBadge(r)}</td>
    <td>${r.config_status === 'ACTIVE'
      ? '<span class="badge-cell badge-ok">生效</span>'
      : '<span class="badge-cell badge-none">已停用</span>'}</td>
    <td>${(r.configured_at || '').replace('T', ' ').slice(0, 16)}</td>
    <td><button class="btn btn-sm" data-del="${r.employee_id}|${r.year}|${r.month}">撤回</button></td>
  </tr>`).join('');
  return `<table class="data"><thead><tr><th>客户经理 ID</th><th>姓名</th><th>战区</th>
    <th>年份</th><th>月份</th><th>目标门店</th><th>来源</th><th>状态</th>
    <th>配置时间</th><th>操作</th></tr></thead>
    <tbody>${body || `<tr><td colspan="10" style="text-align:center;padding:16px">
      暂无目标配置</td></tr>`}</tbody></table>`;
}

async function loadTargets() {
  const payload = await getJSON('/api/sfa/targets', {});
  const d = payload.data;
  const demo = d.demo || {};
  const demoRows = (d.summary && d.summary.demo_rows) || 0;
  $('target-count').textContent = d.summary.configured_rows;
  $('target-list-sub').textContent =
    `共 ${d.rows.length} 条 · 生效 ${d.summary.configured_rows} 条 · 停用 ${d.summary.disabled_rows} 条`
    + (demoRows ? ` · 其中示例 ${demoRows} 条` : '');
  $('target-foot').textContent =
    `${d.note} 配置库：${payload.meta.target_db.db_path}`;
  const demoBox = $('demo-note');
  if (demoBox) {
    demoBox.classList.toggle('hidden', !demoRows);
    if (demoRows) {
      demoBox.innerHTML = `<div class="notice-body"><b>当前有 ${demoRows} 条示例目标（DEMO）</b>：`
        + `${demo.formula || ''}；覆盖 ${(demo.employees || 0)} 人 / `
        + `${(demo.months || []).join('、')} 月。这些数字不是真实业务目标，`
        + `请在正式使用前清除或替换为真实目标。</div>`;
    }
  }
  $('target-table').innerHTML = targetTable(d.rows);
  $('target-table').querySelectorAll('button[data-del]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const [employee_id, year, month] = btn.dataset.del.split('|');
      try {
        await postJSON('/api/sfa/targets/delete',
          { employee_id, year: Number(year), month: Number(month) });
        await loadTargets();
        await loadAll();
        toast('已撤回该条目标');
      } catch (err) { toast(`撤回失败：${err.message}`); }
    });
  });
  return d;
}

async function openTargetModal() {
  $('target-modal').classList.remove('hidden');
  $('target-msg').textContent = '';
  $('import-msg').textContent = '';
  try { await loadTargets(); } catch (err) {
    $('target-msg').className = 'form-msg err';
    $('target-msg').textContent = `读取目标库失败：${err.message}`;
  }
}

function bindTargetModal() {
  $('btn-config-target').addEventListener('click', openTargetModal);
  $('btn-target-list').addEventListener('click', openTargetModal);
  $('target-close').addEventListener('click', () => $('target-modal').classList.add('hidden'));
  $('target-done').addEventListener('click', () => $('target-modal').classList.add('hidden'));

  $('t-save').addEventListener('click', async () => {
    const msg = $('target-msg');
    msg.className = 'form-msg';
    msg.textContent = '保存中…';
    try {
      const payload = await postJSON('/api/sfa/targets', {
        items: [{
          employee_id: $('t-employee').value.trim(),
          employee_name: $('t-name').value.trim() || null,
          year: Number($('t-year').value.trim()),
          month: Number($('t-month').value.trim()),
          target_store_count: Number($('t-value').value.trim()),
          config_status: $('t-status').value,
          note: $('t-note').value.trim() || null,
        }],
      });
      const d = payload.data;
      if (d.errors && d.errors.length) {
        // 校验类错误（目标为 0 / 非 2026 年 / 缺字段）不会抛 HTTP 错误，
        // 而是逐条返回在 errors 里；这里必须如实显示失败，不能报「已保存」。
        msg.className = 'form-msg err';
        msg.textContent = `未保存：${d.errors[0].error}`;
        await loadTargets();
        return;
      }
      msg.className = 'form-msg ok';
      msg.textContent = `已保存：新增 ${d.created} 条、更新 ${d.updated} 条`
        + (d.warnings.length ? ` · ${d.warnings.join('；')}` : '');
      await loadTargets();
      await loadAll();
    } catch (err) {
      msg.className = 'form-msg err';
      msg.textContent = `保存失败：${err.message}`;
    }
  });

  $('t-template').addEventListener('click', () => {
    $('t-csv').value = '客户经理ID,客户经理姓名,年份,月份,月度目标门店数,配置状态,备注\n'
      + 'MGR-001,沈亦辰,2026,9,20,ACTIVE,\n';
  });

  // ---- 示例目标（DEMO）：只为让完成率图表可读，全程标注、可一键清除
  $('btn-seed-demo').addEventListener('click', async () => {
    const msg = $('target-msg');
    msg.className = 'form-msg';
    msg.textContent = '正在生成示例目标…';
    try {
      const payload = await postJSON('/api/sfa/targets/seed-demo', {});
      const d = payload.data;
      msg.className = 'form-msg ok';
      msg.textContent = `已生成示例目标：新增 ${d.seeded} 条、更新 ${d.updated} 条`
        + `（${d.employees} 人 / ${d.months.join('、')} 月）`
        + (d.skipped_real_targets ? ` · 跳过 ${d.skipped_real_targets} 条真实目标（不覆盖）` : '');
      await loadTargets();
      await loadAll();
    } catch (err) {
      msg.className = 'form-msg err';
      msg.textContent = `生成失败：${err.message}`;
    }
  });

  $('btn-clear-demo').addEventListener('click', async () => {
    const msg = $('target-msg');
    msg.className = 'form-msg';
    msg.textContent = '正在清除示例目标…';
    try {
      const payload = await postJSON('/api/sfa/targets/clear-demo', {});
      const d = payload.data;
      msg.className = d.deleted ? 'form-msg ok' : 'form-msg';
      msg.textContent = d.deleted
        ? `已清除 ${d.deleted} 条示例目标，页面回到「未配置」状态`
        : '当前没有示例目标，无需清除';
      await loadTargets();
      await loadAll();
    } catch (err) {
      msg.className = 'form-msg err';
      msg.textContent = `清除失败：${err.message}`;
    }
  });

  $('t-import').addEventListener('click', async () => {
    const msg = $('import-msg');
    msg.className = 'form-msg';
    msg.textContent = '导入中…';
    try {
      const payload = await postJSON('/api/sfa/targets/import', { csv: $('t-csv').value });
      const d = payload.data;
      const failed = (d.errors || []).length;
      msg.className = (d.created + d.updated) === 0 && failed ? 'form-msg err' : 'form-msg ok';
      msg.textContent = `导入完成：解析 ${d.parsed_rows} 行，新增 ${d.created}、更新 ${d.updated}`
        + (failed ? ` · ${failed} 行有问题（首条：第 ${d.errors[0].row} 行 ${d.errors[0].error}）` : '');
      await loadTargets();
      await loadAll();
    } catch (err) {
      msg.className = 'form-msg err';
      msg.textContent = `导入失败：${err.message}`;
    }
  });
}

/* ------------------------------------------------------------ 加载 */
async function loadDetail() {
  const host = $('detail-table');
  if (!host.querySelector('table')) loadingBox(host, '正在查询明细…');
  try {
    const payload = await getJSON('/api/sfa/visit/detail', {
      ...params(), page: state.page, limit: state.limit,
      sort: state.sort, order: state.order, q: state.q,
    });
    renderTable(host, payload.data.rows);
    renderDetailFooter(payload);
  } catch (err) {
    host.innerHTML = `<div class="inline-empty"><b>明细加载失败</b><span>${err.message}</span></div>`;
  }
}

function markLoading() {
  hideTooltip();
  setStatus('查询中…');
  loadingBox($('chart-rank'), '正在查询数据…');
  loadingBox($('chart-trend'), '正在查询数据…');
  $('chart-region').innerHTML = '<div class="inline-empty">正在查询数据…</div>';
}

async function loadAll() {
  markLoading();
  showAlert('');
  try {
    state.options = (await getJSON('/api/sfa/visit/options', params())).data;
    renderFilterOptions();
    const payload = await getJSON('/api/sfa/visit', params());
    state.data = payload;
    // 一条目标都没配置时，「完成率排名」必然是空图；首次加载自动落到「拜访门店数」，
    // 但仍然把完成率列显示成「未配置」，不造一个假完成率出来。用户手动切换后不再干预。
    if (!state.rankPicked && state.rankMode === 'rate'
        && (payload.data.kpi.employee_configured || 0) === 0) {
      state.rankMode = 'stores';
      $('rank-mode').querySelectorAll('button').forEach(b =>
        b.classList.toggle('on', b.dataset.mode === 'stores'));
    }
    renderKpi(payload.data.kpi);
    renderRegion(payload.data.regions);
    renderRank(payload.data.employee_rank);
    renderTrend(payload.data.trend, payload.data.coverage);
    renderCoverage(payload.data.coverage);
    renderNotices(payload);
    renderCapability(payload);
    setStatus(`就绪 · ${(payload.meta.generated_at || '').slice(11, 19)}`);
    writeUrl();
    await loadDetail();
  } catch (err) {
    setStatus('加载失败', true);
    showAlert(`数据加载失败：${err.message}`);
    emptyBox($('chart-rank'), '加载失败', err.message);
    emptyBox($('chart-trend'), '加载失败', err.message);
  }
}

/* ------------------------------------------------------------ 事件绑定 */
function bind() {
  $('f-month').addEventListener('change', (e) => {
    state.months = e.target.value; state.page = 1; writeUrl(); loadAll();
  });
  $('f-region').addEventListener('change', (e) => {
    state.regions = e.target.value; state.employee = ''; state.page = 1;
    writeUrl(); loadAll();
  });
  $('f-md').addEventListener('change', (e) => {
    state.marketDepartment = e.target.value; state.employee = ''; state.page = 1;
    writeUrl(); loadAll();
  });
  $('f-employee').addEventListener('change', (e) => {
    state.employee = e.target.value; state.page = 1; writeUrl(); loadAll();
  });
  $('btn-reset').addEventListener('click', () => {
    state.months = ''; state.regions = ''; state.marketDepartment = '';
    state.employee = ''; state.page = 1; state.sort = ''; state.order = 'asc';
    state.q = ''; $('detail-q').value = '';
    writeUrl(); loadAll();
  });
  $('btn-refresh').addEventListener('click', loadAll);

  $('rank-mode').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-mode]');
    if (!btn) return;
    state.rankMode = btn.dataset.mode;
    state.rankPicked = true;
    $('rank-mode').querySelectorAll('button')
      .forEach(b => b.classList.toggle('on', b === btn));
    if (state.data) renderRank(state.data.data.employee_rank);
  });

  $('trend-metric').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-metric]');
    if (!btn) return;
    state.trendMetric = btn.dataset.metric;
    $('trend-metric').querySelectorAll('button')
      .forEach(b => b.classList.toggle('on', b === btn));
    if (state.data) renderTrend(state.data.data.trend, state.data.data.coverage);
  });

  let timer = 0;
  $('detail-q').addEventListener('input', (e) => {
    clearTimeout(timer);
    const value = e.target.value.trim();
    timer = setTimeout(() => {
      state.q = value; state.page = 1; writeUrl(); loadDetail();
    }, 260);
  });
  $('detail-prev').addEventListener('click', () => {
    if (state.page > 1) { state.page -= 1; writeUrl(); loadDetail(); }
  });
  $('detail-next').addEventListener('click', () => {
    state.page += 1; writeUrl(); loadDetail();
  });
  $('drawer-close').addEventListener('click', () => $('modal').classList.add('hidden'));
  $('modal').addEventListener('click', (e) => {
    if (e.target.id === 'modal') $('modal').classList.add('hidden');
  });
  $('target-modal').addEventListener('click', (e) => {
    if (e.target.id === 'target-modal') $('target-modal').classList.add('hidden');
  });
  bindTargetModal();
}

readUrl();
bind();
loadAll();
