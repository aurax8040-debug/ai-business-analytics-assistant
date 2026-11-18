/* =====================================================================
 * app.js — 全国经营驾驶舱 V2（集团总览）
 * 状态、筛选联动与渲染。页面不含任何业务常量数据；全部数值来自 /api/*
 * 的真实查询，前端不重算指标、不生成数据、不跨系统相加金额。
 *
 * V2 结构：6 张跨模块 KPI（/api/overview）+ 四条业务线月度走势
 *          + 战区 / KA / SKU 结构 + 品类与规格 + SFA 与批发概览 + 明细
 * ===================================================================== */
import { api } from './api.js';
import * as CH from './charts.js';

const $ = (id) => document.getElementById(id);
const DEFAULTS = { months: '', regions: '', channel: '', ka: '', customer: '', category: '', sku: '' };
/* 四条业务线在页签里逐条展示、从不同屏比较，因此共用品牌蓝一条主线：
   绿 / 橙按设计规范只用于状态（涨跌、低于目标），不拿来区分业务线。 */
const SERIES_COLOR = { erp: '#1a6ff0', pos: '#1a6ff0', wholesale: '#1a6ff0', sfa: '#1a6ff0' };
/* SKU 排名用同色系梯度：视觉上属于同一维度的排序，不靠颜色区分含义 */
const SKU_SHADES = ['#1a6ff0', '#3c85f3', '#5797f5', '#71a9f7', '#8bbaf9',
                    '#a3c9fb', '#b9d7fc', '#cbe2fd', '#dcedfe', '#eaf4ff'];

const state = {
  ...DEFAULTS,
  regionSide: 'pos',
  trendSeries: 'erp',
  structSide: 'category',
  detailSource: 'pos',
  detailPage: 1,
  meta: null,
  data: null,
  busy: false,
  pending: false,
  detailToken: 0,
};

/* ------------------------------------------------------------ 工具 */
function params() {
  const p = {};
  for (const k of Object.keys(DEFAULTS)) if (state[k] !== '') p[k] = state[k];
  return p;
}
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast._timer); toast._timer = setTimeout(() => t.classList.remove('show'), 2200);
}
function alertBox(msg) {
  const a = $('alert');
  if (!msg) { a.classList.add('hidden'); return; }
  a.textContent = msg; a.classList.remove('hidden');
}
function hasValues(rows, key) {
  return Array.isArray(rows) && rows.some(r => r && r[key] !== null && r[key] !== undefined);
}
function status(text, ok = true) {
  const b = $('status-badge'); b.textContent = text;
  b.className = 'status' + (ok ? '' : ' off');
}
function opt(value, label) {
  const o = document.createElement('option'); o.value = value; o.textContent = label; return o;
}
function pctText(v, digits = 2, suffix = '%') {
  if (v === null || v === undefined) return '—';
  return `${v >= 0 ? '▲ ' : '▼ '}${CH.fmtNum(Math.abs(v), digits)}${suffix}`;
}
function momHTML(v, suffix = '%') {
  if (v === null || v === undefined) return '';
  return `<span class="kpi-mom ${CH.growthClass(v)}">${pctText(v, 2, suffix)}</span>`;
}
/* 金额拆成「数值 + 单位」，避免 KPI 卡里出现 8 位数 */
function splitMoney(v) {
  if (v === null || v === undefined) return { num: '—', unit: '' };
  const n = Number(v), a = Math.abs(n);
  if (a >= 1e8) return { num: (n / 1e8).toFixed(2), unit: '亿元' };
  if (a >= 1e4) return { num: CH.fmtNum(n / 1e4, 0), unit: '万元' };
  return { num: CH.fmtNum(n, 0), unit: '元' };
}
function kpiNumbers(card) {
  if (card.fmt === 'money') return splitMoney(card.value);
  if (card.fmt === 'pct') {
    return { num: card.value === null || card.value === undefined ? '—' : CH.fmtNum(card.value, 1),
             unit: '%' };
  }
  return { num: card.value === null || card.value === undefined ? '—' : CH.fmtNum(card.value, 0),
           unit: card.unit || '' };
}

/* ------------------------------------------------------------ 筛选器初始化 */
function fillAll(key, build) {
  document.querySelectorAll(`select[data-filter="${key}"]`).forEach(sel => {
    const keep = sel.value;
    sel.innerHTML = '';
    build(sel);
    if (keep) sel.value = keep;
  });
}
function fillSelects(m) {
  fillAll('months', s => {
    s.appendChild(opt('', '全部月份'));
    for (let i = 1; i <= 10; i++) s.appendChild(opt(String(i), `${i} 月`));
  });
  fillAll('regions', s => {
    s.appendChild(opt('', '五大战区合计'));   // 完整范围写在标题徽章与页脚，下拉里不截断
    m.regions.forEach(x => s.appendChild(opt(x, x)));
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
  $('chip-period').textContent = 'Y26 · 2026 年 1–10 月';
  $('chip-period').title = m.period;
}
function syncControls() {
  document.querySelectorAll('select[data-filter]').forEach(s => {
    const k = s.dataset.filter;
    if (k in state) s.value = state[k];
  });
  document.querySelectorAll('#region-side button').forEach(b =>
    b.classList.toggle('on', b.dataset.side === state.regionSide));
  document.querySelectorAll('#trend-side button').forEach(b =>
    b.classList.toggle('on', b.dataset.series === state.trendSeries));
  document.querySelectorAll('#struct-side button').forEach(b =>
    b.classList.toggle('on', b.dataset.struct === state.structSide));
  document.querySelectorAll('#detail-source button').forEach(b =>
    b.classList.toggle('on', b.dataset.source === state.detailSource));

  const labels = [];
  if (state.months) labels.push(`月份：${state.months} 月`);
  if (state.regions) labels.push(`战区：${state.regions}`);
  if (state.channel) labels.push(`渠道：${state.channel}`);
  if (state.ka) labels.push(`KA：${state.ka}`);
  if (state.customer) labels.push(`客户：${state.customer}`);
  if (state.category) labels.push(`品类：${state.category}`);
  if (state.sku) labels.push(`SKU：${state.sku}`);
  $('active-chips').innerHTML = labels.map(x => `<span class="chip">${x}</span>`).join('');
}

/* ------------------------------------------------------------ 加载 */
const HOSTS = ['chart-trend', 'region-rank', 'chart-category', 'chart-sku',
               'chart-sku-structure', 'chart-ka', 'chart-wh-inv'];
function markLoading() {
  HOSTS.forEach(id => { const h = $(id); if (h) CH.loading(h); });
  ['trend-foot', 'rank-foot', 'category-foot', 'sku-foot', 'structure-foot', 'ka-foot',
   'sfa-foot', 'wh-foot', 'structure-tag'].forEach(id => { const e = $(id); if (e) e.textContent = ''; });
  if (!state.data) {
    $('kpi-row').innerHTML = Array.from({ length: 6 }, () =>
      `<div class="kpi-card has-icon"><span class="kpi-icon">·</span>
        <div class="kpi-body"><div class="kpi-top"><span class="kpi-label">读取中…</span></div>
        <div class="kpi-main"><span class="kpi-value">—</span></div></div></div>`).join('');
  }
  CH.loading($('detail-table'), '正在读取明细…');
  status('查询中…', false);
}

async function loadAll() {
  if (state.busy) { state.pending = true; return; }
  state.busy = true;
  state.pending = false;
  markLoading(); alertBox(null);
  try {
    const p = params();
    const [ov, dash] = await Promise.all([api.overview(p), api.dashboard(p)]);
    state.data = { ov: ov.data, dash: dash.data, meta: ov.meta };
    renderAll(ov.meta);
    status('数据已更新');
  } catch (e) {
    alertBox(`数据查询失败：${e.message}。请确认后端服务已启动、ETL 数据库可读。`);
    HOSTS.forEach(id => { const h = $(id); if (h) CH.failed(h, e.message); });
    status('查询失败', false);
  } finally {
    state.busy = false;
    if (state.pending) { state.pending = false; setTimeout(() => { loadAll(); }, 0); return; }
    loadDetail();
  }
}

function renderAll(meta) {
  renderKPI(); renderTrend(); renderRank(); renderKA(); renderSKU();
  renderStructure(); renderSfaBlock(); renderWholesaleBlock();
  $('chip-period').textContent = 'Y26 · 2026 年 1–10 月';
  $('chip-period').title = meta.period;
}

/* ------------------------------------------------------------ 第一行：6 张跨模块 KPI */
const KPI_ICON = {
  erp_amount: '出', pos_amount: '销', sfa_stores: '访',
  sfa_rate: '率', wh_out: '批', wh_inv: '存',
};
function renderKPI() {
  const cards = state.data.ov.kpi || [];
  if (!cards.length) { $('kpi-row').innerHTML = ''; return; }
  $('kpi-row').innerHTML = cards.map(c => {
    const { num, unit } = kpiNumbers(c);
    const delta = c.delta ? momHTML(c.delta.pct, c.delta.pp ? 'pp' : '%') : '';
    const title = `响应筛选：${c.scope}`;
    /* 图标统一用品牌蓝底片：绿 / 橙按规范只表示状态，不承担「这是哪个模块」的分类职责 */
    return `<div class="kpi-card has-icon">
      <span class="kpi-icon">${KPI_ICON[c.key] || '·'}</span>
      <div class="kpi-body">
        <div class="kpi-top"><span class="kpi-label" title="${title}">${c.label}</span></div>
        <div class="kpi-main">
          <span class="kpi-value">${num}${unit ? `<small>${unit}</small>` : ''}</span>${delta}
        </div>
        <div class="kpi-foot" title="${c.note} · 响应 ${c.scope}">${c.note}</div>
      </div>
    </div>`;
  }).join('');
}

/* ------------------------------------------------------------ 第二行：经营趋势（4 条线切换） */
function renderTrend() {
  const t = state.data.ov.trend;
  const key = state.trendSeries;
  const s = t.series[key];
  const host = $('chart-trend');
  const rows = s.values || [];
  $('trend-tag').textContent = s.label;
  if (!hasValues(rows, undefined) && !rows.some(v => v !== null && v !== undefined)) {
    CH.empty(host, '当前筛选条件下该模块无月度数据');
    $('trend-foot').textContent = '缺口保持断点，不补零、不连线';
    return;
  }
  const isMoney = s.kind === 'money';
  CH.lineChart(host, {
    labels: t.months.map(m => `${m} 月`), height: 208, area: true,
    tipPrefix: `${s.label} · `,
    axisFormat: (v) => (isMoney ? CH.fmtCompact(v, '¥') : CH.fmtNum(v, 0)),
    tipFormat: (v) => (isMoney ? CH.fmtMoney(v) : `${CH.fmtNum(v, 0)} ${s.unit}`),
    series: [{ name: s.label, color: SERIES_COLOR[key], values: rows }],
  });
  const idx = rows.length - 1;
  const last = rows[idx];
  const prevIdx = rows.slice(0, idx).map((v, i) => (v !== null && v !== undefined ? i : -1))
    .filter(i => i >= 0).pop();
  const prev = prevIdx === undefined ? null : rows[prevIdx];
  const mom = (last !== null && last !== undefined && prev) ? CH.fmtPct((last - prev) * 100 / prev) : '—';
  /* 相邻自然月才叫环比；中间有断点时说清「比哪个月」，不把隔月差当环比。
     SFA 的 9 月窗口只有 9/28–9/30，不能当成完整月份做月度对比，必须点明。 */
  let cmp;
  if (key === 'sfa') cmp = `较 ${t.months[prevIdx]} 月（该月窗口非整月）`;
  else if (prevIdx === idx - 1) cmp = '环比';
  else cmp = `较 ${t.months[prevIdx]} 月`;
  $('trend-foot').textContent = isMoney
    ? `${t.months[idx]} 月 ${CH.fmtMoney(last)} · ${cmp} ${mom} · 四条业务线各自核算，不共用刻度`
    : `${t.months[idx]} 月 ${CH.fmtNum(last, 0)} ${s.unit} · ${cmp} ${mom} · 拜访数据自 9 月起`;
}

/* ------------------------------------------------------------ 第三行：战区 TOP5 / KA / SKU */
function renderRank() {
  const d = state.data.dash.region;
  const side = state.regionSide;
  const key = side === 'pos' ? 'pos_amount' : 'erp_amount';
  const share = side === 'pos' ? 'pos_share_pct' : 'erp_share_pct';
  const items = (d.items || []).filter(x => x[key] !== null && x[key] !== undefined);
  if (!items.length) {
    CH.empty($('region-rank'), '当前筛选条件下无战区数据');
    $('rank-foot').textContent = '仅五大战区，不含战区F、战区G';
    return;
  }
  const max = Math.max(...items.map(x => x[key] || 0), 1);
  $('region-rank').innerHTML = `<div class="rank-list">` + items.map(r => `
    <div class="rank-row" data-region="${r.name}" style="cursor:pointer">
      <span class="rank-no">${r.rank}</span>
      <span class="rank-name">${r.name}</span>
      <span class="rank-bar"><i style="width:${((r[key] || 0) / max * 100).toFixed(1)}%"></i></span>
      <span class="rank-val">${CH.fmtCompact(r[key], '¥')}</span>
      <span class="rank-share">${r[share] === null ? '—' : CH.fmtNum(r[share], 1) + '%'}</span>
    </div>`).join('') + `</div>`;
  $('region-rank').querySelectorAll('.rank-row').forEach(row =>
    row.addEventListener('click', () => setRegion(row.dataset.region)));
  $('rank-foot').textContent =
    `${side === 'pos' ? 'POS 终端销售' : 'ERP 出货'}口径 · 点击行联动全部筛选 · 仅五大战区`;
}
function setRegion(name) {
  state.regions = (state.regions === name) ? '' : name;
  state.detailPage = 1;
  syncControls(); loadAll();
  toast(state.regions ? `已联动战区：${name}` : '已恢复五大战区合计');
}

function renderKA() {
  const d = state.data.dash.customer;
  const rows = (d.pos_ka || []).slice(0, 7);
  if (!hasValues(rows, 'amount')) {
    CH.empty($('chart-ka'), '当前筛选条件下 POS 无 KA 终端数据');
    $('ka-foot').textContent = '';
    return;
  }
  const shades = ['#1a6ff0', '#4f92f7', '#7ab0f9', '#9cc2fb', '#bcd7fd', '#d3e4fe', '#e6effe'];
  const total = rows.reduce((a, b) => a + (b.amount || 0), 0);
  const top = rows[0];
  CH.donutChart($('chart-ka'), {
    size: 168, legendMode: 'share',
    items: rows.map((r, i) => ({ label: r.name, value: r.amount, color: shades[i % shades.length] })),
    centerValue: `${CH.fmtNum((top.share_pct || 0), 1)}%`,
    centerLabel: `${top.name} 占比`,
    onSelect: (label) => {
      state.ka = (state.ka === label) ? '' : label;
      state.detailPage = 1; syncControls(); loadAll();
      toast(state.ka ? `已联动 KA：${label}` : '已清除 KA 筛选');
    },
  });
  $('ka-foot').textContent =
    `POS KA 共 ${d.pos_total} 个 · TOP7 合计 ${CH.fmtCompact(total, '¥')} · 点击图例或扇区联动筛选`;
}

function renderSKU() {
  const d = state.data.dash.sku;
  const shown = (d.items || []).slice(0, 10);
  if (!hasValues(shown, 'amount')) {
    CH.empty($('chart-sku'), '当前筛选条件下无 SKU 动销数据');
    $('sku-foot').textContent = '';
    return;
  }
  CH.hbarChart($('chart-sku'), {
    items: shown.map((r, i) => ({
      /* 标签用商品名（品类A 30# 30粒装），编码放提示框——编码对人不构成信息 */
      label: `${r.rank}. ${r.sku_name || r.name}`, value: r.amount, raw: r,
      color: SKU_SHADES[i % SKU_SHADES.length],
      tipExtra: `<div class="tt-row"><span>SKU 编码</span><b>${r.name}</b></div>
                 <div class="tt-row"><span>粒数 / 规格</span><b>${r.pieces_per_box}粒 / ${r.specification}#</b></div>
                 <div class="tt-row"><span>销售盒数</span><b>${CH.fmtNum(r.boxes)}</b></div>`,
    })),
    rowH: 19, labelW: 126, valueW: 64, maxLabel: 14, clickable: true,
    tipFormat: (v) => CH.fmtMoney(v), valueFormat: (v) => CH.fmtCompact(v, '¥'),
    tipLabel: 'POS 销售额',
    onClick: (it) => {
      state.sku = (state.sku === it.raw.name) ? '' : it.raw.name;
      state.detailPage = 1; syncControls(); loadAll();
      toast(state.sku ? `已下钻 SKU：${it.raw.sku_name || it.raw.name}` : '已清除 SKU 下钻');
    },
  });
  const dd = d.drilldown;
  $('sku-foot').textContent = dd
    ? `下钻 ${dd.sku_code} · ${dd.detail?.sku_name || ''} · 点击其他条形切换`
    : `命中 ${d.total_skus} 个 SKU · 展示 TOP ${shown.length} · 点击条形下钻`;
}

/* ------------------------------------------------------------ 第四行：品类 / 粒数结构 */
function renderStructure() {
  const d = state.data.dash.sku;
  const cat = state.data.dash.category;
  const catHost = $('chart-category');
  const pieceHost = $('chart-sku-structure');
  const isCat = state.structSide === 'category';
  catHost.classList.toggle('hidden', !isCat);
  pieceHost.classList.toggle('hidden', isCat);
  /* 隐藏的那一侧要清空：否则它会一直挂着 HTML 里的「正在查询数据…」占位，
     既误导自动化判活，也可能在切换回来的一瞬间闪一下假加载态 */
  (isCat ? pieceHost : catHost).innerHTML = '';
  $('struct-title').textContent = isCat ? '品类结构' : '粒数结构';
  $('structure-tag').textContent = isCat ? '品类A · 品类B · 品类C'
    : `${(d.by_pieces || []).length} 粒数 / ${(d.by_spec || []).length} 规格`;

  if (isCat) {
    if (!hasValues(cat.items, 'amount')) {
      CH.empty(catHost, '当前筛选条件下无品类数据');
      $('structure-foot').textContent = '';
      return;
    }
    const shades = ['#1a6ff0', '#4f92f7', '#9cc2fb'];
    const total = cat.items.reduce((a, b) => a + (b.amount || 0), 0);
    const top = [...cat.items].sort((a, b) => (b.share_pct || 0) - (a.share_pct || 0))[0];
    CH.donutChart(catHost, {
      size: 168, legendMode: 'share',
      items: cat.items.map((r, i) => ({ label: r.name, value: r.amount,
                                        color: shades[i % shades.length] })),
      centerValue: `${CH.fmtNum(top.share_pct || 0, 1)}%`,
      centerLabel: `${top.name}占比`,
    });
    $('structure-foot').textContent =
      `POS 合计 ${CH.fmtCompact(total, '¥')} · ` +
      cat.items.map(r => `${r.name} ${r.share_pct === null ? '—' : CH.fmtNum(r.share_pct, 1) + '%'}`).join(' · ');
    return;
  }

  const rows = (d.by_pieces || []).slice(0, 8);
  if (!hasValues(rows, 'amount')) {
    CH.empty(pieceHost, '当前筛选条件下无粒数结构数据');
    $('structure-foot').textContent = '';
    return;
  }
  CH.hbarChart(pieceHost, {
    items: rows.map((r, i) => ({
      label: `${r.name} 粒`, value: r.amount,
      badge: r.share_pct, color: SKU_SHADES[i % SKU_SHADES.length],
      tipExtra: `<div class="tt-row"><span>SKU 数</span><b>${r.skus}</b></div>
                 <div class="tt-row"><span>销售盒数</span><b>${CH.fmtNum(r.boxes)}</b></div>`,
    })),
    rowH: 19, labelW: 52, valueW: 64, badgeW: 44, maxLabel: 8,
    badgeFormat: (v) => `${CH.fmtNum(v, 1)}%`,
    tipFormat: (v) => CH.fmtMoney(v), valueFormat: (v) => CH.fmtCompact(v, '¥'),
    tipLabel: 'POS 销售额',
  });
  const specs = (d.by_spec || []).slice(0, 3)
    .map(r => `${r.name}# ${r.share_pct === null ? '—' : CH.fmtNum(r.share_pct, 1) + '%'}`).join(' / ');
  $('structure-foot').textContent =
    `规格 TOP3：${specs || '—'} · 共 ${d.combos} 种「粒数×规格」组合`;
}

/* ------------------------------------------------------------ 第四行：SFA / 批发概览 */
function renderSfaBlock() {
  const s = state.data.ov.sfa;
  /* 区间内没有任何拜访记录时，不画 0 值进度条——「没采集」和「一家没去」不是一回事 */
  const empty = !!s.no_window_data;
  const num = (v, maskable = true) => {
    if (v === null || v === undefined) return '—';
    if (maskable && empty) return '—';
    return CH.fmtNum(v, 0);
  };
  $('sfa-stats').innerHTML = [
    /* 客户经理是组织维度事实，不随窗口变，不做空值遮蔽 */
    ['客户经理', s.employee_total, '人', false],
    ['目标门店', s.target_store_total, '家', true],
    ['实际拜访', s.actual_store_total, '家', true],
  ].map(([label, v, u, maskable]) => `
    <div class="stat-block">
      <div class="stat-label">${label}</div>
      <div class="stat-value">${num(v, maskable)}<small>${num(v, maskable) === '—' ? '' : u}</small></div>
    </div>`).join('');

  const rate = empty ? null : s.completion_rate;
  const cov = empty ? null : s.coverage_rate;
  const bar = (name, v, extra = '') => `
    <div class="progress-item">
      <span class="progress-name">${name}</span>
      <span class="progress-track"><i class="${v !== null && v < 90 ? 'is-amber' : ''}"
        style="width:${v === null || v === undefined ? 0 : Math.max(0, Math.min(100, v)).toFixed(1)}%"></i></span>
      <span class="progress-val">${v === null || v === undefined ? '未采集' : CH.fmtNum(v, 1) + '%'}${extra}</span>
    </div>`;
  $('sfa-progress').innerHTML =
    bar('拜访达成率', rate) + bar('门店覆盖率', cov);
  if (empty) {
    $('sfa-foot').textContent = `所选区间无拜访记录（SFA 数据自 ${s.data_from || '—'} 起）`;
    $('sfa-tag').textContent = '未采集';
    return;
  }
  $('sfa-foot').textContent = s.target_is_demo
    ? '目标含示例数据（DEMO）· 完成率仅演示展示逻辑 · ' +
      `覆盖 ${s.covered_stores} / ${s.universe_stores} 家`
    : `目标 ${s.target_store_total} 家 · 覆盖 ${s.covered_stores} / ${s.universe_stores} 家`;
  $('sfa-tag').textContent = s.target_is_demo ? 'DEMO 目标' : '窗口累计';
}

function renderWholesaleBlock() {
  const w = state.data.ov.wholesale;
  const card = (label, v, unit) => {
    const { num, unit: u } = splitMoney(v);
    return `<div class="stat-block">
      <div class="stat-label">${label}</div>
      <div class="stat-value">${num}<small>${u || unit || ''}</small></div>
    </div>`;
  };
  $('wh-stats').innerHTML =
    card('采购入库', w.purchase_amount) + card('批发出库', w.sales_amount) +
    card('期末库存', w.inventory_amount);

  const series = w.inventory_series || [];
  if (series.length) {
    CH.lineChart($('chart-wh-inv'), {
      labels: series.map(x => (x.label || '').slice(5) + '月'), height: 104, area: true, ticks: 2,
      axisFormat: (v) => CH.fmtCompact(v, '¥'),
      tipFormat: (v) => CH.fmtMoney(v), tipPrefix: '库存金额 · ',
      series: [{ name: '期末库存金额', color: '#1a6ff0',
                 values: series.map(x => x.value) }],
    });
  } else {
    CH.empty($('chart-wh-inv'), '无库存月度快照');
  }
  $('wh-foot').textContent =
    `采购 / 出库 / 库存三类金额独立核算，永不相加 · 库存为 ${w.anchor_date} 时点值`;
}

/* ------------------------------------------------------------ 第五行：经营明细 */
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
  pos: ['full_date', 'region_name_std', 'channel_type', 'ka_system', 'store_name',
        'category', 'sku_code', 'sales_boxes', 'sales_amount'],
  erp: ['full_date', 'region_name_std', 'channel_type', 'customer_name',
        'category', 'sku_code', 'sales_boxes', 'sales_amount', 'gross_profit'],
};
function tableHTML(rows, cols) {
  if (!rows.length) return '';
  return `<table class="data"><thead><tr>` +
    cols.map(c => `<th>${COL_LABELS[c] || c}</th>`).join('') + `</tr></thead><tbody>` +
    rows.map(r => `<tr>` + cols.map(c => {
      const v = r[c];
      const txt = NUMERIC.has(c) && v !== null && v !== undefined
        ? CH.fmtNum(v, Number.isInteger(v) ? 0 : 2) : (v ?? '—');
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
    const cols = DETAIL_COLS[state.detailSource];
    if (!d.rows.length) {
      CH.empty($('detail-table'), '当前筛选条件下无明细记录');
    } else {
      $('detail-table').innerHTML = tableHTML(d.rows, cols);
    }
    const pages = Math.max(1, Math.ceil(d.total / d.page_size));
    $('detail-count').textContent =
      `共 ${CH.fmtNum(d.total)} 行 · 每页 ${d.page_size} 行 · 2026 年 1–10 月`;
    $('detail-page').textContent = `第 ${d.page} / ${pages} 页`;
    $('detail-prev').disabled = d.page <= 1;
    $('detail-next').disabled = d.page >= pages;
  } catch (e) {
    if (token !== state.detailToken) return;
    CH.failed($('detail-table'), e.message);
    $('detail-count').textContent = '明细读取失败';
  }
}

/* ------------------------------------------------------------ 抽屉 */
const DRAWER_COLS = {
  pos: ['full_date', 'region_name_std', 'channel_type', 'ka_system', 'store_name',
        'sku_code', 'sku_name', 'category', 'sales_boxes', 'sales_amount', 'sales_te'],
  erp: ['full_date', 'region_name_std', 'channel_type', 'customer_id', 'customer_name',
        'sku_code', 'sku_name', 'category', 'sales_boxes', 'sales_amount',
        'shipment_te', 'cost_amount', 'gross_profit'],
};
async function loadDrawer() {
  CH.loading($('drawer-table'), '正在读取明细…');
  const p = { ...params(), source: state.detailSource, page: state.drawerPage || 1, limit: 20 };
  try {
    const res = await api.detail(p);
    const d = res.data;
    $('drawer-sub').textContent =
      `${state.detailSource === 'erp' ? 'ERP 出货明细' : 'POS 终端销售明细'} · ${res.meta.region_label}`;
    $('drawer-count').textContent = `共 ${CH.fmtNum(d.total)} 行 · 2026 年 1–10 月`;
    $('page-label').textContent = `第 ${d.page} 页`;
    if (!d.rows.length) { CH.empty($('drawer-table'), '当前筛选条件下无明细记录'); return; }
    $('drawer-table').innerHTML = tableHTML(d.rows, DRAWER_COLS[state.detailSource]);
  } catch (e) {
    CH.failed($('drawer-table'), e.message);
  }
}
function openDrawer() {
  $('modal').classList.remove('hidden');
  state.drawerPage = 1;
  loadDrawer();
}

/* ------------------------------------------------------------ 事件绑定 */
function bind() {
  document.addEventListener('change', e => {
    const sel = e.target.closest('select[data-filter]');
    if (!sel) return;
    state[sel.dataset.filter] = sel.value;
    state.detailPage = 1;
    syncControls(); loadAll();
  });
  $('trend-side')?.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.trendSeries = b.dataset.series; syncControls();
    if (state.data) renderTrend();
  });
  $('region-side')?.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.regionSide = b.dataset.side; syncControls();
    if (state.data) renderRank();
  });
  $('struct-side')?.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.structSide = b.dataset.struct; syncControls();
    if (state.data) renderStructure();
  });
  $('detail-source')?.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.detailSource = b.dataset.source; state.detailPage = 1;
    syncControls(); loadDetail();
  });
  $('btn-reset').addEventListener('click', () => {
    Object.assign(state, DEFAULTS);
    state.regionSide = 'pos'; state.trendSeries = 'erp'; state.structSide = 'category';
    state.detailSource = 'pos'; state.detailPage = 1;
    syncControls(); loadAll(); toast('已重置为五大战区合计 · Y26 1–10 月');
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
  document.addEventListener('keydown', e => { if (e.key === 'Escape') $('modal').classList.add('hidden'); });
  $('page-prev').addEventListener('click', () => {
    state.drawerPage = Math.max(1, (state.drawerPage || 1) - 1); loadDrawer();
  });
  $('page-next').addEventListener('click', () => { state.drawerPage = (state.drawerPage || 1) + 1; loadDrawer(); });
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
      renderTrend(); renderRank(); renderKA(); renderSKU(); renderStructure();
      renderWholesaleBlock();
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
