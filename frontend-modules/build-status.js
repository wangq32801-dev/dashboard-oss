import { shadowStatus, reconcileShadow } from './data-client.js';

let lastStatus = null;
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function renderDetail(status, loading = false) {
  const panel = document.getElementById('shadowDetailPanel');
  if (!panel) return;
  if (loading) { panel.innerHTML = '<h3>数据层对账</h3><div class="sd-muted">正在检查 TickTick…</div>'; return; }
  const summary = status && status.shadow_summary || {};
  const changed = Number(summary.changed || 0), missing = Number(summary.missing || 0), extra = Number(summary.extra || 0);
  const sources = Object.entries(status && status.local_sources || {}).filter(([, ok]) => ok).length;
  const checked = summary.checked_at || status && status.checked_at || '尚无完整对账记录';
  const details = status && status.shadow_details || {};
  const changedRows = (details.changed || []).slice(0, 5).map(x => `<li>${esc(x.id)}${x.fields?.length ? ` <span class="sd-muted">(${esc(x.fields.join(', '))})</span>` : ''}</li>`).join('');
  const missingRows = (details.missing || []).slice(0, 5).map(x => `<li>${esc(x)}</li>`).join('');
  const extraRows = (details.extra || []).slice(0, 5).map(x => `<li>${esc(x)}</li>`).join('');
  const list = changedRows || missingRows || extraRows ? `<div class="sd-list">${changedRows ? `<b>变更条目</b><ul>${changedRows}</ul>` : ''}${missingRows ? `<b>缺失条目</b><ul>${missingRows}</ul>` : ''}${extraRows ? `<b>多出条目</b><ul>${extraRows}</ul>` : ''}</div>` : '<div class="sd-muted">暂无字段级差异；完整对账仍由 shadow_live_check.py 生成。</div>';
  panel.innerHTML = `<h3>数据层对账</h3><div class="sd-grid"><div class="sd-cell"><span class="sd-num">${changed}</span><span class="sd-label">变更</span></div><div class="sd-cell"><span class="sd-num">${missing}</span><span class="sd-label">缺失</span></div><div class="sd-cell"><span class="sd-num">${extra}</span><span class="sd-label">多出</span></div></div><div class="sd-muted">TickTick：${status && status.ticktick_reachable ? '在线' : '不可达'}${status && status.ticktick_tasks != null ? ` · ${status.ticktick_tasks} 个任务` : ''} · 本地源 ${sources} 个</div><div class="sd-muted">检查时间：${esc(checked)}</div>${list}<button type="button" class="sd-refresh">运行完整对账</button>`;
  const button = panel.querySelector('.sd-refresh');
  if (button) button.addEventListener('click', async (event) => {
    event.stopPropagation(); button.disabled = true; button.textContent = '对账中…';
    try { const result = await reconcileShadow(); if (result) lastStatus = result; renderDetail(lastStatus || {}); }
    catch (error) { button.disabled = false; button.textContent = '重新对账'; panel.insertAdjacentHTML('afterbegin', `<div class="sd-muted">${esc(error.message || '对账失败')}</div>`); }
  });
}

async function showDetail() {
  const panel = document.getElementById('shadowDetailPanel');
  if (!panel) return;
  panel.classList.toggle('show');
  if (!panel.classList.contains('show')) return;
  if (lastStatus) renderDetail(lastStatus);
  else await refreshBuildStatus(true);
}

export async function refreshBuildStatus() {
  const el = document.getElementById('shadowStatus');
  if (!el) return;
  try {
    const status = await shadowStatus();
    lastStatus = status;
    const summary = status && status.shadow_summary;
    const changed = Number(summary && summary.changed || 0);
    const missing = Number(summary && summary.missing || 0);
    const extra = Number(summary && summary.extra || 0);
    const drift = changed + missing + extra;
    const checked = (summary && (summary.checked_at || summary.revision)) || status.checked_at || '';
    if (checked) el.title = `影子对账：变更 ${changed} · 缺失 ${missing} · 多出 ${extra} · ${checked}`;
    el.setAttribute('aria-label', drift
      ? `数据层在线，有差异：变更 ${changed}、缺失 ${missing}、多出 ${extra}`
      : '数据层在线，影子对账无差异');
    el.textContent = status && status.ticktick_reachable
      ? `· 数据层在线${status.ticktick_tasks != null ? ` · ${status.ticktick_tasks}任务` : ''}${drift ? ` · 差异 ${drift}` : ' · 无差异'}`
      : '· 数据层待检查';
    el.closest('.codex-build-badge')?.setAttribute('title', '点击查看数据层对账详情');
  } catch (_) { el.textContent = '· 数据层离线'; }
}

export function initBuildStatus() {
  const badge = document.querySelector('.codex-build-badge');
  if (!badge || badge.dataset.shadowBound) return;
  // 同步中心已接管同一徽标和详情面板；这里只更新影子状态文字，避免双监听一次点击开了又关。
  if (window.__syncUIReady) return;
  badge.dataset.shadowBound = '1';
  badge.setAttribute('role', 'button'); badge.setAttribute('tabindex', '0');
  badge.addEventListener('click', (event) => { event.stopPropagation(); showDetail(); });
  badge.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); showDetail(); } });
  document.addEventListener('click', (event) => { const panel = document.getElementById('shadowDetailPanel'); if (panel && !panel.contains(event.target) && event.target !== badge) panel.classList.remove('show'); });
}
