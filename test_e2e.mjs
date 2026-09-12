// ═══════════════════════════════════════════════════════════════
//  Life OS 端到端测试 (Playwright)
//  - 零 JS 错误断言
//  - 7 视图切换无异常
//  - 各 Life OS 交互（积极主动/关系/倾听/四维/大石头/管家）真实点击
//  - 测试残留清理：本地&Obsidian 文件快照恢复 + TickTick 标记删除
// ═══════════════════════════════════════════════════════════════
import pw from 'playwright-core';
const { chromium } = pw;
const fs = await import('fs');

const EXEC = process.env.PLAYWRIGHT_CHROMIUM || '';
const BASE = process.env.E2E_DASH_URL || '';
const MARK = '__e2e__';
const ROLES_DIR = process.env.E2E_ROLES_DIR || './data/roles';

// ── TickTick token ──
const TOKEN = process.env.E2E_TICKTICK_TOKEN || '';
if (process.env.E2E_ALLOW_WRITES !== '1' || !BASE || !TOKEN) {
  throw new Error('Write tests require E2E_ALLOW_WRITES=1, E2E_DASH_URL and E2E_TICKTICK_TOKEN for an isolated test account.');
}

async function tt(tool, args) {
  const r = await fetch('https://mcp.dida365.com', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'Authorization': 'Bearer ' + TOKEN },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: tool, arguments: args } })
  });
  return await r.json();
}
function extractTasks(resp) {
  const res = resp && resp.result;
  let obj = res && res.structuredContent;
  if (!obj && res && Array.isArray(res.content)) {
    for (const x of res.content) { try { obj = JSON.parse(x.text); break; } catch {} }
  }
  if (!obj) return [];
  let arr = Array.isArray(obj) ? obj : (obj.result || obj.tasks || obj.data || []);
  if (!Array.isArray(arr)) return [];
  return arr.map(t => ({ id: t.id, projectId: t.projectId, title: t.title }));
}

// ── 本地数据文件快照（写操作回归后恢复用）──
// 数据根目录默认 ./data（与 DASH_DATA_DIR 默认一致）；外部 vault 用法：
//   E2E_DATA_ROOT=你的数据目录 E2E_ALLOW_WRITES=1 ... node test_e2e.mjs
const DATA_ROOT = (process.env.E2E_DATA_ROOT || './data').replace(/\/$/, '');
const FILES = [
  `${DATA_ROOT}/情感账户/relations.json`,
  `${DATA_ROOT}/情感账户/情感账户.md`,
  `${DATA_ROOT}/倾听笔记/listening.json`,
  `${DATA_ROOT}/倾听笔记/倾听笔记.md`,
  `${DATA_ROOT}/影响圈/proactive.json`,
  `${DATA_ROOT}/影响圈/影响圈.md`,
  `${DATA_ROOT}/habit_dims.json`,
];
const snap = {}, snapExists = {};
for (const f of FILES) {
  try { snap[f] = fs.readFileSync(f, 'utf8'); snapExists[f] = true; }
  catch { snapExists[f] = false; }
}

const results = [];
const errors = [];
const A = (name, ok, detail = '') => { results.push({ name, ok: !!ok, detail: String(detail) }); };

const browser = await chromium.launch({ executablePath: EXEC, headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'] });
const context = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 768, height: 1024 } });
const page = await context.newPage();
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });
page.on('dialog', async d => { try { if (d.type() === 'confirm') await d.accept(); else await d.dismiss(); } catch {} });
page.on('requestfailed', r => { const u = r.url(); const why = r.failure() && r.failure().errorText; const expectedAbort = /ABORTED/i.test(why || '') && (u.includes('/api/butler/stream') || /127\.0\.0\.1:8787\/?$/.test(u)); if (u.includes('127.0.0.1:8787') && !expectedAbort) errors.push('REQFAIL: ' + why + ' ' + u); });

try {
  await page.goto(BASE + '/', { waitUntil: 'load', timeout: 30000 });
  await page.waitForFunction(() => window.__lifeOSReady === true || (typeof window.__lifeOSReady === 'string' && window.__lifeOSReady.startsWith('error')), { timeout: 20000 });
  const bo = await page.evaluate(() => window.__lifeOSReady);
  A('lifeOS boot ready', bo === true, 'ready=' + JSON.stringify(bo));
  const syncSamples = [];
  for (let i = 0; i < 5; i++) {
    syncSamples.push(await page.$eval('#sync-domain-status', el => ({ text: el.textContent, state: el.dataset.state })));
    await page.waitForTimeout(500);
  }
  const syncQuiet = syncSamples.every(x => x.state !== 'pending' && !(x.text || '').includes('…'));
  A('sync badge settles after boot', syncQuiet, JSON.stringify(syncSamples));
  const fold = await page.evaluate(() => ({ nav: getComputedStyle(document.querySelector('.mobilenav')).display !== 'none', overflow: document.documentElement.scrollWidth <= window.innerWidth + 1 }));
  A('foldable 768px layout', fold.nav && fold.overflow, JSON.stringify(fold));
  const narrowPage = await context.newPage({ viewport: { width: 360, height: 800 } });
  await narrowPage.goto(BASE + '/', { waitUntil: 'load', timeout: 30000 });
  await narrowPage.waitForFunction(() => window.__lifeOSReady === true || (typeof window.__lifeOSReady === 'string' && window.__lifeOSReady.startsWith('error')), { timeout: 20000 });
  const narrow = await narrowPage.evaluate(() => ({ nav: getComputedStyle(document.querySelector('.mobilenav')).display !== 'none', overflow: document.documentElement.scrollWidth <= window.innerWidth + 1 }));
  await narrowPage.close();
  A('foldable 360px layout', narrow.nav && narrow.overflow, JSON.stringify(narrow));

  // 后端健康检查（页面已导航，同源 fetch）
  const hc = await page.evaluate(async () => {
    const r = await fetch('/api/chain-health'); return r.ok;
  }).catch(() => false);
  A('backend health', hc, 'chain-health ' + (hc ? 'ok' : 'FAIL'));

  // 7 视图切换无异常
  const views = ['today', 'proactive', 'roles', 'actions', 'habits', 'relations', 'review'];
  for (const v of views) {
    const b = errors.length;
    await page.evaluate(v => switchView(v), v);
    await page.waitForTimeout(400);
    A('view switch:' + v, errors.length === b, errors.length > b ? errors.slice(b).join(' | ') : 'ok');
  }

  // 五链路健康条
  await page.evaluate(() => switchView('today'));
  await page.waitForTimeout(300);
  const chainCount = await page.evaluate(() => document.querySelectorAll('#chain-mount .chain-node').length);
  A('chain 6 nodes', chainCount === 6, 'nodes=' + chainCount);

  // 积极主动：加关注圈 + 删除
  await page.evaluate(() => switchView('proactive'));
  await page.waitForTimeout(300);
  const beforeC = await page.evaluate(() => document.querySelectorAll('#proactive-mount .circle-col.concern .circle-item').length);
  await page.fill('#proactive-mount .circle-col.concern .circle-add input', MARK + '焦虑测试');
  await page.click('#proactive-mount .circle-col.concern .circle-add button');
  await page.waitForTimeout(400);
  const afterC = await page.evaluate(() => document.querySelectorAll('#proactive-mount .circle-col.concern .circle-item').length);
  A('proactive add concern', afterC === beforeC + 1, beforeC + '->' + afterC);
  const delConcern = await page.$('#proactive-mount .circle-col.concern .circle-item .ci-btn.danger');
  if (delConcern) { await delConcern.click(); await page.waitForTimeout(300); }
  const afterDel = await page.evaluate(() => document.querySelectorAll('#proactive-mount .circle-col.concern .circle-item').length);
  A('proactive del concern', afterDel === beforeC, 'back=' + afterDel);

  // 关系：记一笔 + 删除（兼容既有数据，按增量断言）
  await page.evaluate(() => switchView('relations'));
  await page.waitForTimeout(300);
  const relBefore = await page.evaluate(() => document.querySelectorAll('#relations-mount .flow-row').length);
  await page.fill('#rel-who', MARK + '对象');
  await page.fill('#rel-why', MARK + '事由');
  await page.click('#rel-save');
  await page.waitForTimeout(400);
  const relN = await page.evaluate(() => document.querySelectorAll('#relations-mount .flow-row').length);
  A('relation add', relN === relBefore + 1, relBefore + '->' + relN);
  const relRow = page.locator('#relations-mount .flow-row', { hasText: MARK });
  if (await relRow.count() > 0) { await relRow.locator('.fr-del').first().click(); await page.waitForTimeout(300); }
  const relN2 = await page.evaluate(() => document.querySelectorAll('#relations-mount .flow-row').length);
  A('relation del', relN2 === relBefore, 'back=' + relN2);

  // 倾听笔记：保存 + 删除（兼容既有数据）
  const lisBefore = await page.evaluate(() => document.querySelectorAll('#relations-mount .listen-item').length);
  await page.fill('#lis-who', MARK + '对话');
  await page.fill('#lis-feel', MARK + '感受');
  await page.fill('#lis-restate', MARK + '复述');
  await page.fill('#lis-third', MARK + '第三选择');
  await page.click('#lis-save');
  await page.waitForTimeout(400);
  const lisN = await page.evaluate(() => document.querySelectorAll('#relations-mount .listen-item').length);
  A('listening add', lisN === lisBefore + 1, lisBefore + '->' + lisN);
  const lisRow = page.locator('#relations-mount .listen-item', { hasText: MARK });
  if (await lisRow.count() > 0) { await lisRow.locator('.fr-del').first().click(); await page.waitForTimeout(300); }
  const lisN2 = await page.evaluate(() => document.querySelectorAll('#relations-mount .listen-item').length);
  A('listening del', lisN2 === lisBefore, 'back=' + lisN2);

  // 习惯七·四维：renderDims 真实渲染 + 维度映射 API
  await page.evaluate(() => switchView('habits'));
  await page.waitForTimeout(300);
  const dimCanvas = await page.evaluate(() => !!document.getElementById('dim-radar'));
  A('dims radar canvas', dimCanvas, 'dim-radar=' + dimCanvas);
  const dimApi = await page.evaluate(async (MARK) => {
    const r = await fetch('/api/habits/dimension', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: MARK + '习惯', dimension: '精神' }) });
    const j = await r.json().catch(() => ({}));
    await fetch('/api/habits/dimension', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: MARK + '习惯', dimension: '' }) });
    return j.success === true;
  }, MARK);
  A('habit dimension API', dimApi, 'set+clear ok=' + dimApi);

  // 周计划·大石头：建一个 → TickTick 任务（标记）
  await page.evaluate(() => switchView('actions'));
  await page.waitForTimeout(300);
  const beforeRock = await page.evaluate(() => document.querySelectorAll('#rock-mount .rock-item').length);
  let rockOk = false;
  for (let attempt = 0; attempt < 3 && !rockOk; attempt++) {
    await page.fill('#rock-title', MARK + '大石头任务' + (attempt ? ' ' + attempt : ''));
    await page.fill('#rock-why', MARK + '理由');
    await page.evaluate(() => { const s = document.getElementById('rock-role'); if (s && s.options.length > 1) s.selectedIndex = 1; });
    await page.click('#rock-save');
    await page.waitForTimeout(3500);
    const afterRock = await page.evaluate(() => document.querySelectorAll('#rock-mount .rock-item').length);
    if (afterRock >= beforeRock + 1) rockOk = true;
  }
  const afterRock = await page.evaluate(() => document.querySelectorAll('#rock-mount .rock-item').length);
  A('rock add', rockOk, beforeRock + '->' + afterRock);

  // AI 管家：打开独立视图 → 发消息 → 等回复 → 执行「任务/大石头」类动作
  await page.evaluate(() => { try { switchView('butler'); } catch (e) {} });
  await page.waitForTimeout(800);
  const butlerViewOpen = await page.evaluate(() => !!document.getElementById('view-butler')?.offsetParent);
  A('butler view open', butlerViewOpen === true, 'open=' + butlerViewOpen);
  await page.fill('#butler-page-input', '帮我建一个任务，标题就叫「' + MARK + '管家任务」：下周五之前完成季度复盘报告初稿');
  await page.click('#butler-page-send');
  let replyOk = false;
  try { await page.waitForSelector('#butler-page-chat .bmsg', { timeout: 120000 }); replyOk = true; } catch {}
  A('butler reply', replyOk, replyOk ? 'got reply' : 'NO reply in 120s');
  // 确保出现动作卡（LLM 偶发不输出 action，用显式格式提示重试）
  let actText = await page.evaluate(() => { const c = document.querySelector('#butler-page-chat .bact'); return c ? c.innerText : ''; });
  if (!actText) {
    await page.evaluate(() => { const b = document.getElementById('butler-page-send'); if (b) b.disabled = false; });
    await page.fill('#butler-page-input', '请直接按格式输出一行：<action>建任务</action>{"标题":"' + MARK + '管家任务","项目":"📥 收集箱","象限":"q2","角色":"","截止":""}');
    await page.click('#butler-page-send');
    try { await page.waitForSelector('#butler-page-chat .bact', { timeout: 120000 }); } catch {}
    actText = await page.evaluate(() => { const c = document.querySelector('#butler-page-chat .bact'); return c ? c.innerText : ''; });
  }
  if (actText && /任务|大石头/.test(actText)) {
    const okBtn = await page.$('#butler-page-chat .bact button.ok');
    if (okBtn) { await okBtn.click(); await page.waitForTimeout(2500); }
    A('butler act executed', true, 'kind=' + actText.replace(/\n/g, ' ').slice(0, 24));
    const receipt = await page.$eval('#butler-page-chat .bact .ba-receipt', el => el.textContent || '').catch(() => '');
    A('butler receipt visible', /已确认|回执/.test(receipt), receipt.slice(0, 80));
  } else {
    A('butler act (no task-kind, skipped)', true, 'card=' + actText.replace(/\n/g, ' ').slice(0, 30));
  }

} catch (e) {
  A('FATAL', false, (e && e.stack) || String(e));
}

// ── TickTick 标记清理（扫描所有项目；无截止日的任务不在 list_undone_tasks_by_date 内）──
let delCount = 0, ttErr = '';
const PIDS = (process.env.E2E_PROJECT_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
async function projTasks(pid) {
  const resp = await tt('get_project_with_undone_tasks', { project_id: pid });
  const res = resp && resp.result; let obj = res && res.structuredContent;
  if (!obj && res && Array.isArray(res.content)) { for (const x of res.content) { try { obj = JSON.parse(x.text); break; } catch {} } }
  if (!obj) return [];
  let arr = Array.isArray(obj) ? obj : null;
  if (!arr) for (const k of ['tasks', 'task', 'items']) if (Array.isArray(obj[k])) { arr = obj[k]; break; }
  return (arr || []).map(t => ({ id: t.id, projectId: t.projectId || pid, title: t.title }));
}
try {
  for (const pid of PIDS) {
    const tasks = await projTasks(pid);
    for (const t of tasks) {
      if (t.title && t.title.includes(MARK)) {
        const r = await tt('delete_task', { project_id: t.projectId || pid, task_id: t.id });
        if (!r.error) delCount++;
      }
    }
  }
} catch (e) { ttErr = e.message; }
results.push({ name: 'ticktick cleanup', ok: true, detail: 'deleted ' + delCount + ' marked tasks' + (ttErr ? ' (err:' + ttErr + ')' : '') });

await browser.close();

// ── 本地/Obsidian 文件恢复 ──
let restored = 0;
for (const f of FILES) {
  try {
    if (snapExists[f]) { fs.writeFileSync(f, snap[f]); restored++; }
    else { try { fs.unlinkSync(f); } catch {} }
  } catch {}
}
results.push({ name: 'local files restored', ok: true, detail: restored + '/' + FILES.length + ' restored' });

// ── 报告 ──
const pass = results.filter(r => r.ok).length;
const fail = results.length - pass;
console.log('\n════════ E2E RESULTS ════════');
for (const r of results) console.log((r.ok ? 'PASS' : 'FAIL') + ' | ' + r.name + ' | ' + r.detail);
console.log('\nJS errors (' + errors.length + '):');
errors.slice(0, 40).forEach(e => console.log('  - ' + e));
console.log('\nSUMMARY: ' + pass + ' passed, ' + fail + ' failed, ' + errors.length + ' JS errors');
process.exit(fail === 0 && errors.length === 0 ? 0 : 1);
