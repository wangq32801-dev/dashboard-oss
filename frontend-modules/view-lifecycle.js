// Batch 6: tiny view lifecycle registry for the monolithic SPA.
// It owns cleanup bookkeeping without knowing any business state.  Inline view
// renderers can gradually register timers, observers, streams and chart cleanup
// functions while the legacy fallback remains usable.

export function createViewLifecycle() {
  const views = new Map();
  let current = '';
  let generation = 0;
  const metrics = { transitions: 0, mounts: 0, unmounts: 0, cleanups: 0 };

  function ensure(name) {
    if (!views.has(name)) views.set(name, { mount: null, unmount: null, cleanups: new Set(), mounts: 0, unmounts: 0 });
    return views.get(name);
  }
  function register(name, handlers = {}) {
    const row = ensure(String(name));
    if (typeof handlers.mount === 'function') row.mount = handlers.mount;
    if (typeof handlers.unmount === 'function') row.unmount = handlers.unmount;
    return () => {
      row.mount = null;
      row.unmount = null;
      cleanup(String(name));
    };
  }
  function track(name, disposer) {
    if (typeof disposer !== 'function') return () => {};
    const row = ensure(String(name));
    row.cleanups.add(disposer);
    return () => row.cleanups.delete(disposer);
  }
  function cleanup(name) {
    const row = views.get(String(name));
    if (!row) return;
    for (const disposer of [...row.cleanups]) {
      try { disposer(); } catch (_) { /* cleanup must be best effort */ }
      metrics.cleanups += 1;
      row.cleanups.delete(disposer);
    }
  }
  function transition(name, context = {}) {
    name = String(name || '');
    if (!name || name === current) return generation;
    if (current) {
      const old = ensure(current);
      try { if (old.unmount) old.unmount(context); } catch (_) {}
      cleanup(current);
      old.unmounts += 1;
      metrics.unmounts += 1;
    }
    current = name;
    generation += 1;
    metrics.transitions += 1;
    const next = ensure(name);
    next.mounts += 1;
    metrics.mounts += 1;
    try { if (next.mount) next.mount(context); } catch (_) {}
    return generation;
  }
  function getCurrent() { return current; }
  function snapshot() {
    const registered = {};
    for (const [name, row] of views) registered[name] = { cleanups: row.cleanups.size, mounts: row.mounts, unmounts: row.unmounts };
    return { current, generation, metrics: { ...metrics }, registered };
  }
  return { register, track, cleanup, transition, getCurrent, snapshot };
}
