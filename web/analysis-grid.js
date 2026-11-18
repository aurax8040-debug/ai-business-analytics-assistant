/* 综合经营分析 V2 —— 战区 → 市场部 → 客户类型 交叉表 + 达成率联动
 * 所有数字来自 /api/analysis/grid（后端一次算好），前端只负责排版与交互，
 * 不在浏览器里重算任何比率，避免「表格与右图对不上」。 */

const S = {
  year: 2026, month: 0, region: '', dept: '', category: '', origin: '', segment: '', q: '',
  sort: 'te', order: 'desc',
  page: 1, size: 200,
  data: null,
  collapsed: new Set(),     // key 集合：收起的分组
  selected: '',             // 当前高亮行 key
  attLevel: 'segment',
  savedName: null,
  yearNote: '',             // 旧书签带 Y25 时的提示语
};

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

/* ------------------------------------------------------------ 格式化 */
const nf = (v, d = 0) => (v === null || v === undefined
  ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }));

function pct(v, d = 2) {
  if (v === null || v === undefined) return '—';
  return `${Number(v).toFixed(d)}%`;
}

function attClass(v) {
  if (v === null || v === undefined) return 'none';
  if (v >= 95) return 'g';
  if (v >= 80) return 'y';
  return 'r';
}

function yoyHtml(v) {
  if (v === null || v === undefined) return '<span class="bi-yoy none">—</span>';
  const cls = v >= 0 ? 'pos' : 'neg';
  const arrow = v >= 0 ? '▲' : '▼';
  return `<span class="bi-yoy ${cls}">${arrow} ${Math.abs(v).toFixed(1)}%</span>`;
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('on'), 1800);
}

/* ------------------------------------------------------------ 筛选器 */
function qs() {
  const p = new URLSearchParams();
  p.set('year', S.year);
  p.set('months', S.month === 0 ? 'all' : String(S.month));
  if (S.region) p.set('region', S.region);
  if (S.dept) p.set('dept', S.dept);
  if (S.category) p.set('category', S.category);
  if (S.origin) p.set('origin', S.origin);
  if (S.segment) p.set('segment', S.segment);
  if (S.q) p.set('q', S.q);
  p.set('size', '500');
  return p.toString();
}

function fillSelect(el, items, placeholder, cur) {
  // 只有一个选项时不要「全部」占位 —— 年份只有 2026，留个空项等于允许选空年份
  const head = items.length > 1 ? `<option value="">${placeholder}</option>` : '';
  el.innerHTML = head
    + items.map((x) => `<option value="${x.value}"${String(x.value) === String(cur) ? ' selected' : ''}>
        ${x.label}</option>`).join('');
}

async function bootOptions() {
  const res = await fetch('/api/analysis/grid/options');
  const o = (await res.json()).data;
  fillSelect($('#f-year'), o.years, '全部年份', S.year);
  $('#f-month').innerHTML = o.months.map((m) => `<option value="${m.value}">${m.label}</option>`).join('');
  fillSelect($('#f-region'), o.regions, '全部战区', '');
  fillSelect($('#f-dept'), o.depts, '全部市场部', '');
  fillSelect($('#f-category'), o.categories, '全部品类', '');
  fillSelect($('#f-segment'), o.segments, '全部客户类型', '');
  $('#f-origin').innerHTML = o.origins.map((x) =>
    `<option value="${x.value}">${x.label}${x.note ? `（${x.note}）` : ''}</option>`).join('');
  $('#f-month').value = '0';
  const saved = localStorage.getItem('bi.analysis.params');
  if (saved) {
    try {
      const v = JSON.parse(saved);
      S.savedName = v.name || null;
      Object.assign(S, v.filters || {});
      S.year = 2026;     // 旧存档里可能存着 2025，年份现在不可切换
      syncFiltersFromState();
    } catch (e) { /* 忽略损坏的本地参数 */ }
  }
  renderSavedChip();
}

/* 兼容旧入口携带的筛选参数：/region.html 会跳到
   /analysis.html?dimension=region&months=3&regions=战区A，
   这些书签不该因为改版就失效。dimension 只是历史标记，本页不需要。
   优先级：URL > 本机保存的常用参数 > 默认值。 */
function readUrl() {
  const q = new URLSearchParams(location.search);
  const year = Number(q.get('year'));
  // 本页只有 Y26：旧书签带 year=2025 时按 2026 取数，并提示一句，不静默换口径
  if (year === 2026) S.year = 2026;
  else if (year) S.yearNote = `${year} 年不在本页范围（仅 Y26），已按 2026 年取数`;
  const months = (q.get('months') || '').trim();
  if (months && months !== 'all' && /^\d+$/.test(months)) {
    const m = Number(months);
    if (m >= 1 && m <= 12) S.month = m;
  }
  const region = (q.get('region') || q.get('regions') || '').trim();
  if (region) S.region = region;
  const dept = (q.get('dept') || '').trim();
  if (dept) S.dept = dept;
  const category = (q.get('category') || '').trim();
  if (category) S.category = category;
  const origin = (q.get('origin') || '').trim();
  if (origin) S.origin = origin;
  const segment = (q.get('segment') || '').trim();
  if (segment) S.segment = segment;
}

function syncFiltersFromState() {
  $('#f-year').value = String(S.year);
  $('#f-month').value = String(S.month);
  $('#f-region').value = S.region;
  $('#f-dept').value = S.dept;
  $('#f-category').value = S.category;
  $('#f-origin').value = S.origin;
  $('#f-segment').value = S.segment;
}

function renderSavedChip() {
  const host = $('#bi-filter .bi-actions');
  const old = $('#saved-chip');
  if (old) old.remove();
  if (!S.savedName) return;
  const chip = document.createElement('span');
  chip.id = 'saved-chip';
  chip.className = 'bi-chip';
  chip.innerHTML = `已保存：<b>${S.savedName}</b><u>×</u>`;
  chip.querySelector('u').addEventListener('click', () => {
    localStorage.removeItem('bi.analysis.params');
    S.savedName = null;
    renderSavedChip();
    toast('已清除保存的参数');
  });
  host.insertBefore(chip, host.firstChild);
}

function readFilters() {
  S.year = Number($('#f-year').value) || 2026;
  S.month = Number($('#f-month').value) || 0;
  S.region = $('#f-region').value;
  S.dept = $('#f-dept').value;
  S.category = $('#f-category').value;
  S.origin = $('#f-origin').value;
  S.segment = $('#f-segment').value;
  S.q = ($('#f-q').value || '').trim();
  S.page = 1;
}

/* ------------------------------------------------------------ 取数 */
async function load() {
  const res = await fetch(`/api/analysis/grid?${qs()}`);
  const payload = await res.json();
  if (!payload.ok) {
    $('#grid-body').innerHTML = `<tr><td colspan="8" class="bi-empty">${payload.error || '查询失败'}</td></tr>`;
    return;
  }
  S.data = payload.data;
  $('#bi-updated').textContent = `更新于 ${payload.meta.generated_at.replace('T', ' ').slice(11, 19)}`;
  // 所选月份若全落在观测范围外（11–12 月），如实提示「无数据」，而不是显示一张全 0 的表
  const observed = payload.data.months_observed || [];
  $('#grid-sub').textContent = `${payload.meta.month_scope} · ${S.year} 年`
    + (observed.length ? '' : ' · 所选月份尚无出货数据')
    + (payload.data.budget_available ? '' : ' · 该年无预算库');

  // 页脚把「预算从哪来、分母含哪几个月、数据范围是什么」写在明处，
  // 避免看数的人误以为这是真实下达目标，或以为补的那些行是 ERP 原生数据
  const batch = payload.data.budget_batch || {};
  const prorated = payload.meta.budget_allocation === 'origin_prorated';
  $('#foot-budget').textContent = 'MOCK_BUDGET_TE（模拟预算，非真实下达目标）'
    + (batch.available ? ` · ${batch.batch_id} · 版本 ${batch.version}` : ' · 当前年份无预算库')
    + (prorated ? ' · 产地筛选下预算按实际 TE 占比分摊（估算）' : '');
  $('#foot-months').textContent = observed.length
    ? `已观测 ${observed[0]}–${observed[observed.length - 1]} 月，达成率分母只含已观测月份`
    : '所选月份尚无业务发生，不计入分母';
  const ext = payload.data.demo_ext || {};
  $('#foot-ext').textContent = ext.available
    ? `ERP 出货事实 + 演示补充（产地 ${ext.origin_skus} 个 SKU · ClubZ ${ext.clubz_customers} 家`
      + ` · 客户集团 ${ext.customer_groups} 个）`
    : '仅 ERP 出货事实（未挂载演示补充库）';
  $('#foot-ext').title = ext.note || '';
  renderTable();
  renderAttain();
}

/* ------------------------------------------------------------ 树形表 */
/* 排序只在「同一层级的兄弟节点之间」发生，绝不跨层重排 ——
   否则战区行与市场部行会被混在一起，树形结构断掉。 */
function sortSiblings(items) {
  const dir = S.order === 'asc' ? 1 : -1;
  const key = S.sort;
  const val = (r) => {
    if (key === 'attainment') return r.attainment_pct ?? -Infinity;
    if (key === 'yoy') return r.yoy_pct ?? -Infinity;
    if (key === 'budget') return r.budget_te ?? -Infinity;
    if (key === 'last_te') return r.last_te ?? -Infinity;
    return r.te ?? -Infinity;
  };
  const list = [...items];
  if (key === 'name') {
    list.sort((a, b) => a.name.localeCompare(b.name, 'zh') * dir);
  } else {
    list.sort((a, b) => (val(a) - val(b)) * dir);
  }
  return list;
}

function visibleRows() {
  const out = [];
  for (const r of sortSiblings(S.data.regions)) {
    out.push(r);
    if (S.collapsed.has(r.key)) continue;
    for (const d of sortSiblings(r.children)) {
      out.push(d);
      if (S.collapsed.has(d.key)) continue;
      for (const g of d.children) out.push(g);   // 客户类型固定按 ClubZ/NKA/LKA/批发/Others
    }
  }
  return out;
}

function renderTable() {
  const sorted = visibleRows();
  const total = sorted.length;
  const pages = Math.max(1, Math.ceil(total / S.size));
  if (S.page > pages) S.page = pages;
  const start = (S.page - 1) * S.size;
  const pageRows = sorted.slice(start, start + S.size);

  const body = $('#grid-body');
  if (!pageRows.length) {
    body.innerHTML = '<tr><td colspan="8" class="bi-empty">当前筛选下没有数据</td></tr>';
  } else {
    body.innerHTML = pageRows.map((r) => {
      const hasChild = r.depth < 2;
      const collapsed = S.collapsed.has(r.key);
      const toggle = hasChild
        ? `<button class="bi-toggle" data-key="${r.key}" type="button">${collapsed ? '+' : '−'}</button>`
        : '<span class="bi-toggle leaf"></span>';
      const zone = r.depth === 0 ? `<td class="l zone">${r.name}</td>` : '<td class="l zone"></td>';
      const label = r.depth === 0 ? '<span style="color:#6b7f96;font-weight:600">小计</span>' : r.name;
      const name = `<td class="l name">${toggle}<span class="bi-indent" style="width:${r.depth * 14}px"></span>${label}</td>`;
      return `<tr class="lv${r.depth}${S.selected === r.key ? ' selected' : ''}" data-key="${r.key}"
        data-depth="${r.depth}" data-name="${r.name}">
        ${zone}${name}
        <td>${r.last_te === null ? '<span class="bi-null">—</span>' : nf(r.last_te)}</td>
        <td>${r.budget_te === null ? '<span class="bi-null">—</span>' : nf(r.budget_te)}</td>
        <td>${r.te === null ? '<span class="bi-null">—</span>' : nf(r.te)}</td>
        <td class="bi-att ${attClass(r.attainment_pct)}">${pct(r.attainment_pct)}</td>
        <td>${yoyHtml(r.yoy_pct)}</td>
        <td>${r.gap_te === null ? '<span class="bi-null">—</span>'
          : `<span class="${r.gap_te >= 0 ? 'bi-yoy pos' : 'bi-yoy neg'}">${r.gap_te >= 0 ? '+' : ''}${nf(r.gap_te)}</span>`}</td>
      </tr>`;
    }).join('');
  }

  const n = S.data.national;
  $('#grid-foot').innerHTML = `<tr>
    <td class="l">合计</td>
    <td class="l">全国合计（五大战区）</td>
    <td>${n.last_te === null ? '—' : nf(n.last_te)}</td>
    <td>${n.budget_te === null ? '—' : nf(n.budget_te)}</td>
    <td>${n.te === null ? '—' : nf(n.te)}</td>
    <td class="bi-att ${attClass(n.attainment_pct)}">${pct(n.attainment_pct)}</td>
    <td>${yoyHtml(n.yoy_pct)}</td>
    <td>${n.gap_te === null ? '—' : `${n.gap_te >= 0 ? '+' : ''}${nf(n.gap_te)}`}</td>
  </tr>`;

  $('#pager-info').textContent = `共 ${total} 行 · 第 ${S.page}/${pages} 页 · 每页 ${S.size} 行`;
  $('#pager-btns').innerHTML = `
    <button data-p="prev" ${S.page <= 1 ? 'disabled' : ''}>上一页</button>
    ${Array.from({ length: pages }, (_, i) => i + 1).slice(0, 8)
      .map((p) => `<button data-p="${p}" class="${p === S.page ? 'on' : ''}">${p}</button>`).join('')}
    <button data-p="next" ${S.page >= pages ? 'disabled' : ''}>下一页</button>`;

  body.querySelectorAll('.bi-toggle[data-key]').forEach((b) => {
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      const k = b.dataset.key;
      if (S.collapsed.has(k)) S.collapsed.delete(k);
      else S.collapsed.add(k);
      renderTable();
    });
  });
  body.querySelectorAll('tr[data-key]').forEach((tr) => {
    tr.addEventListener('click', () => selectRow(tr.dataset.key, true));
  });
  $$('#grid thead th[data-sort]').forEach((th) => {
    th.classList.toggle('sorted', th.dataset.sort === S.sort);
    const arrow = th.querySelector('.bi-arrow');
    if (arrow) arrow.textContent = th.dataset.sort === S.sort
      ? (S.order === 'asc' ? '↑' : '↓') : '↕';
  });
}

function selectRow(key, drill) {
  S.selected = key;
  const row = S.data.rows.find((r) => r.key === key);
  renderTable();
  const tr = document.querySelector(`tr[data-key="${CSS.escape(key)}"]`);
  if (tr) tr.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  // 空分类（既无实际也无预算）没有可下钻的对象，避免弹出一个空明细
  const empty = row && row.actual_missing && row.budget_missing;
  if (drill && row && row.depth === 2 && !empty) openDrill(row);
  if (drill && empty) toast(`${row.name} 在当前筛选下没有出货记录`);
}

/* ------------------------------------------------------------ 右侧达成率 */
const MAX_SCALE = 150;   // 进度条满格代表 150%，>150% 截断但保留真实百分比

function bar(v) {
  const cls = attClass(v);
  if (v === null || v === undefined) {
    return `<div class="bi-track"><i class="none" style="width:100%"></i><u style="left:66.6%"></u></div>`;
  }
  const w = Math.min(100, Math.max(0, (v / MAX_SCALE) * 100));
  return `<div class="bi-track"><i class="${cls}" style="width:${w.toFixed(1)}%"></i>
    <u style="left:${(100 / MAX_SCALE * 100).toFixed(1)}%"></u></div>`;
}

function renderAttain() {
  const host = $('#attwrap');
  const regions = S.data.regions;
  if (!regions.length) {
    host.innerHTML = '<p class="bi-empty">当前筛选下没有数据</p>';
    return;
  }
  const segOrder = S.data.segments.map((s) => s.label);

  host.innerHTML = regions.map((r) => {
    const depts = r.children;
    const parts = depts.map((d) => {
      if (S.attLevel === 'dept') {
        return `<div class="bi-attrow ${S.selected === d.key ? 'sel' : ''}" data-key="${d.key}">
            <span class="lb">${d.name}</span>${bar(d.attainment_pct)}
            <span class="vl ${attClass(d.attainment_pct)}">${pct(d.attainment_pct, 1)}</span>
          </div>`;
      }
      const segs = [...d.children]
        .filter((x) => !(x.actual_missing && x.budget_missing))
        .sort((a, b) => segOrder.indexOf(a.name) - segOrder.indexOf(b.name));
      return `<div class="bi-attdept">
          <h4>${d.name} <span style="color:#95a6b8;font-weight:400">${pct(d.attainment_pct, 1)}</span></h4>
          ${segs.map((s) => `<div class="bi-attrow ${S.selected === s.key ? 'sel' : ''}" data-key="${s.key}">
            <span class="lb">${s.name}</span>${bar(s.attainment_pct)}
            <span class="vl ${attClass(s.attainment_pct)}">${pct(s.attainment_pct, 1)}</span>
          </div>`).join('')}
        </div>`;
    }).join('');

    return `<div class="bi-attgroup">
        <h3 data-key="${r.key}" style="cursor:pointer">${r.name}
          <span>${pct(r.attainment_pct, 1)} · ${nf(r.te)} / ${nf(r.budget_te)} TE</span></h3>
        ${parts}
      </div>`;
  }).join('');

  host.querySelectorAll('[data-key]').forEach((el) => {
    el.addEventListener('click', () => {
      const key = el.dataset.key;
      const row = S.data.rows.find((x) => x.key === key);
      if (!row) return;
      // 展开父级，保证目标行可见
      if (row.depth === 1) S.collapsed.delete(row.parent);
      if (row.depth === 2) {
        S.collapsed.delete(row.parent);
        S.collapsed.delete(row.parent.split('|s:')[0]);
      }
      S.page = 1;
      selectRow(key, false);
      renderAttain();
    });
  });
}

/* ------------------------------------------------------------ 下钻 */
/* 三层：KA 系统 / 客户集团 → 客户 → ERP 出货行。
   集团只有一个同名客户时（多数 NKA 是这样）不重复显示两层，直接一行客户。 */
let drillNode = '';

async function openDrill(row) {
  const d = $('#drill');
  d.classList.remove('hidden');
  drillNode = row.key;
  $('#drill-title').textContent = `${row.name} · 客户明细`;
  $('#drill-scope').textContent = '加载中…';
  $('#drill-body').innerHTML = '<p class="bi-empty">加载中…</p>';

  const url = `/api/analysis/grid/detail?${qs()}&node=${encodeURIComponent(row.key)}`;
  const payload = await (await fetch(url)).json();
  if (!payload.ok) {
    $('#drill-body').innerHTML = `<p class="bi-empty">${payload.error || '加载失败'}</p>`;
    return;
  }
  const data = payload.data;
  const scope = data.scope;
  $('#drill-scope').textContent = [
    scope.region || '全国', scope.dept || '全部市场部', scope.segment || '全部客户类型',
  ].join(' · ') + ` ｜ 合计 ${nf(data.total_te)} TE ｜ ${data.groups.length} 个客户集团`
    + ` · ${data.customers.length} 个客户主体`;

  if (!data.customers.length) {
    $('#drill-body').innerHTML = '<p class="bi-empty">该组合下没有出货记录</p>';
    return;
  }

  const rows = [];
  for (const g of data.groups) {
    const single = g.customer_count === 1 && g.customers[0].customer === g.group;
    if (!single) rows.push({ kind: 'group', g });
    for (const c of g.customers) rows.push({ kind: 'customer', g, c, single });
  }

  $('#drill-body').innerHTML = `
    <table class="bi-drill-table">
      <thead><tr>
        <th class="l">KA 系统 / 客户</th><th>类型</th><th>ERP 渠道</th>
        <th>${data.is_wholesale ? '档口数' : '客户数'}</th>
        <th>TE</th><th>占比</th><th class="no-sort">ERP 明细</th>
      </tr></thead>
      <tbody>
        ${rows.map((r) => {
          if (r.kind === 'group') {
            return `<tr class="bi-dg">
              <td class="l"><b>${r.g.group}</b> <span class="bi-tag">${r.g.group_type}</span></td>
              <td colspan="2" class="bi-muted">${r.g.group_note || ''}</td>
              <td>${r.g.customer_count}</td>
              <td>${nf(r.g.te)}</td>
              <td>${pct(r.g.share_pct, 1)}</td>
              <td></td>
            </tr>`;
          }
          const c = r.c;
          const stalls = (c.stalls || []).length
            ? `<ul class="bi-stall">${c.stalls.map((s) => `
                <li><span>${s.stall}</span><span>${s.region}·${s.dept}</span>
                    <span>${nf(s.te)}</span></li>`).join('')}
                ${c.stall_count > c.stalls.length
                  ? `<li><span>…另有 ${c.stall_count - c.stalls.length} 个档口</span></li>` : ''}
              </ul>`
            : '';
          return `<tr class="bi-dc${r.single ? ' flat' : ''}">
            <td class="l">${r.single
              ? `<b>${c.customer}</b> <span class="bi-tag">${r.g.group_type}</span>`
              : `<span class="bi-sub-indent"></span>${c.customer}`}
              ${c.stall_count && !r.single
                ? `<span class="bi-muted"> · ${c.stall_count} 个档口</span>` : ''}
              ${stalls}</td>
            <td>${c.customer_type}</td>
            <td>${c.channel_type}</td>
            <td>${c.stall_count || '—'}</td>
            <td>${nf(c.te)}</td>
            <td>${pct(c.share_pct, 1)}</td>
            <td><button class="bi-mini" data-lines="${c.customer_id}"
              data-name="${c.customer}" type="button">查看</button></td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
    <p class="bi-drill-note">${data.note}</p>
    <div class="bi-lines" id="drill-lines"></div>`;

  $('#drill-body').querySelectorAll('button[data-lines]').forEach((b) => {
    b.addEventListener('click', () => openLines(b.dataset.lines, b.dataset.name));
  });
}

/* 第三层：某个客户的 ERP 出货行（月 × SKU），证明每个数字都能点回单据 */
async function openLines(customerId, name) {
  const host = $('#drill-lines');
  host.innerHTML = `<p class="bi-empty">正在读取 ${name} 的 ERP 出货行…</p>`;
  const url = `/api/analysis/grid/lines?${qs()}`
    + `&node=${encodeURIComponent(drillNode)}&customer=${encodeURIComponent(customerId)}`;
  const payload = await (await fetch(url)).json();
  if (!payload.ok) {
    host.innerHTML = `<p class="bi-empty">${payload.error || '加载失败'}</p>`;
    return;
  }
  const d = payload.data;
  const peak = Math.max(1, ...d.months.map((m) => m.te));
  host.innerHTML = `
    <div class="bi-lines-head">
      <b>${d.customer}</b> · ERP 出货明细
      <span>${d.line_count} 个「月×SKU」组合 · 来自 ${nf(d.source_rows)} 条出货行 · 合计 ${nf(d.total_te)} TE</span>
    </div>
    <div class="bi-lines-chart">
      ${d.months.map((m) => `<div class="bi-lbar" title="${m.month} 月 ${nf(m.te)} TE">
        <i style="height:${Math.max(3, (m.te / peak) * 100).toFixed(0)}%"></i>
        <span>${m.month}</span></div>`).join('')}
    </div>
    <table class="bi-lines-table">
      <thead><tr>
        <th>月份</th><th class="l">品类</th><th class="l">SKU</th>
        <th>箱数</th><th>TE</th><th>金额(元)</th>
      </tr></thead>
      <tbody>${d.lines.map((l) => `<tr>
        <td>${l.month} 月</td><td class="l">${l.category}</td><td class="l">${l.sku}</td>
        <td>${nf(l.boxes)}</td><td>${nf(l.te, 1)}</td><td>${nf(l.amount)}</td>
      </tr>`).join('')}</tbody>
    </table>
    <p class="bi-drill-note">${d.note}</p>`;
  host.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/* ------------------------------------------------------------ 事件 */
function bind() {
  $('#btn-query').addEventListener('click', () => { readFilters(); load(); });
  $('#f-year').addEventListener('change', () => { readFilters(); load(); });
  $('#f-month').addEventListener('change', () => { readFilters(); load(); });
  $('#f-region').addEventListener('change', () => { readFilters(); load(); });
  $('#f-dept').addEventListener('change', () => { readFilters(); load(); });
  $('#f-category').addEventListener('change', () => { readFilters(); load(); });
  $('#f-origin').addEventListener('change', () => { readFilters(); load(); });
  $('#f-segment').addEventListener('change', () => { readFilters(); load(); });
  $('#f-q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { readFilters(); load(); }
  });

  $('#btn-reset').addEventListener('click', () => {
    Object.assign(S, { year: 2026, month: 0, region: '', dept: '', category: '',
      origin: '', segment: '', q: '', page: 1 });
    $('#f-year').value = 2026; $('#f-month').value = '0'; $('#f-region').value = '';
    $('#f-dept').value = ''; $('#f-category').value = ''; $('#f-origin').value = '';
    $('#f-segment').value = ''; $('#f-q').value = '';
    S.collapsed.clear(); S.selected = '';
    load();
    toast('已重置筛选');
  });

  $('#btn-save').addEventListener('click', () => {
    const name = window.prompt('给这套参数起个名字（保存在本机浏览器）', '我的常用视图');
    if (!name) return;
    localStorage.setItem('bi.analysis.params', JSON.stringify({
      name,
      filters: { year: S.year, month: S.month, region: S.region, dept: S.dept,
                 category: S.category, origin: S.origin, segment: S.segment },
    }));
    S.savedName = name;
    renderSavedChip();
    toast('参数已保存到本机');
  });

  $('#btn-export').addEventListener('click', async () => {
    const res = await fetch(`/api/analysis/grid/export?${qs()}`);
    const payload = await res.json();
    if (!payload.ok) { toast('导出失败'); return; }
    const blob = new Blob(['\ufeff' + payload.data.csv], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = payload.data.filename;
    a.click();
    URL.revokeObjectURL(a.href);
    toast('已导出 CSV');
  });

  $('#btn-expand').addEventListener('click', () => { S.collapsed.clear(); renderTable(); });
  $('#btn-collapse').addEventListener('click', () => {
    S.data.regions.forEach((r) => S.collapsed.add(r.key));
    renderTable();
  });

  $$('#grid thead th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => {
      const k = th.dataset.sort;
      if (S.sort === k) S.order = S.order === 'asc' ? 'desc' : 'asc';
      else { S.sort = k; S.order = 'desc'; }
      renderTable();
    });
  });

  $('#pager-btns').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    const p = b.dataset.p;
    if (p === 'prev') S.page = Math.max(1, S.page - 1);
    else if (p === 'next') S.page += 1;
    else S.page = Number(p);
    renderTable();
  });

  $('#att-level').addEventListener('change', () => {
    S.attLevel = $('#att-level').value;
    renderAttain();
  });

  $('#drill-close').addEventListener('click', () => $('#drill').classList.add('hidden'));
  $('#drill').addEventListener('click', (e) => {
    if (e.target.id === 'drill') $('#drill').classList.add('hidden');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $('#drill').classList.add('hidden');
  });
}

(async function start() {
  bind();
  await bootOptions();
  readUrl();               // URL 里的筛选优先于本机保存的参数
  syncFiltersFromState();
  await load();
  if (S.yearNote) toast(S.yearNote);
})();
