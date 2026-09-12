// 交互测试·第一步：全量可交互元素清单采集（真实浏览器遍历）。
// 遍历 4 个一级入口 + 全局 6 分区 + 主要弹窗，收集全部 button/input/select/textarea/
// [role]/contenteditable/带 onclick 的元素，输出带稳定 selector 的清单 JSON。
// Usage: DASH_URL=http://127.0.0.1:8787 node tests/uitest-inventory.mjs
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

const browser = await chromium.launch({executablePath: EXEC, headless: true, args: ['--no-sandbox']});
const ctx = await browser.newContext({viewport: {width: 412, height: 915}, hasTouch: true, isMobile: true, serviceWorkers: 'block'});
const page = await ctx.newPage();
const inventory = [];

function selectorOf(el) {
  return el.evaluate(e => {
    if (e.id) return '#' + CSS.escape(e.id);
    const view = e.closest('.view')?.id || '';
    const scope = e.closest('.modal-overlay,[role="dialog"],.advisor-ov,.stat-ov,.cmdk-overlay,.mobilenav,.sidebar')?.className || '';
    const cls = (String(e.className).split(' ')[0] || e.tagName.toLowerCase());
    const text = (e.textContent || '').trim().slice(0, 14);
    let s = `${view ? '#' + view + ' ' : ''}${scope ? '.' + String(scope).split(' ')[0] + ' ' : ''}${e.tagName.toLowerCase()}.${cls}`;
    if (text) s += ` "${text}"`;
    return s;
  });
}

async function harvest(where) {
  const found = await page.evaluate(() => {
    const els = [...document.querySelectorAll('button, input, select, textarea, [role="button"], [role="tab"], a[href], [contenteditable="true"], [onclick]')];
    return els.filter(e => {
      if (!e.offsetParent && !e.closest('.mobilenav')) return false; // 不可见跳过（底栏 fixed 特例）
      const r = e.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }).map(e => ({
      tag: e.tagName.toLowerCase(),
      type: e.type || '',
      id: e.id || '',
      dv: e.dataset.view || e.dataset.action || '',
      text: (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 24),
      ph: e.placeholder || '',
      aria: e.getAttribute('aria-label') || '',
      ce: e.getAttribute('contenteditable') || '',
    }));
  });
  for (const el of found) {
    const key = `${where}|${el.tag}|${el.id}|${el.dv}|${el.text}|${el.ph}`;
    if (inventory.some(x => x._key === key)) continue;
    inventory.push({...el, where, _key: key});
  }
}

await page.goto(BASE + '/', {waitUntil: 'load', timeout: 30000});
await page.waitForFunction(() => window.__lifeOSReady === true, null, {timeout: 20000});
await page.waitForTimeout(2500);

const steps = [
  ['今日(默认)', async () => {}],
  ['全局·角色', async () => { await page.click('.mn-item[data-view="global"]'); await page.click('.gseg-tab[data-view="roles"]'); }],
  ['全局·行动', async () => { await page.click('.gseg-tab[data-view="actions"]'); }],
  ['全局·习惯', async () => { await page.click('.gseg-tab[data-view="habits"]'); }],
  ['全局·关系', async () => { await page.click('.gseg-tab[data-view="relations"]'); }],
  ['全局·健康', async () => { await page.click('.gseg-tab[data-view="health"]'); }],
  ['全局·影响圈', async () => { await page.click('.gseg-tab[data-view="proactive"]'); }],
  ['复盘', async () => { await page.click('.mn-item[data-view="review"]'); }],
  ['管家', async () => { await page.click('.mn-item[data-view="butler"]'); }],
];
for (const [name, act] of steps) {
  try { await act(); await page.waitForTimeout(1200); await harvest(name); } catch (e) { console.error('harvest skip', name, String(e).slice(0, 80)); }
}

// 弹窗/覆盖层采集（逐个打开→采集→关闭）
const overlays = [
  ['弹窗·新建角色', async () => { await page.click('.gseg-tab[data-view="roles"]'); await page.waitForTimeout(600); await page.click('#view-roles .add-inline, #view-roles [class*="add"], #view-roles button'); }],
  ['弹窗·新建习惯', async () => { await page.click('.gseg-tab[data-view="habits"]'); await page.waitForTimeout(600); const b = await page.$('#view-habits .add-inline, #view-habits [class*="add"]'); if (b) await b.click(); }],
  ['弹窗·编辑行动', async () => { await page.click('.gseg-tab[data-view="actions"]'); await page.waitForTimeout(600); const t = await page.$('#view-actions .action-item'); if (t) await t.click(); }],
  ['弹窗·明细面板', async () => { await page.click('.mn-item[data-view="today"]'); await page.waitForTimeout(800); await page.click('.band-seg[data-stat="pending"]'); }],
  ['弹窗·命令板', async () => { await page.click('button.cmdk-hint').catch(async () => { await page.evaluate(() => openCmdk()); }); }],
];
for (const [name, act] of overlays) {
  try {
    await act();
    await page.waitForTimeout(800);
    await harvest(name);
    // 关闭：优先取消/✕，再 Escape
    const close = await page.$('.modal-overlay.show .btn-cancel, .sp-x, .cmdk-overlay.show #cmdk-input');
    await page.keyboard.press('Escape');
    await page.waitForTimeout(400);
  } catch (e) { console.error('overlay skip', name, String(e).slice(0, 80)); }
}

fs.writeFileSync('/tmp/uitest-inventory.json', JSON.stringify(inventory, null, 1));
const byWhere = {};
inventory.forEach(x => { byWhere[x.where] = (byWhere[x.where] || 0) + 1; });
console.log('TOTAL', inventory.length, JSON.stringify(byWhere));
await browser.close();
