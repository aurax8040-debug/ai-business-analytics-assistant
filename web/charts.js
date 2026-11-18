/* =====================================================================
 * charts.js — 零依赖 SVG 图表引擎（全国经营驾驶舱 · UI V1.1 / Y26 单年）
 * 所有输入均来自 /api/* 的真实查询结果；本文件不含任何静态业务数据。
 * 空值一律断开线段 / 不画柱子，绝不按 0 填充。
 * ===================================================================== */
const NS = 'http://www.w3.org/2000/svg';
const C = {
  ink: '#1a2a44', muted: '#8999ae', grid: '#e9eff7', axis: '#c9d7e9',
  blue: '#2f6fe4', blue2: '#5b9bf8', blue3: '#9dc2fb', teal: '#0e9f8a',
  amber: '#e8a33d', purple: '#7c63d6', red: '#df4640', green: '#159a68',
  barTrack: '#eef3fb',
};
const PALETTE = ['#2f6fe4', '#5b9bf8', '#9dc2fb', '#7c63d6', '#0e9f8a',
                 '#4aa3e8', '#8fbf5a', '#e07a9b', '#5f6b7d', '#c08a4a'];

/* ---------------------------------------------------------- 工具 */
export function fmtNum(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
export function fmtCompact(v, unit = '') {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  const n = Number(v), a = Math.abs(n);
  const pre = unit === '¥' ? '¥' : '';
  if (a >= 1e8) return `${pre}${fmtNum(n / 1e8, 2)}亿${unit === '¥' ? '' : unit}`;
  if (a >= 1e4) return `${pre}${fmtNum(n / 1e4, 1)}万${unit === '¥' ? '' : unit}`;
  return `${pre}${fmtNum(n, a < 100 && a % 1 !== 0 ? 2 : 0)}${unit === '¥' ? '' : unit}`;
}
export function fmtMoney(v) { return fmtCompact(v, '¥'); }
export function fmtPct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return `${Number(v) >= 0 ? '+' : ''}${fmtNum(v, digits)}%`;
}
export function growthClass(v) {
  if (v === null || v === undefined) return 'flat';
  return Number(v) >= 0 ? 'up' : 'down';
}
function node(tag, attrs = {}, text = null) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) e.setAttribute(k, v);
  if (text !== null) e.textContent = text;
  return e;
}
function svgRoot(host, h) {
  host.innerHTML = '';
  const w = Math.max(240, host.clientWidth || 420);
  const s = node('svg', { viewBox: `0 0 ${w} ${h}`, width: '100%', height: h,
                          preserveAspectRatio: 'xMidYMid meet', class: 'chart-svg' });
  host.appendChild(s);
  return { s, w, h };
}
let tipEl = null;
function tooltip() {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.className = 'chart-tooltip hidden';
    document.body.appendChild(tipEl);
  }
  return tipEl;
}
export function hideTooltip() { if (tipEl) tipEl.classList.add('hidden'); }
function showTip(evt, html) {
  const t = tooltip();
  t.innerHTML = html;
  t.classList.remove('hidden');
  const x = Math.min(evt.clientX + 14, window.innerWidth - t.offsetWidth - 12);
  const y = Math.max(8, evt.clientY - t.offsetHeight - 12);
  t.style.left = `${x}px`; t.style.top = `${y}px`;
}
export function loading(host, text = '正在查询数据…') {
  host.innerHTML = `<div class="state-box"><div class="spinner"></div><div>${text}</div></div>`;
}
export function empty(host, text = '当前筛选条件下无数据') {
  host.innerHTML = `<div class="state-box state-empty"><div class="state-icon">◌</div><div>${text}</div>
    <div class="state-sub">无记录不等于 0，已按冻结口径保留为空</div></div>`;
}
export function failed(host, text) {
  host.innerHTML = `<div class="state-box state-error"><div class="state-icon">!</div><div>数据加载失败</div>
    <div class="state-sub"></div></div>`;
  host.querySelector('.state-sub').textContent = text || '请检查后端服务';
}

/* ---------------------------------------------------------- 折线趋势图（Y26 单序列 + 面积） */
let areaSeq = 0;
export function lineChart(host, opt) {
  const labels = opt.labels || [];
  const series = (opt.series || []).filter(x => x && x.values);
  if (!labels.length || !series.length) return empty(host, opt.emptyText || '当前筛选条件下无趋势数据');
  const H = opt.height || 142, P = { l: 54, r: 10, t: 20, b: 24 };
  const { s, w } = svgRoot(host, H);
  const iw = w - P.l - P.r, ih = H - P.t - P.b;
  const vals = series.flatMap(x => x.values)
    .filter(v => v !== null && v !== undefined && Number.isFinite(Number(v))).map(Number);
  if (!vals.length) return empty(host, '当前筛选条件下无可绘制数值');
  const vmax = Math.max(...vals, 0), vmin = Math.min(...vals, 0);
  const max = vmax === 0 ? 1 : vmax * 1.16;
  const min = vmin < 0 ? vmin * 1.16 : 0;
  const X = i => P.l + (labels.length === 1 ? iw / 2 : (i * iw) / (labels.length - 1));
  const Y = v => P.t + ih - ((Number(v) - min) / (max - min)) * ih;

  const TICKS = Math.max(2, Math.min(4, opt.ticks || 4));
  for (let t = 0; t <= TICKS - 1; t++) {
    const v = min + ((max - min) * t) / (TICKS - 1), y = Y(v);
    s.appendChild(node('line', { x1: P.l, y1: y, x2: w - P.r, y2: y,
                                 stroke: C.grid, 'stroke-width': 1 }));
    s.appendChild(node('text', { x: P.l - 7, y: y + 3, 'text-anchor': 'end',
                                 class: 'axis-text' }, opt.axisFormat ? opt.axisFormat(v) : fmtCompact(v)));
  }
  labels.forEach((lb, i) => s.appendChild(node('text', {
    x: X(i), y: H - 6, 'text-anchor': 'middle', class: 'axis-text' }, String(lb))));

  const segmentsOf = (values) => {
    const segs = []; let cur = [];
    values.forEach((v, i) => {
      if (v === null || v === undefined) { if (cur.length) segs.push(cur); cur = []; return; }
      cur.push([X(i), Y(v)]);
    });
    if (cur.length) segs.push(cur);
    return segs;
  };

  series.forEach((se, si) => {
    const color = se.color || PALETTE[si % PALETTE.length];
    const segs = segmentsOf(se.values);
    if (opt.area && si === 0) {
      const gid = `biArea${++areaSeq}`;
      const defs = node('defs');
      const lg = node('linearGradient', { id: gid, x1: '0', y1: '0', x2: '0', y2: '1' });
      lg.appendChild(node('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': '0.24' }));
      lg.appendChild(node('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': '0.01' }));
      defs.appendChild(lg); s.appendChild(defs);
      segs.filter(g => g.length > 1).forEach(g => {
        const base = P.t + ih;
        s.appendChild(node('polygon', {
          points: `${g[0][0]},${base} ${g.map(p => `${p[0]},${p[1]}`).join(' ')} ${g[g.length - 1][0]},${base}`,
          fill: `url(#${gid})`, stroke: 'none' }));
      });
    }
    segs.filter(g => g.length > 1).forEach(g => s.appendChild(node('polyline', {
      points: g.map(p => `${p[0]},${p[1]}`).join(' '), fill: 'none', stroke: color,
      'stroke-width': 2.3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      'stroke-dasharray': se.dashed ? '5 4' : null })));
    se.values.forEach((v, i) => {
      if (v === null || v === undefined) return;
      s.appendChild(node('circle', { cx: X(i), cy: Y(v), r: 2.8, fill: '#fff',
                                     stroke: color, 'stroke-width': 1.7 }));
    });
  });

  const hit = node('rect', { x: P.l, y: P.t, width: iw, height: ih, fill: 'transparent',
                             class: 'chart-hit' });
  s.appendChild(hit);
  const guide = node('line', { y1: P.t, y2: P.t + ih, stroke: C.axis,
                               'stroke-dasharray': '3 3', 'stroke-width': 1, opacity: 0 });
  s.appendChild(guide);
  hit.addEventListener('mousemove', ev => {
    const rect = s.getBoundingClientRect();
    const vx = ((ev.clientX - rect.left) / rect.width) * w;
    const i = Math.max(0, Math.min(labels.length - 1,
      Math.round(((vx - P.l) / iw) * (labels.length - 1))));
    guide.setAttribute('x1', X(i)); guide.setAttribute('x2', X(i));
    guide.setAttribute('opacity', 1);
    const rows = series.map(se => {
      const v = se.values[i];
      const txt = v === null || v === undefined ? '无数据'
        : (opt.tipFormat ? opt.tipFormat(v) : fmtCompact(v));
      return `<div class="tt-row"><i style="background:${se.color || PALETTE[0]}"></i>
        <span>${se.name}</span><b>${txt}</b></div>`;
    }).join('');
    const mom = opt.momValues ? opt.momValues[i] : null;
    const momRow = mom === null || mom === undefined ? '' :
      `<div class="tt-row"><span>环比上月</span><b>${fmtPct(mom)}</b></div>`;
    showTip(ev, `<div class="tt-title">${opt.tipPrefix || ''}${labels[i]}</div>${rows}${momRow}`);
  });
  hit.addEventListener('mouseleave', () => { guide.setAttribute('opacity', 0); hideTooltip(); });
}

/* ---------------------------------------------------------- 纵向柱状图（五大战区） */
export function columnChart(host, opt) {
  const items = opt.items || [];
  if (!items.length) return empty(host, opt.emptyText || '当前筛选条件下无战区数据');
  const H = opt.height || 144, P = { l: 62, r: 8, t: 20, b: 24 };
  const { s, w } = svgRoot(host, H);
  const iw = w - P.l - P.r, ih = H - P.t - P.b;
  const vals = items.map(i => (i.value === null || i.value === undefined ? null : Number(i.value)));
  const real = vals.filter(v => v !== null && Number.isFinite(v));
  if (!real.length) return empty(host, '当前筛选条件下无可绘制数值');
  const max = Math.max(...real, 0) * 1.22 || 1;
  for (let t = 0; t <= 3; t++) {
    const y = P.t + ih - (ih * t) / 3;
    s.appendChild(node('line', { x1: P.l, y1: y, x2: w - P.r, y2: y, stroke: C.grid }));
    s.appendChild(node('text', { x: P.l - 6, y: y + 3, 'text-anchor': 'end', class: 'axis-text' },
      opt.axisFormat ? opt.axisFormat((max * t) / 3) : fmtCompact((max * t) / 3)));
  }
  const slot = iw / items.length, bw = Math.min(42, slot * 0.54);
  items.forEach((it, i) => {
    const cx = P.l + slot * i + slot / 2;
    s.appendChild(node('text', { x: cx, y: H - 6, 'text-anchor': 'middle', 'class': 'axis-text' },
      String(it.label)));
    const v = vals[i];
    if (v === null) {
      s.appendChild(node('text', { x: cx, y: P.t + ih - 4, 'text-anchor': 'middle',
        class: 'axis-text' }, '无数据'));
      return;
    }
    const bh = Math.max(2, (v / max) * ih);
    const rect = node('rect', { x: cx - bw / 2, y: P.t + ih - bh, width: bw, height: bh,
      rx: 3, fill: it.color || PALETTE[i % PALETTE.length], class: opt.clickable ? 'bar-click' : 'bar-click',
      opacity: 0.94 });
    s.appendChild(rect);
    s.appendChild(node('text', { x: cx, y: P.t + ih - bh - 5, 'text-anchor': 'middle',
      class: 'column-value' }, opt.valueFormat ? opt.valueFormat(v) : fmtCompact(v)));
    rect.addEventListener('mousemove', ev => showTip(ev,
      `<div class="tt-title">${it.label}</div><div class="tt-row"><span>${opt.tipLabel || '销售额'}</span><b>${opt.tipFormat ? opt.tipFormat(v) : fmtCompact(v)}</b></div>${it.tipExtra || ''}`));
    rect.addEventListener('mouseleave', hideTooltip);
    if (opt.onClick) rect.addEventListener('click', () => opt.onClick(it, i));
  });
}

/* ---------------------------------------------------------- 横向条形排名 */
export function hbarChart(host, opt) {
  const items = opt.items || [];
  if (!items.length) return empty(host, opt.emptyText || '当前筛选条件下无排名数据');
  const rowH = opt.rowH || 24, H = Math.max(112, items.length * rowH + 16);
  const { s, w } = svgRoot(host, H);
  const labelW = opt.labelW || 76, valueW = opt.valueW || 66;
  const hasBadge = items.some(i => i.badge !== undefined && i.badge !== null);
  const badgeW = hasBadge ? (opt.badgeW || 42) : 0;
  const x0 = labelW + 5;
  const barW = Math.max(24, w - x0 - valueW - badgeW - 10);
  const max = Math.max(...items.map(i => Math.abs(Number(i.value) || 0)), 1);
  items.forEach((it, i) => {
    const y = 6 + i * rowH;
    const color = it.color || PALETTE[i % PALETTE.length];
    s.appendChild(node('text', { x: labelW, y: y + 13, 'text-anchor': 'end',
                                 class: 'bar-label' }, String(it.label).slice(0, opt.maxLabel || 12)));
    s.appendChild(node('rect', { x: x0, y: y + 3, width: barW, height: 12, rx: 6,
                                 fill: C.barTrack }));
    const bw = Math.max(2, (Math.abs(Number(it.value) || 0) / max) * barW);
    s.appendChild(node('rect', { x: x0, y: y + 3, width: bw, height: 12, rx: 6,
                                 fill: color, class: opt.clickable ? 'bar-click' : null,
                                 opacity: 0.94 }));
    s.appendChild(node('text', { x: x0 + barW + 6, y: y + 13, class: 'bar-value' },
      opt.valueFormat ? opt.valueFormat(it.value) : fmtCompact(it.value)));
    if (it.badge !== undefined && it.badge !== null) {
      s.appendChild(node('text', { x: w - 4, y: y + 13, 'text-anchor': 'end',
        class: `bar-badge ${opt.badgeFormat ? 'neutral' : growthClass(it.badge)}` },
        opt.badgeFormat ? opt.badgeFormat(it.badge) : fmtPct(it.badge)));
    }
    if (opt.onClick) {
      const g = node('rect', { x: 0, y: y, width: w, height: rowH, fill: 'transparent',
                               class: 'bar-click' });
      s.appendChild(g);
      g.addEventListener('click', () => opt.onClick(it, i));
      g.addEventListener('mousemove', ev => showTip(ev,
        `<div class="tt-title">${it.label}</div><div class="tt-row"><span>${opt.tipLabel || '数值'}</span><b>${opt.tipFormat ? opt.tipFormat(it.value) : fmtCompact(it.value)}</b></div>${it.tipExtra || ''}`));
      g.addEventListener('mouseleave', hideTooltip);
    }
  });
}

/* ---------------------------------------------------------- 分组柱状图（保留） */
export function groupedBar(host, opt) {
  const labels = opt.labels || [], series = opt.series || [];
  if (!labels.length) return empty(host, '当前筛选条件下无品类数据');
  const H = opt.height || 180, P = { l: 52, r: 10, t: 14, b: 26 };
  const { s, w } = svgRoot(host, H);
  const iw = w - P.l - P.r, ih = H - P.t - P.b;
  const vals = series.flatMap(x => x.values).filter(v => v !== null && v !== undefined).map(Number);
  const max = Math.max(...vals, 1) * 1.14;
  for (let t = 0; t <= 3; t++) {
    const y = P.t + ih - (ih * t) / 3;
    s.appendChild(node('line', { x1: P.l, y1: y, x2: w - P.r, y2: y, stroke: C.grid }));
    s.appendChild(node('text', { x: P.l - 6, y: y + 3, 'text-anchor': 'end',
                                 class: 'axis-text' }, fmtCompact((max * t) / 3)));
  }
  const slot = iw / labels.length, bw = Math.min(26, (slot - 14) / series.length);
  labels.forEach((lb, i) => {
    const cx = P.l + slot * i + slot / 2;
    s.appendChild(node('text', { x: cx, y: H - 6, 'text-anchor': 'middle',
                                 class: 'axis-text' }, lb));
    series.forEach((se, si) => {
      const v = se.values[i];
      if (v === null || v === undefined) return;
      const bh = Math.max(1, (Number(v) / max) * ih);
      const x = cx - (series.length * bw) / 2 + si * bw + 1;
      const rect = node('rect', { x, y: P.t + ih - bh, width: bw - 3, height: bh,
        rx: 3, fill: se.color || PALETTE[si], class: 'bar-click' });
      s.appendChild(rect);
      rect.addEventListener('mousemove', ev => showTip(ev,
        `<div class="tt-title">${lb} · ${se.name}</div><div class="tt-row"><span>销售额</span><b>${fmtMoney(v)}</b></div>`));
      rect.addEventListener('mouseleave', hideTooltip);
    });
  });
}

/* ---------------------------------------------------------- 环形占比图 */
export function donutChart(host, opt) {
  const items = (opt.items || []).filter(i => (Number(i.value) || 0) > 0);
  if (!items.length) return empty(host, '当前筛选条件下无占比数据');
  const size = opt.size || 132, r = size / 2 - 6, ir = r * 0.63, cx = size / 2, cy = size / 2;
  host.innerHTML = '';
  const wrap = document.createElement('div'); wrap.className = 'donut-wrap'; host.appendChild(wrap);
  const s = node('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size });
  wrap.appendChild(s);
  const total = items.reduce((a, b) => a + Number(b.value || 0), 0);
  let ang = -Math.PI / 2;
  items.forEach((it, i) => {
    const frac = Number(it.value) / total, a2 = ang + frac * Math.PI * 2;
    const color = it.color || PALETTE[i % PALETTE.length];
    const p = [['M', cx + r * Math.cos(ang), cy + r * Math.sin(ang)],
               ['A', r, r, 0, frac > 0.5 ? 1 : 0, 1, cx + r * Math.cos(a2), cy + r * Math.sin(a2)],
               ['L', cx + ir * Math.cos(a2), cy + ir * Math.sin(a2)],
               ['A', ir, ir, 0, frac > 0.5 ? 1 : 0, 0, cx + ir * Math.cos(ang), cy + ir * Math.sin(ang)],
               ['Z']].map(x => x.join(' ')).join(' ');
    const path = node('path', { d: p, fill: color, class: 'bar-click', opacity: 0.95 });
    s.appendChild(path);
    path.addEventListener('mousemove', ev => showTip(ev,
      `<div class="tt-title">${it.label}</div><div class="tt-row"><span>销售额</span><b>${fmtMoney(it.value)}</b></div><div class="tt-row"><span>占比</span><b>${fmtNum(frac * 100, 1)}%</b></div>`));
    path.addEventListener('mouseleave', hideTooltip);
    if (opt.onSelect) path.addEventListener('click', () => opt.onSelect(it.label));
    ang = a2;
  });
  s.appendChild(node('text', { x: cx, y: cy - 1, 'text-anchor': 'middle', class: 'donut-total' },
    opt.centerValue || fmtCompact(total, '¥')));
  s.appendChild(node('text', { x: cx, y: cy + 14, 'text-anchor': 'middle', class: 'donut-caption' },
    opt.centerLabel || '合计'));
  const legend = document.createElement('div'); legend.className = 'donut-legend';
  items.forEach((it, i) => {
    const row = document.createElement('div'); row.className = 'legend-row';
    row.innerHTML = `<i style="background:${it.color || PALETTE[i % PALETTE.length]}"></i>
      <span></span><b></b>`;
    row.querySelector('span').textContent = it.label;
    row.querySelector('b').textContent = opt.legendMode === 'amount'
      ? `${fmtMoney(it.value)} · ${fmtNum((Number(it.value) / total) * 100, 1)}%`
      : `${fmtNum((Number(it.value) / total) * 100, 1)}%`;
    if (opt.onSelect) {
      row.classList.add('bar-click');
      row.addEventListener('click', () => opt.onSelect(it.label));
    }
    legend.appendChild(row);
  });
  wrap.appendChild(legend);
}

export { C as COLORS, PALETTE };
