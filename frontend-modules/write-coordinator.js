const OUTBOX_KEY = 'dash_write_outbox_v1';
const RECEIPT_KEY = 'dash_write_receipts_v1';
const MAX_OUTBOX = 50;
const MAX_RECEIPTS = 30;
const MAX_AGE = 7 * 86400000;

function safeParse(raw, fallback) {
  try { const value = JSON.parse(raw); return Array.isArray(value) ? value : fallback; }
  catch (_) { return fallback; }
}

function makeId(now = Date.now()) {
  return `w_${now.toString(36)}_${Math.random().toString(36).slice(2, 9)}`;
}

function cleanRows(rows, now = Date.now()) {
  return rows.filter(row => row && row.id && now - Number(row.createdAt || now) < MAX_AGE).slice(-MAX_OUTBOX);
}

function entityId(body) {
  if (!body || typeof body !== 'object') return '';
  const entity = body.task || body.habit || body.entry || body.role || {};
  return String(body.entityId || body.id || entity.id || '');
}

export function createWriteCoordinator({send, storage, onChange, onResult, now} = {}) {
  if (typeof send !== 'function') throw new Error('write coordinator requires send(route, payload)');
  const store = storage || (typeof localStorage !== 'undefined' ? localStorage : null);
  const clock = typeof now === 'function' ? now : (() => Date.now());
  let rows = cleanRows(store ? safeParse(store.getItem(OUTBOX_KEY), []) : [], clock());
  let receipts = store ? safeParse(store.getItem(RECEIPT_KEY), []) : [];
  let listeners = [];

  function persist() {
    rows = cleanRows(rows, clock());
    receipts = receipts.slice(-MAX_RECEIPTS);
    try {
      if (store) {
        store.setItem(OUTBOX_KEY, JSON.stringify(rows));
        store.setItem(RECEIPT_KEY, JSON.stringify(receipts));
      }
    } catch (_) {}
    const snapshot = list();
    listeners.forEach(fn => { try { fn(snapshot); } catch (_) {} });
    if (typeof onChange === 'function') { try { onChange(snapshot); } catch (_) {} }
  }

  function list() { return rows.map(row => ({...row, payload: undefined})); }
  function listReceipts() { return receipts.map(row => ({...row})); }
  function find(id) { return rows.find(row => row.id === id); }

  async function transmit(row, timeoutMs, isRetry = false) {
    row.status = 'sending'; row.lastAttemptAt = clock(); row.error = ''; persist();
    try {
      const body = await send(row.route, row.payload, timeoutMs);
      if (body && body.success === false) {
        row.status = body.uncertain ? 'pending' : 'failed';
        row.error = String(body.error || (body.uncertain ? '服务器仍在处理' : '写入失败')).slice(0, 240);
        persist();
        if (isRetry && typeof onResult === 'function') onResult({route:row.route,body,error:row.error});
        return body;
      }
      rows = rows.filter(item => item.id !== row.id);
      const receipt = body && body.receipt || {};
      receipts.push({
        id: row.id, route: row.route, domain: row.domain || receipt.domain || '',
        entityId: receipt.entityId || entityId(body), verified: receipt.verified !== false,
        duplicate: !!(body && body.duplicate), completedAt: clock(), message: receipt.message || '已写入并确认'
      });
      persist();
      if (isRetry && typeof onResult === 'function') onResult({route:row.route,body,error:''});
      return body;
    } catch (error) {
      row.status = 'pending';
      row.error = String(error && error.message || error || '网络中断，等待重试').slice(0, 240);
      persist();
      if (isRetry && typeof onResult === 'function') onResult({route:row.route,body:null,error:row.error});
      throw error;
    }
  }

  async function execute(route, payload, {domain = '', timeoutMs = 15000} = {}) {
    const mutationId = String(payload && payload.clientMutationId || makeId(clock()));
    let row = rows.find(item => item.route === route && item.mutationId === mutationId);
    if (!row) {
      row = {id: makeId(clock()), route, domain, mutationId,
        payload: {...(payload || {}), clientMutationId: mutationId},
        status: 'pending', error: '', createdAt: clock(), lastAttemptAt: 0};
      rows.push(row); persist();
    }
    return transmit(row, timeoutMs, false);
  }

  async function retry(id, timeoutMs = 20000) {
    const row = find(id); if (!row) return {success: false, error: '待同步记录不存在'};
    return transmit(row, timeoutMs, true);
  }

  async function retryAll() {
    const pending = rows.filter(row => row.status !== 'sending');
    const results = [];
    for (const row of pending) {
      try { results.push(await transmit(row, 20000, true)); }
      catch (error) { results.push({success: false, error: String(error && error.message || error)}); }
    }
    return results;
  }

  function discard(id) { rows = rows.filter(row => row.id !== id); persist(); }
  function subscribe(fn) { listeners.push(fn); return () => { listeners = listeners.filter(x => x !== fn); }; }
  persist();
  return {execute, retry, retryAll, discard, list, listReceipts, subscribe};
}
