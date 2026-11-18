/* =====================================================================
 * page-context.js — 全站页面上下文采集器（唯一来源）
 *
 * 为什么单独一个文件：
 *   8 个 BI 页面的筛选控件 id / 语义都不一样，如果让每个页面自己拼 context，
 *   会出现 8 套「什么叫当前筛选」的定义，AI 拿到的范围就会跟用户看到的对不上。
 *   这里用一张注册表把「页面 → 字段 → 控件」写死，前端采集、后端校验、
 *   测试断言都读同一份定义。
 *
 * 三条硬规则：
 *   1. 只采集页面上真实存在的控件，没有就是空串，绝不虚构（例如批发页没有 KA 系统）；
 *   2. 值优先取 option.value（干净的业务值），需要人名的字段取 option.text（如门店名 / SKU 名）；
 *   3. 任何采集异常都吞掉并返回已采到的部分 —— 助手不能因为某个页面改版就整个打不开。
 *
 * 暴露：window.PageContext = { PAGES, current(), collect(), onChange(fn), label(ctx) }
 * ===================================================================== */
(function () {
  'use strict';

  /* ------------------------------------------------------------ 注册表
   * fields[].mode
   *   value  → 取 select.value（默认）
   *   label  → 取选中项文本，去掉「（12 店）」这类后缀
   *   int    → 转整数
   *   month  → 单月数字；0 / 空 = 累计
   */
  const PAGES = {
    cockpit: {
      page_id: 'cockpit',
      page_name: 'AI 经营驾驶舱',
      module: 'cockpit',
      path: '/',
      fields: [
        { key: 'year', sel: '#f-year', mode: 'int' },
        { key: 'month', sel: '#f-month', mode: 'month' },
        { key: 'region', sel: '#f-region' },
        { key: 'customer', sel: '#f-ka', mode: 'label' }
      ]
    },
    analysis: {
      page_id: 'analysis',
      page_name: '战区销售达成跟进',
      module: 'analysis',
      path: '/analysis.html',
      fields: [
        { key: 'year', sel: '#f-year', mode: 'int' },
        { key: 'month', sel: '#f-month', mode: 'month' },
        { key: 'region', sel: '#f-region' },
        { key: 'department', sel: '#f-dept' },
        { key: 'category', sel: '#f-category' },
        { key: 'segment', sel: '#f-segment' }
      ]
    },
    pos: {
      page_id: 'pos',
      page_name: 'POS 分析',
      module: 'pos',
      path: '/pos.html',
      fields: [
        { key: 'month', sel: '#f-month', mode: 'month' },
        { key: 'region', sel: '#f-region' },
        { key: 'ka_system', sel: '#f-ka' },
        { key: 'category', sel: '#f-category' },
        { key: 'sku', sel: '#f-sku', mode: 'label' },
        { key: 'store', sel: '#f-store', mode: 'label' }
      ]
    },
    sfa_visit: {
      page_id: 'sfa_visit',
      page_name: 'SFA 拜访分析',
      module: 'sfa',
      path: '/sfa-visit.html',
      fields: [
        { key: 'month', sel: '#f-month', mode: 'month' },
        { key: 'region', sel: '#f-region' },
        { key: 'department', sel: '#f-md' },
        { key: 'employee', sel: '#f-employee', mode: 'label' }
      ]
    },
    sfa_profile: {
      page_id: 'sfa_profile',
      page_name: 'SFA 门店画像',
      module: 'sfa',
      path: '/sfa-profile.html',
      fields: [
        { key: 'month', sel: '#f-month', mode: 'month' },
        { key: 'region', sel: '#f-region' },
        { key: 'department', sel: '#f-md' },
        { key: 'employee', sel: '#f-employee', mode: 'label' },
        { key: 'ka_system', sel: '#f-ka' },
        { key: 'sku', sel: '#f-sku', mode: 'label' }
      ]
    },
    wholesale: {
      page_id: 'wholesale',
      page_name: '批发经营分析',
      module: 'wholesale',
      path: '/wholesale.html',
      fields: [
        { key: 'month', sel: '#f-month', mode: 'month' },
        { key: 'region', sel: '#f-region' },
        { key: 'customer', sel: '#f-customer' },
        { key: 'stall', sel: '#f-stall', mode: 'label' },
        { key: 'sku', sel: '#f-sku', mode: 'label' }
      ]
    },
    sc: {
      page_id: 'sc',
      page_name: '供应链看板',
      module: 'sc',
      path: '/sc.html',
      fields: [
        // 供应链页只有月份一个筛选（值形如 2026-10，由 month 模式解析）
        { key: 'month', sel: '#f-month', mode: 'month' }
      ]
    }
  };

  const ALL_WORDS = ['全部', '全部门店', '全部战区', '五大战区', '所有', '不限', '合计'];

  /* ------------------------------------------------------------ 工具 */
  function cleanLabel(text) {
    let s = String(text || '').trim();
    // 「KA客户A（12 店）」「全部 SKU（72）」→ 去掉括号补充说明
    s = s.replace(/[（(][^（()）]*[)）]\s*$/, '').trim();
    if (!s) return '';
    if (ALL_WORDS.some((w) => s === w || s.startsWith(w))) return '';
    return s;
  }

  function readOne(field) {
    const el = document.querySelector(field.sel);
    if (!el) return { ok: false, value: '', label: '' };
    const rawValue = el.value == null ? '' : String(el.value).trim();
    let label = '';
    const opt = el.selectedOptions && el.selectedOptions[0];
    if (opt) label = cleanLabel(opt.textContent);
    const mode = field.mode || 'value';
    if (mode === 'int') {
      const n = Number(rawValue);
      return { ok: true, value: Number.isFinite(n) ? n : 0, label };
    }
    if (mode === 'month') {
      let n = Number(rawValue);
      if (!Number.isFinite(n)) {
        // 供应链页的月份值是 'YYYY-MM' 整月快照，取月份数字
        const m = /^(\d{4})-(\d{1,2})$/.exec(rawValue);
        if (m) n = Number(m[2]);
      }
      return { ok: true, value: Number.isFinite(n) ? n : 0, label };
    }
    if (mode === 'months') {
      const vals = Array.from(el.selectedOptions || [])
        .map((o) => Number(o.value)).filter((n) => n >= 1 && n <= 12);
      return { ok: true, value: vals.sort((a, b) => a - b), label };
    }
    if (mode === 'label') {
      // 门店 / SKU 这类要人名而不是编码
      return { ok: true, value: label || cleanLabel(rawValue), label: label || rawValue };
    }
    return { ok: true, value: cleanLabel(rawValue) || rawValue, label };
  }

  function detectPage() {
    const forced = (document.body && document.body.dataset && document.body.dataset.page) || '';
    if (forced && PAGES[forced]) return PAGES[forced];
    const path = location.pathname.replace(/\/index\.html$/, '/');
    const hit = Object.values(PAGES).find((p) => p.path === path);
    return hit || null;
  }

  /* ------------------------------------------------------------ 采集 */
  function collect() {
    const page = detectPage();
    if (!page) return null;

    const out = {
      page_id: page.page_id,
      page_name: page.page_name,
      module: page.module,
      year: 2026,
      region: '', department: '', customer: '',
      channel: '', ka_system: '', sku: '', store: '', category: '', segment: '',
      months: [],
      filters: {},
      captured_at: new Date().toISOString()
    };

    page.fields.forEach((f) => {
      const r = readOne(f);
      if (!r.ok) return;                       // 控件不存在：不虚构
      out.filters[f.key] = r.value;
      if (f.key === 'months') { out.months = r.value; return; }
      if (f.key === 'month') {
        // 单月控件：0 或空 = 累计（用已发生月份）
        if (r.value) out.months = [Number(r.value)];
        return;
      }
      out[f.key] = r.value;
    });

    // 驾驶舱的筛选状态在 JS state 里，DOM 只是渲染结果；有快照就用快照（更准）
    if (page.page_id === 'cockpit' && typeof window.__ckFilters === 'function') {
      try {
        const s = window.__ckFilters();
        if (s) {
          out.year = s.year || out.year;
          out.region = s.region || '';
          out.customer = s.customer || '';
          out.months = Array.isArray(s.months) ? s.months : out.months;
        }
      } catch (e) { /* 快照失败不阻断 */ }
    }

    if (!out.months || !out.months.length) out.months = [];
    out.date_range = {
      months: out.months,
      label: out.months.length
        ? (out.months.length === 1
            ? `${out.year} 年 ${out.months[0]} 月`
            : `${out.year} 年 ${out.months[0]}–${out.months[out.months.length - 1]} 月`)
        : `${out.year} 年累计`
    };
    return out;
  }

  /* ------------------------------------------------------------ 人话标签 */
  const MODULE_LABEL = {
    cockpit: '经营总览', dashboard: '全国驾驶舱', analysis: '战区达成',
    pos: '终端动销', sfa: '拜访执行', wholesale: '批发经营', sc: '供应链'
  };

  function label(ctx) {
    if (!ctx) return '未识别页面';
    const bits = [];
    if (ctx.customer) bits.push(ctx.customer);
    else if (ctx.region) bits.push(ctx.region);
    else bits.push('全国');
    if (ctx.ka_system) bits.push(ctx.ka_system);
    if (ctx.store) bits.push(ctx.store);
    if (ctx.department) bits.push(ctx.department);
    if (ctx.category) bits.push(ctx.category);
    if (ctx.sku) bits.push(ctx.sku);
    const m = (ctx.months && ctx.months.length)
      ? (ctx.months.length === 1 ? `${ctx.months[0]} 月` : `${ctx.months[0]}–${ctx.months[ctx.months.length - 1]} 月`)
      : '';
    bits.push(m ? `${ctx.year || 2026} 年 ${m}` : `${ctx.year || 2026} 年累计`);
    return bits.join(' · ');
  }

  function current() {
    return collect();
  }

  /* ------------------------------------------------------------ 变更通知 */
  const listeners = [];
  let timer = 0;

  function emit() {
    const ctx = collect();
    if (!ctx) return;
    listeners.forEach((fn) => {
      try { fn(ctx); } catch (e) { /* 单个订阅者出错不影响其他 */ }
    });
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(emit, 260);            // 防抖：连续切换只发最后一次
  }

  function onChange(fn) {
    if (typeof fn === 'function') listeners.push(fn);
    return () => {
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  // 捕获阶段监听：页面自己的 change 回调不影响我们；input 覆盖搜索框
  document.addEventListener('change', schedule, true);
  document.addEventListener('input', schedule, true);

  window.PageContext = { PAGES, current, collect, onChange, label, MODULE_LABEL };

  // 首屏先算一次，dock 打开时无需等待
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { window.__pageContext = collect(); });
  } else {
    window.__pageContext = collect();
  }
})();
