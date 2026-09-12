// Batch 7: shared modal semantics for the single-page dashboard.
// The legacy UI opens several overlays by toggling classes or inserting nodes.
// This small boundary makes those paths keyboard-safe without owning business state.

const DEFAULT_SELECTOR = [
  '.modal-overlay.show',
  '.role-detail-overlay.show',
  '.cmdk-overlay.show',
  '.stat-ov',
  '.advisor-ov',
  '.butler-drawer.open',
  '.shadow-detail-panel.show',
].join(',');

const FOCUSABLE = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function isVisible(node, view) {
  if (!node || !node.isConnected) return false;
  if (node.hidden || node.getAttribute('aria-hidden') === 'true') return false;
  const style = view && view.getComputedStyle ? view.getComputedStyle(node) : null;
  if (!style) return true;
  // Keep opacity-animated overlays eligible: their first frame can be 0 while
  // they are already interactive and need the initial focus.
  return style.display !== 'none' && style.visibility !== 'hidden';
}

function focusables(root, view) {
  return [...root.querySelectorAll(FOCUSABLE)].filter((node) => isVisible(node, view));
}

export function createDialogA11y({ documentRef = document } = {}) {
  const body = documentRef.body;
  if (!body) return { scan() {}, destroy() {} };

  let active = null;
  let restore = null;
  let observer = null;
  let scanQueued = false;
  const enqueue = typeof queueMicrotask === 'function' ? queueMicrotask : (fn) => Promise.resolve().then(fn);

  function candidates() {
    return [...documentRef.querySelectorAll(DEFAULT_SELECTOR)].filter((node) => isVisible(node, documentRef.defaultView));
  }

  function labelFor(root) {
    const label = root.querySelector('[data-dialog-title],h1,h2,h3,.ad-title,.sp-head,.bh-title');
    if (!label) return null;
    if (!label.id) label.id = `dialog-title-${Math.random().toString(36).slice(2, 9)}`;
    return label.id;
  }

  function activate(root) {
    active = root;
    restore = documentRef.activeElement && documentRef.activeElement !== documentRef.body
      ? documentRef.activeElement
      : null;
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('tabindex', '-1');
    const labelledBy = root.getAttribute('aria-labelledby') || labelFor(root);
    if (labelledBy) {
      root.setAttribute('aria-labelledby', labelledBy);
    } else if (!root.getAttribute('aria-label')) {
      root.setAttribute('aria-label', '对话框');
    }
    root.dataset.dialogA11y = 'active';
    body.classList.add('modal-open');
    enqueue(() => {
      if (active !== root || !root.isConnected) return;
      const first = focusables(root, documentRef.defaultView)[0];
      (first || root).focus({ preventScroll: true });
    });
  }

  function deactivate(root) {
    if (!root) return;
    if (root.dataset.dialogA11y === 'active') delete root.dataset.dialogA11y;
    if (restore && restore.isConnected && typeof restore.focus === 'function') {
      try { restore.focus({ preventScroll: true }); } catch (_) { /* best effort */ }
    }
    active = null;
    restore = null;
    body.classList.remove('modal-open');
  }

  function closeIfStillOpen(root) {
    if (active !== root) return;
    if (root.classList.contains('modal-overlay') || root.classList.contains('role-detail-overlay') || root.classList.contains('cmdk-overlay')) {
      root.classList.remove('show');
    } else if (root.classList.contains('butler-drawer')) {
      if (typeof window.butlerToggle === 'function') window.butlerToggle(false);
      else root.classList.remove('open');
    } else {
      root.remove();
    }
    queueScan();
  }

  function scan() {
    scanQueued = false;
    const list = candidates();
    const next = list[list.length - 1] || null;
    if (next === active) return;
    if (active) deactivate(active);
    if (next) activate(next);
  }

  function queueScan() {
    if (scanQueued) return;
    scanQueued = true;
    enqueue(scan);
  }

  function onKeydown(event) {
    if (!active) return;
    if (event.key === 'Escape') {
      // Existing overlays keep their own close handlers. If none handled it,
      // close the topmost one on the next turn so ESC is never a dead end.
      const root = active;
      setTimeout(() => closeIfStillOpen(root), 0);
      return;
    }
    if (event.key !== 'Tab') return;
    const nodes = focusables(active, documentRef.defaultView);
    if (!nodes.length) {
      event.preventDefault();
      active.focus({ preventScroll: true });
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && documentRef.activeElement === first) {
      event.preventDefault();
      last.focus({ preventScroll: true });
    } else if (!event.shiftKey && documentRef.activeElement === last) {
      event.preventDefault();
      first.focus({ preventScroll: true });
    }
  }

  observer = new MutationObserver(queueScan);
  observer.observe(body, { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'hidden', 'aria-hidden'] });
  documentRef.addEventListener('keydown', onKeydown, true);
  scan();

  function destroy() {
    if (observer) observer.disconnect();
    documentRef.removeEventListener('keydown', onKeydown, true);
    if (active) deactivate(active);
    active = null;
  }

  return { scan, destroy, getActive: () => active };
}
