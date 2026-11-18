/* =====================================================================
   AI 经营助手 · 前端逻辑
   - 只调用 /api/agent/*，不重算任何指标（数字全部来自后端 SQL）
   - 流式优先：/api/agent/stream（SSE）逐节点显示「正在干什么」
   - 流式不可用自动回退 /api/agent/chat（一次性）
   ===================================================================== */

const $ = (id) => document.getElementById(id);
const thread = $('thread');
const questionBox = $('question');
const btnSend = $('btn-send');
const statusEl = $('status');
const toastEl = $('toast');

const QUICK = [
  {code: 'DIAGNOSIS', title: '预算诊断', goal: '纠正前提并定位主要销售缺口',
   question: '战区B市场部为什么没有完成2026年10月销售目标？'},
  {code: 'OPPORTUNITY', title: '增长机会', goal: '识别高增长 SKU 的铺货或低动销机会',
   question: '哪些高增长 SKU 在我负责的门店还没有铺货？',
   context: {region: '战区B', year: 2026, months: [9, 10]}},
  {code: 'PRICE', title: '价格分析', goal: '找出同款 SKU 的可比门店价格异常',
   question: '哪些门店同款 SKU 的价格明显高于同类门店？',
   context: {region: '战区B', year: 2026, months: [10]}},
  {code: 'SUPPLY_CHAIN', title: '短缺风险', goal: '识别原料、包材及受影响生产需求',
   question: '未来两个月哪些水果原料和包材可能短缺？'},
  {code: 'PROCUREMENT', title: '包材采购', goal: '按 V2 公式给出可复核采购建议',
   question: '未来两个月哪些包材需要采购，建议采购多少？'},
  {code: 'SIMULATION', title: '经营模拟', goal: '比较需求增长前后的销售与供应链影响',
   question: '如果未来两个月需求增长20%，会对销售和供应链产生什么影响？'},
];

let history = [];
let busy = false;
let quickContext = null;
let pendingRecoveryId = '';
const recoveryQuestions = new Map();

/* -------------------------------------------------- 会话内容跨页面恢复 */
/* 抽屉换页时 iframe 会重建，DOM 里的消息必然丢失。后端只记「焦点实体」，
 * 不存对话正文，所以这里把「问题 + 报告快照」存进 sessionStorage（同标签页有效），
 * 新页面按同一个 thread_id 重放显示，用户看到的是接着聊而不是空白的欢迎页。
 * 只存渲染所需的最小结构，不存 tool_data（几十 KB，且可随时重新取数）。 */
const MSG_PREFIX = 'ag-msgs-';
const MAX_TURNS = 8;
const MAX_BYTES = 1200000;

function msgKey(tid) { return MSG_PREFIX + tid; }

function loadTurns(tid) {
  try {
    const raw = sessionStorage.getItem(msgKey(tid));
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch (e) { return []; }
}

function saveTurns(tid, list) {
  try {
    const raw = JSON.stringify(list.slice(-MAX_TURNS));
    if (raw.length > MAX_BYTES) return;   // 超限宁可不存，也不要把存储写满
    sessionStorage.setItem(msgKey(tid), raw);
  } catch (e) { /* 隐私模式会抛 QuotaExceeded，忽略即可 */ }
}

function restoreTurns() {
  const list = loadTurns(threadId);
  if (!list.length) return;
  list.forEach((t) => {
    if (!t || !t.q) return;
    addUser(t.q);
    const el = document.createElement('article');
    el.className = 'ag-msg ag-msg-bot';
    el.innerHTML = '<div class="ag-avatar">AI</div>'
      + '<div class="ag-bubble">' + renderReport(t.report || {}, null) + '</div>';
    thread.appendChild(el);
    if (t.headline) {
      history.push({ role: 'user', content: t.q },
                   { role: 'assistant', content: t.headline });
    }
  });
  scrollBottom();
}

/* ------------------------------------------------------- 页内上下文（URL） */
/* 全站抽屉用 /agent.html?embed=1&page=pos&region=战区A&ka_system=KA客户A&...
   把「当前看到的那一屏筛选」带进来：用户不必重复描述范围，问题里明确说了的仍以问题为准
   （后端 understand() 里「问题解析结果」优先于 context）。
   V2：抽屉打开后筛选变了，父页面通过 postMessage 推新上下文进来，不重载 iframe。 */
const QS = new URLSearchParams(location.search);
const EMBED = QS.get('embed') === '1';

// 同一标签页跨页面复用会话：父页面在 URL 里带 thread_id
let threadId = (QS.get('thread_id') || '').trim() || `t-${Date.now().toString(36)}`;

const CTX_KEYS = ['page', 'page_name', 'module', 'region', 'department', 'customer',
  'channel', 'ka_system', 'sku', 'store', 'category', 'segment'];

function normalizeContext(src) {
  const c = {};
  if (!src) return c;
  CTX_KEYS.forEach((k) => {
    const v = src[k];
    if (typeof v === 'string' && v.trim()) c[k] = v.trim();
    else if (typeof v === 'number' && v) c[k] = v;
  });
  const year = Number(src.year || 0);
  if (year) c.year = year;
  const months = (Array.isArray(src.months) ? src.months : [])
    .map((x) => Number(x)).filter((x) => x >= 1 && x <= 12);
  if (months.length) c.months = months;
  return c;
}

function pageContext() {
  return normalizeContext({
    page: QS.get('page'), page_name: QS.get('page_name'), module: QS.get('module'),
    region: QS.get('region'), department: QS.get('department'),
    customer: QS.get('customer'), channel: QS.get('channel'),
    ka_system: QS.get('ka_system'), sku: QS.get('sku'), store: QS.get('store'),
    year: QS.get('year'),
    months: (QS.get('months') || '').split(','),
  });
}
let PAGE_CTX = pageContext();

/* 带上下文的快捷问题：第一问直接就是「你现在看的这一屏」的达成分析 */
function quickList() {
  const list = QUICK.slice();
  const { region, customer, year, months } = PAGE_CTX;
  if (region || customer) {
    const who = customer || region;
    const y = year || 2026;
    const last = (months && months.length) ? months[months.length - 1] : 10;
    const when = (months && months.length === 1) ? `${y} 年 ${last} 月` : `${y} 年 1-${last} 月`;
    list.unshift({code: 'CONTEXT', title: '按当前页面分析',
      goal: `${who} · ${when}`,
      question: `${who} ${when}达成情况，找出未达成的客户，并分析可能原因`});
  }
  return list;
}

/* ------------------------------------------------- 上下文同步（父页面 → iframe）
   抽屉不会因为筛选变化而重载（重载会丢对话），改为父页面 postMessage 推送新上下文。
   这里只更新「当前范围」提示与快捷问，不动已经产生的问答记录。 */
function ctxChipText(c) {
  if (!c || !c.page_name) return '';
  const bits = [c.page_name];
  if (c.store) bits.push(c.store);
  else if (c.customer) bits.push(c.customer);
  else if (c.region) bits.push(c.region);
  if (c.ka_system) bits.push(c.ka_system);
  if (c.department) bits.push(c.department);
  if (c.category) bits.push(c.category);
  if (c.sku) bits.push(c.sku);
  const ms = c.months || [];
  bits.push(`${c.year || 2026} 年 ${ms.length === 1 ? `${ms[0]} 月` : (ms.length ? `${ms[0]}–${ms[ms.length - 1]} 月` : '累计')}`);
  return bits.join(' · ');
}

function applyContext(next) {
  PAGE_CTX = next || {};
  const chip = $('ctx-chip');
  const bar = $('ctxbar');
  const text = ctxChipText(PAGE_CTX);
  if (chip) {
    chip.textContent = text || '';
    chip.title = 'AI 默认按这个范围回答；问题里明确说的条件优先';
  }
  if (bar) bar.classList.toggle('hidden', !text);
  renderQuick();
}

window.addEventListener('message', (e) => {
  if (e.origin && e.origin !== location.origin && e.origin !== 'null') return;
  const d = e.data || {};
  if (d.type !== 'page-context') return;
  applyContext(normalizeContext(d.context));
});

/* ------------------------------------------------------------ 工具函数 */
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toastEl.classList.add('hidden'), 2600);
}

function setStatus(text, kind = '') {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
}

function scrollBottom() {
  thread.scrollTop = thread.scrollHeight;
}

/* ------------------------------------------------------------ 模型状态 */
async function loadHealth() {
  const chip = $('llm-chip');
  try {
    const r = await fetch('/api/agent/health');
    const p = await r.json();
    const llm = (p.data && p.data.llm) || {};
    if (llm.available) {
      chip.textContent = `模型：${llm.model}`;
      chip.title = `${llm.provider} · ${llm.base_url}｜LLM 负责解释，数字由 SQL 计算`;
      chip.className = 'ag-model is-live';
    } else {
      chip.textContent = '模型：未配置（规则模板）';
      chip.title = '未检测到 LLM_API_KEY，报告将由规则模板生成；数字仍来自真实 SQL';
      chip.className = 'ag-model is-template';
    }
  } catch (e) {
    chip.textContent = '模型：不可用';
    chip.className = 'ag-model is-template';
  }
}

const KPI_KEYS = ['intent_accuracy','route_accuracy','drill_effectiveness','budget_gap_precision',
  'adoption_rate','modified_rate','rejection_rate','price_misjudgment_rate','shortage_missed_count',
  'average_latency_ms','p95_latency_ms','average_tokens','average_cost',
  'budget_attainment','stockout_rate','packaging_obsolescence_rate','sku_velocity_lift'];

function kpiValue(m) {
  if (m.value === null || m.value === undefined) return '待采集';
  if (m.key.endsWith('_ms')) return `${Math.round(m.value)} ms`;
  if (m.key === 'average_tokens') return `${Math.round(m.value)} tokens`;
  if (m.key === 'average_cost') return `${m.value}`;
  if (m.key === 'shortage_missed_count') return `${m.value} 个`;
  return `${m.value}%`;
}

async function loadKpis(scene = '') {
  const box = $('kpi-grid'); if (!box) return;
  box.innerHTML = '<span class="ag-kpi-empty">正在读取指标…</span>';
  try {
    const res = await fetch(`/api/agent/kpi${scene ? `?scene=${encodeURIComponent(scene)}` : ''}`);
    const payload = await res.json(); if (!payload.ok) throw new Error('KPI读取失败');
    const all = payload.data?.metrics || [];
    const wanted = new Set(scene ? (payload.data?.scene_mapping?.[scene] || KPI_KEYS) : KPI_KEYS);
    const rows = all.filter((m) => wanted.has(m.key));
    box.innerHTML = rows.map((m) => `<article class="ag-kpi-card" title="${esc(m.formula)}；${esc(m.note || '')}">
      <span>${esc(m.group)}</span><b>${esc(m.name)}</b><strong>${esc(kpiValue(m))}</strong>
      <i data-status="${esc(m.status)}">${esc(m.status)}</i></article>`).join('')
      || '<span class="ag-kpi-empty">当前筛选暂无指标定义</span>';
  } catch (e) { box.innerHTML = '<span class="ag-kpi-empty">KPI 暂不可用，不影响经营分析</span>'; }
}

/* ------------------------------------------------------------ 快捷问题 */
function renderQuick() {
  const box = $('quick');
  const list = quickList();
  box.innerHTML = list.map((q, i) =>
    `<button type="button" data-i="${i}" aria-label="${esc(q.title)}：${esc(q.question)}">
      <span class="ag-quick-title"><b>${esc(q.title)}</b><i>${esc(q.code)}</i></span>
      <span class="ag-quick-goal">${esc(q.goal)}</span>
      <span class="ag-quick-q">${esc(q.question)}</span>
    </button>`).join('');
  box.onclick = (e) => {
    const b = e.target.closest('button[data-i]');
    if (!b) return;
    const item = list[Number(b.dataset.i)];
    questionBox.value = item.question;
    quickContext = item.context || null;
    questionBox.focus();
    ask();
  };
}

/* ------------------------------------------------------------ 消息渲染 */
function addUser(text) {
  const el = document.createElement('article');
  el.className = 'ag-msg ag-msg-user';
  el.innerHTML = `<div class="ag-avatar">你</div><div class="ag-bubble">${esc(text)}</div>`;
  thread.appendChild(el);
  scrollBottom();
}

function addBotLoading() {
  const el = document.createElement('article');
  el.className = 'ag-msg ag-msg-bot';
  el.innerHTML = `<div class="ag-avatar">AI</div>
    <div class="ag-bubble"><span class="ag-loading">
      <span class="spinner"></span><span id="loading-text">理解问题…</span></span></div>`;
  thread.appendChild(el);
  scrollBottom();
  return el;
}

function addBotError(text) {
  const el = document.createElement('article');
  el.className = 'ag-msg ag-msg-bot';
  el.innerHTML = `<div class="ag-avatar">AI</div>
    <div class="ag-bubble"><div class="alert alert-error">${esc(text)}</div></div>`;
  thread.appendChild(el);
  scrollBottom();
}

/* ------------------------------------------------------------ 报告渲染 */
function renderKpis(rows) {
  return `<div class="ag-kpis">${rows.map((r) => `
    <div class="ag-kpi ${r.level || ''}">
      <div class="ag-kpi-label">${esc(r.label)}</div>
      <div class="ag-kpi-value">${esc(r.value)}</div>
      <div class="ag-kpi-hint">${esc(r.hint || '')}</div>
    </div>`).join('')}</div>`;
}

/* P10：七段式报告。每条都带标签（事实 / 推测 / 建议 / 限制），
   让用户一眼分清哪句能直接引用、哪句只是待验证的假设。 */
const SEC_CN = '一二三四五六七八九十';
const KIND_CLASS = {
  fact: 'ag-k-fact', inference: 'ag-k-inf', advice: 'ag-k-adv', limit: 'ag-k-limit',
};

function renderSectionTable(t) {
  const cols = (t && t.columns) || [];
  const rows = (t && t.rows) || [];
  if (!cols.length || !rows.length) return '';
  return `<div class="ag-tbl-wrap"><table class="ag-tbl">
      <thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${r.map((v) =>
        `<td>${esc(v == null || v === '' ? '—' : v)}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>${t.footer ? `<p class="ag-sub">${esc(t.footer)}</p>` : ''}</div>`;
}

function renderSections(rep) {
  return (rep.sections || []).map((s) => {
    const items = (s.items || []).map((it) => `
      <li class="ag-item">
        <span class="ag-tag ${KIND_CLASS[it.kind] || ''}">${esc(it.label || '')}</span>
        <span class="ag-item-text">${esc(it.text || '')}</span>
        ${it.source ? `<span class="ag-item-src">${it.kind === 'fact' ? '依据' : '状态'}：${esc(it.source)}</span>` : ''}
      </li>`).join('');
    return `<div class="ag-sec"><h3>${SEC_CN[s.no - 1] || s.no}、${esc(s.title)}</h3>
      <ul class="ag-items">${items || '<li class="ag-item ag-empty">本段暂无经过 Checker 验证的内容。</li>'}</ul>
      ${renderSectionTable(s.table)}
      ${s.note ? `<p class="ag-note">${esc(s.note)}</p>` : ''}</div>`;
  }).join('');
}

function renderProblems(p) {  const rows = p.rows || [];
  if (!rows.length) return `<p class="ag-sub">${esc(p.summary || '没有未达成客户。')}</p>`;
  return `<p class="ag-sub">${esc(p.summary || '')}</p>
    <table class="ag-tbl">
      <thead><tr>
        <th>#</th><th>客户</th><th>类型</th><th>实际 TE</th><th>预算 TE</th>
        <th>达成率</th><th>缺口 TE</th><th>贡献</th><th>同比</th>
      </tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${r.rank}</td><td>${esc(r.customer)}</td><td>${esc(r.segment)}</td>
        <td>${esc(r.te)}</td><td>${esc(r.budget_te)}</td><td>${esc(r.attainment)}</td>
        <td class="neg">${esc(r.gap_te)}</td><td>${esc(r.share)}</td><td>${esc(r.yoy)}</td>
      </tr>`).join('')}</tbody>
    </table>`;
}

function renderCauses(c) {
  const group = (tag, cls, items, fmt) => {
    const list = (items || []).filter(Boolean);
    if (!list.length) return '';
    return `<div class="ag-cause-group">
      <span class="ag-cause-tag ${cls}">${tag}</span>
      <ul class="ag-list">${list.map(fmt).join('')}</ul>
    </div>`;
  };
  const verified = group('已验证事实', 'ag-tag-ok', c.verified, (x) => {
    const text = typeof x === 'string' ? x : x.text;
    const src = typeof x === 'string' ? '' : (x.source || '');
    return `<li>${esc(text)}${src ? `<span class="ag-sub">（${esc(src)}）</span>` : ''}</li>`;
  });
  const possible = group('可能原因 · 需业务验证', 'ag-tag-mid', c.possible, (x) => {
    if (typeof x === 'string') return `<li>${esc(x)}</li>`;
    const bits = [x.why && `依据：${x.why}`, x.verify && `验证：${x.verify}`].filter(Boolean);
    return `<li>${esc(x.text)}${bits.length ? `<span class="ag-sub">（${esc(bits.join('；'))}）</span>` : ''}</li>`;
  });
  const none = group('暂无数据支持', 'ag-tag-none', c.unsupported, (x) => `<li>${esc(x)}</li>`);
  return verified + possible + (none || '<p class="ag-sub">无</p>');
}

function renderActions(actions) {
  if (!actions || !actions.length) return '<p class="ag-sub">证据不足，暂不给出建议。</p>';
  return `<ol class="ag-actions">${actions.map((a) => `<li>
    <b>${esc(a.action)}</b>
    <div class="ag-meta">责任：${esc(a.owner || '—')}｜验证：${esc(a.check || '—')}</div>
  </li>`).join('')}</ol>`;
}

function renderTrustedChart(chart) {
  if (!chart?.available || !chart.points?.length) return '';
  const max = Math.max(...chart.points.map((p) => Math.abs(Number(p.value) || 0)), 1);
  const points = chart.points.map((p) => {
    const width = Math.max(4, Math.round(Math.abs(Number(p.value) || 0) / max * 100));
    return `<button type="button" class="ag-chart-point" data-chart-followup="${esc(p.followup || '')}"
      title="点击继续追问 ${esc(p.label)}">
      <span class="ag-chart-label">${esc(p.label)}</span>
      <span class="ag-chart-track"><i style="width:${width}%"></i></span>
      <strong>${esc(p.value)}${esc(p.unit || '')}</strong>
    </button>`;
  }).join('');
  return `<section class="ag-chart" data-chart-type="${esc(chart.type)}">
    <div class="ag-chart-head"><div><b>可信图表 · ${esc(chart.title)}</b>
      <span>${chart.type === 'line' ? '趋势' : '对比'} · 点击数据点继续追问</span></div>
      <em>${esc(chart.verification_label || '结果已验证')}</em></div>
    <div class="ag-chart-points">${points}</div>
    <details><summary>查看图表口径与来源</summary>
      <p><b>工具：</b>${esc(chart.source_tool)}　<b>来源：</b>${esc(chart.source || '—')}</p>
      <p><b>口径：</b>${esc(chart.caliber || '—')}</p>
      <p>${esc(chart.verification || '')}</p>
    </details>
  </section>`;
}

function renderBasis(b, report) {
  const tools = (b.tools || []).map((t) =>
    `<code>${esc(t.tool)}</code> ${t.ok ? '✓' : '✗'} ${t.elapsed_ms ?? '—'}ms`).join('　');
  const gaps = (b.data_gaps || []).map((g) => `<li>${esc(g)}</li>`).join('');
  const caveats = (report.caveats || []).map((g) => `<li>${esc(g)}</li>`).join('');
  return `<details class="ag-detail">
    <summary>展开：调用了哪些工具、口径有没有对上</summary>
    <div class="ag-detail-body">
      <p><b>统计期间</b>：${esc(b.period || '—')}</p>
      <p><b>工具</b>：${tools || '—'}</p>
      <p><b>口径自校验</b>：${(b.cross_checks || []).length
        ? (b.cross_checks.every((c) => c.ok) ? '全部通过（各工具算同一个数，结果一致）' : '存在差异，请看下方明细')
        : '单工具查询，无需交叉校验'}</p>
      ${(b.cross_checks || []).map((c) => `<p class="ag-sub">· ${esc(c.name)}：
        ${esc(c.a)} vs ${esc(c.b)}（差 ${esc(c.diff)}）${c.ok ? '✓' : '✗'}</p>`).join('')}
      ${gaps ? `<p><b>数据缺口</b></p><ul class="ag-list">${gaps}</ul>` : ''}
      ${caveats ? `<p><b>口径提示</b></p><ul class="ag-list">${caveats}</ul>` : ''}
    </div>
  </details>`;
}

/* ------------------------------------------------- 数据核对（可展开） */
const TOOL_NAME = {
  query_budget: '预算',
  query_erp_actual: 'ERP 实际出货',
  query_customer_performance: '客户缺口',
  query_pos_performance: '终端 POS',
  query_sfa_visit: 'SFA 拜访',
  query_wholesale_performance: '批发',
};

const GROUP_NAME = {
  by_month: '按月份',
  by_department: '按市场部',
  by_segment: '按客户类型',
  by_customer: '按客户',
  top_gap: '未达成 TOP',
  top_over: '超额 TOP',
  by_ka_system: '按 KA 系统',
  by_store: '按门店',
  by_display_location: '按陈列位置',
  by_display_form: '按陈列形式',
  by_stall: '按档口',
  inventory_month_end: '期末库存（全国口径）',
  rows: '明细',
};

const COL_NAME = {
  month: '月份', year: '年份',
  department: '市场部', region: '战区', region_name: '战区',
  segment: '类型代码', segment_label: '客户类型', label: '客户类型',
  customer: '客户', customer_id: '客户编码',
  budget_te: '预算 TE', te: '实际 TE', sales_te: '终端 TE', purchase_te: '采购 TE',
  last_te: '去年同期 TE', gap_te: '缺口 TE',
  attainment_pct: '达成率', gap_share_pct: '占未达成缺口', gap_vs_budget_pct: '缺口/预算',
  yoy_pct: '同比', gross_margin_pct: '毛利率', completed_rate_pct: '拜访完成率',
  display_recorded_rate_pct: '陈列记录率',
  amount: '金额(元)', sales_amount: '终端金额(元)', cost: '成本(元)',
  gross_profit: '毛利额(元)', inventory_amount: '库存金额(元)',
  boxes: '箱数', sales_boxes: '终端箱数', shipments: '出货单数',
  stores: '门店数', records: '记录数', visits: '拜访次数', completed: '已完成',
  employees: '业代人数', stalls: '档口数', downstream_customers: '下游客户数',
  ending_qty: '期末库存', ka_system: 'KA 系统', store: '门店', city: '城市',
  last_visit: '最近拜访', display_location: '陈列位置', display_form: '陈列形式',
  stall_id: '档口', oos_records: '缺货记录', customers: '客户数',
  budget_missing: '预算缺失', actual_missing: '实际缺失',
};

const METRIC_NAME = {
  te: '实际 TE', budget_te: '预算 TE', boxes: '箱数', amount: '金额(元)',
  cost: '成本(元)', gross_profit: '毛利额(元)', gross_margin_pct: '毛利率',
  shipments: '出货单数', last_year_te: '去年同期 TE', yoy_pct: '同比',
  attainment_pct: '达成率', customers: '客户数', unmet_customers: '未达成客户',
  unmet_gap_te: '未达成缺口 TE', total_te: '合计 TE', total_budget_te: '合计预算 TE',
  met_customers: '已达成客户', budget_missing_customers: '无预算客户',
  actual_missing_customers: '无实际客户', months_covered: '覆盖月数',
  gap_te: '缺口 TE',
  sales_te: '终端 TE', sales_amount: '终端金额(元)', sales_boxes: '终端箱数',
  stores: '门店数', records: '记录数', visits: '拜访次数', completed: '已完成',
  employees: '业代人数', display_recorded_rate_pct: '陈列记录率',
  oos_records: '缺货记录', purchase_te: '采购 TE', stalls: '档口数',
  downstream_customers: '下游客户数', date_range: '覆盖日期', min_month: '起始月',
};

function isPct(k) { return /_pct$/.test(k); }
function isInt(k) {
  return ['month', 'year', 'stores', 'records', 'visits', 'completed', 'employees',
    'stalls', 'downstream_customers', 'shipments', 'oos_records', 'customers',
    'unmet_customers', 'met_customers', 'budget_missing_customers',
    'actual_missing_customers', 'months_covered', 'ending_qty', 'boxes',
    'sales_boxes'].includes(k);
}

function fmtCell(k, v) {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'number') {
    if (k === 'month') return `${v} 月`;
    if (!Number.isFinite(v)) return '—';
    if (isPct(k)) return `${v.toFixed(2)}%`;
    if (isInt(k)) return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
    return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  return esc(String(v));
}

function colName(k) { return COL_NAME[k] || k; }

// 表格列顺序：核对时先看「谁、多少、差多少」，辅助字段靠后
const COL_ORDER = [
  'month', 'customer_id', 'customer', 'segment_label', 'department', 'ka_system',
  'store', 'city', 'stall_id', 'display_location', 'display_form',
  'budget_te', 'te', 'sales_te', 'purchase_te',
  'attainment_pct', 'gap_te', 'gap_share_pct', 'gap_vs_budget_pct',
  'last_te', 'yoy_pct', 'amount', 'sales_amount', 'gross_profit', 'gross_margin_pct',
  'cost', 'boxes', 'sales_boxes', 'shipments', 'stores', 'records', 'visits',
  'completed', 'completed_rate_pct', 'employees', 'last_visit',
  'display_recorded_rate_pct', 'oos_records', 'ending_qty', 'inventory_amount',
];

function tableOf(rows) {
  // 列 = 所有行 key 的并集；有 segment_label 时隐藏原始 segment 代码
  const keys = [];
  rows.forEach((r) => Object.keys(r).forEach((k) => { if (!keys.includes(k)) keys.push(k); }));
  const cols = keys
    .filter((k) => !(k === 'segment' && (keys.includes('segment_label') || keys.includes('label'))))
    .sort((a, b) => {
      const ia = COL_ORDER.indexOf(a); const ib = COL_ORDER.indexOf(b);
      return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
    });
  const head = cols.map((k) => `<th>${colName(k)}</th>`).join('');
  const body = rows.map((r) => `<tr>${cols.map((k) => {
    const v = r[k];
    const cls = k === 'gap_te' && typeof v === 'number' ? (v < 0 ? 'is-neg' : 'is-pos') : '';
    return `<td class="${cls}">${fmtCell(k, v)}</td>`;
  }).join('')}</tr>`).join('');
  return `<div class="ag-tablewrap"><table class="ag-table">
    <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function metricsStrip(m) {
  const keys = Object.keys(m || {});
  if (!keys.length) return '';
  return `<div class="ag-metrics">${keys.map((k) =>
    `<span class="ag-metric"><i>${METRIC_NAME[k] || colName(k)}</i>
       <b>${fmtCell(k, m[k])}</b></span>`).join('')}</div>`;
}

function renderData(list, calls) {
  const tools = (list || []).filter((t) => t && t.groups && t.groups.length);
  if (!tools.length) {
    return (calls || []).length
      ? '<details class="ag-detail ag-data"><summary>数据证据</summary><p class="ag-empty">工具已执行，但当前范围没有可展示的明细行；请查看结论中的数据限制或调整范围后重试。</p></details>'
      : '';
  }
  const totalRows = tools.reduce((s, t) => s + t.groups.reduce((x, g) => x + g.rows.length, 0), 0);
  const tabs = tools.map((t, i) => `<button type="button" class="ag-tab${i === 0 ? ' is-on' : ''}"
      data-k="${esc(t.key)}">${TOOL_NAME[t.tool] || t.label || t.tool}
      <span>${t.ok ? '' : '失败 · '}${t.summary || ''}</span></button>`).join('');

  const panes = tools.map((t, i) => {
    const groups = t.groups.map((g) => `<h4 class="ag-tablecap">${GROUP_NAME[g.name] || g.name}
      <span class="ag-sub2">${g.total} ${g.total > g.rows.length ? `（显示前 ${g.rows.length}）` : ''} 行</span></h4>
      ${tableOf(g.rows)}`).join('');
    const args = Object.entries(t.args || {})
      .filter(([, v]) => v !== '' && v !== null && v !== undefined && !(Array.isArray(v) && !v.length))
      .map(([k, v]) => `${colName(k)}=${Array.isArray(v) ? v.join('、') : v}`).join('，');
    return `<section class="ag-pane${i === 0 ? ' is-on' : ''}" data-k="${esc(t.key)}">
      <div class="ag-pane-scroll">
      ${metricsStrip(t.metrics)}
      ${groups || '<p class="ag-sub">该工具本次没有明细行。</p>'}
      <p class="ag-src"><b>查询条件</b>：${esc(args || '—')}<br>
        <b>数据源</b>：${esc(t.source || '—')}${t.tables && t.tables.length ? `（表 ${esc(t.tables.join(' / '))}）` : ''}<br>
        ${t.caliber ? `<b>口径</b>：${esc(t.caliber)}<br>` : ''}
        ${(t.notes || []).map((n) => `· ${esc(n)}`).join('<br>')}</p>
      </div>
      ${t.error ? `<p class="ag-src is-err">错误：${esc(t.error.message || t.error.type || '')}</p>` : ''}
    </section>`;
  }).join('');

  return `<details class="ag-detail ag-data">
    <summary>数据核对：${tools.length} 个工具 · ${totalRows} 行明细（报告里的数字都能在这里找到）</summary>
    <div class="ag-data-body">
      <div class="ag-tabs">${tabs}</div>
      <div class="ag-panes">${panes}</div>
    </div>
  </details>`;
}

function renderGenerator(g) {
  const mode = g.mode || 'template';

  if (mode === 'llm') {
    const corrected = g.guard?.corrected
      ? `；数字门禁已纠正 ${g.guard.violations?.length || 0} 项` : '';
    return `<div class="ag-gen is-llm"><span class="dot"></span>
      叙事由 ${esc(g.model || 'LLM')} 生成，数字来自 SQL；已通过数字白名单校验${esc(corrected)}</div>`;
  }
  if (mode === 'clarification') {
    return `<div class="ag-gen is-warn"><span class="dot"></span>需要补充信息后才能分析</div>`;
  }
  const why = { not_configured: '未配置模型（LLM_API_KEY）',
    numeric_guard: '模型输出含证据外数字，已回退规则文本' }[g.fallback_reason]
    || (g.fallback_reason ? `模型不可用（${g.fallback_reason}）` : '规则模板');
  return `<div class="ag-gen is-warn"><span class="dot"></span>
    规则模板生成 · ${esc(why)} · 所有数字来自 SQL</div>`;
}

function renderLlmTrace(trace) {
  if (!trace || !trace.length) return '';
  const names = { intent: '意图理解', planner: 'Planner 动态规划',
    supervisor_review: 'Supervisor Review', reporter: '统一 Reporter' };
  const rows = trace.map((item) => {
    const status = item.fallback_used ? '降级' : (item.llm_used ? 'DeepSeek' : '规则');
    const detail = item.fallback_used ? ` · ${item.fallback_reason || '模型不可用'}` : '';
    return `<li><b>${esc(names[item.llm_stage] || item.llm_stage)}</b>：${esc(status)} ·
      ${esc(item.model_name || '—')} · ${esc(item.decision_mode || '—')}${esc(detail)}</li>`;
  }).join('');
  return `<details class="ag-detail"><summary>模型执行审计：${trace.length} 条</summary>
    <ul class="ag-list">${rows}</ul></details>`;
}

const SCENE_GOAL = Object.fromEntries(QUICK.map((item) => [item.code, item.goal]));
let execDetailSeq = 0;

const STATUS_META = {
  SUCCESS: ['成功', 'is-success'],
  PARTIAL: ['部分完成', 'is-partial'],
  FALLBACK: ['降级', 'is-fallback'],
  BLOCKED: ['阻断', 'is-blocked'],
  PERMISSION_DENIED: ['权限不足', 'is-blocked'],
  NEEDS_CLARIFICATION: ['待补充信息', 'is-partial'],
};

function compactConclusions(rep) {
  const out = [];
  const add = (text) => {
    const value = String(text || '').trim();
    if (value && !out.includes(value)) out.push(value);
  };
  // headline 是后端统一 Reporter 已通过数字门禁的主结论；优先按自然分句压成两条。
  String(rep?.headline || '').split(/[；。]\s*/).forEach(add);
  if (out.length < 2) {
    const preferred = (rep?.sections || []).filter((section) =>
      /结论|发现|问题|机会|风险/.test(section.title || ''));
    [...preferred, ...(rep?.sections || [])].forEach((section) => {
      (section.items || []).forEach((item) => {
        if (item.kind === 'fact' || item.kind === 'inference') add(item.text);
      });
    });
  }
  return out.slice(0, 2);
}

function renderPlanDetail(plan) {
  const items = plan?.items || [];
  if (!items.length) return '<p class="ag-exec-empty">本次为简单查询，无复杂任务拆解。</p>';
  return `<ol class="ag-exec-list">${items.map((item) => {
    const title = item.label || item.task_goal || item.goal || item.tool || item.selected_tool || '分析任务';
    const tool = item.tool || item.selected_tool || '';
    const detail = item.why || item.expected_evidence || item.completion_criteria || '';
    return `<li><b>${esc(title)}</b>${tool ? `<code>${esc(tool)}</code>` : ''}
      ${detail ? `<span>${esc(detail)}</span>` : ''}</li>`;
  }).join('')}</ol>`;
}

function renderToolDetail(tools) {
  if (!tools?.length) return '<p class="ag-exec-empty">本次没有执行工具。</p>';
  return `<div class="ag-exec-tools">${tools.map((tool) => `<div class="ag-exec-tool ${tool.ok ? '' : 'is-error'}">
    <div><b>${esc(tool.label || TOOL_NAME[tool.tool] || tool.tool)}</b>
      <code>${esc(tool.tool)}</code><span>${tool.ok ? '成功' : '失败'} · ${esc(tool.elapsed_ms ?? '—')}ms</span></div>
    <p>${esc(tool.ok ? (tool.summary || '已返回数据') : (tool.error?.message || '工具执行失败'))}</p>
  </div>`).join('')}</div>`;
}

function renderCheckerDetail(checker, checks) {
  const rows = checks || checker?.checks || [];
  if (!checker?.status && !rows.length) return '<p class="ag-exec-empty">本次没有独立 Checker 明细。</p>';
  return `<p class="ag-exec-line"><b>状态</b> ${esc(checker?.status || '—')} ${esc(checker?.message || '')}</p>
    ${rows.length ? `<ul class="ag-exec-checks">${rows.map((item) =>
      `<li class="${item.ok ? 'is-ok' : 'is-error'}">${item.ok ? '✓' : '✗'} ${esc(item.name || item.message || '校验项')}
        ${item.diff !== undefined ? `<span>差异 ${esc(item.diff)}</span>` : ''}</li>`).join('')}</ul>` : ''}`;
}

function renderSourceDetail(data) {
  const rows = data?.tool_data || [];
  if (!rows.length) return '<p class="ag-exec-empty">本次没有可展示的数据来源明细。</p>';
  return `<ul class="ag-exec-sources">${rows.map((item) => `<li>
    <b>${esc(item.label || TOOL_NAME[item.tool] || item.tool || '数据工具')}</b>
    <span>${esc(item.source || '来源待披露')}</span>
    ${item.caliber ? `<small>口径：${esc(item.caliber)}</small>` : ''}
    ${item.period && Object.keys(item.period).length ? `<small>期间：${esc(Object.values(item.period).filter(Boolean).join(' · '))}</small>` : ''}
  </li>`).join('')}</ul>`;
}

function renderModelDetail(data, generator) {
  const trace = data?.llm_trace || [];
  const rows = trace.map((item) => {
    const status = item.fallback_used ? '降级' : (item.llm_used ? '模型完成' : '规则执行');
    return `<li><b>${esc({intent: '意图理解', planner: 'Planner', supervisor_review: 'Supervisor Review', reporter: 'Reporter'}[item.llm_stage] || item.llm_stage)}</b>
      <span>${esc(status)} · ${esc(item.model_name || '—')} · ${esc(item.decision_mode || '—')}</span>
      ${item.fallback_used ? `<small>原因：${esc(item.fallback_reason || '模型不可用')}</small>` : ''}</li>`;
  }).join('');
  return `${renderGenerator(generator || {})}
    ${rows ? `<ul class="ag-exec-models">${rows}</ul>` : '<p class="ag-exec-empty">本次没有模型调用轨迹。</p>'}`;
}

function renderExecutionSummary(data, rep) {
  if (!data) return '';
  const plan = data.plan || {};
  const review = [...(data.llm_trace || [])].reverse()
    .find((item) => item.llm_stage === 'supervisor_review');
  const checker = data.report?.checker || {};
  const permission = data.governance?.permission || {};
  const principal = permission.principal || {};
  const quality = data.governance?.data_quality || {};
  const status = data.execution_status || data.failure_handling?.execution_status || 'SUCCESS';
  const [statusLabel, statusClass] = STATUS_META[status] || [status, 'is-partial'];
  const conclusions = compactConclusions(rep || data.report || {});
  const generator = rep?.generator || data.report?.generator || {};
  const lastModel = [...(data.llm_trace || [])].reverse().find((item) => item.model_name);
  const modelName = generator.model || lastModel?.model_name || '规则引擎';
  const modelState = status === 'FALLBACK' || generator.mode === 'template'
    ? `降级 · ${generator.fallback_reason || lastModel?.fallback_reason || '规则模板'}` : '已完成';
  const checkerLabel = checker.status === 'passed' ? '通过' : (checker.status || '未提供');
  const risk = (data.failure_handling?.events || [])[0];
  const detailId = `agent-exec-detail-${++execDetailSeq}`;
  return `<section class="ag-exec" data-status="${esc(status)}" aria-label="Agent 执行摘要">
    <div class="ag-exec-head">
      <span class="ag-exec-status ${statusClass}">${esc(statusLabel)}</span>
      <div><b>${esc(data.scene?.label || '经营分析')}</b><span>${esc(SCENE_GOAL[data.scene?.code] || '经营问题分析')}</span></div>
    </div>
    <ul class="ag-exec-conclusions">${conclusions.length
      ? conclusions.map((text) => `<li>${esc(text)}</li>`).join('')
      : '<li>本次没有可展示的已验证核心结论。</li>'}</ul>
    ${risk ? `<div class="ag-exec-risk"><b>${esc(statusLabel)}</b><span>${esc(risk.detail || risk.fallback_reason || risk.failure_code || '存在需要关注的执行异常')}</span></div>` : ''}
    <div class="ag-exec-meta">
      <span class="${checker.status === 'passed' ? 'is-ok' : 'is-warn'}"><b>Checker</b>${esc(checkerLabel)}</span>
      <span class="${status === 'FALLBACK' ? 'is-warn' : ''}"><b>模型</b>${esc(modelName)} · ${esc(modelState)}</span>
      ${quality.level && quality.level !== 'HIGH' ? `<span class="is-warn"><b>数据质量</b>${esc(quality.level)} · ${esc(quality.conclusion_status || '需关注')}</span>` : ''}
    </div>
    <button type="button" class="ag-exec-toggle" aria-expanded="false" aria-controls="${detailId}">
      <span>查看执行详情</span><i aria-hidden="true">⌄</i>
    </button>
    <div class="ag-exec-detail" id="${detailId}" aria-hidden="true" inert><div class="ag-exec-detail-inner">
      ${renderFailureHandling(data)}
      <section><h4>Planner 任务规划</h4><p class="ag-exec-caption">${esc(plan.generation_mode || plan.mode || '—')}</p>${renderPlanDetail(plan)}</section>
      <section><h4>Supervisor 决策摘要</h4><p>${esc(review?.reason || data.governance?.stop_reason || '按已验证证据完成复核')}</p></section>
      <section><h4>工具调用轨迹</h4>${renderToolDetail(data.tools || [])}</section>
      <section><h4>Checker 校验结果</h4>${renderCheckerDetail(checker, data.cross_checks)}</section>
      <section class="ag-exec-two">
        <div><h4>权限范围</h4><p>${esc(principal.display_name || '集团管理员（Demo）')}</p><small>${esc(permission.enforcement || permission.mode || 'DEMO_READ_ONLY')}</small></div>
        <div><h4>数据质量</h4><p>${esc(quality.level || '—')} · ${esc(quality.conclusion_status || '—')}</p><small>${esc(quality.message || '尚未产生数据质量说明')}</small></div>
      </section>
      <section><h4>数据来源和计算依据</h4>${renderSourceDetail(data)}${renderData(data.tool_data, data.tools)}</section>
      <section><h4>模型调用与 fallback</h4>${renderModelDetail(data, generator)}</section>
    </div></div>
  </section>`;
}

function renderFailureHandling(data) {
  const handling = data && data.failure_handling;
  if (!handling || handling.execution_status === 'SUCCESS') return '';
  const items = (handling.events || []).map((event) => `<li>
    <b>${esc(event.failure_type)} · ${esc(event.failure_code)}</b>
    <span>${esc(event.detail || event.fallback_reason || '已按确定性规则处理')}</span>
    <small>影响：${esc((event.affected_entities || []).join('；') || '本次分析')}
      ｜阶段：${esc(event.failure_stage)}｜重试：${esc(event.retry_count)}</small>
  </li>`).join('');
  const actions = (handling.recovery_actions || [])
    .map((item) => `<li>${esc(item)}</li>`).join('');
  return `<section class="ag-failure" data-status="${esc(handling.execution_status)}">
    <div class="ag-failure-head"><b>${esc(handling.execution_status)}</b>
      <span>是否继续：${handling.can_continue ? '可输出有证据的部分结果' : '当前已停止'}</span></div>
    <ul class="ag-failure-events">${items}</ul>
    <p><b>当前降级模式：</b>${esc(handling.fallback_mode || 'NONE')}
      ｜<b>人工处理：</b>${handling.requires_human_review ? '需要' : '当前不需要'}</p>
    ${actions ? `<div><b>恢复建议</b><ul>${actions}</ul></div>` : ''}
    ${handling.retry_with_request_id ? `<button type="button" class="ag-retry"
      data-retry="${esc(handling.retry_with_request_id)}">按恢复建议重新执行</button>` : ''}
  </section>`;
}

function renderReport(rep, data) {
  const r = rep.result || { rows: [] };
  const fu = rep.followup || {};
  // 完整响应的核心结论已进入紧凑摘要，避免在摘要上方重复一遍；
  // 历史会话快照没有 data，仍保留 headline，保证恢复后可读。
  const head = `${data ? '' : `<div class="ag-headline">${esc(rep.headline || '分析结果')}</div>`}
    <p class="ag-meta">范围：${esc(rep.scope_label)}　｜　期间：${esc(rep.period_label)}</p>
    ${fu.note ? `<p class="ag-followup">承接上一轮：${esc(fu.note)}</p>` : ''}
    ${(rep.data_limits || []).map((l) => `<div class="ag-limit">
      <b>数据限制 · ${esc(l.label)}</b>
      <span>有 ${esc(l.have)}；没有 ${esc(l.missing)}。${esc(l.offer)}</span></div>`).join('')}`;

  if (rep.needs_clarification) {
    return `${head}
      ${renderExecutionSummary(data, rep)}
      <div class="ag-sec"><div class="alert alert-warning">${esc(rep.problems?.summary || '')}</div></div>
      `;
  }

  // 七段式（P10）：结构由后端给，前端只负责按标签渲染，不在前端重排内容
  if (rep.sections && rep.sections.length) {
    return `${head}
      ${renderExecutionSummary(data, rep)}
      ${renderTrustedChart(data?.chart)}
      ${renderSections(rep)}
      ${renderFeedback(data)}`;
  }

  return `${head}
    ${renderExecutionSummary(data, rep)}
    ${renderTrustedChart(data?.chart)}
    <div class="ag-sec"><h3>一、经营结果</h3>${renderKpis(r.rows || [])}</div>
    <div class="ag-sec"><h3>二、主要问题</h3>${renderProblems(rep.problems || {})}</div>
    <div class="ag-sec"><h3>三、经营原因分析</h3>${renderCauses(rep.causes || {})}</div>
    <div class="ag-sec"><h3>四、改善建议</h3>${renderActions(rep.actions)}</div>
    <div class="ag-sec"><h3>五、数据依据</h3>${renderBasis(rep.basis || {}, rep)}</div>`;
}

function suggestedAction(rep) {
  const action = (rep?.actions || []).find((item) => item?.action)?.action;
  if (action) return action;
  for (const section of (rep?.sections || [])) {
    if (!/建议|行动|下一步/.test(section.title || '')) continue;
    const item = (section.items || []).find((row) => row?.text);
    if (item) return item.text;
  }
  return '复核本次经营建议并记录执行结果';
}

function renderFeedback(data) {
  if (!data || !data.run_id || data.needs_clarification) return '';
  const scene = data.scene?.primary_intent || '';
  const suggestion = suggestedAction(data.report || {});
  return `<div class="ag-feedback ag-action-loop" data-run="${esc(data.run_id)}" data-scene="${esc(scene)}">
    <div class="ag-action-head"><b>行动闭环 · 人工确认后创建 Demo 待办</b>
      <small>仅记录待办和反馈；不修改 ERP/采购/价格，不外发通知</small></div>
    <label>建议内容<textarea data-action-suggestion rows="2">${esc(suggestion)}</textarea></label>
    <div class="ag-action-fields">
      <label>负责人<input data-action-owner maxlength="80" placeholder="如：战区B客户经理"></label>
      <label>截止日期<input data-action-due type="date"></label>
    </div>
    <div class="ag-action-buttons">
      <button type="button" data-action-create>确认并创建待办</button>
      <button type="button" data-feedback="reject">驳回建议</button>
    </div>
    <div class="ag-action-tasks" aria-live="polite"><span>正在读取本次待办…</span></div>
  </div>`;
}

function taskButtons(task) {
  if (task.status === 'CONFIRMED') return '<button type="button" data-task-next="IN_PROGRESS">开始执行</button>';
  if (task.status === 'IN_PROGRESS') return '<button type="button" data-task-next="COMPLETED">记录完成</button>';
  if (task.status === 'COMPLETED') return '<button type="button" data-task-next="REVIEWED">效果复盘</button>';
  return '<span class="ag-task-done">已完成复盘</span>';
}

function renderActionTasks(box, tasks) {
  const target = box?.querySelector('.ag-action-tasks');
  if (!target) return;
  if (!tasks?.length) {
    target.innerHTML = '<span>本次尚未创建待办。</span>';
    return;
  }
  target.innerHTML = tasks.map((task) => `<article class="ag-task" data-task-id="${esc(task.task_id)}">
    <div><b>${esc(task.suggestion)}</b><em>${esc(task.status)}</em></div>
    <p>负责人：${esc(task.owner)}　截止：${esc(task.due_date)}</p>
    ${task.result ? `<p>执行结果：${esc(task.result)}</p>` : ''}
    ${task.review_note ? `<p>复盘：${esc(task.review_outcome)} · ${esc(task.review_note)}</p>` : ''}
    ${taskButtons(task)}
  </article>`).join('');
}

async function hydrateActionTasks(box) {
  if (!box?.dataset.run) return;
  const res = await fetch(`/api/agent/actions?run_id=${encodeURIComponent(box.dataset.run)}`);
  const payload = await res.json();
  if (!res.ok || !payload.ok) throw new Error(payload.error?.message || '待办读取失败');
  renderActionTasks(box, payload.data?.tasks || []);
}

async function createActionTask(button) {
  const box = button.closest('.ag-action-loop');
  const suggestion = box.querySelector('[data-action-suggestion]')?.value.trim() || '';
  const owner = box.querySelector('[data-action-owner]')?.value.trim() || '';
  const dueDate = box.querySelector('[data-action-due]')?.value || '';
  if (!suggestion || !owner || !dueDate) throw new Error('请填写建议内容、负责人和截止日期');
  const res = await fetch('/api/agent/actions', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      operation: 'create', confirmed: true, run_id: box.dataset.run, thread_id: threadId,
      suggestion, owner, due_date: dueDate, created_by: 'demo_user'
    })});
  const payload = await res.json();
  if (!res.ok || !payload.ok) throw new Error(payload.error?.message || '待办创建失败');
  await hydrateActionTasks(box);
  toast('已创建 Demo 待办；未修改业务系统，也未发送外部通知');
}

async function transitionActionTask(button) {
  const task = button.closest('.ag-task');
  const box = button.closest('.ag-action-loop');
  const toStatus = button.dataset.taskNext;
  const body = {operation: 'transition', task_id: task.dataset.taskId, to_status: toStatus,
    actor: 'demo_user'};
  if (toStatus === 'IN_PROGRESS') body.note = window.prompt('可选：记录执行计划或当前进展', '') || '';
  if (toStatus === 'COMPLETED') {
    body.result = window.prompt('请记录实际执行结果（必填）', '') || '';
    if (!body.result) return;
  }
  if (toStatus === 'REVIEWED') {
    body.review_outcome = window.prompt('复盘结论：EFFECTIVE / INEFFECTIVE / PENDING', 'PENDING') || '';
    body.review_note = window.prompt('请记录效果证据或待观察项（必填）', '') || '';
    if (!body.review_note) return;
  }
  const res = await fetch('/api/agent/actions', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const payload = await res.json();
  if (!res.ok || !payload.ok) throw new Error(payload.error?.message || '待办更新失败');
  await hydrateActionTasks(box);
}

async function submitFeedback(button) {
  const box = button.closest('.ag-feedback');
  if (!box || box.dataset.done) return;
  const action = button.dataset.feedback;
  const note = action === 'reject' || action === 'modified'
    ? (window.prompt('可选：说明修改或驳回原因', '') || '') : '';
  const modifiedContent = action === 'modified'
    ? (window.prompt('可选：记录修改后的建议内容', '') || '') : '';
  const res = await fetch('/api/agent/feedback', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      run_id: box.dataset.run, thread_id: threadId, scene: box.dataset.scene,
      question: '', suggestion_id: 'report', action, note, modified_content: modifiedContent
    })});
  const payload = await res.json();
  if (!res.ok || !payload.ok) throw new Error(payload.error?.message || '反馈记录失败');
  box.dataset.done = '1';
  box.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  box.querySelector('small').textContent = `已记录：${button.textContent}；未修改业务数据`;
  loadKpis($('kpi-scene')?.value || '');
}

document.addEventListener('click', (event) => {
  const execToggle = event.target.closest('.ag-exec-toggle');
  if (execToggle) {
    const card = execToggle.closest('.ag-exec');
    const panel = document.getElementById(execToggle.getAttribute('aria-controls'));
    const expanded = execToggle.getAttribute('aria-expanded') === 'true';
    execToggle.setAttribute('aria-expanded', String(!expanded));
    execToggle.querySelector('span').textContent = expanded ? '查看执行详情' : '收起执行详情';
    card?.classList.toggle('is-expanded', !expanded);
    panel?.setAttribute('aria-hidden', String(expanded));
    panel?.toggleAttribute('inert', expanded);
    return;
  }
  const button = event.target.closest('[data-feedback]');
  if (button) {
    submitFeedback(button).catch((error) => toast(error.message));
    return;
  }
  const chartPoint = event.target.closest('[data-chart-followup]');
  if (chartPoint) {
    questionBox.value = chartPoint.dataset.chartFollowup || '';
    questionBox.focus();
    ask();
    return;
  }
  const createTask = event.target.closest('[data-action-create]');
  if (createTask) {
    createActionTask(createTask).catch((error) => toast(error.message));
    return;
  }
  const nextTask = event.target.closest('[data-task-next]');
  if (nextTask) {
    transitionActionTask(nextTask).catch((error) => toast(error.message));
    return;
  }
  const retry = event.target.closest('[data-retry]');
  if (!retry) return;
  const requestId = retry.dataset.retry || '';
  const question = recoveryQuestions.get(requestId) || '';
  if (!question) { toast('原问题已不在当前会话，请重新输入'); return; }
  pendingRecoveryId = requestId;
  questionBox.value = question;
  questionBox.focus();
  toast('已恢复原问题与 request_id；确认异常已修复后再次发送');
});

/* ------------------------------------------------------------ 侧栏 */
function updateUnderstood(u, plan, scene) {
  const rows = [
    ['Agent 场景', scene?.label || intentLabel(u.intent)],
    ['分析意图', intentLabel(u.intent)],
    ['战区', u.region || '全国（五大战区）'],
    ['市场部', u.department || '全部'],
    ['客户', u.customer || '全部'],
    ['期间', u.period_label || `${u.year} 年`],
    ['识别方式', u.by === 'rules+llm' ? '规则 + LLM 辅助' : '规则解析'],
    ['Planner', plan?.generation_mode === 'llm_constrained' ? 'DeepSeek 受约束规划' : (plan?.mode || '—')],
  ];
  if (plan && plan.items) {
    const short = { query_budget: '预算', query_erp_actual: 'ERP',
      query_customer_performance: '客户', query_pos_performance: 'POS',
      query_sfa_visit: 'SFA', query_wholesale_performance: '批发',
      query_sku_distribution_opportunities: 'SKU机会',
      query_store_price_anomalies: '价格', forecast_material_demand: '需求预测',
      query_sku_bom: 'BOM', query_material_inventory: '库存/在途',
      calculate_purchase_suggestion: '采购建议' };
    const seen = [];
    plan.items.forEach((i) => {
      const s = short[i.tool] || i.tool;
      if (!seen.includes(s)) seen.push(s);
    });
    rows.push(['查询工具', `${plan.items.length} 次 · ${seen.join('/')}`]);
  }
  $('understood').innerHTML = rows.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  $('u-by').textContent = u.by === 'rules+llm' ? '规则+LLM' : '规则';
}

function intentLabel(k) {
  return { attainment_review: '预算达成复盘', customer_focus: '客户缺口定位',
    pos_review: '终端动销复盘', sfa_review: '拜访执行复盘',
    wholesale_review: '批发经营复盘', overview: '经营概况',
    growth_opportunity: '高增长 SKU 机会', price_anomaly: '门店价格竞争力',
    supply_chain_shortage: '供应链短缺风险', supply_chain_forecast: '包材采购预测',
    supply_chain_growth: '经营情景模拟' }[k] || k || '—';
}

function resetSide() {
  $('steps').innerHTML = '';
  $('tools').innerHTML = '';
  $('checks').innerHTML = '<div class="ag-kv-empty">等待分析</div>';
  $('step-count').textContent = '0';
  $('tool-count').textContent = '0';
  $('check-count').textContent = '—';
}

function pushStep(node, status, detail) {
  const names = {
    understand_question: '理解问题', supervise_task: 'Supervisor 路由',
    preflight_check: '数据与权限预检', plan_direct_query: '生成直达计划',
    plan_analysis: 'Planner 任务拆解',
    call_business_tools: '调用业务工具', analyze_results: '计算达成与缺口',
    check_results: 'Checker 确定性校验', supervisor_review: 'Supervisor 证据复核',
    generate_report: '生成报告', ask_clarification: '需要补充信息',
    handle_failure: '取数失败',
  };
  const li = document.createElement('li');
  li.className = status || '';
  li.innerHTML = `<span class="ag-step-t">${esc(names[node] || node)}</span>
    ${detail ? `<span class="ag-step-d">${esc(detail)}</span>` : ''}`;
  $('steps').appendChild(li);
  $('step-count').textContent = String($('steps').children.length);
}

function renderTools(tools) {
  $('tools').innerHTML = tools.length ? tools.map((t) => `<div class="ag-tool ${t.ok ? '' : 'bad'}">
    <div class="ag-tool-top">
      <span class="ag-tool-name">${esc(t.tool)}</span>
      <span class="ag-tool-ms">${t.elapsed_ms ?? '—'}ms</span>
    </div>
    <div class="ag-tool-sum">${esc(t.ok ? (t.summary || '') : ('失败：' + (t.error?.message || '未知')))}</div>
  </div>`).join('') : '<div class="ag-kv-empty">当前还没有工具调用</div>';
  $('tool-count').textContent = String(tools.length);
}

function renderChecks(checks, checker) {
  const state = checker || {};
  const status = state.status
    ? `<div class="ag-check ag-check-state"><span class="${state.status === 'passed' ? 'ag-ok' : 'ag-no'}">${state.status === 'passed' ? '✓' : '!'}</span><b>${esc(state.status)}</b><span>${esc(state.message || '')}</span></div>` : '';
  if (!checks || !checks.length) {
    $('checks').innerHTML = status || '<div class="ag-kv-empty">本次查询没有独立 Checker 明细</div>';
    $('check-count').textContent = '—';
    return;
  }
  $('checks').innerHTML = status + checks.map((c) => `<div class="ag-check">
    <span class="${c.ok ? 'ag-ok' : 'ag-no'}">${c.ok ? '✓' : '✗'}</span>
    <span>${esc(c.name)}${c.diff !== undefined ? `<span class="ag-sub">
      （差 ${esc(c.diff)}）</span>` : ''}</span></div>`).join('');
  $('check-count').textContent = `${checks.filter((c) => c.ok).length}/${checks.length}`;
}

/* ------------------------------------------------------------ 提问主流程 */
async function ask() {
  const q = questionBox.value.trim();
  if (!q || busy) return;
  busy = true;
  btnSend.disabled = true;
  questionBox.value = '';
  addUser(q);
  const bubble = addBotLoading();
  resetSide();
  setStatus('分析中…', 'busy');

  const loadingText = () => bubble.querySelector('#loading-text');
  const ctx = { ...PAGE_CTX, ...(quickContext || {}) };
  quickContext = null;
  const recoveryId = pendingRecoveryId;
  pendingRecoveryId = '';

  let data = null;
  try {
    data = await runStream(q, ctx, (evt) => {
      if (evt.event === 'start') {
        loadingText().textContent = '理解问题…';
      } else if (evt.event === 'node') {
        pushStep(evt.node, 'start', evt.detail);
        loadingText().textContent = `${nodeName(evt.node)}…`;
      } else if (evt.event === 'tool') {
        const box = $('tools');
        const div = document.createElement('div');
        div.className = `ag-tool ${evt.ok ? '' : 'bad'}`;
        div.innerHTML = `<div class="ag-tool-top">
            <span class="ag-tool-name">${esc(evt.tool)}</span>
            <span class="ag-tool-ms">${evt.elapsed_ms ?? '—'}ms</span></div>
          <div class="ag-tool-sum">${esc(evt.ok ? (evt.summary || '') : '失败')}</div>`;
        box.appendChild(div);
        $('tool-count').textContent = String(box.children.length);
        loadingText().textContent = `${evt.label || evt.tool}…`;
      }
    }, recoveryId);
  } catch (e) {
    // 流式失败 → 回退一次性接口
    try {
      data = await runOnce(q, ctx, recoveryId);
    } catch (e2) {
      bubble.remove();
      addBotError(`分析失败：${e2.message || e}。你可以重试或开始新会话；本次失败未修改任何业务数据。`);
      setStatus('失败', 'error');
      busy = false;
      btnSend.disabled = false;
      return;
    }
  }

  bubble.remove();
  if (!data) {
    addBotError('没有拿到分析结果，请重试。');
    setStatus('失败', 'error');
    busy = false;
    btnSend.disabled = false;
    return;
  }

  renderAnswer(data);
  updateUnderstood(data.understood, data.plan, data.scene);
  renderTools(data.tools || []);
  renderChecks(data.report?.checker?.checks || data.cross_checks || [], data.report?.checker || {});
  setStatus(`${data.execution_status || 'SUCCESS'} · ${Math.round((data.timings?.total || 0))}ms`,
            data.execution_status === 'SUCCESS' ? 'ok' : 'error');
  loadKpis($('kpi-scene')?.value || '');
  history.push({ role: 'user', content: q },
               { role: 'assistant', content: data.report?.headline || '' });
  // 存一份快照，切页面后能接着看（不存 tool_data）
  const turns = loadTurns(threadId);
  turns.push({ q, report: data.report || {}, headline: data.report?.headline || '' });
  saveTurns(threadId, turns);
  busy = false;
  btnSend.disabled = false;
  questionBox.focus();
}

function nodeName(n) {
  return { understand_question: '理解问题', supervise_task: 'Supervisor 路由',
    preflight_check: '数据与权限预检', plan_direct_query: '生成直达计划',
    plan_analysis: 'Planner 任务拆解', call_business_tools: 'Executor 查询数据',
    check_results: 'Checker 校验', supervisor_review: 'Supervisor 证据复核',
    analyze_results: '计算缺口',
    generate_report: '生成报告' }[n] || n;
}

function renderAnswer(data) {
  if (data.run_id && data.question) recoveryQuestions.set(data.run_id, data.question);
  const el = document.createElement('article');
  el.className = 'ag-msg ag-msg-bot';
  el.innerHTML = `<div class="ag-avatar">AI</div>
    <div class="ag-bubble">${renderReport(data.report || {}, data)}</div>`;
  thread.appendChild(el);
  el.querySelectorAll('.ag-action-loop').forEach((box) => {
    hydrateActionTasks(box).catch((error) => {
      const target = box.querySelector('.ag-action-tasks');
      if (target) target.textContent = error.message;
    });
  });
  // 滚到这条回答的开头（而不是最底部）：结论和 KPI 才是第一眼要看的。
  // 用 rect 差值算相对滚动容器的位置，避免 offsetTop 参照物不是 .ag-thread 时偏掉。
  const top = el.getBoundingClientRect().top - thread.getBoundingClientRect().top
    + thread.scrollTop;
  thread.scrollTop = Math.max(0, top - 8);
}

/* ------------------------------------------------------------ 两种调用方式 */
async function runStream(q, ctx, onEvent, requestId = '') {
  const res = await fetch('/api/agent/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question: q, thread_id: threadId, context: ctx, history,
      request_id: requestId, parent_request_id: requestId }),
  });
  if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let done = null;
  while (true) {
    const { done: fin, value } = await reader.read();
    if (fin) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const evt = parseSse(raw);
      if (!evt) continue;
      onEvent(evt);
      if (evt.event === 'done') done = evt.data;
      if (evt.event === 'error') throw new Error(evt.error?.message || 'agent error');
    }
    // done 已到就不等连接关闭（服务端可能还挂着 keep-alive）
    if (done) { try { await reader.cancel(); } catch (e) { /* 忽略 */ } break; }
  }
  if (!done) throw new Error('流式响应未返回结果');
  return done;
}

function parseSse(raw) {
  let name = 'message';
  const dataLines = [];
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) name = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return null;
  let payload = {};
  try { payload = JSON.parse(dataLines.join('\n')); } catch (e) { return null; }
  return { event: name, ...payload };
}

async function runOnce(q, ctx, requestId = '') {
  const res = await fetch('/api/agent/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question: q, thread_id: threadId, context: ctx, history,
      request_id: requestId, parent_request_id: requestId }),
  });
  const p = await res.json();
  if (!p.ok) throw new Error(p.error?.message || '请求失败');
  return p.data;
}

/* ------------------------------------------------------------ 事件绑定 */
// 数据核对的工具页签：委托到线程容器，避免每次渲染后重新绑定
thread.addEventListener('click', (e) => {
  const tab = e.target.closest('.ag-tab');
  if (!tab) return;
  const body = tab.closest('.ag-data-body');
  if (!body) return;
  const k = tab.dataset.k;
  body.querySelectorAll('.ag-tab').forEach((n) => n.classList.toggle('is-on', n === tab));
  body.querySelectorAll('.ag-pane').forEach((n) => n.classList.toggle('is-on', n.dataset.k === k));
});

$('ask-form').addEventListener('submit', (e) => {
  e.preventDefault();
  ask();
});

questionBox.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    ask();
  }
});

$('btn-clear').addEventListener('click', () => {
  thread.querySelectorAll('.ag-msg:not(.ag-welcome)').forEach((n) => n.remove());
  history = [];
  try { sessionStorage.removeItem(msgKey(threadId)); } catch (e) { /* 忽略 */ }
  threadId = `t-${Date.now().toString(36)}`;
  // 新会话号要同步回父页面，否则切到别的页面时抽屉还用旧的 thread_id，
  // 会把刚清掉的对话又恢复出来。
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ type: 'thread-reset', thread_id: threadId },
                                location.origin);
    }
  } catch (e) { /* 独立打开 agent.html 时没有父页面 */ }
  $('understood').innerHTML = '<div class="ag-kv-empty">还没有提问</div>';
  $('u-by').textContent = '—';
  resetSide();
  setStatus('就绪');
  toast('已开始新会话');
  questionBox.focus();
});

/* embed 模式（页内抽屉）里输入框只有 460px 宽，长 placeholder 会被截断 */
if (EMBED) {
  questionBox.placeholder = '接着当前筛选问，例如：谁没达成？为什么？';
  document.title = 'AI 经营助手 · 页内对话';
}

restoreTurns();
applyContext(PAGE_CTX);
resetSide();
loadHealth();
loadKpis();
$('kpi-scene')?.addEventListener('change', (e) => loadKpis(e.target.value));
