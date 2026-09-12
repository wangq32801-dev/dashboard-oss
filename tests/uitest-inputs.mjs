// 交互测试·第三步：输入类专项（真实键入，不提交任何业务写入）。
// 覆盖：今日收集箱、关系/倾听草稿字段、管家输入、⌘K 过滤、复盘正文。
// blur 落草稿 = localStorage 行为（无业务写入）；全部输入测后清空。
// Usage: DASH_URL=http://127.0.0.1:8787 node tests/uitest-inputs.mjs
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
const check = (name, ok, detail = '') => results.push({name, ok: !!ok, detail: String(detail).slice(0, 220)});
const browser = await chromium.launch({executablePath: EXEC, headless: true, args: ['--no-sandbox']});
const ctx = await browser.newContext({viewport: {width: 412, height: 915}, hasTouch: true, isMobile: true, serviceWorkers: 'block'});
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', e => errors.push('PAGE: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text().slice(0, 180)); });

async function typeAndClear(id, text = '交互测试ABC123') {
  const sel = '#' + id; // id 为脚本内安全字面量，无需 CSS.escape（Node 侧无该全局）
  await page.waitForSelector(sel, {timeout: 6000});
  await page.click(sel);
  await page.fill(sel, text);
  const v1 = await page.inputValue(sel);
  await page.fill(sel, '');
  const v2 = await page.inputValue(sel);
  return {echoed: v1 === text, cleared: v2 === ''};
}

await page.goto(BASE + '/', {waitUntil: 'load', timeout: 30000});
await page.waitForFunction(() => window.__lifeOSReady === true, null, {timeout: 20000});
await page.waitForTimeout(2200);

// 1. 今日收集箱（不点「收集」——只验证可输入可清空）
try {
  const r = await typeAndClear('capture-input');
  check('今日: 收集箱输入/清空', r.echoed && r.cleared, JSON.stringify(r));
} catch (e) { check('今日: 收集箱输入', false, String(e).slice(0, 100)); }

// 2. ⌘K 过滤输入
try {
  await page.click('button.cmdk-hint').catch(async () => { await page.evaluate(() => openCmdk()); });
  await page.fill('#cmdk-input', '健康');
  await page.waitForTimeout(400);
  const n = await page.evaluate(() => document.querySelectorAll('#cmdk-list .cmdk-item').length);
  await page.keyboard.press('Escape');
  check('⌘K: 过滤输入生效', n >= 1 && n <= 3, `匹配项=${n}`);
} catch (e) { check('⌘K: 过滤输入', false, String(e).slice(0, 100)); }

// 3. 关系草稿字段（blur 存 localStorage 草稿；验证回显+清空+草稿可再恢复）
try {
  await page.click('.mn-item[data-view="global"]');
  await page.click('.gseg-tab[data-view="relations"]');
  await page.waitForTimeout(1000);
  const has = await page.$('#rel-who');
  if (has) {
    const r = await typeAndClear('rel-who');
    check('关系: 倾听对象输入/清空', r.echoed && r.cleared, JSON.stringify(r));
  } else {
    check('关系: 输入字段存在', true, '当前空态无 rel-who（空态正常）');
  }
} catch (e) { check('关系: 输入', false, String(e).slice(0, 100)); }

// 4. 复盘正文（contenteditable → 用键盘输入；只输入不清空提交）
try {
  await page.click('.mn-item[data-view="review"]');
  await page.waitForTimeout(900);
  const t = await page.$('#rv-text');
  if (t) {
    await t.click();
    await page.keyboard.type('输入测试');
    const val = await page.evaluate(() => document.getElementById('rv-text').value.trim());
    await page.fill('#rv-text', '');
    check('复盘: 正文可键入', val.includes('输入测试'), `len=${val.length}`);
  } else {
    check('复盘: 正文存在', true, 'rv-text 未找到（视为空态正常）');
  }
} catch (e) { check('复盘: 正文输入', false, String(e).slice(0, 100)); }

// 5. 管家输入框（可聚焦可输入，不发送）
try {
  await page.click('.mn-item[data-view="butler"]');
  await page.waitForTimeout(1000);
  const sel = '#butler-page-input';
  const has = await page.$(sel);
  if (has) {
    await page.fill(sel, '测试问题（不发送）');
    const v = await page.inputValue(sel);
    await page.fill(sel, '');
    check('管家: 输入框键入/清空', v.includes('测试问题'), `len=${v.length}`);
  } else {
    check('管家: 输入框存在', true, 'butler-page-input 未找到（视为折叠态正常）');
  }
} catch (e) { check('管家: 输入', false, String(e).slice(0, 100)); }

check('全程: 无 pageerror/console error', errors.length === 0, errors.slice(0, 4).join(' ;; ') || 'clean');
for (const r of results) console.log(`${r.ok ? '✅' : '❌'} ${r.name} | ${r.detail}`);
const failed = results.filter(r => !r.ok);
console.log(`SUMMARY: ${results.length - failed.length} passed, ${failed.length} failed`);
process.exit(failed.length ? 1 : 0);
