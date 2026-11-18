/* =====================================================================
 * cockpit.js — AI 经营驾驶舱首页
 *
 * 原则
 *   1. 所有指标由 /api/cockpit 一次下发，前端不做任何口径计算（只做显示格式化）。
 *   2. 筛选条件（月份 / 战区 / KA）是唯一状态源，任何交互都改它再重取，
 *      保证地图、KPI、趋势、KA、利润五块始终同一口径。
 *   3. 预测与实测在视觉上分线型（实线 / 虚线）与文案上分字段，不混为一个数。
 *   4. AI 洞察只在点击时请求 /api/cockpit/insight，不随页面加载自动执行。
 * ===================================================================== */
import { api } from './api.js';
import { CHINA_MAP } from './china-map.js';

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};

const state = {
  month: 0,
  region: '',
  ka: '',
  grain: 'month',
  metric: 'te',
  data: null,
  options: null,
  loading: false,
};

/* ------------------------------------------------------------------ 格式化 */
const nf = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v)
  ? '—'
  : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }));

function fmtTe(v, d = 1) {
  if (v === null || v === undefined) return '—';
  const wan = v / 10000;
  if (Math.abs(wan) >= 10000) return `${nf(wan / 10000, 2)} 亿`;
  return `${nf(wan, d)} 万`;
}
function fmtMoney(v, d = 2) {
  if (v === null || v === undefined) return '—';
  if (Math.abs(v) >= 1e8) return `${nf(v / 1e8, d)} 亿`;
  if (Math.abs(v) >= 1e4) return `${nf(v / 1e4, d)} 万`;
  return nf(v, 0);
}
function fmtPct(v, d = 1) { return v === null || v === undefined ? '—' : `${nf(v, d)}%`; }
function deltaClass(v) { return v === null || v === undefined ? 'flat' : (v > 0.0001 ? 'up' : (v < -0.0001 ? 'down' : 'flat')); }
function sign(v, d = 1, suffix = '') {
  if (v === null || v === undefined) return '—';
  const s = v > 0 ? '+' : '';
  return `${s}${nf(v, d)}${suffix}`;
}
function attClass(v) {
  if (v === null || v === undefined) return 'mid';
  return v >= 100 ? 'good' : (v >= 95 ? 'mid' : 'bad');
}

// 气泡标签的相对位置：战区A在东侧、战区E在西侧，标签朝空白处放，避免五个标签互相压住
const LABEL_POS = {
  战区A: ['right', 0], 战区B: ['top', 0], 战区C: ['bottom', 0], 战区D: ['left', 0], 战区E: ['top', 0],
};

const ICONS = {
  target: '<path d="M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z"/><path d="M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9z"/><path d="M12 13.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z"/>',
  box: '<path d="M3 8.5 12 4l9 4.5v7L12 20l-9-4.5z"/><path d="M3 8.5 12 13l9-4.5"/><path d="M12 13v7"/>',
  trend: '<path d="M3 16l5.5-5.5 3.5 3L21 5"/><path d="M15 5h6v6"/>',
  money: '<path d="M4 6h16v12H4z"/><path d="M8 12h8"/><path d="M12 9v6"/>',
  wallet: '<path d="M4 7h13l3 4v6H4z"/><path d="M4 7V5h12"/><path d="M16 12h3"/>',
  percent: '<path d="M6 18 18 6"/><circle cx="7.5" cy="7.5" r="2.2"/><circle cx="16.5" cy="16.5" r="2.2"/>',
};
const KPI_ICON = {
  te_attainment: 'target', te_actual: 'box', te_forecast: 'trend',
  erp_amount: 'money', gross_profit: 'wallet', gross_margin: 'percent',
};

/* ------------------------------------------------------------------ 状态与提示 */
function setStatus(text, kind = 'ok') {
  $('#status').textContent = text;
  $('#status-dot').className = `ck-dot is-${kind}`;
}
let toastTimer = null;
function toast(msg) {
  const t = $('#ck-toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2000);
}
function showError(msg) {
  const box = $('#ck-error');
  box.textContent = msg;
  box.classList.remove('hidden');
  setStatus('查询失败', 'err');
}
function clearError() { $('#ck-error').classList.add('hidden'); }

/* ------------------------------------------------------------------ 初始化 */
async function boot() {
  setStatus('加载筛选项…', 'busy');
  try {
    const opt = await api.cockpitOptions();
    state.options = opt.data;
    fillSelects();
    bindEvents();
    await load();
  } catch (err) {
    showError(`初始化失败：${err.message}`);
  }
}

function fillSelects() {
  const o = state.options;
  const y = $('#f-year');
  y.innerHTML = o.years.map((x) => `<option value="${x.value}">${x.label}</option>`).join('');
  y.disabled = false;

  $('#f-month').innerHTML = o.months.map((x) => `<option value="${x.value}">${x.label}</option>`).join('');
  $('#f-region').innerHTML = '<option value="">全部战区</option>'
    + o.regions.map((x) => `<option value="${x.value}">${x.label}</option>`).join('');
  $('#f-ka').innerHTML = `<option value="">全部客户</option>`
    + `<optgroup label="按渠道">${(o.groups || []).map((g) =>
      `<option value="${g.key}">${g.label}（${g.customers} 家）</option>`).join('')}</optgroup>`
    + (o.groups || []).map((g) => {
      const members = (o.kas || []).filter((x) => x.group === g.key);
      if (!members.length) return '';
      return `<optgroup label="${g.label}">${members.map((x) =>
        `<option value="${x.customer_id}">${x.label}</option>`).join('')}</optgroup>`;
    }).join('');
}

/* 页内 AI 助手（/agent-dock.js）读取当前筛选：只返回快照，不改页面状态。
   这样「在这里问」问到的就是上面这一屏看到的范围，不用再描述一遍。 */
window.__ckFilters = () => {
  const o = state.options || {};
  const ka = state.ka || '';
  const hit = (o.kas || []).find((x) => x.customer_id === ka);
  const allMonths = (o.months || [])
    .map((x) => Number(x.value))
    .filter((v) => v >= 1 && v <= 12);
  return {
    year: 2026,
    month: state.month || 0,                                   // 0 = 累计
    months: state.month ? [state.month] : allMonths,            // 累计时取全部已发生月份
    region: state.region || '',
    customer: hit ? hit.label : '',                             // 渠道分组不当作客户传
  };
};

function bindEvents() {
  $('#f-month').addEventListener('change', (e) => { state.month = Number(e.target.value); load(); });
  $('#f-region').addEventListener('change', (e) => { state.region = e.target.value; load(); });
  $('#f-ka').addEventListener('change', (e) => { state.ka = e.target.value; load(); });
  $('#f-year').addEventListener('change', () => toast('当前数据只覆盖 2026 年，年份不可切换'));
  $('#btn-refresh').addEventListener('click', () => load(true));
  $('#btn-reset').addEventListener('click', () => {
    state.month = 0; state.region = ''; state.ka = '';
    $('#f-month').value = '0'; $('#f-region').value = ''; $('#f-ka').value = '';
    load();
  });
  $('#btn-ai').addEventListener('click', openInsight);
  $('#btn-ai-2').addEventListener('click', openInsight);
  $('#ai-close').addEventListener('click', closeInsight);
  $('#ai-mask').addEventListener('click', closeInsight);
  $('#ai-regen').addEventListener('click', () => loadInsight(true));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeInsight(); });

  document.querySelectorAll('#trend-grain button').forEach((b) => {
    b.addEventListener('click', () => {
      state.grain = b.dataset.grain;
      document.querySelectorAll('#trend-grain button').forEach((x) => x.classList.toggle('on', x === b));
      renderTrend();
    });
  });
  document.querySelectorAll('#trend-metric button').forEach((b) => {
    b.addEventListener('click', () => {
      state.metric = b.dataset.metric;
      document.querySelectorAll('#trend-metric button').forEach((x) => x.classList.toggle('on', x === b));
      renderTrend();
    });
  });

  let rt = null;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => { if (state.data) { renderMap(); renderCompare(); renderTrend(); renderProfit(); } }, 160);
  });
}

/* ------------------------------------------------------------------ 取数 */
async function load(withToast = false) {
  if (state.loading) return;
  state.loading = true;
  setStatus('查询中…', 'busy');
  try {
    const res = await api.cockpit({ month: state.month, region: state.region, ka: state.ka });
    state.data = res;
    clearError();
    renderAll(res);
    setStatus(`已更新 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`, 'ok');
    if (withToast) toast('已按当前筛选刷新');
  } catch (err) {
    showError(`数据加载失败：${err.message}`);
  } finally {
    state.loading = false;
  }
}

function renderAll(res) {
  const d = res.data;
  const m = res.meta;
  $('#period-chip').textContent = m.period_label;
  const regionTxt = m.region === '全部' ? '全国' : m.region;
  const kaTxt = m.ka === '全部' ? '全部客户' : (m.ka_label || labelOfKa(m.ka));
  $('#scope-chip').textContent = `${regionTxt} · ${kaTxt}`;
  $('#budget-chip').textContent = 'TE 预算 · 模拟数据';
  // 批次号是内部追溯用，Demo 不放在卡面上，鼠标悬停可查
  $('#budget-chip').title = `TE 预算批次 ${m.budget_batch.batch_id}（MOCK_BUDGET_TE）`;
  renderKpi(d.kpi);
  renderMap();
  renderCompare();
  renderTrend();
  renderKa();
  renderProfit();
  renderAlerts();
  renderFooterNote(m, d);
}

function labelOfKa(cid) {
  const grp = (state.options?.groups || []).find((g) => g.key === cid);
  if (grp) return grp.label;
  const hit = (state.options?.kas || []).find((x) => x.customer_id === cid);
  return hit ? hit.label : cid;
}

/* ------------------------------------------------------------------ KPI */
function renderKpi(items) {
  const host = $('#kpi-grid');
  host.innerHTML = '';
  items.forEach((it) => {
    const card = el('article', `ck-kpi${it.primary ? ' is-primary' : ''}`);
    const dv = it.delta?.value;
    const unit = it.delta?.unit || '';
    const dTxt = it.delta?.value === null || it.delta?.value === undefined
      ? '—'
      : (unit === 'pp' ? `${sign(dv, 2)}pp` : `${sign(dv, 1)}%`);
    card.innerHTML = `
      <div class="ck-kpi-head">
        <span class="ck-kpi-icon">
          <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor"
               stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${ICONS[KPI_ICON[it.key]] || ICONS.box}</svg>
        </span>
        <span class="ck-kpi-label">${it.label}</span>
      </div>
      <div class="ck-kpi-value">
        <b>${it.key === 'te_actual' || it.key === 'te_forecast' ? nf(it.value / 10000, 1)
          : it.key === 'erp_amount' || it.key === 'gross_profit' ? fmtMoney(it.value)
            : nf(it.value, it.digits ?? 1)}</b>
        <span class="ck-kpi-unit">${unitOf(it)}</span>
      </div>
      <div class="ck-kpi-delta">
        <span class="ck-dv ${deltaClass(dv)}">${dTxt}</span>
        <span class="ck-dl">${it.delta?.label || ''}</span>
      </div>
      <div class="ck-kpi-sub">${it.sub || ''}</div>`;
    card.title = it.caliber || '';
    if (it.key === 'te_forecast') card.insertAdjacentHTML('beforeend', '<span class="ck-kpi-badge">模型外推</span>');
    host.appendChild(card);
  });
}
function unitOf(it) {
  if (it.unit === 'TE') return '万 TE';   // 数值已折算为万 TE，单位只出现一次
  if (it.unit === '元') return '元';
  if (it.unit === '%') return '%';
  return it.unit || '';
}

/* ------------------------------------------------------------------ 地图 */
function sizeOf(host, minH = 220) {
  const r = host.getBoundingClientRect();
  return { w: Math.max(320, r.width), h: Math.max(minH, r.height) };
}

function renderMap() {
  const d = state.data?.data;
  if (!d) return;
  const host = $('#ck-map');
  const { w, h } = sizeOf(host, 176);
  const items = d.region.items;
  const maxTe = Math.max(1, ...items.map((r) => r.te || 0));

  const vb = CHINA_MAP.viewBox.split(' ').map(Number);   // 0 0 w h
  const mapW = vb[2], mapH = vb[3];
  const pad = 6;
  const scale = Math.min((w - pad * 2) / mapW, (h - pad * 2) / mapH);
  const offX = (w - mapW * scale) / 2;
  const offY = (h - mapH * scale) / 2;

  const provinces = CHINA_MAP.provinces.map((p) =>
    `<path class="ck-province" d="${p.d}" data-name="${p.name}"></path>`).join('');

  // 南海诸岛：标准地图的右下角插图
  let inset = '';
  if (CHINA_MAP.inset) {
    const iw = CHINA_MAP.inset.w, ih = CHINA_MAP.inset.h;
    const bx = mapW - iw - 10, by = mapH - ih - 8;
    inset = `<g transform="translate(${bx},${by})">
      <rect class="ck-inset-box" x="-3" y="-3" width="${iw + 6}" height="${ih + 6}" rx="3"></rect>
      <path class="ck-inset" d="${CHINA_MAP.inset.d}"></path>
      <text class="ck-inset-label" x="${iw / 2}" y="${ih + 12}" text-anchor="middle">南海诸岛</text>
    </g>`;
  }

  const bubbles = items.map((r) => {
    const anchor = CHINA_MAP.anchors[r.name];
    if (!anchor) return '';
    const x = anchor[0], y = anchor[1];
    const rad = 9 + 22 * Math.sqrt((r.te || 0) / maxTe);
    const active = state.region === r.name;
    const dim = state.region && !active ? ' is-dim' : '';
    const [side] = LABEL_POS[r.name] || ['top', 0];
    let lx = 0, ly = 0, anchorAttr = 'middle';
    if (side === 'right') { lx = rad + 13; ly = -1; anchorAttr = 'start'; }
    else if (side === 'left') { lx = -rad - 13; ly = -1; anchorAttr = 'end'; }
    else if (side === 'bottom') { lx = 0; ly = rad + 13; }
    else { lx = 0; ly = -rad - 13; }
    return `<g class="ck-bubble${dim}" data-region="${r.name}" transform="translate(${x},${y})"
      ><title>${r.name}：实际 ${fmtTe(r.te, 1)} TE / 预算 ${fmtTe(r.budget_te, 1)} TE，达成 ${fmtPct(r.attainment_pct, 2)}；金额 ${fmtMoney(r.amount, 1)} 元</title>
      <circle class="ck-bub-halo" r="${rad + 5}"></circle>
      <circle class="ck-bub-core" r="${rad}"></circle>
      <text x="${lx}" y="${ly}" text-anchor="${anchorAttr}" paint-order="stroke"
            stroke="#ffffff" stroke-width="3.2">${r.name}</text>
    </g>`;
  }).join('');

  host.innerHTML = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img">
    <defs>
      <linearGradient id="ckBubbleGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#4f92f7"></stop>
        <stop offset="100%" stop-color="#1a63e6"></stop>
      </linearGradient>
      <filter id="ckShadow" x="-30%" y="-30%" width="160%" height="160%">
        <feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#1a6ff0" flood-opacity="0.18"></feDropShadow>
      </filter>
    </defs>
    <g transform="translate(${offX},${offY}) scale(${scale})">
      ${provinces}
      ${inset}
      <g filter="url(#ckShadow)">${bubbles}</g>
    </g>
  </svg>`;

  host.querySelectorAll('.ck-bubble').forEach((g) => {
    g.addEventListener('click', () => {
      const name = g.dataset.region;
      state.region = state.region === name ? '' : name;
      $('#f-region').value = state.region;
      load(true);
    });
  });

  // 战区 TE 结构（占比）——全页唯一出现「战区占比」的地方。
  // 预算 / 实际绝对值 / 达成率 / 全年预测只在《预算达成与销售预测》里出现一次，
  // 毛利只在《利润分析》里出现一次，这里不重复第二遍；绝对量与预算走气泡悬浮提示。
  const strip = $('#region-strip');
  const body = strip.closest('.ck-map-body');
  if (body) body.classList.toggle('is-solo', !!state.region);
  const sumTe = items.reduce((a, r) => a + (r.te || 0), 0) || 1;
  const entSum = items.reduce((a, r) => a + (r.entity_count || 0), 0);
  // 预测只进悬浮提示，不占版面：全页「战区全年预测」的唯一主展示在《预算达成与销售预测》
  const sumFc = items.reduce((a, r) => a + (r.forecast_te || 0), 0) || 1;
  const RAMP = ['#1a63e6', '#3d84f2', '#6aa5f8', '#96c2fb', '#c3dafa'];
  const shareOf = (r) => ((r.te || 0) / sumTe) * 100;
  const scopeName = state.region ? '当前范围' : '全国合计';

  strip.innerHTML = `
    <div class="ck-share-title">战区 TE 结构<em>占比 = 该战区实际 TE ÷ ${scopeName}；1 组 = 1 客户 × 1 市场部</em></div>
    <div class="ck-share-bar" role="img" aria-label="五大战区实际 TE 占比">
      ${items.map((r, i) => `<span style="width:${shareOf(r).toFixed(2)}%;background:${RAMP[i % RAMP.length]}"
        title="${r.name} 占比 ${fmtPct(shareOf(r), 1)}"></span>`).join('')}
    </div>
    <div class="ck-share-rows">
      ${items.map((r, i) => `<div class="ck-srow${state.region === r.name ? ' is-active' : ''}" data-region="${r.name}"
        title="${r.name}｜实际 ${fmtTe(r.te, 1)} TE（占比 ${fmtPct(shareOf(r), 1)}）· 全年预测 ${fmtTe(r.forecast_te, 1)} TE（${fmtPct((r.forecast_te || 0) / sumFc * 100, 1)}）· 预算 ${fmtTe(r.budget_te, 1)} TE · 达成 ${fmtPct(r.attainment_pct, 2)} · 金额 ${fmtMoney(r.amount, 1)} 元 · 毛利 ${fmtMoney(r.gross_profit, 1)} 元">
        <i class="ck-sdot" style="background:${RAMP[i % RAMP.length]}"></i>
        <div class="ck-sbody">
          <div class="ck-sline"><span class="ck-sname">${r.name}</span><span class="ck-sshare">${fmtPct(shareOf(r), 1)}</span></div>
          <div class="ck-smeta">${r.entity_count} 组</div>
        </div>
      </div>`).join('')}
      <div class="ck-srow is-total">
        <i class="ck-sdot"></i>
        <div class="ck-sbody">
          <div class="ck-sline"><span class="ck-sname">${scopeName}</span><span class="ck-sshare">100.0%</span></div>
          <div class="ck-smeta">${entSum} 组</div>
        </div>
      </div>
    </div>`;
  strip.querySelectorAll('.ck-srow[data-region]').forEach((n) => {
    n.addEventListener('click', () => {
      const name = n.dataset.region;
      state.region = state.region === name ? '' : name;
      $('#f-region').value = state.region;
      load(true);
    });
  });
}

/* ------------------------------------------------------------------ 预算 vs 预测 */
function renderCompare() {
  const d = state.data?.data;
  if (!d) return;
  const host = $('#cmp-chart');
  const rows = d.compare;
  // 战区行的金额 / 毛利也从战区明细里取，只放悬浮提示：版面只留预算·实际·达成·预测四列
  const regMap = new Map((d.region.items || []).map((r) => [r.name, r]));
  const max = Math.max(1, ...rows.flatMap((r) => [r.budget_te || 0, r.actual_te || 0]));
  const head = `<div class="ck-cmp-head">
      <span>战区</span>
      <span>同期预算 / 同期实际（TE）</span>
      <span class="ta-r">达成率</span>
      <span class="ta-r">全年预测</span>
    </div>`;
  host.innerHTML = head + rows.map((r) => {
    const bw = Math.max(2, (r.budget_te || 0) / max * 100);
    const aw = Math.max(2, (r.actual_te || 0) / max * 100);
    const rg = r.level === 'region' ? regMap.get(r.label) : null;
    const money = rg
      ? `；金额 ${fmtMoney(rg.amount, 1)} 元 · 毛利 ${fmtMoney(rg.gross_profit, 1)} 元（毛利率 ${fmtPct(rg.gross_margin_pct, 2)}）`
      : '';
    return `<div class="ck-cmp-row" data-region="${r.level === 'region' ? r.label : ''}"
      title="${r.label}｜同期预算 ${fmtTe(r.budget_te, 1)} TE / 同期实际 ${fmtTe(r.actual_te, 1)} TE，达成 ${fmtPct(r.attainment_pct, 2)}；全年预测 ${fmtTe(r.forecast_te, 1)} TE（1–10 月实际 + 11–12 月模型外推），占全年预算 ${fmtPct(r.forecast_year_pct, 2)}${money}">
      <div class="ck-cmp-label">${r.label}</div>
      <div class="ck-cmp-bars">
        <div class="ck-bar-wrap"><div class="ck-bar" style="width:${bw}%"></div></div>
        <div class="ck-bar-wrap"><div class="ck-bar is-actual" style="width:${aw}%"></div></div>
      </div>
      <div class="ck-cmp-val">
        <b class="${attClass(r.attainment_pct)}">${fmtPct(r.attainment_pct, 2)}</b>
        <span>${fmtTe(r.actual_te, 1)} / ${fmtTe(r.budget_te, 1)}</span>
      </div>
      <div class="ck-cmp-fc">
        <b>${fmtTe(r.forecast_te, 1)}</b>
        <span class="${attClass(r.forecast_year_pct)}">${fmtPct(r.forecast_year_pct, 2)}</span>
      </div>
    </div>`;
  }).join('');
  host.querySelectorAll('.ck-cmp-row[data-region]').forEach((n) => {
    n.style.cursor = 'pointer';
    n.addEventListener('click', () => {
      const name = n.dataset.region;
      if (!name) return;
      state.region = state.region === name ? '' : name;
      $('#f-region').value = state.region;
      load(true);
    });
  });

  const t = d.totals;
  // 卡面只留一句话，完整口径进悬浮提示（Demo 展示优先，不留审计脚注）
  const cmpNote = $('#cmp-note');
  cmpNote.textContent = '预测 = 1–10 月实际 + 11–12 月外推';
  cmpNote.title = '条长 = 同期预算 / 同期实际；预测列为全年 12 个月（1–10 月实际 + 11–12 月外推），'
    + '与同期预算不同期，不可相减。';
}

/* ------------------------------------------------------------------ 趋势 */
function renderTrend() {
  const d = state.data?.data;
  if (!d) return;
  const host = $('#trend-chart');
  const { w, h } = sizeOf(host);
  const isQ = state.grain === 'quarter';
  const isAmount = state.metric === 'amount';
  const pad = { l: 46, r: 12, t: 12, b: 22 };
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;

  let series;      // [{key,label,points:[{x,y,val}],dash}]
  let labels;
  let unit;

  if (isQ) {
    labels = d.trend.quarters.map((q) => q.label);
    const act = d.trend.quarters.map((q, i) => ({ i, v: q.actual_te }));
    const fc = d.trend.quarters.map((q, i) => ({ i, v: q.forecast_te }));
    // 预算不在趋势图里重复：预算口径统一收敛到《预算达成与销售预测》一张卡
    series = [
      { key: 'actual', label: '实际', pts: act, cls: 'ck-line-actual' },
      { key: 'fc', label: '预测', pts: fc, cls: 'ck-line-fc' },
    ];
    unit = 'TE';
  } else {
    labels = d.trend.months.map((m) => m.label.replace(' 月', ''));
    const act = d.trend.months.map((m, i) => ({ i, v: isAmount ? m.amount : m.actual_te }));
    const fc = d.trend.months.map((m, i) => ({ i, v: isAmount ? null : m.forecast_te }));
    series = [
      { key: 'actual', label: '实际', pts: act, cls: 'ck-line-actual' },
      { key: 'fc', label: '预测', pts: fc, cls: 'ck-line-fc' },
    ];
    unit = isAmount ? '元' : 'TE';
  }

  const allVals = series.flatMap((s) => s.pts.map((p) => p.v)).filter((v) => v !== null && v !== undefined);
  const max = Math.max(1, ...allVals) * 1.12;
  const n = labels.length;
  const xOf = (i) => pad.l + (n === 1 ? iw / 2 : (iw * i) / (n - 1));
  const yOf = (v) => pad.t + ih - (v / max) * ih;

  const ticks = 4;
  const grid = Array.from({ length: ticks + 1 }, (_, k) => {
    const v = (max / ticks) * k;
    const y = yOf(v);
    const txt = isAmount ? fmtMoney(v, 0) : fmtTe(v, 0);
    return `<line class="ck-grid-line" x1="${pad.l}" y1="${y}" x2="${w - pad.r}" y2="${y}"></line>
      <text class="ck-axis" x="${pad.l - 6}" y="${y + 3}" text-anchor="end">${txt}</text>`;
  }).join('');

  const pathOf = (pts, onlyOccurred) => {
    const seg = pts.filter((p) => p.v !== null && p.v !== undefined);
    if (!seg.length) return '';
    let dstr = '';
    let started = false;
    seg.forEach((p, k) => {
      const closeGap = onlyOccurred && k > 0 && seg[k - 1].i !== p.i - 1;
      dstr += `${!started || closeGap ? 'M' : 'L'}${xOf(p.i).toFixed(1)},${yOf(p.v).toFixed(1)} `;
      started = true;
    });
    return dstr.trim();
  };

  const lines = series.map((s) => {
    if (s.key === 'fc') {
      // 预测线从最后一个实际点接上，视觉上不断裂
      const actual = series.find((x) => x.key === 'actual').pts.filter((p) => p.v !== null && p.v !== undefined);
      const fcs = s.pts.filter((p) => p.v !== null && p.v !== undefined);
      if (!fcs.length) return '';
      const start = actual.length ? actual[actual.length - 1] : null;
      const dstr = (start ? `M${xOf(start.i)},${yOf(start.v)} ` : '')
        + fcs.map((p) => `L${xOf(p.i).toFixed(1)},${yOf(p.v).toFixed(1)}`).join(' ');
      return `<path class="ck-line-fc" d="${dstr}"></path>`
        + fcs.map((p) => `<circle class="ck-dot-fc" cx="${xOf(p.i)}" cy="${yOf(p.v)}" r="3"></circle>`).join('');
    }
    return `<path class="${s.cls}" fill="none" d="${pathOf(s.pts, false)}"></path>`
      + (s.key === 'actual' ? s.pts.filter((p) => p.v !== null && p.v !== undefined)
        .map((p) => `<circle class="ck-dot-actual" cx="${xOf(p.i)}" cy="${yOf(p.v)}" r="3.2"></circle>`).join('') : '');
  }).join('');

  const selIdx = isQ ? -1 : d.trend.months.findIndex((m) => m.selected);
  const selBand = (!isQ && selIdx >= 0)
    ? `<rect class="ck-xsel" x="${xOf(selIdx) - iw / (n * 2)}" y="${pad.t}" width="${iw / n}" height="${ih}" rx="4"></rect>`
    : '';

  const xLabels = labels.map((t, i) =>
    `<text class="ck-axis" x="${xOf(i)}" y="${h - 6}" text-anchor="middle">${t}</text>`).join('');

  host.innerHTML = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">
    ${grid}${selBand}${lines}${xLabels}
  </svg>`;

  const t = d.totals;
  const note = $('#trend-note');
  if (isAmount) {
    note.textContent = '金额仅 ERP 实际，不做外推';
    note.title = 'TE 预测模型不外推到金额，避免编造价格假设。';
  } else if (isQ) {
    note.textContent = 'Q4 = 10 月实际 + 11–12 月预测';
    note.title = 'Q1–Q3 为实际，Q4 为「10 月实际 + 11–12 月预测」。预算与达成率见上方《预算达成与销售预测》，本图只表时间走势。';
  } else {
    note.textContent = `虚线 = 11–12 月预测（${fmtTe(t.forecast_tail_te, 1)} TE）`;
    note.title = `1–10 月为 ERP 实际（累计 ${fmtTe(t.te_ytd, 1)} TE）；11–12 月为模型外推，虚线即预测、不是实际。`;
  }
}

/* ------------------------------------------------------------------ 客户贡献（KA / 批发） */
// KA = ERP customer_type NKA（KA客户A/KA客户B/KA客户C/KA客户D/KA客户E/小象/叮咚）；
// 批发单独一列；区域商超（地方连锁）只在顶部结构条里汇总，不占列。
const MIX_COLOR = { ka: '#1a63e6', rt: '#c3dafa', ws: '#7fb4f9' };

function renderKa() {
  const d = state.data?.data;
  if (!d) return;
  const groups = d.channel?.groups || [];
  const byKey = Object.fromEntries(groups.map((g) => [g.key, g]));
  renderChannelMix(groups);

  const solo = !!state.ka;                       // 已筛到某渠道组 / 某客户时只留一列
  const host = $('#ka-split');
  host.classList.toggle('is-solo', solo);

  let cols;
  if (solo) {
    const hit = groups.find((g) => (g.items || []).length);
    cols = hit ? [hit] : [];
  } else {
    cols = [byKey.ka, byKey.ws].filter(Boolean);
  }

  const limit = solo ? 12 : 8;
  host.innerHTML = cols.length ? cols.map((g) => {
    const items = (g.items || []).slice(0, limit);
    const max = Math.max(1, ...items.map((x) => x.te || 0));
    const rows = items.map((k) => `
      <div class="ck-ka-row" data-ka="${k.customer_id}"
        title="${k.name}｜TE ${fmtTe(k.te, 1)} TE（占比 ${fmtPct(k.share_pct, 2)}）· 金额 ${fmtMoney(k.amount)} 元 · 毛利 ${fmtMoney(k.gp)} 元 · 毛利率 ${fmtPct(k.gross_margin_pct, 2)} · TE 达成 ${fmtPct(k.attainment_pct, 2)}">
        <div class="ck-ka-name">${k.customer_type === 'NKA' ? '<i>KA</i>' : ''}${k.name}</div>
        <div class="ck-ka-bar"><span style="width:${Math.max(1.5, (k.te || 0) / max * 100)}%"></span></div>
        <div class="ck-ka-val">${fmtTe(k.te, 1)}</div>
        <div class="ck-ka-share">${fmtPct(k.share_pct, 1)}</div>
      </div>`).join('');
    return `<section class="ck-ka-col">
      <div class="ck-ka2-head">
        <b>${g.label}</b>
        <span>${g.customers} 家 · ${fmtTe(g.te, 1)} TE</span>
      </div>
      ${solo ? `<div class="ck-ka2-sub">金额 ${fmtMoney(g.amount, 1)} 元 · 毛利 ${fmtMoney(g.gross_profit, 1)} 元
        · TE 达成 <b class="ck-att-${attClass(g.attainment_pct)}">${fmtPct(g.attainment_pct, 1)}</b></div>` : ''}
      <div class="ck-ka-head"><span>客户</span><span></span>
        <span class="ta-r">TE</span><span class="ta-r">占比</span></div>
      ${rows || '<p class="ck-ka-empty">该渠道在当前筛选下没有出货</p>'}
      ${solo && items.length === 1 ? regionSplit(items[0]) : ''}
    </section>`;
  }).join('') : '<p class="ck-ka-empty">当前筛选下没有客户出货</p>';

  host.querySelectorAll('.ck-ka-row').forEach((n) => {
    n.addEventListener('click', () => {
      state.ka = state.ka === n.dataset.ka ? '' : n.dataset.ka;
      $('#f-ka').value = state.ka;
      load(true);
    });
  });

  const ka = byKey.ka, ws = byKey.ws, rt = byKey.rt;
  const note = $('#ka-note');
  if (solo) {
    const cur = cols[0];
    note.textContent = cur ? `${cur.label} · ${cur.customers} 家 · 点客户行看分战区` : '当前筛选下没有客户出货';
    note.title = '占比 = 该客户 TE ÷ 当前范围 TE；点上方「恢复全部客户」回到两列。';
    $('#ka-tag').textContent = cur ? `${cur.label} · ${cur.customers} 家` : '无数据';
  } else {
    // 卡面一行讲清占比，完整判据进悬浮提示：Demo 不留审计脚注
    note.textContent = '占比 = 客户 TE ÷ 当前范围 TE';
    note.title = `KA = ERP 客户类型 NKA（${d.ka.total} 家）；批发 = ERP 渠道类型「批发」`
      + `（${ws ? ws.customers : 0} 家、${ws ? fmtTe(ws.te, 1) : '—'} 万 TE、Y26）`
      + (rt ? `；区域商超 ${rt.customers} 家（${fmtPct(rt.share_pct, 1)}）只在结构条汇总` : '');
    $('#ka-tag').textContent = `KA ${ka ? ka.customers : 0} 家 · 批发 ${ws ? ws.customers : 0} 家`;
  }
}

// 单客户下钻：把该客户的 TE 拆到战区，避免只剩一行时整卡空着
function regionSplit(k) {
  const rows = k.by_region || [];
  if (!rows.length) return '';
  const max = Math.max(1, ...rows.map((r) => r.te || 0));
  return `<div class="ck-kar">
    <div class="ck-kar-head">该客户分战区</div>
    ${rows.map((r) => `
      <div class="ck-kar-row">
        <span class="ck-kar-name">${r.name}</span>
        <i class="ck-kar-bar"><em style="width:${Math.max(2, (r.te || 0) / max * 100)}%"></em></i>
        <b>${fmtTe(r.te, 1)}</b><u>${fmtPct(r.share_pct, 1)}</u>
      </div>`).join('')}
  </div>`;
}

function renderChannelMix(groups) {
  const host = $('#mix-strip');
  if (!host || !groups.length) return;

  // 已筛到单渠道 / 单客户时，结构条只剩 100%，改为一条可撤销的筛选提示
  if (state.ka) {
    const isGroup = groups.some((g) => g.key === state.ka);
    const cur = groups.find((g) => g.key === state.ka) || groups.find((g) => (g.items || []).length);
    const name = isGroup ? cur.label : ((cur?.items || [])[0]?.name || state.ka);
    host.innerHTML = `<div class="ck-mix-filter">
      <span>当前范围：<b>${name}</b>${isGroup && cur ? ` · ${cur.customers} 家` : ''}</span>
      <button type="button" id="mix-clear">恢复全部客户 ×</button>
    </div>`;
    host.querySelector('#mix-clear').addEventListener('click', () => {
      state.ka = '';
      $('#f-ka').value = '';
      load(true);
    });
    return;
  }

  const active = (k) => (state.ka === k ? ' is-active' : '');
  host.innerHTML = `
    <div class="ck-mix-head">客户结构<em>占当前范围 TE · 点图例可筛</em></div>
    <div class="ck-mix-bar">${groups.map((g) => `
      <span data-group="${g.key}" class="${active(g.key).trim()}"
        style="width:${Math.max(1, g.share_pct || 0).toFixed(2)}%;background:${MIX_COLOR[g.key] || '#cfdcf0'}"
        title="${g.label}：${fmtTe(g.te, 1)} TE（${fmtPct(g.share_pct, 2)}）· ${g.customers} 家"></span>`).join('')}
    </div>
    <div class="ck-mix-legend">${groups.map((g) => `
      <button type="button" data-group="${g.key}" class="${active(g.key).trim()}"
        title="${g.label}：${g.customers} 家 · ${fmtTe(g.te, 1)} TE">
        <i style="background:${MIX_COLOR[g.key] || '#cfdcf0'}"></i>${g.label}
        <b>${fmtPct(g.share_pct, 1)}</b>
      </button>`).join('')}
    </div>`;

  const pick = (key) => {
    state.ka = state.ka === key ? '' : key;
    $('#f-ka').value = state.ka;
    load(true);
  };
  host.querySelectorAll('.ck-mix-legend button').forEach((b) => {
    b.addEventListener('click', () => pick(b.dataset.group));
  });
  host.querySelectorAll('.ck-mix-bar span').forEach((s) => {
    s.addEventListener('click', () => pick(s.dataset.group));
  });
}

/* ------------------------------------------------------------------ 利润 */
function renderProfit() {
  const d = state.data?.data;
  if (!d) return;
  const p = d.profit;
  $('#pf-gp').textContent = `${fmtMoney(p.national.gross_profit)} 元`;
  $('#pf-gm').textContent = fmtPct(p.national.gross_margin_pct);
  const rows = p.regions;
  const max = Math.max(1, ...rows.map((r) => r.gross_profit || 0));
  $('#profit-chart').innerHTML = rows.map((r) => `
    <div class="ck-pf-row">
      <div class="ck-pf-name">${r.name}</div>
      <div class="ck-pf-bar"><span style="width:${Math.max(2, (r.gross_profit || 0) / max * 100)}%"></span></div>
      <div class="ck-pf-val">${fmtMoney(r.gross_profit, 1)}<em>${fmtPct(r.gross_margin_pct)}</em></div>
    </div>`).join('');
}

/* ------------------------------------------------------------------ 异常 */
function renderAlerts() {
  const d = state.data?.data;
  if (!d) return;
  const list = d.alerts;
  $('#alert-count').textContent = list.length ? `${list.length} 条` : '无';
  const host = $('#alert-list');
  if (!list.length) {
    host.innerHTML = '<p class="ck-alert-empty">当前筛选下没有触发阈值的异常。'
      + '这不等于没有问题，只说明本页规则（达成 < 95%、毛利率偏离 > 0.8pp、月度环比 > 12%）未命中。</p>';
    return;
  }
  host.innerHTML = list.map((a, i) => `
    <div class="ck-alert is-${a.level}" data-i="${i}">
      <i></i>
      <div><h4>${a.title}</h4><p>${a.evidence}</p></div>
    </div>`).join('');
  host.querySelectorAll('.ck-alert').forEach((n) => {
    n.addEventListener('click', () => {
      const a = list[Number(n.dataset.i)];
      const dim = a.dimension || {};
      if (dim.region) { state.region = dim.region; $('#f-region').value = dim.region; load(true); }
      else if (dim.ka) { state.ka = dim.ka; $('#f-ka').value = dim.ka; load(true); }
      else toast('该异常为趋势类，未绑定单一维度');
    });
  });
}

function renderFooterNote(meta, d) {
  const t = d.totals;
  $('#map-tag').textContent = meta.region === '全部'
    ? '实际 TE · 5 大战区'
    : `实际 TE · ${meta.region}（已筛选）`;
  document.title = `AI 经营驾驶舱 · ${meta.period_label} · 达成 ${fmtPct(t.attainment_pct)}`;
}

/* ------------------------------------------------------------------ AI 抽屉 */
function openInsight() {
  const mask = $('#ai-mask');
  const drawer = $('#ai-drawer');
  mask.classList.remove('hidden');
  drawer.classList.remove('hidden');
  drawer.setAttribute('aria-hidden', 'false');
  loadInsight();
}
function closeInsight() {
  $('#ai-mask').classList.add('hidden');
  $('#ai-drawer').classList.add('hidden');
  $('#ai-drawer').setAttribute('aria-hidden', 'true');
}

async function loadInsight(regen = false) {
  const body = $('#ai-body');
  body.innerHTML = '<div class="ck-loading"><span></span><span></span><span></span><span></span></div>';
  $('#ai-scope').textContent = '正在按当前筛选生成…';
  try {
    const res = await api.cockpitInsight({ month: state.month, region: state.region, ka: state.ka });
    renderInsight(res);
    if (regen) toast('已按当前筛选重新生成');
  } catch (err) {
    body.innerHTML = `<div class="ck-error">生成失败：${err.message}</div>`;
    $('#ai-scope').textContent = '生成失败';
  }
}

function renderInsight(res) {
  const d = res.data;
  const m = res.meta;
  $('#ai-scope').textContent = `${m.filters.period} · 战区 ${m.filters.region} · KA ${m.filters.ka === '全部' ? '全部' : labelOfKa(m.filters.ka)}`;
  $('#ai-engine').textContent = `${m.engine.name} · ${m.generated_at.replace('T', ' ').slice(0, 16)}`;

  const parts = [];

  parts.push(`<section class="ck-sec">
    <h3>经营总体评价 <span>事实</span></h3>
    <p class="ck-hl">${d.summary.headline}</p>
    ${d.summary.facts.map((f) => `<div class="ck-fact"><i></i><div>${f.text}</div></div>`).join('')}
  </section>`);

  if (d.insufficient) {
    parts.push(`<section class="ck-sec"><div class="ck-caveat is-high">
      <b>数据不足提示：</b>${d.insufficient}</div></section>`);
  }

  parts.push(`<section class="ck-sec">
    <h3>经营异常发现 <span>事实</span><em>${d.findings.length} 条</em></h3>
    ${d.findings.length ? d.findings.map((f) => `
      <div class="ck-find is-${f.level}">
        <h4>${f.title}</h4>
        <p>${f.fact}</p>
        <p style="margin-top:3px;color:#5a7091">影响：${f.impact}</p>
      </div>`).join('') : '<p class="ck-fact">当前筛选下没有触发阈值的异常。</p>'}
  </section>`);

  parts.push(`<section class="ck-sec">
    <h3>异常原因分析 <span>推测</span><em>必须验证后才可作为决策依据</em></h3>
    ${d.causes.length ? d.causes.map((c) => `
      <div class="ck-cause">
        <div><span class="ck-cause-tag">推测 · 置信度${c.confidence}</span></div>
        <p>${c.statement}</p>
        <ul>${c.basis.map((b) => `<li>${b}</li>`).join('')}
        <li><b>验证方式：</b>${c.verify}</li></ul>
      </div>`).join('') : '<p class="ck-fact">证据不足以支撑归因，不做推测。</p>'}
  </section>`);

  parts.push(`<section class="ck-sec">
    <h3>经营行动建议 <span>建议</span><em>${d.actions.length} 项</em></h3>
    ${d.actions.map((a) => `
      <div class="ck-act">
        <b>${a.action}</b>
        <dl>
          <dt>责任</dt><dd>${a.owner}</dd>
          <dt>时间</dt><dd>${a.horizon}</dd>
          <dt>预期</dt><dd>${a.expected}</dd>
          <dt>验证</dt><dd>${a.verify}</dd>
        </dl>
      </div>`).join('')}
  </section>`);

  parts.push(`<section class="ck-sec">
    <h3>数据依据及来源 <span>可复核</span></h3>
    <table class="ck-table">
      <thead><tr><th>指标</th><th>取值</th><th>口径 / 来源</th></tr></thead>
      <tbody>${d.evidence.map((e) => `<tr>
        <td>${e.metric}</td><td>${e.value}</td>
        <td>${e.caliber}<br><span style="color:#a3b3c9">${e.source}</span></td></tr>`).join('')}
      </tbody>
    </table>
  </section>`);

  parts.push(`<section class="ck-sec">
    <h3>边界与前提</h3>
    ${d.caveats.map((c) => `<div class="ck-caveat${c.level === 'high' ? ' is-high' : ''}">${c.text}</div>`).join('')}
    <p class="ck-foot-note">${m.disclaimer}</p>
  </section>`);

  $('#ai-body').innerHTML = parts.join('');
  $('#ai-body').scrollTop = 0;
}

/* ------------------------------------------------------------------ 启动 */
boot();
