/* =====================================================================
 * agent-dock.js — 全站 AI 经营助手抽屉
 *
 * V2 改造要点（相对 V1 的「驾驶舱私有浮层」）：
 *   1. 不再依赖页面上必须先有一个 #btn-agent-dock：没有就自己注入右下角悬浮按钮；
 *   2. 从右侧滑出，可展开（460 → 760）与收起，不跳页、不改页面布局；
 *   3. 页面上下文由 /page-context.js 统一采集，筛选一变就 postMessage 给 iframe，
 *      用户不必关掉重开，下一问自动用最新条件；
 *   4. 同一浏览器标签内用 sessionStorage 复用同一个 thread_id —— 切页面不丢会话。
 *
 * 为什么仍然用 iframe：
 *   对话、报告、数据核对三套渲染已经在 /agent.js 里，复制一份必然出现「两套前端各改各的」。
 *   这里只做外壳。同源 iframe，SSE / fetch 行为不变；embed=1 时 agent.css 去掉导航与右栏。
 *
 * 兼容：驾驶舱原有的 #btn-agent-dock 仍然有效（与悬浮按钮触发同一个 open()）。
 * ===================================================================== */

(function () {
  'use strict';

const DOCK_ID = 'agent-dock';
const THREAD_KEY = 'ag-thread-id';
// 展开状态必须跨页面记住：用户在驾驶舱打开助手，点进 POS 分析时
// 助手应该还在原位，而不是每换一页都要重新点一次入口。
const OPEN_KEY = 'ag-dock-open';
const WIDE_KEY = 'ag-dock-wide';

function flagGet(key, dflt) {
  try {
    const v = sessionStorage.getItem(key);
    return v === null ? dflt : v === '1';
  } catch (e) {
    return dflt;
  }
}

function flagSet(key, on) {
  try { sessionStorage.setItem(key, on ? '1' : '0'); } catch (e) { /* 无痕模式忽略 */ }
}

/* ------------------------------------------------------------ 会话号 */
function threadId() {
  try {
    let t = sessionStorage.getItem(THREAD_KEY);
    if (!t) {
      t = `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
      sessionStorage.setItem(THREAD_KEY, t);
    }
    return t;
  } catch (e) {
    return `t-${Date.now().toString(36)}`;
  }
}

/* --------------------------------------------------------------- 当前筛选 */
function readFilters() {
  // 优先用统一采集器：它知道每个页面哪些控件真实存在
  try {
    if (window.PageContext && typeof window.PageContext.collect === 'function') {
      const ctx = window.PageContext.collect();
      if (ctx) return ctx;
    }
  } catch (e) { /* 采集失败退到旧快照 */ }
  try {
    // cockpit.js 暴露的只读快照；拿不到就当作「全部战区 · 累计」
    return (typeof window.__ckFilters === 'function' ? window.__ckFilters() : null) || null;
  } catch (e) {
    return null;
  }
}

function scopeLabel(f) {
  try {
    if (window.PageContext && f && f.page_id) return window.PageContext.label(f);
  } catch (e) { /* 用下面的兜底文案 */ }
  if (!f) return '全部战区 · 2026 年 1-10 月';
  const who = f.customer || f.region || '全部战区';
  const months = f.months || [];
  const last = months.length ? months[months.length - 1] : 10;
  const when = months.length === 1 ? `2026 年 ${last} 月` : `2026 年 1-${last} 月`;
  return `${who} · ${when}`;
}

function buildSrc(f) {
  const p = new URLSearchParams({ embed: '1' });
  p.set('year', String((f && f.year) || 2026));
  if (f && f.region) p.set('region', f.region);
  if (f && f.customer) p.set('customer', f.customer);
  if (f && f.months && f.months.length) p.set('months', f.months.join(','));
  // V2：把页面身份与模块专属筛选也带上，后端据此判断「默认范围」而不是「限制」
  if (f && f.page_id) p.set('page', f.page_id);
  if (f && f.page_name) p.set('page_name', f.page_name);
  if (f && f.module) p.set('module', f.module);
  if (f && f.department) p.set('department', f.department);
  if (f && f.channel) p.set('channel', f.channel);
  if (f && f.ka_system) p.set('ka_system', f.ka_system);
  if (f && f.sku) p.set('sku', f.sku);
  if (f && f.store) p.set('store', f.store);
  p.set('thread_id', threadId());
  return `/agent.html?${p.toString()}`;
}

/* ------------------------------------------------------------------ 外壳 */
function buildFab() {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'ck-agent-fab';
  b.id = 'agent-fab';
  b.setAttribute('aria-label', '打开 AI 经营助手');
  b.setAttribute('aria-expanded', 'false');
  b.title = '问 AI 经营助手（本页数据可直接核对）';
  b.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" '
    + 'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.9-.9L3 20.5l1.4-4.1A8.4 8.4 0 0 1 12 3.1a8.4 8.4 0 0 1 9 8.4z"/>'
    + '<path d="M8.5 10.5h7M8.5 13.8h4.5"/></svg>';
  document.body.appendChild(b);
  return b;
}

function buildDock() {
  const dock = document.createElement('aside');
  dock.className = 'ck-dock hidden';
  dock.id = DOCK_ID;
  dock.setAttribute('role', 'dialog');
  dock.setAttribute('aria-modal', 'false');
  dock.setAttribute('aria-label', 'AI 经营助手（页内对话）');
  dock.innerHTML = `
    <header class="ck-dock-head">
      <span class="ck-dock-brand"><i></i>AI 经营助手</span>
      <span class="ck-dock-scope" id="dock-scope">—</span>
      <span class="ck-dock-acts">
        <button class="ck-dock-btn" id="dock-wide" type="button"
                title="展开 / 收起" aria-label="展开或收起">⤢</button>
        <button class="ck-dock-btn ck-dock-x" id="dock-close" type="button" aria-label="关闭">×</button>
      </span>
    </header>
    <div class="ck-dock-body"><iframe id="dock-frame" title="AI 经营助手对话" loading="lazy"></iframe></div>
    <footer class="ck-dock-foot">
      <span>当前范围：<b id="dock-scope-foot">—</b></span>
      <span class="ck-dock-note">数字由 SQL 计算 · 模型只做解释</span>
    </footer>`;
  document.body.appendChild(dock);
  return dock;
}

/* ------------------------------------------------------------------ 主逻辑 */
function init() {
  // 驾驶舱有卡片按钮；其它页面没有，由这里补一个右下角入口
  const pageBtn = document.getElementById('btn-agent-dock');
  const fab = document.getElementById('agent-fab') || buildFab();
  const triggers = [fab, pageBtn].filter(Boolean);

  const $ = (id) => document.getElementById(id);
  let dock = null;
  let frame = null;
  let loadedSrc = '';
  let lastCtx = null;

  function ensureDock() {
    if (dock) return dock;
    dock = buildDock();
    frame = $('dock-frame');
    $('dock-close').addEventListener('click', close);
    $('dock-wide').addEventListener('click', () => {
      setWide(!dock.classList.contains('is-wide'));
    });
    // 宽度是用户的偏好，切页后继续沿用（不强制回到 460px）
    setWide(flagGet(WIDE_KEY, false));
    // 焦点在 iframe 里时，键盘事件不会冒泡到父文档，需要向 iframe 内部也挂一次
    frame.addEventListener('load', () => {
      try {
        frame.contentWindow.addEventListener('keydown', (e) => {
          if (e.key === 'Escape') close();
        });
        // 首帧之后补发一次上下文，避免 iframe 初始化时父页面还没采集完
        pushContext(frame, lastCtx);
      } catch (e) { /* 同源不会失败；真失败也不影响主流程 */ }
    });
    return dock;
  }

  function setWide(wide) {
    if (!dock) return;
    dock.classList.toggle('is-wide', !!wide);
    const btn = $('dock-wide');
    if (btn) {
      btn.textContent = wide ? '⤡' : '⤢';
      btn.title = wide ? '收起宽度' : '展开宽度';
    }
    flagSet(WIDE_KEY, !!wide);
  }

  function pushContext(fr, ctx) {
    if (!fr || !fr.contentWindow || !ctx) return;
    try {
      fr.contentWindow.postMessage({ type: 'page-context', context: ctx }, location.origin);
    } catch (e) { /* iframe 还没就绪，下一轮再发 */ }
  }

  function syncScope(ctx) {
    const label = scopeLabel(ctx);
    const a = $('dock-scope'), b = $('dock-scope-foot');
    if (a) a.textContent = label;
    if (b) b.textContent = label;
  }

  function open(force = false) {
    ensureDock();
    const f = readFilters();
    lastCtx = f;
    const src = buildSrc(f);
    syncScope(f);

    if (!loadedSrc) {
      frame.src = src;                       // 首次：懒加载
      loadedSrc = src;
    } else if (src !== loadedSrc && force) {
      frame.src = src;
      loadedSrc = src;
    } else if (src !== loadedSrc) {
      // 不重载 iframe（会丢对话），只把新上下文推进去
      pushContext(frame, f);
    }
    dock.classList.remove('hidden');
    triggers.forEach((t) => t.setAttribute('aria-expanded', 'true'));
    fab.classList.add('is-open');
    flagSet(OPEN_KEY, true);
  }

  function close() {
    if (!dock) return;
    dock.classList.add('hidden');
    triggers.forEach((t) => t.setAttribute('aria-expanded', 'false'));
    fab.classList.remove('is-open');
    flagSet(OPEN_KEY, false);
  }

  triggers.forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      if (!dock || dock.classList.contains('hidden')) open();
      else close();
    });
    btn.setAttribute('aria-expanded', 'false');
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
  });

  // 筛选变了：如果抽屉开着，实时把新上下文推进去（不打断对话）
  try {
    if (window.PageContext && typeof window.PageContext.onChange === 'function') {
      window.PageContext.onChange((ctx) => {
        lastCtx = ctx;
        if (!dock || dock.classList.contains('hidden')) return;
        syncScope(ctx);
        pushContext(frame, ctx);
      });
    }
  } catch (e) { /* 没有采集器也能用，只是不自动同步 */ }

  // 抽屉内点了「新会话」：会话号由 iframe 生成，父页面必须跟着换，
  // 否则用户切到下一页面时，buildSrc 仍用旧 thread_id，清掉的对话又回来了。
  window.addEventListener('message', (e) => {
    if (e.origin !== location.origin) return;
    const d = e.data || {};
    if (d.type !== 'thread-reset' || !d.thread_id) return;
    try { sessionStorage.setItem(THREAD_KEY, d.thread_id); } catch (err) { /* 忽略 */ }
    loadedSrc = '';                         // 下次重建 iframe 时用新会话
  });

  // 上一页开着 → 本页直接接着开。上下文由 buildSrc 按当前页重算，
  // 对话内容由 agent.js 依据同一个 thread_id 从 sessionStorage 恢复。
  if (flagGet(OPEN_KEY, false)) {
    // 等首屏布局稳定再开，避免抽屉宽度在字体加载后跳一下
    requestAnimationFrame(() => open());
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

})();
