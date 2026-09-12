import assert from 'node:assert/strict';
import { createViewLifecycle } from '../frontend-modules/view-lifecycle.js';

const life = createViewLifecycle();
let cleaned = 0;
for (const name of ['today', 'health', 'actions', 'roles', 'habits', 'review']) {
  life.register(name);
  life.track(name, () => { cleaned += 1; });
}
const names = ['today', 'health', 'actions', 'roles', 'habits', 'review'];
for (let i = 0; i < 50; i += 1) life.transition(names[i % names.length]);
const snapshot = life.snapshot();
assert.equal(50, snapshot.metrics.transitions);
assert.equal(49, snapshot.metrics.unmounts);
assert.equal(0, snapshot.registered.health.cleanups);
assert.equal(0, snapshot.registered.today.cleanups);
assert.equal(6, cleaned);
console.log('view lifecycle 50-switch contract ok');
