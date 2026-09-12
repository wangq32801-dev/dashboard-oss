// Shared JSON transport for the SPA. Kept dependency-free so it can be loaded
// independently of the monolith and reused by future views.
export function apiURL(path) {
  if (typeof location === 'undefined') return path;
  const pathname = location.pathname || '/';
  const marker = pathname.indexOf('/api/');
  const base = marker >= 0 ? pathname.slice(0, marker) : (pathname.endsWith('/') ? pathname : pathname.slice(0, pathname.lastIndexOf('/') + 1));
  return `${base.endsWith('/') ? base : `${base}/`}${path.replace(/^\//, '')}`;
}

export async function requestJSON(path, {method = 'GET', body, timeoutMs = 15000, fetchImpl = fetch, signal} = {}) {
  const controller = new AbortController();
  const forwardAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', forwardAbort, {once: true});
  }
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const options = {method, signal: controller.signal, headers: {Accept: 'application/json'}};
    if (body !== undefined && body !== null) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetchImpl(apiURL(path), options);
    if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('dash-network', {detail: {online: true}}));
    let payload;
    try { payload = await response.json(); }
    catch (_) { payload = {success: false, error: `HTTP ${response.status}（响应不是有效 JSON）`}; }
    if (!response.ok && payload.success !== false) {
      payload.success = false;
      payload.error = payload.error || `HTTP ${response.status}`;
    }
    return payload;
  } catch (error) {
    if (typeof window !== 'undefined' && !(error && error.name === 'AbortError' && signal && signal.aborted)) window.dispatchEvent(new CustomEvent('dash-network', {detail: {online: false}}));
    throw error;
  } finally { clearTimeout(timer); if (signal) signal.removeEventListener('abort', forwardAbort); }
}

if (typeof window !== 'undefined') window.__apiClient = {apiURL, requestJSON};
