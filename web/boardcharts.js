/* =====================================================================
 * boardcharts.js — 三个新看板专用的补充图形（不修改 charts.js）
 * 为什么单独放一个文件：charts.js 的 donutChart 把 tooltip 文案写死成
 * 「销售额 / ¥」，而拜访、门店、库存这些看板的单位是「家 / 次 / 盒」。
 * 为了不改动已验收页面的图形组件，这里只补一个可指定单位的环形图。
 * 所有数值都来自后端返回的真实聚合值；不画任何静态假数据。
 * ===================================================================== */

const NS = 'http://www.w3.org/2000/svg';
const PALETTE = ['#2f6fe4', '#0e9f8a', '#e8a33d', '#7c63d6', '#df4640',
                 '#4aa3e8', '#8fbf5a', '#e07a9b', '#5f6b7d', '#c08a4a'];

function node(tag, attrs = {}, text = null) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) el.setAttribute(k, v);
  }
  if (text !== null) el.textContent = text;
  return el;
}

function tip() {
  // 复用 charts.js 已有的 .chart-tooltip 节点，避免页面上出现两个浮层
  let el = document.querySelector('.chart-tooltip');
  if (!el) {
    el = document.createElement('div');
    el.className = 'chart-tooltip hidden';
    document.body.appendChild(el);
  }
  return el;
}

function showTip(ev, html) {
  const el = tip();
  if (!el) return;
  el.innerHTML = html;
  el.classList.remove('hidden');
  const pad = 12, rect = el.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = ev.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = ev.clientY - rect.height - pad;
  el.style.left = `${Math.max(4, x)}px`;
  el.style.top = `${Math.max(4, y)}px`;
}

function hideTip() {
  const el = tip();
  if (el) el.classList.add('hidden');
}

export function fmtInt(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return Number(v).toLocaleString('zh-CN');
}

export function fmtNum(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return Number(v).toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

export function fmtPct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return `${fmtNum(v, digits)}%`;
}

export function emptyBox(host, text, sub) {
  host.innerHTML = `<div class="inline-empty"><b>${text}</b>${sub ? `<span>${sub}</span>` : ''}</div>`;
}

export function loadingBox(host, text = '正在查询数据…') {
  host.innerHTML = `<div class="inline-empty"><div class="spinner"></div><span>${text}</span></div>`;
}

/* ---------------------------------------------------------- 计数环形图 */
export function donutCount(host, opt) {
  const raw = opt.items || [];
  if (!raw.length || raw.every(i => !(Number(i.value) > 0))) {
    return emptyBox(host, opt.emptyText || '当前筛选条件下无数据', opt.emptySub);
  }
  const size = opt.size || 132;
  const r = size / 2 - 6, ir = r * 0.63, cx = size / 2, cy = size / 2;
  host.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'donut-wrap';
  host.appendChild(wrap);

  const svg = node('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size });
  wrap.appendChild(svg);
  const total = raw.reduce((a, b) => a + Number(b.value || 0), 0);

  // 只有一个分段时（例如覆盖 100%）画整圆，避免 A 指令退化
  if (raw.length === 1) {
    svg.appendChild(node('circle', { cx, cy, r: r, fill: raw[0].color || PALETTE[0],
                                    opacity: 0.95 }));
    svg.appendChild(node('circle', { cx, cy, r: ir, fill: '#fff' }));
  } else {
    let ang = -Math.PI / 2;
    raw.forEach((it, i) => {
      const frac = Number(it.value || 0) / total;
      if (!frac) return;
      const a2 = ang + frac * Math.PI * 2;
      const d = [
        ['M', cx + r * Math.cos(ang), cy + r * Math.sin(ang)],
        ['A', r, r, 0, frac > 0.5 ? 1 : 0, 1, cx + r * Math.cos(a2), cy + r * Math.sin(a2)],
        ['L', cx + ir * Math.cos(a2), cy + ir * Math.sin(a2)],
        ['A', ir, ir, 0, frac > 0.5 ? 1 : 0, 0, cx + ir * Math.cos(ang), cy + ir * Math.sin(ang)],
        ['Z'],
      ].map(x => x.join(' ')).join(' ');
      const path = node('path', { d, fill: it.color || PALETTE[i % PALETTE.length],
                                  opacity: 0.95, class: 'bar-click' });
      svg.appendChild(path);
      path.addEventListener('mousemove', ev => showTip(ev,
        `<div class="tt-title">${it.label}</div>
         <div class="tt-row"><span>${opt.unit || '数量'}</span><b>${fmtInt(it.value)}</b></div>
         <div class="tt-row"><span>占比</span><b>${fmtNum(frac * 100, 1)}%</b></div>`));
      path.addEventListener('mouseleave', hideTip);
      if (opt.onClick) path.addEventListener('click', () => opt.onClick(it, i));
      ang = a2;
    });
  }

  svg.appendChild(node('text', { x: cx, y: cy - 1, 'text-anchor': 'middle',
                                 class: 'donut-total' },
    opt.centerValue || fmtInt(total)));
  svg.appendChild(node('text', { x: cx, y: cy + 14, 'text-anchor': 'middle',
                                 class: 'donut-caption' }, opt.centerLabel || '合计'));

  const legend = document.createElement('div');
  legend.className = 'donut-legend';
  raw.forEach((it, i) => {
    const row = document.createElement('div');
    row.className = 'legend-row';
    row.innerHTML = '<i></i><span></span><b></b>';
    row.querySelector('i').style.background = it.color || PALETTE[i % PALETTE.length];
    row.querySelector('span').textContent = it.label;
    row.querySelector('b').textContent = opt.legendMode === 'count'
      ? `${fmtInt(it.value)}${opt.unitShort || ''}`
      : `${fmtNum((Number(it.value || 0) / total) * 100, 1)}%`;
    legend.appendChild(row);
  });
  wrap.appendChild(legend);
}

/* ---------------------------------------------------------- 双线趋势（目标 vs 实际） */
export function trendTwo(host, opt) {
  const labels = opt.labels || [];
  const series = opt.series || [];
  if (!labels.length) return emptyBox(host, opt.emptyText || '当前筛选条件下无趋势数据');
  const drawn = series.filter(s => (s.values || []).some(v => v !== null && v !== undefined));
  if (!drawn.length) return emptyBox(host, opt.emptyText || '当前筛选条件下无趋势数据');

  const H = opt.height || 196;
  const P = { l: opt.padLeft || 52, r: 14, t: 16, b: 26 };
  host.innerHTML = '';
  const width = Math.max(320, host.clientWidth || 560);
  const svg = node('svg', { viewBox: `0 0 ${width} ${H}`, width: '100%', height: H,
                            class: 'chart-svg' });
  host.appendChild(svg);
  const iw = width - P.l - P.r, ih = H - P.t - P.b;

  const all = drawn.flatMap(s => s.values)
    .filter(v => v !== null && v !== undefined).map(Number);
  // 库存这类水平波动小的序列，从 0 起画会把曲线压成一条直线；
  // opt.zeroBase === false 时按数据区间取基线，坐标轴标签仍照实标注。
  const rawMax = Math.max(...all, 1);
  const rawMin = opt.zeroBase === false ? Math.min(...all) : 0;
  const span = Math.max(rawMax - rawMin, rawMax * 0.02, 1);
  const max = rawMax + span * 0.12;
  const min = opt.zeroBase === false ? Math.max(0, rawMin - span * 0.12) : 0;
  const axisFormat = opt.axisFormat || fmtInt;
  // 点多于 60 个时不再画圆点：304 个点会连成一条粗带子，遮住曲线本身。
  const dense = labels.length > 60;
  const X = i => P.l + (labels.length === 1 ? iw / 2 : (i * iw) / (labels.length - 1));
  const Y = v => P.t + ih - ((Number(v) - min) / (max - min || 1)) * ih;

  for (let t = 0; t <= 3; t++) {
    const y = P.t + ih - (ih * t) / 3;
    svg.appendChild(node('line', { x1: P.l, y1: y, x2: width - P.r, y2: y,
                                   stroke: '#e9eff7' }));
    svg.appendChild(node('text', { x: P.l - 7, y: y + 3, 'text-anchor': 'end',
                                   class: 'axis-text' },
                         axisFormat(Math.round(min + ((max - min) * t) / 3))));
  }
  // 横轴标签：点太密时只标首、中、尾，避免 304 个日期叠在一起。
  const tickIdx = dense
    ? [0, Math.floor((labels.length - 1) / 2), labels.length - 1]
    : labels.map((_, i) => i);
  tickIdx.forEach((i) => svg.appendChild(node('text', {
    x: X(i), y: H - 7, 'text-anchor': 'middle', class: 'axis-text',
  }, String(labels[i]))));

  series.forEach((se) => {
    const pts = [];
    (se.values || []).forEach((v, i) => {
      if (v === null || v === undefined) return;
      pts.push([X(i), Y(v)]);
    });
    if (pts.length > 1) {
      svg.appendChild(node('polyline', {
        points: pts.map(p => p.join(',')).join(' '), fill: 'none', stroke: se.color,
        'stroke-width': 2.4, 'stroke-linejoin': 'round',
        'stroke-dasharray': se.dashed ? '5 4' : null,
      }));
    }
    if (dense) return;
    (se.values || []).forEach((v, i) => {
      if (v === null || v === undefined) return;
      svg.appendChild(node('circle', { cx: X(i), cy: Y(v), r: 3, fill: '#fff',
                                       stroke: se.color, 'stroke-width': 1.8 }));
    });
  });

  const hit = node('rect', { x: P.l, y: P.t, width: iw, height: ih,
                             fill: 'transparent', class: 'chart-hit' });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', ev => {
    const box = svg.getBoundingClientRect();
    const vx = ((ev.clientX - box.left) / box.width) * width;
    const i = Math.max(0, Math.min(labels.length - 1,
      Math.round(((vx - P.l) / iw) * (labels.length - 1))));
    const rows = series.map(s => {
      const v = s.values[i];
      const txt = v === null || v === undefined ? '无数据'
        : (opt.valueFormat ? opt.valueFormat(v) : fmtInt(v));
      return `<div class="tt-row"><i style="background:${s.color}"></i>
        <span>${s.name}</span><b>${txt}</b></div>`;
    }).join('');
    showTip(ev, `<div class="tt-title">${labels[i]}</div>${rows}`);
  });
  hit.addEventListener('mouseleave', hideTip);
}

/* ---------------------------------------------------------- 横向排名（可带完成率角标） */
export function rankBar(host, opt) {
  const items = opt.items || [];
  if (!items.length) return emptyBox(host, opt.emptyText || '当前筛选条件下无排名数据');
  const rowH = opt.rowH || 26;
  const H = Math.max(100, items.length * rowH + 14);
  host.innerHTML = '';
  const width = Math.max(320, host.clientWidth || 560);
  const svg = node('svg', { viewBox: `0 0 ${width} ${H}`, width: '100%', height: H,
                            class: 'chart-svg' });
  host.appendChild(svg);
  const labelW = opt.labelW || 92, valueW = opt.valueW || 58, rateW = opt.rateW || 50;
  const x0 = labelW + 6;
  const barW = Math.max(40, width - x0 - valueW - rateW - 14);
  const max = Math.max(...items.map(i => Math.abs(Number(i.value) || 0)), 1);

  items.forEach((it, i) => {
    const y = 5 + i * rowH;
    svg.appendChild(node('text', { x: labelW, y: y + 14, 'text-anchor': 'end',
                                   class: 'bar-label' },
      String(it.label).slice(0, opt.maxLabel || 10)));
    svg.appendChild(node('rect', { x: x0, y: y + 5, width: barW, height: 12, rx: 6,
                                   fill: '#eef3fb' }));
    const bw = Math.max(2, (Math.abs(Number(it.value) || 0) / max) * barW);
    svg.appendChild(node('rect', { x: x0, y: y + 5, width: bw, height: 12, rx: 6,
                                   fill: it.color || PALETTE[0], opacity: 0.94 }));
    svg.appendChild(node('text', { x: x0 + barW + 7, y: y + 15, class: 'bar-value' },
      opt.valueFormat ? opt.valueFormat(it.value) : fmtInt(it.value)));
    if (it.rate !== undefined && it.rate !== null) {
      svg.appendChild(node('text', {
        x: width - 4, y: y + 15, 'text-anchor': 'end',
        class: `bar-badge ${it.rate >= 100 ? 'down' : 'up'}`,
      }, fmtPct(it.rate)));
    } else if (it.rateText) {
      svg.appendChild(node('text', { x: width - 4, y: y + 15, 'text-anchor': 'end',
                                     class: 'bar-badge neutral' }, it.rateText));
    }
    const g = node('rect', { x: 0, y: y, width: width, height: rowH, fill: 'transparent',
                             class: 'bar-click' });
    svg.appendChild(g);
    if (opt.onClick) g.addEventListener('click', () => opt.onClick(it, i));
    g.addEventListener('mousemove', ev => showTip(ev,
      `<div class="tt-title">${it.label}</div>${it.tip || ''}`));
    g.addEventListener('mouseleave', hideTip);
  });
}

/* ---------------------------------------------------------- 价格区间条（min–P25–中位–P75–max） */
export function rangeBar(host, opt) {
  const items = opt.items || [];
  if (!items.length) return emptyBox(host, opt.emptyText || '当前筛选条件下无价格数据');
  const rowH = opt.rowH || 26;
  const H = Math.max(90, items.length * rowH + 16);
  host.innerHTML = '';
  const width = Math.max(320, host.clientWidth || 560);
  const svg = node('svg', { viewBox: `0 0 ${width} ${H}`, width: '100%', height: H,
                            class: 'chart-svg' });
  host.appendChild(svg);
  const labelW = opt.labelW || 116;
  const x0 = labelW + 8, barW = Math.max(60, width - x0 - 62);
  const max = Math.max(...items.map(i => Number(i.max) || 0), 1) * 1.05;
  const X = v => x0 + (Number(v) / max) * barW;

  for (let t = 0; t <= 2; t++) {
    const v = (max * t) / 2;
    svg.appendChild(node('line', { x1: X(v), y1: 4, x2: X(v), y2: H - 14, stroke: '#e9eff7' }));
    svg.appendChild(node('text', { x: X(v), y: H - 4, 'text-anchor': 'middle',
                                   class: 'axis-text' }, `¥${Math.round(v)}`));
  }

  items.forEach((it, i) => {
    const y = 6 + i * rowH;
    svg.appendChild(node('text', { x: labelW, y: y + 12, 'text-anchor': 'end',
                                   class: 'bar-label' },
      String(it.label).slice(0, opt.maxLabel || 14)));
    // 全距（淡）
    svg.appendChild(node('rect', { x: X(it.min), y: y + 4, width: Math.max(1, X(it.max) - X(it.min)),
                                   height: 8, rx: 4, fill: '#dbe6f6' }));
    // 四分位距（实）
    svg.appendChild(node('rect', { x: X(it.p25), y: y + 4,
                                   width: Math.max(1.5, X(it.p75) - X(it.p25)),
                                   height: 8, rx: 4, fill: '#2f6fe4', opacity: 0.9 }));
    // 中位数
    svg.appendChild(node('line', { x1: X(it.median), y1: y + 1, x2: X(it.median), y2: y + 15,
                                   stroke: '#1a2a44', 'stroke-width': 2 }));
    svg.appendChild(node('text', { x: width - 4, y: y + 12, 'text-anchor': 'end',
                                   class: 'bar-value' }, `¥${Math.round(it.median)}`));
    const g = node('rect', { x: 0, y: y, width: width, height: rowH, fill: 'transparent',
                             class: 'bar-click' });
    svg.appendChild(g);
    g.addEventListener('mousemove', ev => showTip(ev,
      `<div class="tt-title">${it.label}</div>
       <div class="tt-row"><span>记录数</span><b>${it.n}</b></div>
       <div class="tt-row"><span>最低 / 最高</span><b>¥${Math.round(it.min)} / ¥${Math.round(it.max)}</b></div>
       <div class="tt-row"><span>P25 / 中位 / P75</span><b>${Math.round(it.p25)} / ${Math.round(it.median)} / ${Math.round(it.p75)}</b></div>
       ${it.extraTip || ''}`));
    g.addEventListener('mouseleave', hideTip);
  });
}

export function groupedColumns(host, opt) {
  const labels = opt.labels || [];
  const series = opt.series || [];
  if (!labels.length) return emptyBox(host, opt.emptyText || '当前筛选条件下无数据');
  const H = opt.height || 200, P = { l: 48, r: 12, t: 18, b: 26 };
  host.innerHTML = '';
  const width = Math.max(320, host.clientWidth || 560);
  const svg = node('svg', { viewBox: `0 0 ${width} ${H}`, width: '100%', height: H,
                            class: 'chart-svg' });
  host.appendChild(svg);
  const iw = width - P.l - P.r, ih = H - P.t - P.b;
  const all = series.flatMap(s => s.values).filter(v => v !== null && v !== undefined)
    .map(Number);
  const max = Math.max(...all, 1) * 1.2;

  for (let t = 0; t <= 3; t++) {
    const y = P.t + ih - (ih * t) / 3;
    svg.appendChild(node('line', { x1: P.l, y1: y, x2: width - P.r, y2: y,
                                   stroke: '#e9eff7' }));
    svg.appendChild(node('text', { x: P.l - 6, y: y + 3, 'text-anchor': 'end',
                                   class: 'axis-text' }, fmtInt(Math.round((max * t) / 3))));
  }
  const slot = iw / labels.length;
  const bw = Math.min(30, (slot - 16) / series.length);
  labels.forEach((lb, i) => {
    const cx = P.l + slot * i + slot / 2;
    svg.appendChild(node('text', { x: cx, y: H - 7, 'text-anchor': 'middle',
                                   class: 'axis-text' }, String(lb)));
    series.forEach((se, si) => {
      const v = se.values[i];
      const x = cx - (series.length * bw) / 2 + si * bw + 1;
      if (v === null || v === undefined) {
        svg.appendChild(node('text', { x: x + bw / 2, y: P.t + ih - 3,
                                       'text-anchor': 'middle', class: 'axis-text' }, '无'));
        return;
      }
      const bh = Math.max(2, (Number(v) / max) * ih);
      const rect = node('rect', { x, y: P.t + ih - bh, width: bw - 2, height: bh, rx: 2,
                                  fill: se.color, opacity: 0.94, class: 'bar-click' });
      svg.appendChild(rect);
      rect.addEventListener('mousemove', ev => showTip(ev,
        `<div class="tt-title">${lb} · ${se.name}</div>
         <div class="tt-row"><span>数值</span><b>${fmtInt(v)}${opt.unit || ''}</b></div>`));
      rect.addEventListener('mouseleave', hideTip);
    });
  });
  const legend = document.createElement('div');
  legend.className = 'mini-legend';
  series.forEach(se => {
    const span = document.createElement('span');
    span.innerHTML = `<i style="background:${se.color}"></i>${se.name}`;
    legend.appendChild(span);
  });
  if (opt.legendHost) {
    const box = document.getElementById(opt.legendHost);
    if (box) { box.innerHTML = ''; box.appendChild(legend); }
  }
}
