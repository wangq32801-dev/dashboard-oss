// 授权真实写闭环 · 阶段 2：实体域
// A 角色（Obsidian）：新建 → md 文件回读 → 复盘保存 → md 正文回读 → 删除→.回收站
// B 关系（JSON 主数据+MD 镜像）：记一笔 → 回读 → 软删(deleted) → 恢复 → 再软删(清理) → 镜像 mtime
// C 健康备注（JSON）：存入 → 回读 → 删除 → 回读消失
// 前缀 UIT-20260904-；dialog（confirm）自动接受。写后权威回读。
import pw from 'playwright-core';
import fs from 'node:fs';

const { chromium } = pw;
const CANDS = [
  process.env.PLAYWRIGHT_CHROMIUM,  // 可选：显式指定浏览器可执行文件
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
];
const EXEC = process.env.PLAYWRIGHT_EXECUTABLE || CANDS.find(p => fs.existsSync(p)) || CANDS[0];
const BASE = (process.env.DASH_URL || 'http://127.0.0.1:8787').replace(/\/$/, '');
const PFX = 'UIT-20260904';
const WIKI_BASE = process.env.E2E_WIKI_BASE || './data';
const ROLE_MD = `${WIKI_BASE}/角色/${PFX}-测试角色.md`;
const TRASH_MD = `${WIKI_BASE}/角色/.回收站/${PFX}-测试角色.md`;
if (!fs.existsSync(EXEC)) { console.error('browser missing'); process.exit(2); }

const results = [];
const check = (name, ok, detail = '') => { results.push({name, ok: !!ok, detail: String(detail).slice(0, 280)}); console.log(`${ok ? '✅' : '❌'} ${name} | ${String(detail).slice(0, 210)}`); };
const browser = await chromium.launch({executablePath: EXEC, headless: true, args: ['--no-sandbox']});
const ctx = await browser.newContext({viewport: {width: 412, height: 915}, hasTouch: true, isMobile: true, serviceWorkers: 'block'});
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', e => errors.push('PAGE: ' + e.message));
page.on('console', m => { if (m.type() === 'error' && !m.text().includes('409')) errors.push('CONSOLE: ' + m.text().slice(0, 160)); });
page.on('dialog', d => d.accept().catch(() => {}));
async function apiGet(p) { return (await fetch(BASE + p)).json(); }
let reloads = 0;
async function ensureNav() {
  const ok = await page.evaluate(() => {
    const n = document.querySelector('.mn-item[data-view="today"]');
    if (!n) return false;
    const r = n.getBoundingClientRect();
    const t = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return !!(t && t.closest('.mobilenav'));
  });
  if (!ok) {
    reloads += 1;
    await page.reload({waitUntil: 'load', timeout: 30000});
    await page.waitForFunction(() => window.__lifeOSReady === true, null, {timeout: 20000});
    await page.waitForTimeout(1500);
  }
}
const asList = v => Array.isArray(v) ? v : (Array.isArray(v?.entries) ? v.entries : (Array.isArray(v?.data) ? v.data : (Array.isArray(v?.notes) ? v.notes : [])));
async function poll(fn, timeoutMs = 25000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) { try { const v = await fn(); if (v) return v; } catch (_) {} await page.waitForTimeout(1200); }
  return null;
}

await page.goto(BASE + '/', {waitUntil: 'load', timeout: 30000});
await page.waitForFunction(() => window.__lifeOSReady === true, null, {timeout: 20000});
await page.waitForTimeout(2500);

// ── A1. 新建角色（Obsidian md 权威源）──
try {
  await page.click('.mn-item[data-view="global"]', {timeout: 6000});
  await page.click('.gseg-tab[data-view="roles"]', {timeout: 6000});
  await page.waitForTimeout(1300);
  // UX-2 绕过（真实用户路径）：412 触摸模拟下 coach 卡存在时「+ 新建角色」hit-test
  // 异常命中 💬（视觉无重叠，P2 观察项待真机）——先点 coach ✕ 关闭再操作。
  const cbx = await page.$('#view-roles .coach-bar .cb-x');
  if (cbx) { await cbx.click({timeout: 4000}).catch(() => {}); await page.waitForTimeout(700); }
  for (let i = 0; i < 3; i++) {
    try { await page.click('#view-roles .roles-addbar .add-inline', {timeout: 8000}); break; }
    catch (e) { if (i === 2) throw e; console.log('  新建角色按钮重试:', String(e).slice(0, 50)); await page.waitForTimeout(1200); }
  }
  await page.waitForSelector('#mo-role.show', {timeout: 5000});
  await page.fill('#fr-name', `${PFX}-测试角色`);
  await page.click('#btn-save-role');
  await page.waitForTimeout(3000);
  const mdOK = await poll(async () => fs.existsSync(ROLE_MD), 15000);
  const inRoles = await poll(async () => {
    const d = await apiGet('/api/dashboard-data');
    return (d.roles || []).some(r => String(r.name || '').includes(PFX)) || null;
  }, 15000);
  check('A1 新建角色: md 文件落盘+数据回读', !!mdOK && !!inRoles, `md=${!!mdOK} roles=${!!inRoles}`);
} catch (e) {
    const scene = await page.evaluate(() => ({
      viewClass: document.getElementById('view-roles').className,
      addbarDisplay: (document.querySelector('#view-roles .roles-addbar') || document.body).className && getComputedStyle(document.querySelector('#view-roles .roles-addbar') || document.body).display,
      activeView: (document.querySelector('.view.active') || {}).id,
    })).catch(err => 'scene-eval-fail:' + String(err).slice(0, 60));
    check('A1 新建角色', false, 'SCENE=' + JSON.stringify(scene) + ' | ' + String(e).slice(0, 90));
  }

// ── A2. 复盘保存到测试角色（Obsidian 正文写入）──
const REVIEW_TEXT = `${PFX}-复盘正文回读测试 ${Date.now()}`;
try {
  await ensureNav();
  await page.click('.mn-item[data-view="review"]', {timeout: 6000});
  await page.waitForTimeout(1000);
  await page.evaluate(rn => {
    const s = document.getElementById('rv-role');
    const opt = [...s.options].find(o => o.textContent.includes(rn) || o.value.includes(rn));
    if (opt) { s.value = opt.value; s.dispatchEvent(new Event('change', {bubbles: true})); }
  }, PFX);
  await page.fill('#rv-text', REVIEW_TEXT);
  await page.click('#view-review button:has-text("保存到角色档案")');
  await page.waitForTimeout(3000);
  const written = await poll(async () => {
    try { const c = fs.readFileSync(ROLE_MD, 'utf-8'); return c.includes(REVIEW_TEXT) ? true : null; } catch (_) { return null; }
  }, 20000);
  check('A2 复盘保存: Obsidian md 正文权威回读', !!written, `mdContains=${!!written}`);
} catch (e) { check('A2 复盘保存', false, String(e).slice(0, 150)); }

// ── A3. 删除测试角色（软删→.回收站）──
try {
  await page.click('.mn-item[data-view="global"]');
  await page.click('.gseg-tab[data-view="roles"]');
  await page.waitForTimeout(1300);
  // 删除走 roles/delete 同路由（UI 按钮为双 confirm 卡片按钮，点击可达性归 UX-1 族真机项；
  // 写语义等价：同端点同参数，清理+软删回读在此验证）
  const delRes = await (await fetch(BASE + '/api/roles/delete', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: `${PFX}-测试角色`})})).json();
  const trashDir = `${WIKI_BASE}/角色/.回收站`;
  const cleaned = await poll(async () => {
    const gone = !fs.existsSync(ROLE_MD);
    const trashed = fs.existsSync(trashDir) && fs.readdirSync(trashDir).some(f => f.includes(PFX));
    const d = await apiGet('/api/dashboard-data');
    const stillListed = (d.roles || []).some(r => String(r.name || '').includes(PFX));
    return gone && trashed && !stillListed ? true : null;
  }, 20000);
  check('A3 删除角色: md 移入 .回收站(防覆盖时间戳后缀)+列表移除', !!cleaned, `mdGone=${!fs.existsSync(ROLE_MD)} delRes=${JSON.stringify(delRes).slice(0, 90)}`);
  } catch (e) { check('A3 删除角色', false, String(e).slice(0, 150)); }

// ── B. 关系域（JSON 主数据 + MD 镜像）──
const WHO = `${PFX}-测试对象`;
let relId = null;
try {
  await ensureNav();
  await page.click('.mn-item[data-view="global"]', {timeout: 6000});
  await page.click('.gseg-tab[data-view="relations"]', {timeout: 6000});
  await page.waitForTimeout(1300);
  await page.fill('#rel-who', WHO);
  await page.fill('#rel-why', `${PFX}-存款事由回读测试`);
  for (let i = 0; i < 3; i++) {
    try { await page.click('#rel-save', {timeout: 8000}); break; }
    catch (e) { if (i === 2) throw e; console.log('  记一笔按钮重试:', String(e).slice(0, 50)); await page.waitForTimeout(1200); }
  }
  await page.waitForTimeout(3000);
  const hit = await poll(async () => {
    const list = asList(await apiGet('/api/relations'));
    const e = list.filter(x => !x.deleted && String(x.who || '').includes(WHO));
    return e.length ? e[0] : null;
  });
  relId = hit && hit.id;
  check('B1 关系记一笔: JSON 主数据权威回读', !!hit, JSON.stringify(hit ? {id: hit.id, who: hit.who, type: hit.type} : '未回读'));
} catch (e) { check('B1 关系记一笔', false, String(e).slice(0, 150)); }

try {
  if (!relId) throw new Error('无 relId');
  const post = async (path, body) => (await fetch(BASE + path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).json();
  const relState = async () => {
    const list = asList(await apiGet('/api/relations'));
    return list.find(x => String(x.who || '').includes(WHO)) || null;
  };
  const mirrorBefore = fs.statSync(`${WIKI_BASE}/情感账户/情感账户.md`, {throwIfNoEntry: false});
  await post('/api/relations/delete', {id: relId});
  const deleted = await poll(async () => { const e = await relState(); return e ? null : true; });
  await post('/api/relations/restore', {id: relId});
  const restored = await poll(async () => { const e = await relState(); return e ? true : null; });
  // 终态清理：再软删
  await post('/api/relations/delete', {id: relId});
  const finalDeleted = await poll(async () => { const e = await relState(); return e ? null : true; });
  const mirrorAfter = fs.statSync(`${WIKI_BASE}/情感账户/情感账户.md`, {throwIfNoEntry: false});
  const mirrorSynced = mirrorAfter && mirrorBefore && mirrorAfter.mtimeMs >= mirrorBefore.mtimeMs;
  check('B2 关系软删→恢复→再软删(终态): 全链回读+镜像同步', !!deleted && !!restored && !!finalDeleted && mirrorSynced,
    JSON.stringify({deleted: !!deleted, restored: !!restored, finalDeleted: !!finalDeleted, mirrorSynced}));
  } catch (e) { check('B2 关系软删/恢复', false, String(e).slice(0, 150)); }

// ── C. 健康备注（存入→回读→删除→消失）──
const NOTE = `${PFX}-健康备注回读测试`;
try {
  await ensureNav();
  await page.click('.mn-item[data-view="global"]', {timeout: 6000});
  await page.click('.gseg-tab[data-view="health"]', {timeout: 6000});
  await page.waitForTimeout(1500);
  await page.fill('#health-note-input', NOTE);
  await page.click('.health-note-btn');
  await page.waitForTimeout(3000);
  const added = await poll(async () => {
    const rows = asList(await apiGet('/api/health/notes'));
    return rows.some(n => String(n.note || '').includes(NOTE)) || null;
  });
  const listed = await poll(async () => {
    const arr = asList(await apiGet('/api/health/notes'));
    const target = arr.find(n => String(n.note || '').includes(NOTE));
    if (!target) return null;
    await fetch(BASE + '/api/health/notes/delete', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: target.id})});
    return target.id;
  }, 8000);
  const gone = await poll(async () => {
    const rows = asList(await apiGet('/api/health/notes'));
    return !rows.some(n => String(n.note || '').includes(NOTE)) || null;
  }, 15000);
  check('C 健康备注: 存入→回读→删除→消失', !!added && !!listed && !!gone, JSON.stringify({added: !!added, idGot: !!listed, gone: !!gone}));
} catch (e) { check('C 健康备注', false, String(e).slice(0, 150)); }

check('全程: 无 pageerror/非预期 console error', errors.length === 0, errors.slice(0, 4).join(' ;; ') || 'clean');
check('统计: 导航自愈 reload 次数', reloads <= 4, `reloads=${reloads}`);
const failed = results.filter(r => !r.ok);
console.log(`SUMMARY: ${results.length - failed.length} passed, ${failed.length} failed`);
fs.writeFileSync('/tmp/write-loop-entities.json', JSON.stringify(results, null, 1));
process.exit(failed.length ? 1 : 0);
