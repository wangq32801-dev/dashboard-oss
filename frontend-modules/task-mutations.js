function apiURL(path) {
  if (typeof location === 'undefined') return path;
  const pathname = location.pathname || '/';
  const marker = pathname.indexOf('/api/');
  const base = marker >= 0 ? pathname.slice(0, marker) : (pathname.endsWith('/') ? pathname : pathname.slice(0, pathname.lastIndexOf('/') + 1));
  const prefix = base.endsWith('/') ? base : `${base}/`;
  return `${prefix}${path.replace(/^\//, '')}`;
}

async function postJSON(url, payload, fetchImpl = fetch, timeoutMs = 15000) {
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const options = {method:'POST', headers:{'Content-Type':'application/json', Accept:'application/json'}, body:JSON.stringify(payload)};
    if (controller) options.signal = controller.signal;
    const response = await fetchImpl(url, options);
    if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('dash-network', {detail: {online: true}}));
    const body = await response.json().catch(() => ({}));
    if (!response.ok && body.success !== false) {
      body.success = false;
      body.error = body.error || `HTTP ${response.status}`;
    }
    return body;
  } catch (error) {
    if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('dash-network', {detail: {online: false}}));
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export const updateTask = (payload, fetchImpl) => postJSON(apiURL('/api/tasks/update'), payload, fetchImpl);
export const archiveTask = (payload, fetchImpl) => postJSON(apiURL('/api/tasks/archive'), payload, fetchImpl);
export const unarchiveTask = (payload, fetchImpl) => postJSON(apiURL('/api/tasks/unarchive'), payload, fetchImpl);
export const completeTask = (payload, fetchImpl) => postJSON(apiURL('/api/tasks/update'), {...payload, status: 2}, fetchImpl);
if (typeof window !== 'undefined') window.__taskMutations = {updateTask, archiveTask, unarchiveTask, completeTask};
