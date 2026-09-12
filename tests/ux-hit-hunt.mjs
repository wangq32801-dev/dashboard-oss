// UX-1/UX-2 根因猎捕：复现「导航失效」与「coach 拦截」，在命中异常瞬间
// dump elementsFromPoint 全栈 + 所有可能拦截的 fixed/absolute 覆盖层清单。
import pw from 'playwright-core';
import fs from 'node:fs';

const { chromium } = pw;
const CANDS = [
  process.env.PLAYWRIGHT_CHROMIUM,  // 可选：显式指定浏览器可执行文件
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
];
const EXEC = process.env.PLAYWRIGHT_EXECUTABLE || CANDS.find(p => fs.existsSync(p)) || CANDS[0];
const BASE = (process.env.DASH_URL || 'http://127.0.0.1:8787').replace(/\/$/, '');
const browser = await chromium.launch({executablePath: EXEC, headless: true, args: ['--no-sandbox']});
const ctx = await browser.newContext({viewport: {width: 412, height: 915}, hasTouch: true, isMobile: true, serviceWorkers: 'block'});
const page = await ctx.newPage();

await page.goto(BASE + '/', {waitUntil: 'load', timeout: 30000});
await page.waitForFunction(() => window.__lifeOSReady === true, null, {timeout: 20000});
await page.waitForTimeout(2500);

const VIEWS = ['global', 'review', 'butler', 'today'];
const GLOBALS = ['roles', 'actions', 'habits', 'relations', 'health', 'proactive'];

let navFail = 0, coachIntercept = 0, rounds = 0;
const findings = [];

for (let round = 0; round < 4; round++) {
  // 完整跨视图序列（每视图内点几个安全按钮，制造真实使用密度）
  for (const v of VIEWS) {
    try { await page.click(`.mn-item[data-view="${v}"]`, {timeout: 5000}); } catch (e) { navFail++; }
    await page.waitForTimeout(700);
    if (v === 'global') {
      for (const g of GLOBALS) {
        try { await page.click(`.gseg-tab[data-view="${g}"]`, {timeout: 5000}); await page.waitForTimeout(600); } catch (e) { navFail++; }
      }
    }
  }
  rounds++;
  // 逐视图检查「该视图的可点按钮」被谁拦截（重点：roles 的 addbar 与 coach）
  for (const v of [
    ['roles', async () => { await page.click('.mn-item[data-view="global"]', {timeout: 4000}).catch(() => navFail++); await page.click('.gseg-tab[data-view="roles"]', {timeout: 4000}).catch(() => navFail++); }],
    ['today', async () => { await page.click('.mn-item[data-view="today"]', {timeout: 4000}).catch(() => navFail++); }],
  ]) {
    try { await v[1](); await page.waitForTimeout(900); } catch (_) { continue; }
    const dump = await page.evaluate((view) => {
      const out = {view, blockers: []};
      const targets = view === 'roles'
        ? [['addbar', document.querySelector('#view-roles .roles-addbar .add-inline')]]
        : [['mn-today', document.querySelector('.mn-item[data-view="today"]')]];
      for (const [label, el] of targets) {
        if (!el) { out.blockers.push({label, missing: true}); continue; }
        const r = el.getBoundingClientRect();
        const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
        const stack = document.elementsFromPoint(cx, cy).slice(0, 6).map(e =>
          e.tagName + '#' + (e.id || '') + '.' + String(e.className).split(' ').slice(0, 2).join('.') + ' [' + Math.round(e.getBoundingClientRect().top) + ',' + Math.round(e.getBoundingClientRect().bottom) + ']');
        const direct = document.elementFromPoint(cx, cy);
        out.blockers.push({label, rect: [Math.round(r.top), Math.round(r.bottom)], hitIsSelf: el.contains(direct), stack});
      }
      // 全页 fixed 高层覆盖层清单
      out.overlays = [...document.querySelectorAll('body *')].filter(e => {
        const s = getComputedStyle(e);
        if (s.display === 'none' || s.visibility === 'hidden' || +s.opacity === 0) return false;
        const z = parseInt(s.zIndex);
        if (!(z > 100) || !(s.position === 'fixed' || s.position === 'absolute')) return false;
        const r = e.getBoundingClientRect();
        return r.width > 30 && r.height > 30;
      }).slice(0, 10).map(e => e.tagName + '#' + (e.id || '') + '.' + String(e.className).split(' ').slice(0, 2).join('.') + ' z=' + getComputedStyle(e).zIndex);
      out.bodyClass = document.body.className.slice(0, 60);
      out.scrollY = Math.round(scrollY);
      return out;
    }, v[0]).catch(e => ({err: String(e).slice(0, 80)}));
    findings.push(dump);
    const b = JSON.stringify(dump);
    if (b.includes('cb-ask') || b.includes('coach-bar')) coachIntercept++;
  }
}
console.log('rounds:', rounds, 'navFail:', navFail, 'coachIntercept:', coachIntercept);
console.log(JSON.stringify(findings, null, 1));
await browser.close();
process.exit(0);
