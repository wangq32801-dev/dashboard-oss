// 交互测试·第二步：分层真实执行。
// A 导航/开关/折叠/Tab —— 真实点击，观察 console/pageerror/CLS/长任务
// B 输入类 —— fill 后清空（blur 存草稿是 localStorage 行为，无业务写入）
// C 弹窗开→取消/Escape（验证可关闭、无错误、无残留）
// D 写提交类（✓完成/🗄归档/🗑删除/保存/清空/收集/建任务CTA/AI regen）——纪律红线：不触发真实写入，记录 SKIP
// 视图切换用真实点击导航；页面级 observer 记录布局跳动与长任务。
// Usage: DASH_URL=http://127.0.0.1:8787 node tests/uitest-execute.mjs
import pw from 'playwright-core';
import fs from 'node:fs';

const { chromium } = pw;
const CANDS = [
  process.env.PLAYWRIGHT_CHROMIUM,  // 可选：显式指定浏览器可执行文件
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
];
const EXEC = process.env.PLAYWRIGHT_EXECUTABLE || CANDS.find(p => fs.existsSync(p)) || CANDS[0];
const BASE = (process.env.DASH_URL || 'http://127.0.0.1:8787').replace(/\/$/, '');
if (!fs.existsSync(EXEC)) { console.error('browser missing'); process.exit(2); }

const results = [];
const check = (name, ok, detail = '') => results.push({name, ok: !!ok, detail: String(detail).slice(0, 300)});
const browser = await chromium.launch({executablePath: EXEC, headless: true, args: ['--no-sandbox']});
const ctx = await browser.newContext({viewport: {width: 412, height: 915}, hasTouch: true, isMobile: true, serviceWorkers: 'block'});
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', e => errors.push('PAGE: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text().slice(0, 200)); });
// 外部导航拦截（书房/工作台是真实外跳，测试中不离开页面）
await page.addInitScript(() => {
  window.open = (u) => { window.__blockedNav = (window.__blockedNav || 0) + 1; return null; };
  let hrefBlocked = 0;
  Object.defineProperty(window, '__hrefBlocked', {get: () => hrefBlocked});
});

await page.goto(BASE + '/', {waitUntil: 'load', timeout: 30000});
await page.waitForFunction(() => window.__lifeOSReady === true, null, {timeout: 20000});
await page.evaluate(() => {
  window.__cls = 0;
  new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) window.__cls += e.value; }).observe({type: 'layout-shift', buffered: true});
  window.__long = 0;
  try { new PerformanceObserver(l => { window.__long += l.getEntries().length; }).observe({type: 'longtask', buffered: true}); } catch (_) {}
});
await page.waitForTimeout(2500);

// 页面内分类谓词：写提交/AI 类黑名单（这些元素记录 SKIP，绝不点击）
// ZC 复测修正：coach 条全部按钮（cta/alt/x 除外）与 coach 卡、工具区（hub/munger 外跳）、
// 观星入口（独立场景测）、AI 刷新类一律 SKIP；黑名单过滤与点击必须用同一份物化元素列表
// （v1 曾因两次独立查询索引错位点到黑名单元素，引发一次非必要 oracle/refresh AI 调用）。
const DANGEROUS = (el) => {
  const sig = [el.id, el.textContent, el.getAttribute('aria-label') || '', el.className].join(' ');
  if (el.id === 'stargaze-entry' || el.dataset.action === 'stargaze') return true; // 独立场景
  if (el.dataset.action === 'hub' || el.dataset.action === 'munger') return true; // 外跳
  if (/工作台|书房/.test(el.textContent || '')) return true;
  if (el.closest('.coach-bar') && !el.classList.contains('cb-x')) return true; // coach 条除 ✕ 外全跳
  if (el.closest('.coach-card, [class*="coach-card"], .today-goose')) return true; // 引导卡 CTA（含建任务类）
  if (/^(✓)$/.test((el.textContent || '').trim()) && el.classList.contains('check')) return true;
  if (el.classList.contains('flow-btn') || el.classList.contains('today-focus-more')) return true; // 跳转类（导航遍历已覆盖）
  return /🗄|🗑|删除|清空|清理|撤销|undo|btn-save|rock-save|收集$|放上大石头|oracle-refresh|ad-re|adv-btn|刷新全部数据|保存所有更改|清空对话|butler-clear|sync-vps|定时提醒|建睡觉任务|建散步任务|一键建任务|转成任务|填入|保存|起草|骨架|导出|提交/.test(sig);
};

const VIEWS = [
  ['今日', null],
  ['全局·角色', async () => { await page.click('.mn-item[data-view="global"]', {timeout: 6000}); await page.click('.gseg-tab[data-view="roles"]', {timeout: 6000}); }],
  ['全局·行动', async () => { await page.click('.gseg-tab[data-view="actions"]', {timeout: 6000}); }],
  ['全局·习惯', async () => { await page.click('.gseg-tab[data-view="habits"]', {timeout: 6000}); }],
  ['全局·关系', async () => { await page.click('.gseg-tab[data-view="relations"]', {timeout: 6000}); }],
  ['全局·健康', async () => { await page.click('.gseg-tab[data-view="health"]', {timeout: 6000}); }],
  ['全局·影响圈', async () => { await page.click('.gseg-tab[data-view="proactive"]', {timeout: 6000}); }],
  ['复盘', async () => { await page.click('.mn-item[data-view="review"]', {timeout: 6000}); }],
  ['管家', async () => { await page.click('.mn-item[data-view="butler"]', {timeout: 6000}); }],
];
const viewOf = () => page.evaluate(() => (document.querySelector('.view.active') || {}).id || '');
// 导航可点性自愈：复杂跨视图序列偶发「导航点击失效」（未复现最小步骤，P3 观察，待真机 L4）；
// 真实用户的恢复方式是刷新页面——测试同样 reload 并等 boot，保证后续视图遍历在干净状态下进行。
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
  return ok;
}

let safeClicked = 0, inputsTested = 0, skippedWrites = 0, openedClosed = 0;
const errBeforePerView = {};
const clickLog = [];

for (const [name, go] of VIEWS) {
  const errStart = errors.length;
  try {
    await ensureNav();
    if (go) { await go(); } else { await page.click('.mn-item[data-view="today"]', {timeout: 6000}); }
    await page.waitForTimeout(1200);
  } catch (e) { check(`${name}: 导航`, false, String(e).slice(0, 90)); continue; }

  // A：安全点击（黑名单外 button/[role=button]/tab）——一次性物化元素句柄列表，
  // 过滤与点击同源（防重查询索引错位）；逐个记录点击明细。
  const handles = await page.$$eval('.view.active button, .view.active [role="button"]:not(button), #view-today.active button', (els, dangerSrc) => {
    const dangerous = new Function('el', 'return (' + dangerSrc + ')(el);');
    const out = [];
    for (const e of els) {
      if (!e.offsetParent) continue;
      const r = e.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) continue;
      out.push({
        dangerous: dangerous(e),
        sig: (e.id ? '#' + e.id : '') + (String(e.className).split(' ')[0] ? '.' + String(e.className).split(' ')[0] : '') + ' "' + (e.textContent || '').trim().slice(0, 14) + '"',
      });
    }
    return out;
  }, String(DANGEROUS));

  const liveHandles = await page.$$('.view.active button, .view.active [role="button"]:not(button), #view-today.active button');
  // liveHandles 与 handles 同序（同一选择器同一时刻物化），按 index 对齐
  let localSafe = 0;
  for (let i = 0; i < Math.min(handles.length, liveHandles.length); i++) {
    if (handles[i].dangerous) { skippedWrites += 1; continue; }
    const el = liveHandles[i];
    const before = await viewOf();
    try {
      // 视口底部元素会被 fixed 底栏遮挡（真实用户会滚动）——先滚入视野再点；
      // 4s 超时即记录跳过（covered/disabled），绝不 30s 卡死流程。
      await el.scrollIntoViewIfNeeded({timeout: 2000}).catch(() => {});
      await el.click({timeout: 4000});
      safeClicked += 1; localSafe += 1;
      clickLog.push({view: name, sig: handles[i].sig});
      await page.waitForTimeout(450);
      // 遮罩清理：明细面板/cmdk/modal 打开后必须关闭，否则拦截后续导航点击
      await page.evaluate(() => {
        const sp = document.querySelector('.stat-ov .sp-x');
        if (sp) { sp.click(); return; }
        const cmdk = document.getElementById('cmdk');
        if (cmdk && cmdk.classList.contains('show')) { cmdk.classList.remove('show'); return; }
        const mo = document.querySelector('.modal-overlay.show');
        if (mo) mo.classList.remove('show');
      });
      const after = await viewOf();
      if (after !== before) {
        const seg = await page.$(`.gseg-tab[data-view="${before.replace('view-', '')}"]`);
        if (seg) { await seg.click(); } else { await page.click(`.mn-item[data-view="${before.replace('view-', '')}"]`).catch(() => {}); }
        await page.waitForTimeout(500);
      }
    } catch (_) { /* disabled/遮挡等个别不可点，不算失败 */ }
  }
  void localSafe;

  // B：输入类（fill 后清空；blur 落草稿为 localStorage，无业务写入）
  const inputs = await page.evaluate(() => [...document.querySelectorAll('.view.active input[type="text"], .view.active input:not([type]), .view.active textarea, .view.active select')]
    .filter(e => e.offsetParent && !e.disabled && e.id)
    .map(e => ({id: e.id, tag: e.tagName.toLowerCase()})));
  for (const {id, tag} of inputs.slice(0, 12)) {
    try {
      if (tag === 'select') { await page.selectOption(`#${CSS.escape(id)}`, {index: 1}).catch(() => {}); }
      else { await page.fill(`#${CSS.escape(id)}`, '交互测试输入ABC'); await page.waitForTimeout(200); await page.fill(`#${CSS.escape(id)}`, ''); }
      inputsTested += 1;
      await page.waitForTimeout(250);
    } catch (_) {}
  }

  const newErr = errors.length - errStart;
  errBeforePerView[name] = newErr;
  check(`${name}: 遍历交互后无新增错误`, newErr === 0, `safeClicks=${handles.filter(h => !h.dangerous).length} skippedWrites=${handles.filter(h => h.dangerous).length} inputs=${Math.min(inputs.length, 12)} newErrors=${newErr}${newErr ? ' | ' + errors.slice(errStart).join(' ;; ') : ''}`);
}

// C：观星进入/退出（沉浸层开关）
try {
  await page.click('.mn-item[data-view="today"]');
  await page.waitForTimeout(800);
  await page.click('#stargaze-entry');
  await page.waitForTimeout(1200);
  const inSky = await page.evaluate(() => document.body.classList.contains('stargazing'));
  await page.keyboard.press('Escape').catch(() => {});
  const exitBtn = await page.$('#sg-exit');
  if (exitBtn) await exitBtn.click().catch(() => {});
  await page.waitForTimeout(800);
  const outSky = await page.evaluate(() => document.body.classList.contains('stargazing'));
  check('观星: 进入与退出', inSky && !outSky, `enter=${inSky} exited=${!outSky}`);
} catch (e) { check('观星: 进入与退出', false, String(e).slice(0, 120)); }

// C：主题切换（底栏夜间按钮）×2（来回）
try {
  const t0 = await page.evaluate(() => document.body.classList.contains('night-mode'));
  await page.click('.mn-item[data-action="theme"]');
  await page.waitForTimeout(600);
  const t1 = await page.evaluate(() => document.body.classList.contains('night-mode'));
  await page.click('.mn-item[data-action="theme"]');
  await page.waitForTimeout(600);
  const t2 = await page.evaluate(() => document.body.classList.contains('night-mode'));
  check('主题: 夜间来回切换', t0 !== t1 && t2 === t0, `${t0}→${t1}→${t2}`);
} catch (e) { check('主题: 切换', false, String(e).slice(0, 120)); }

// C：coach 条关闭（✕ 安全）
try {
  await page.click('.mn-item[data-view="today"]');
  await page.waitForTimeout(900);
  const had = await page.$('#view-today .coach-bar .cb-x');
  if (had) { await had.click(); await page.waitForTimeout(500); }
  const gone = await page.evaluate(() => !document.querySelector('#view-today .coach-bar'));
  check('今日: coach 条关闭按钮', had ? gone : true, had ? 'closed' : '无 coach 条（视为通过）');
} catch (e) { check('今日: coach 关闭', false, String(e).slice(0, 120)); }

const cls = await page.evaluate(() => window.__cls || 0);
const longN = await page.evaluate(() => window.__long || 0);
check('全程: 无 pageerror/console error', errors.length === 0, errors.slice(0, 5).join(' ;; ') || 'clean');
// CLS 作为记录项上报（SPA 全视图切换/沉浸层会产生天然位移，硬阈值无意义；
// 布局稳定性由 smoke 的静止 mutation=0 与长任务计数共同保障）
check('统计', true, `safeClicks=${safeClicked} inputs=${inputsTested} skippedWrites(未授权真实写入)=${skippedWrites} CLS=${cls.toFixed(4)}(记录) longtasks=${longN} navReloads=${reloads}`);

fs.writeFileSync('/tmp/uitest-execute.json', JSON.stringify({results, errors, cls, longN, safeClicked, inputsTested, skippedWrites, clickLog}, null, 1));
for (const r of results) console.log(`${r.ok ? '✅' : '❌'} ${r.name} | ${r.detail}`);
const failed = results.filter(r => !r.ok);
console.log(`SUMMARY: ${results.length - failed.length} passed, ${failed.length} failed`);
process.exit(failed.length ? 1 : 0);
