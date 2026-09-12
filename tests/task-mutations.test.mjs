import assert from 'node:assert/strict';
import { completeTask } from '../frontend-modules/task-mutations.js';

const calls = [];
const fakeFetch = async (url, options) => {
  calls.push({ url, options });
  return { ok: true, json: async () => ({ success: true }) };
};

const result = await completeTask({ id: 'task-contract-1', projectId: 'project-1' }, fakeFetch);
assert.equal(result.success, true);
assert.equal(calls.length, 1);
assert.equal(calls[0].url, '/api/tasks/update');
assert.equal(calls[0].options.method, 'POST');
assert.ok(calls[0].options.signal instanceof AbortSignal);
const body = JSON.parse(calls[0].options.body);
assert.equal(body.id, 'task-contract-1');
assert.equal(body.projectId, 'project-1');
assert.equal(body.status, 2);

console.log('task completion contract ok');

globalThis.location = { pathname: '/dashboard/' };
calls.length = 0;
await completeTask({ id: 'task-prefix-1', projectId: 'project-1' }, fakeFetch);
assert.equal(calls[0].url, '/dashboard/api/tasks/update');
console.log('reverse-proxy API prefix contract ok');

globalThis.location = { pathname: '/dashboard/index.html' };
calls.length = 0;
await completeTask({ id: 'task-file-1', projectId: 'project-1' }, fakeFetch);
assert.equal(calls[0].url, '/dashboard/api/tasks/update');
console.log('file-entry API prefix contract ok');

const { shadowStatus, reconcileShadow } = await import('../frontend-modules/data-client.js');
calls.length = 0;
await shadowStatus({ fetchImpl: fakeFetch });
assert.equal(calls[0].url, '/dashboard/api/shadow/status?check=1');
console.log('data client API prefix contract ok');
calls.length = 0;
await reconcileShadow({ fetchImpl: fakeFetch });
assert.equal(calls[0].url, '/dashboard/api/shadow/reconcile');
assert.equal(calls[0].options.method, 'POST');
assert.equal(calls[0].options.body, '{}');
console.log('shadow reconcile client contract ok');

const { requestJSON } = await import('../frontend-modules/api-client.js');
calls.length = 0;
await requestJSON('/api/butler/actions', { method: 'GET', fetchImpl: fakeFetch });
assert.equal(calls[0].url, '/dashboard/api/butler/actions');
assert.equal(calls[0].options.headers.Accept, 'application/json');
console.log('shared api client contract ok');

const external = new AbortController();
let transportedSignal;
const pendingFetch = async (_url, options) => {
  transportedSignal = options.signal;
  return await new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }));
};
const cancelled = requestJSON('/api/health?days=7', { fetchImpl: pendingFetch, signal: external.signal }).catch(e => e.name);
external.abort();
assert.equal(await cancelled, 'AbortError');
assert.equal(transportedSignal.aborted, true);
console.log('shared api client external abort contract ok');

const { streamSSE } = await import('../frontend-modules/sse-client.js');
let streamed = '';
const sseFetch = async () => {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode('data: {"delta":"甲"}\n\n'));
      controller.enqueue(encoder.encode('data: {"delta":"乙","done":true}\n\n'));
      controller.close();
    }
  });
  return { ok: true, body };
};
const sseResult = await streamSSE('/api/butler/stream', {}, { fetchImpl: sseFetch, onDelta: (d) => { streamed += d; } });
assert.equal(sseResult.text, '甲乙');
assert.equal(streamed, '甲乙');
assert.equal(sseResult.payload.done, true);
console.log('shared SSE parser contract ok');
