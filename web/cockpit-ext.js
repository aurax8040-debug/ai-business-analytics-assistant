/* =====================================================================
 * cockpit-ext.js — 经营驾驶舱「并入模块」渲染
 *
 * 原「全国经营驾驶舱」(/dashboard.html) 与 AI 经营驾驶舱有 60% 重复
 * （6 张 KPI、月度趋势、战区、KA 结构）。重复部分已删除，只把该页独有的
 * 五块并进本页：SFA 拜访执行 / 批发库存与出库 / SKU 动销 TOP10 /
 * 品类与粒数结构 / 经营明细。
 *
 * 数据全部来自只读接口，前端不重算指标、不造数：
 *   /api/overview  → sfa、wholesale（与原驾驶舱同源）
 *   /api/dashboard → sku、category
 *   /api/detail    → 明细行（服务端分页）
 *
 * 联动范围：月份、战区（与原驾驶舱口径一致：SFA / 批发不响应客户、渠道维度）。
 * ===================================================================== */
import { api } from './api.js';
import * as CH from './charts.js';

const $ = (id) => document.getElementById(id);

const SKU_SHADES = ['#1a6ff0', '#3c85f3', '#5797f5', '#71a9f7', '#8bbaf9',
                    '#a3c9fb', '#b9d7fc', '#cbe2fd', '#dcedfe', '#eaf4ff'];

const state = {
  structSide: 'category',
  detailSource: 'pos',
  detailPage: 1,
  detailToken: 0,
  data: null,
  busy: false,
};

/* ------------------------------------------------------------ 参数 */
/** 从驾驶舱主体读取当前筛选。只取月份与战区，不传客户（口径不同，避免误联动）。 */
function params() {
  if (typeof window.__ckFilters !== 'function') return {};
  const f = window.__ckFilters() || {};
  const p = {};
  if (f.month) p.months = String(f.month);
  if (f.region) p.regions = f.region;
  return p;
}

function scopeText() {
  if (typeof window.__ckFilters !== 'function') return '';
  const f = window.__ckFilters() || {};
  const m = f.month ? `${f.month} 月` : '2026 年 1–10 月累计';
  return f.region ? `${m} · ${f.region}` : `${m} · 五大战区合计`;
}

const money = (v) => {
  if (v === null || v === undefined) return { num: '—', unit: '' };
  const abs = Math.abs(v);
  if (abs >= 1e8) return { num: (v / 1e8).toFixed(2), unit: '亿元' };
  if (abs >= 1e4) return { num: (v / 1e4).toFixed(2), unit: '万元' };
  return { num: CH.fmtNum(v, 0), unit: '元' };
};

const statHTML = (label, num, unit, title = '') => `
  <div class="ck-stat"${title ? ` title="${title}"` : ''}>
    <div class="lb">${label}</div>
    <div class="vl">${num}${unit ? `<small>${unit}</small>` : ''}</div>
  </div>`;

/* ------------------------------------------------------------ SFA 拜访执行 */
function renderSfa(ov) {
  const s = ov.sfa || {};
  /* 区间内没有拜访记录时，不把「没采集」画成 0——两家不是一回事 */
  const empty = !!s.no_window_data;
  const val = (v, maskable = true) =>
    (v === null || v === undefined || (maskable && empty)) ? '—' : CH.fmtNum(v, 0);

  $('x-sfa-stats').innerHTML =
    statHTML('客户经理', val(s.employee_total, false), '人') +
    statHTML('目标门店', val(s.target_store_total), '家') +
    statHTML('实际拜访', val(s.actual_store_total), '家');

  const bar = (name, v) => `
    <div class="ck-prog-i">
      <span class="ck-prog-n">${name}</span>
      <span class="ck-prog-t"><i class="${v !== null && v !== undefined && v < 90 ? 'is-amber' : ''}"
        style="width:${v === null || v === undefined ? 0 : Math.max(0, Math.min(100, v)).toFixed(1)}%"></i></span>
      <span class="ck-prog-v">${v === null || v === undefined ? '未采集' : CH.fmtNum(v, 1) + '%'}</span>
    </div>`;
  $('x-sfa-prog').innerHTML =
    bar('拜访达成率', empty ? null : s.completion_rate) +
    bar('门店覆盖率', empty ? null : s.coverage_rate);

  if (empty) {
    $('x-sfa-tag').textContent = '未采集';
    $('x-sfa-foot').textContent = `所选区间无拜访记录（SFA 数据自 ${s.data_from || '—'} 起）`;
    return;
  }
  $('x-sfa-tag').textContent = s.target_is_demo ? 'DEMO 目标' : '窗口累计';
  $('x-sfa-foot').textContent = s.target_is_demo
    ? `目标含示例数据（DEMO）· 覆盖 ${s.covered_stores} / ${s.universe_stores} 家`
    : `目标 ${s.target_store_total} 家 · 覆盖 ${s.covered_stores} / ${s.universe_stores} 家`;
}

/* ------------------------------------------------------------ 批发库存与出库 */
function renderWholesale(ov) {
  const w = ov.wholesale || {};
  const card = (label, v) => {
    const { num, unit } = money(v);
    return statHTML(label, num, unit);
  };
  $('x-wh-stats').innerHTML =
    card('采购入库', w.purchase_amount) + card('批发出库', w.sales_amount) +
    card('期末库存', w.inventory_amount);

  const series = w.inventory_series || [];
  if (series.length) {
    CH.lineChart($('x-wh-chart'), {
      labels: series.map((x) => String(x.label || '').slice(5) + '月'),
      height: 104, area: true, ticks: 2,
      axisFormat: (v) => CH.fmtCompact(v, '¥'),
      tipFormat: (v) => CH.fmtMoney(v), tipPrefix: '库存金额 · ',
      series: [{ name: '期末库存金额', color: '#1a6ff0', values: series.map((x) => x.value) }],
    });
  } else {
    CH.empty($('x-wh-chart'), '无库存月度快照');
  }
  $('x-wh-foot').textContent =
    `采购 / 出库 / 库存三类金额独立核算、永不相加 · 库存为 ${w.anchor_date || '—'} 时点值`;
}

/* ------------------------------------------------------------ SKU 动销 TOP10 */
function renderSku(dash) {
  const d = dash.sku || {};
  const shown = (d.items || []).slice(0, 10);
  if (!shown.length || !shown.some((r) => r.amount)) {
    CH.empty($('x-sku-chart'), '当前筛选条件下无 SKU 动销数据');
    $('x-sku-foot').textContent = '';
    return;
  }
  CH.hbarChart($('x-sku-chart'), {
    items: shown.map((r, i) => ({
      label: `${r.rank}. ${r.sku_name || r.name}`, value: r.amount, raw: r,
      color: SKU_SHADES[i % SKU_SHADES.length],
      tipExtra: `<div class="tt-row"><span>SKU 编码</span><b>${r.name}</b></div>
                 <div class="tt-row"><span>粒数 / 规格</span><b>${r.pieces_per_box}粒 / ${r.specification}#</b></div>
                 <div class="tt-row"><span>销售盒数</span><b>${CH.fmtNum(r.boxes)}</b></div>`,
    })),
    rowH: 17, labelW: 118, valueW: 62, maxLabel: 14,
    tipFormat: (v) => CH.fmtMoney(v), valueFormat: (v) => CH.fmtCompact(v, '¥'),
    tipLabel: 'POS 销售额',
  });
  $('x-sku-foot').textContent =
    `命中 ${d.total_skus} 个 SKU · 展示 TOP ${shown.length} · 完整清单见 POS 分析`;
}

/* ------------------------------------------------------------ 品类与粒数结构 */
function renderStructure(dash) {
  const d = dash.sku || {};
  const cat = dash.category || {};
  const host = $('x-struct-chart');
  const isCat = state.structSide === 'category';
  $('x-struct-tag').textContent = isCat
    ? '品类A · 品类B · 品类C'
    : `${(d.by_pieces || []).length} 粒数 / ${(d.by_spec || []).length} 规格`;

  if (isCat) {
    const items = cat.items || [];
    if (!items.length || !items.some((r) => r.amount)) {
      CH.empty(host, '当前筛选条件下无品类数据');
      $('x-struct-foot').textContent = '';
      return;
    }
    const shades = ['#1a6ff0', '#4f92f7', '#9cc2fb'];
    const total = items.reduce((a, b) => a + (b.amount || 0), 0);
    const top = [...items].sort((a, b) => (b.share_pct || 0) - (a.share_pct || 0))[0];
    CH.donutChart(host, {
      size: 150, legendMode: 'share',
      items: items.map((r, i) => ({ label: r.name, value: r.amount,
                                    color: shades[i % shades.length] })),
      centerValue: `${CH.fmtNum(top.share_pct || 0, 1)}%`,
      centerLabel: `${top.name}占比`,
    });
    $('x-struct-foot').textContent = `POS 合计 ${CH.fmtCompact(total, '¥')} · ` +
      items.map((r) => `${r.name} ${r.share_pct === null ? '—' : CH.fmtNum(r.share_pct, 1) + '%'}`).join(' · ');
    return;
  }

  const rows = (d.by_pieces || []).slice(0, 8);
  if (!rows.length || !rows.some((r) => r.amount)) {
    CH.empty(host, '当前筛选条件下无粒数结构数据');
    $('x-struct-foot').textContent = '';
    return;
  }
  CH.hbarChart(host, {
    items: rows.map((r, i) => ({
      label: `${r.name} 粒`, value: r.amount, badge: r.share_pct,
      color: SKU_SHADES[i % SKU_SHADES.length],
      tipExtra: `<div class="tt-row"><span>SKU 数</span><b>${r.skus}</b></div>
                 <div class="tt-row"><span>销售盒数</span><b>${CH.fmtNum(r.boxes)}</b></div>`,
    })),
    rowH: 15, labelW: 50, valueW: 62, badgeW: 42, maxLabel: 8,
    badgeFormat: (v) => `${CH.fmtNum(v, 1)}%`,
    tipFormat: (v) => CH.fmtMoney(v), valueFormat: (v) => CH.fmtCompact(v, '¥'),
    tipLabel: 'POS 销售额',
  });
  const specs = (d.by_spec || []).slice(0, 3)
    .map((r) => `${r.name}# ${r.share_pct === null ? '—' : CH.fmtNum(r.share_pct, 1) + '%'}`).join(' / ');
  $('x-struct-foot').textContent =
    `规格 TOP3：${specs || '—'} · 共 ${d.combos} 种「粒数×规格」组合`;
}

/* ------------------------------------------------------------ 经营明细 */
const COL_LABELS = {
  full_date: '日期', region_name_std: '战区', channel_type: '渠道', ka_system: 'KA 系统',
  store_name: '门店', customer_name: '客户', category: '品类',
  sku_code: 'SKU 编码', sales_boxes: '销售盒数', sales_amount: '销售金额',
  gross_profit: '毛利额',
};
const NUMERIC = new Set(['sales_boxes', 'sales_amount', 'gross_profit']);
const DETAIL_COLS = {
  pos: ['full_date', 'region_name_std', 'channel_type', 'ka_system', 'store_name',
        'category', 'sku_code', 'sales_boxes', 'sales_amount'],
  erp: ['full_date', 'region_name_std', 'channel_type', 'customer_name',
        'category', 'sku_code', 'sales_boxes', 'sales_amount', 'gross_profit'],
};

function tableHTML(rows, cols) {
  return '<table><thead><tr>' +
    cols.map((c) => `<th class="${NUMERIC.has(c) ? 'num' : ''}">${COL_LABELS[c] || c}</th>`).join('') +
    '</tr></thead><tbody>' +
    rows.map((r) => '<tr>' + cols.map((c) => {
      const v = r[c];
      const txt = NUMERIC.has(c) && v !== null && v !== undefined
        ? CH.fmtNum(v, Number.isInteger(v) ? 0 : 2)
        : (v === null || v === undefined || v === '' ? '—' : v);
      return `<td class="${NUMERIC.has(c) ? 'num' : (c === 'store_name' || c === 'customer_name' ? 'name' : '')}">${txt}</td>`;
    }).join('') + '</tr>').join('') + '</tbody></table>';
}

async function loadDetail() {
  const token = ++state.detailToken;
  CH.loading($('x-detail-table'), '正在读取明细…');
  const p = { ...params(), source: state.detailSource, page: state.detailPage, limit: 10 };
  try {
    const res = await api.detail(p);
    if (token !== state.detailToken) return;
    const d = res.data;
    if (!d.rows.length) {
      $('x-detail-table').innerHTML = '<div class="ck-tbl-empty">当前筛选条件下无明细记录</div>';
    } else {
      $('x-detail-table').innerHTML = tableHTML(d.rows, DETAIL_COLS[state.detailSource]);
    }
    const pages = Math.max(1, Math.ceil(d.total / d.page_size));
    $('x-detail-count').textContent =
      `共 ${CH.fmtNum(d.total)} 行 · 每页 ${d.page_size} 行 · ${scopeText()}`;
    $('x-detail-page').textContent = `第 ${d.page} / ${pages} 页`;
    $('x-detail-prev').disabled = d.page <= 1;
    $('x-detail-next').disabled = d.page >= pages;
    $('x-detail-tag').textContent = state.detailSource === 'erp' ? 'ERP 出货' : 'POS 终端销售';
  } catch (e) {
    if (token !== state.detailToken) return;
    CH.failed($('x-detail-table'), e.message);
    $('x-detail-count').textContent = '明细读取失败';
  }
}

/* ------------------------------------------------------------ 加载 */
function markLoading() {
  ['x-sku-chart', 'x-struct-chart', 'x-wh-chart'].forEach((id) => {
    const h = $(id); if (h) CH.loading(h);
  });
  ['x-sfa-stats', 'x-sfa-prog', 'x-sfa-foot', 'x-wh-stats', 'x-wh-foot',
   'x-sku-foot', 'x-struct-foot'].forEach((id) => {
    const e = $(id); if (e) e.innerHTML = '';
  });
}

async function loadAll() {
  if (state.busy) return;
  state.busy = true;
  markLoading();
  try {
    const p = params();
    const [ov, dash] = await Promise.all([api.overview(p), api.dashboard(p)]);
    state.data = { ov: ov.data, dash: dash.data };
    renderSfa(ov.data);
    renderWholesale(ov.data);
    renderSku(dash.data);
    renderStructure(dash.data);
  } catch (e) {
    ['x-sku-chart', 'x-struct-chart', 'x-wh-chart'].forEach((id) => {
      const h = $(id); if (h) CH.failed(h, e.message);
    });
  } finally {
    state.busy = false;
    loadDetail();
  }
}

/* ------------------------------------------------------------ 事件 */
function bind() {
  document.querySelectorAll('#x-struct-tabs button').forEach((b) => {
    b.addEventListener('click', () => {
      state.structSide = b.dataset.struct;
      document.querySelectorAll('#x-struct-tabs button')
        .forEach((x) => x.classList.toggle('on', x === b));
      if (state.data) renderStructure(state.data.dash);
    });
  });
  document.querySelectorAll('#x-detail-src button').forEach((b) => {
    b.addEventListener('click', () => {
      state.detailSource = b.dataset.source;
      state.detailPage = 1;
      document.querySelectorAll('#x-detail-src button')
        .forEach((x) => x.classList.toggle('on', x === b));
      loadDetail();
    });
  });
  $('x-detail-prev').addEventListener('click', () => {
    if (state.detailPage > 1) { state.detailPage -= 1; loadDetail(); }
  });
  $('x-detail-next').addEventListener('click', () => {
    state.detailPage += 1; loadDetail();
  });

  /* 与主体筛选联动：主体先更新自己的 state（先注册），这里延后一拍读取最新值 */
  const refresh = () => setTimeout(() => { state.detailPage = 1; loadAll(); }, 0);
  ['f-month', 'f-region', 'f-ka', 'f-year'].forEach((id) => {
    const el = $(id); if (el) el.addEventListener('change', refresh);
  });
  ['btn-reset', 'btn-refresh'].forEach((id) => {
    const el = $(id); if (el) el.addEventListener('click', refresh);
  });
}

/* 主体脚本是 module，__ckFilters 可能稍后才挂上；轮询等待后首次渲染 */
function boot() {
  let tries = 0;
  const tick = () => {
    if (typeof window.__ckFilters === 'function' || tries++ > 40) {
      bind();
      loadAll();
      return;
    }
    setTimeout(tick, 100);
  };
  tick();
}

boot();
