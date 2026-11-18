/* =====================================================================
 * api.js — 前后端数据接口客户端
 * 页面只通过这里取数；任何指标计算都在后端 SQL / ETL 视图完成。
 * ===================================================================== */
async function getJSON(path, params = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') url.searchParams.set(k, String(v));
  });
  const res = await fetch(url.toString(), { cache: 'no-store' });
  let payload;
  try {
    payload = await res.json();
  } catch (e) {
    throw new Error(`接口返回非 JSON（HTTP ${res.status}）`);
  }
  if (!res.ok || payload.ok === false) {
    throw new Error(payload.error || `HTTP ${res.status}`);
  }
  return payload;
}

export const api = {
  meta: () => getJSON('/api/meta'),
  health: () => getJSON('/api/health'),
  scope: () => getJSON('/api/scope'),
  dashboard: (params) => getJSON('/api/dashboard', params),
  // 集团总览：跨模块 KPI + 四条业务线趋势（各模块各查各的视图，金额不相加）
  overview: (params) => getJSON('/api/overview', params),
  detail: (params) => getJSON('/api/detail', params),
  // 单区块接口，供后续 5 个模块与测试复用
  kpi: (p) => getJSON('/api/kpi', p),
  trend: (p) => getJSON('/api/trend', p),
  region: (p) => getJSON('/api/region', p),
  channel: (p) => getJSON('/api/channel', p),
  category: (p) => getJSON('/api/category', p),
  sku: (p) => getJSON('/api/sku', p),
  customer: (p) => getJSON('/api/customer', p),
  // AI 经营驾驶舱：一次取全页数据；洞察按需单独请求（不在页面加载时自动调用）
  cockpit: (p) => getJSON('/api/cockpit', p),
  cockpitOptions: () => getJSON('/api/cockpit/options'),
  cockpitInsight: (p) => getJSON('/api/cockpit/insight', p),
};
