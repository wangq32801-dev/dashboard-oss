import { apiURL } from './api-client.js';

// Shared SSE parser: one timeout/cancel/error contract for all AI streams.
export async function streamSSE(path, body, {onDelta, onDone, onError, timeoutMs = 60000, fetchImpl = fetch, signal: externalSignal} = {}) {
  const abort = new AbortController();
  const relayAbort = () => { try { abort.abort(); } catch (_) {} };
  if (externalSignal) {
    if (externalSignal.aborted) relayAbort();
    else externalSignal.addEventListener('abort', relayAbort, {once: true});
  }
  const timer = setTimeout(() => { try { abort.abort(); } catch (_) {} }, timeoutMs);
  const out = {text: '', payload: null, err: null};
  let reader = null, doneSeen = false, buffer = '';
  const handle = (raw) => {
    const line = raw.trim();
    if (!line || line.startsWith(':') || !line.startsWith('data:')) return;
    const value = line.slice(5).trim();
    if (!value || value === '[DONE]') return;
    try {
      const obj = JSON.parse(value);
      if (obj.delta) { out.text += obj.delta; onDelta?.(obj.delta, out.text); }
      if (obj.done) { doneSeen = true; out.payload = obj; onDone?.(obj, out.text); }
      if (obj.error) { out.err = obj.error; onError?.(obj.error); }
    } catch (_) { /* ignore malformed frames; the final error/timeout remains visible */ }
  };
  try {
    const response = await fetchImpl(apiURL(path), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: abort.signal});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    if (!response.body) throw new Error('空响应体');
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, {stream: true});
      while (true) {
        const end = buffer.indexOf('\n\n');
        if (end < 0) break;
        const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        frame.split('\n').forEach(handle);
      }
      if (doneSeen) { try { await reader.cancel(); } catch (_) {} break; }
    }
    if (buffer.trim()) handle(buffer.trim());
    return out;
  } finally {
    clearTimeout(timer);
    try { externalSignal?.removeEventListener('abort', relayAbort); } catch (_) {}
    try { if (reader?.cancel) await reader.cancel(); } catch (_) {}
    try { abort.abort(); } catch (_) {}
  }
}

if (typeof window !== 'undefined') window.__sseClient = {streamSSE};
