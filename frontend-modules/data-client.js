// Browser-safe data client; integration into the monolith is intentionally staged.
function apiURL(path) {
  if (typeof location === 'undefined') return path;
  const pathname = location.pathname || '/';
  const marker = pathname.indexOf('/api/');
  const base = marker >= 0 ? pathname.slice(0, marker) : (pathname.endsWith('/') ? pathname : pathname.slice(0, pathname.lastIndexOf('/') + 1));
  return `${base.endsWith('/') ? base : `${base}/`}${path.replace(/^\//, '')}`;
}

export async function getJSON(url, {method = 'GET', body, timeoutMs = 12000, cache = 'no-store', fetchImpl = fetch} = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const options = {method, cache, signal: controller.signal, headers: {Accept: 'application/json'}};
    if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
    const response = await fetchImpl(url, options);
    let payload = null;
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error((payload && payload.error) || `HTTP ${response.status}`);
    return payload;
  } finally { clearTimeout(timer); }
}

export const shadowStatus = (options) => getJSON(apiURL('/api/shadow/status?check=1'), options);
export const reconcileShadow = (options = {}) => getJSON(apiURL('/api/shadow/reconcile'), {
  ...options, method: 'POST', body: options.body || {}, timeoutMs: options.timeoutMs || 95000
});
