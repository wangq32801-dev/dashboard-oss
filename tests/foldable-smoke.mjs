// Foldable/mobile regression smoke test.
// Chromium touch emulation is a responsive-layout surrogate, not proof of
// Safari/WebKit compatibility; final Safari acceptance remains a real-device check.
// Usage: DASH_URL=http://127.0.0.1:8787 node tests/foldable-smoke.mjs
// Optional: PLAYWRIGHT_EXECUTABLE=/path/to/chrome, FOLDABLE_SMOKE_WAIT_MS=1500
//
// IA-2.1（2026-09-04）：导航断言全部改为真实点击用户路径（禁止用
// page.evaluate(() => switchView(...)) 代替点击），并覆盖：
// 全局分区记忆（dash_last_global_view）、默认回落、冷启动恢复高亮一致、
// 全局态导航高度 ≤120px、六分区逐个热区/可达/激活、键盘 Tab/Enter/Space、
// 右端分区滚入可视区。评估类调用只允许用于「读状态」，不允许驱动导航。
import pw from 'playwright-core';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const { chromium } = pw;
// 浏览器选择：显式 env > Playwright 缓存（与 playwright-core 配套的 Chrome for Testing）> 系统 Chrome。
// 缓存优先：系统 Chrome 常被用户日常会话占用（自动化启动会 SIGABRT/EPERM），只作最后兜底。
const msCache = path.join(os.homedir(), 'Library/Caches/ms-playwright');
const cacheCandidates = fs.existsSync(msCache)
  ? fs.readdirSync(msCache)
      .filter(d => /^chromium-\d+$/.test(d))
      .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]))
      .map(d => path.join(msCache, d, 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'))
  : [];
const BROWSER_CANDIDATES = [
  process.env.PLAYWRIGHT_CHROMIUM || '',
  ...cacheCandidates,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
];
const EXEC = process.env.PLAYWRIGHT_EXECUTABLE || BROWSER_CANDIDATES.find(p => p && fs.existsSync(p)) || BROWSER_CANDIDATES[0];
const BASE = (process.env.DASH_URL || 'http://127.0.0.1:8787').replace(/\/$/, '');
const AUTH_USER = process.env.VPS_AUTH_USER || '';
const AUTH_PASS = process.env.VPS_AUTH_PASS || '';
const WAIT_MS = Math.max(500, Number(process.env.FOLDABLE_SMOKE_WAIT_MS || 1500));
const VIEWPORTS = [
  {name: 'folded', width: 412, height: 915, touch: true, mobileNav: true},
  {name: 'expanded', width: 768, height: 1024, touch: true, mobileNav: true},
  {name: 'mac-narrow', width: 1024, height: 768, touch: false, mobileNav: false},
  {name: 'mac-wide', width: 1440, height: 900, touch: false, mobileNav: false},
];
const GLOBAL_VIEWS = ['roles', 'actions', 'habits', 'relations', 'health', 'proactive'];

if (!fs.existsSync(EXEC)) {
  console.error(`foldable smoke skipped: browser executable not found (${EXEC})`);
  process.exit(2);
}

const results = [];
const check = (name, ok, detail = '') => results.push({name, ok: !!ok, detail: String(detail)});
const browser = await chromium.launch({executablePath: EXEC, headless: true, args: ['--no-sandbox', '--disable-gpu']});

function watchErrors(page, errors) {
  page.on('pageerror', e => errors.push('PAGE: ' + e.message));
  page.on('console', msg => { if (msg.type() === 'error') errors.push('CONSOLE: ' + msg.text()); });
  page.on('requestfailed', req => {
    const url = req.url();
    if (url.startsWith(BASE + '/') && !url.includes('/api/butler/stream')) {
      errors.push('REQUEST: ' + (req.failure()?.errorText || 'failed') + ' ' + url);
    }
  });
  page.on('response', response => {
    const url = response.url();
    if (url.startsWith(BASE + '/') && response.status() >= 400 && !url.includes('/api/butler/stream')) {
      errors.push('HTTP ' + response.status() + ': ' + url);
    }
  });
}

const waitView = (page, id, timeout = 6000) =>
  page.waitForFunction(v => document.querySelector('.view.active')?.id === v, id, {timeout});

const activeId = page => page.evaluate(() => document.querySelector('.view.active')?.id || '');

// 真实点击路径进入健康视图：触屏走 全局 → 分区条「健康」；桌面走侧栏子项「健康」。
async function navigateToHealthByClick(page, vp) {
  if (vp.mobileNav) {
    await page.click('.mn-item[data-view="global"]');
    await waitView(page, 'view-actions'); // 全新会话无记忆键 → 默认「行动」
    await page.click('.gseg-tab[data-view="health"]');
  } else {
    await page.click('.nav-sub[data-view="health"]');
  }
  await waitView(page, 'view-health');
}

async function boot(page) {
  await page.goto(BASE + '/', {waitUntil: 'load', timeout: 30000});
  await page.waitForFunction(() => window.__lifeOSReady === true ||
    (typeof window.__lifeOSReady === 'string' && window.__lifeOSReady.startsWith('error')), {timeout: 20000});
}

try {
  for (const vp of VIEWPORTS) {
    // Emulate touch/mobile media queries in Chromium. Safari-specific behavior
    // is intentionally not claimed by this automated suite.
    const contextOptions = {
      viewport: {width: vp.width, height: vp.height},
      hasTouch: vp.touch,
      isMobile: vp.touch,
      serviceWorkers: 'block',
    };
    if (AUTH_USER && AUTH_PASS) contextOptions.httpCredentials = {username: AUTH_USER, password: AUTH_PASS};
    const context = await browser.newContext(contextOptions);
    const page = await context.newPage();
    const errors = [];
    watchErrors(page, errors);
    try {
      await boot(page);
      const layout = await page.evaluate(() => ({
        ready: window.__lifeOSReady === true,
        overflow: document.documentElement.scrollWidth <= window.innerWidth + 1,
        nav: !!document.querySelector('.mobilenav') && getComputedStyle(document.querySelector('.mobilenav')).display !== 'none',
      }));
      check(`${vp.name}: boot`, layout.ready, JSON.stringify(layout));
      check(`${vp.name}: no horizontal overflow`, layout.overflow, `scrollWidth=${await page.evaluate(() => document.documentElement.scrollWidth)}`);
      check(`${vp.name}: navigation mode`, layout.nav === vp.mobileNav, `mobileNav=${layout.nav}`);

      // ── 导航入口计数（IA-2.1 问题2.1/桌面 4+6）：状态读取，不驱动导航 ──
      const entries = await page.evaluate(() => ({
        mnPrimary: document.querySelectorAll('.mn-item[data-view]').length,
        navPrimary: document.querySelectorAll('.nav-item[data-view]').length,
        navSubs: document.querySelectorAll('.nav-sub').length,
        themeBtn: document.querySelectorAll('.mn-item[data-action="theme"]').length,
        inlineOnclickNav: document.querySelectorAll('.nav-item[onclick],.nav-sub[onclick],.nav-tool[onclick],.mn-item[onclick],.gseg-tab[onclick]').length,
      }));
      if (vp.mobileNav) {
        const ok = entries.mnPrimary === 4 && entries.themeBtn === 1 && entries.inlineOnclickNav === 0;
        check(`${vp.name}: mobile primary entries = 4 (+theme counted separately)`, ok, JSON.stringify(entries));

        // 核心交互契约：展开「全局」前后，五个底栏按钮的水平位置和宽度必须完全不变。
        await page.click('.mn-item[data-view="today"]');
        await waitView(page, 'view-today');
        const navBeforeGlobal = await page.evaluate(() => [...document.querySelectorAll('.mn-inner > .mn-item')].map(el => {
          const r = el.getBoundingClientRect();
          return {left: Math.round(r.left * 10) / 10, width: Math.round(r.width * 10) / 10};
        }));
        await page.click('.mn-item[data-view="global"]');
        await waitView(page, 'view-actions');
        const navAfterGlobal = await page.evaluate(() => [...document.querySelectorAll('.mn-inner > .mn-item')].map(el => {
          const r = el.getBoundingClientRect();
          return {left: Math.round(r.left * 10) / 10, width: Math.round(r.width * 10) / 10};
        }));
        check(`${vp.name}: primary nav geometry is invariant when Global opens`,
          JSON.stringify(navBeforeGlobal) === JSON.stringify(navAfterGlobal),
          JSON.stringify({before: navBeforeGlobal, after: navAfterGlobal}));
        await page.click('.mn-item[data-view="today"]');
        await waitView(page, 'view-today');
      } else {
        const ok = entries.navPrimary === 4 && entries.navSubs === 6 && entries.inlineOnclickNav === 0;
        check(`${vp.name}: desktop primary = 4 + global subs = 6`, ok, JSON.stringify(entries));
      }

      // ── IA-3 今日重做验收（折叠态 412×915 实机路径，真实点击）──
      if (vp.name === 'folded') {
        // 验收1：首屏无需滚动能看到 状态摘要 + 今日重点 + ≥1 条今日行动或明确空态
        await page.waitForFunction(() => {
          const b = document.getElementById('today-band');
          return b && b.dataset.rsig && b.children.length > 0;
        }, {timeout: 15000});
        const first = await page.evaluate(() => {
          const vis = e => { if (!e) return null; const r = e.getBoundingClientRect(); return {top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height), shown: !!e.offsetParent, text:(e.textContent||'').trim()}; };
          const navTop = document.querySelector('.mobilenav')?.getBoundingClientRect().top || innerHeight;
          return {
            band: vis(document.getElementById('today-band')),
            rock: vis(document.getElementById('today-rock')),
            focus: vis(document.getElementById('today-focus')),
            firstAction: vis(document.querySelector('#today-focus-list .today-task') || document.querySelector('#today-focus-list .today-focus-empty')),
            scrollY: Math.round(window.scrollY), vh: window.innerHeight, navTop: Math.round(navTop),
          };
        });
        const safeBottom = first.navTop;
        check('folded: first screen shows status summary without scroll', first.band && first.band.shown && first.band.top >= 0 && first.band.bottom <= safeBottom, JSON.stringify(first));
        check('folded: first screen shows today focus', first.rock && first.rock.shown && first.rock.top >= 0 && first.rock.bottom <= safeBottom, JSON.stringify(first));
        check('folded: first screen shows rendered action or empty state', first.firstAction && first.firstAction.shown && first.firstAction.top >= 0 && first.firstAction.bottom <= safeBottom && !first.firstAction.text.includes('加载中'), JSON.stringify(first));

        // 首页夜间主题：真实点击切换，关键表面必须转暗且标题/正文保持可读。
        if (await page.evaluate(() => document.body.classList.contains('night-mode'))) {
          await page.click('.mn-item[data-action="theme"]');
          await page.waitForFunction(() => !document.body.classList.contains('night-mode'));
        }
        await page.click('.mn-item[data-action="theme"]');
        await page.waitForFunction(() => document.body.classList.contains('night-mode'));
        await page.waitForTimeout(1800); // 等主题色过渡完成后再量计算色
        const nightToday = await page.evaluate(() => {
          const rgb = value => (String(value).match(/[\d.]+/g) || []).slice(0, 3).map(Number);
          const luminance = value => { const c=rgb(value); return c.length===3 ? (c[0]*.2126+c[1]*.7152+c[2]*.0722) : -1; };
          const sample = selector => { const el=document.querySelector(selector); const cs=el&&getComputedStyle(el); return cs ? {bg:cs.backgroundColor, bgLum:luminance(cs.backgroundColor), color:cs.color, colorLum:luminance(cs.color)} : null; };
          return {
            on: document.body.classList.contains('night-mode'),
            title: sample('#view-today .view-head h1'),
            rock: sample('#view-today .today-rock-card'),
            focus: sample('#view-today .today-focus-card'),
            band: sample('#view-today .today-layout>.today-band'),
            habit: sample('#view-today .today-habit-card'),
            capture: sample('#view-today .today-capture-card'),
            input: sample('#view-today .today-capture-card input'),
            quote: sample('#view-today .quote-card .q-text'),
          };
        });
        const darkSurfaces = ['rock','focus','band','habit','capture','input'].every(k => nightToday[k] && nightToday[k].bgLum >= 0 && nightToday[k].bgLum < 100);
        const readableText = nightToday.title?.colorLum > 180 && nightToday.quote?.colorLum > 150;
        check('folded: Today switches to complete readable night theme', nightToday.on && darkSurfaces && readableText, JSON.stringify(nightToday));
        await page.screenshot({path: '/private/tmp/dashboard-today-night-412x915.png'});
        await page.click('.mn-item[data-action="theme"]');
        await page.waitForFunction(() => !document.body.classList.contains('night-mode'));
        const healthHasReadiness = await page.evaluate(async () => {
          try { const r=await fetch(new URL('api/health?days=7', document.baseURI)); const d=await r.json(); return !!d?.readiness?.available; }
          catch (_) { return false; }
        });
        if (healthHasReadiness) {
          await page.waitForFunction(() => !document.getElementById('today-band')?.textContent.includes('暂无健康数据'), {timeout:15000});
        }
        const firstHealth = await page.evaluate(() => document.getElementById('today-band')?.textContent || '');
        check('folded: Today consumes available health without visiting Health first',
          !healthHasReadiness || !firstHealth.includes('暂无健康数据'), firstHealth);
        // 首屏截图供人工复核（不随 git 入库，路径记录在维护手册）
        await page.screenshot({path: '/private/tmp/dashboard-ia3-today-412x915.png'});
        // 布局稳定等待：懒加载/健康兜底会间歇推移布局——连续两帧（300ms 间隔）关键锚点
        // 位置一致才计算滚动量与断言，消除时序敏感（根治本组 flaky）。
        const layoutSettled = () => page.waitForFunction(() => {
          const sig = () => JSON.stringify(['today-band', 'today-focus-list', 'habit-quick', 'capture-input'].map(id => {
            const e = document.getElementById(id);
            if (!e) return null;
            const r = e.getBoundingClientRect();
            return [Math.round(r.top), Math.round(r.height)];
          }));
          const a = sig();
          return new Promise(res => setTimeout(() => res(sig() === a ? a : false), 300));
        }, null, {timeout: 8000}).then(() => true).catch(() => false);
        await layoutSettled();
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.waitForTimeout(250);
        await layoutSettled();
        // 验收2：一次常规滚动即可到达习惯打卡，再继续少量即可到达快速收集。
        const need = await page.evaluate(() => {
          const hq = document.getElementById('habit-quick');
          const cap = document.getElementById('capture-input');
          const navTop=document.querySelector('.mobilenav')?.getBoundingClientRect().top||innerHeight;
          const hTop = hq ? hq.getBoundingClientRect().top : -1;
          const cBottom = cap ? cap.getBoundingClientRect().bottom : -1;
          return {
            habitNeed:hTop<0?-1:Math.max(0,Math.round(hTop-(navTop-90))),
            captureNeed:cBottom<0?-1:Math.max(0,Math.round(cBottom-(navTop-30)))
          };
        });
        check('folded: habit/capture remain within compact scroll reach',
          need.habitNeed>=0&&need.habitNeed<=620&&need.captureNeed>=0&&need.captureNeed<=800, JSON.stringify(need));
        const l3 = await page.evaluate(() => {
          const main=document.querySelector('#view-today .today-layout')?.getBoundingClientRect();
          const columns=[...document.querySelectorAll('#view-today .today-layout>.today-primary,#view-today .today-layout>.today-aside')].map(e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width};});
          return {main:main&&{left:main.left,right:main.right,width:main.width},columns};
        });
        check('folded: Today primary/aside form one full-width non-overlapping column',
          !!l3.main && l3.columns.length===2 &&
          l3.columns.every(c=>c.left>=l3.main.left-1&&c.right<=l3.main.right+1&&c.width>=l3.main.width-2) &&
          l3.columns[1].top>=l3.columns[0].bottom-1, JSON.stringify(l3));
        await page.evaluate(n => window.scrollTo(0, n), Math.max(0, need.habitNeed));
        await page.waitForTimeout(400);
        const habitReach = await page.evaluate(() => {
          const vis = e => { if (!e) return false; const r = e.getBoundingClientRect(); return r.top < window.innerHeight - 40 && r.bottom > 0 && !!e.offsetParent; };
          const navTop=document.querySelector('.mobilenav')?.getBoundingClientRect().top||innerHeight;
          const clear=e=>{if(!e)return false;const r=e.getBoundingClientRect();return r.top<navTop-20&&r.bottom>0;};
          return {habit: vis(document.getElementById('habit-quick'))&&clear(document.getElementById('habit-quick')), y: Math.round(window.scrollY)};
        });
        check('folded: light scroll reaches habit quick', habitReach.habit, JSON.stringify(habitReach));
        await page.evaluate(n => window.scrollTo(0, n), Math.max(0, need.captureNeed));
        await page.waitForTimeout(250);
        // 本质语义：capture 可见、底部不越过导航、中心点命中自身（不被 fixed 导航遮挡）。
        // 懒加载会间歇推移布局——按当前位置最多补滚 2 次（等价真实用户再滚一下）。
        const captureProbe = () => page.evaluate(() => {
          const e = document.getElementById('capture-input'), nav = document.querySelector('.mobilenav');
          if (!e || !nav) return {capture: false, need: 80};
          const r = e.getBoundingClientRect(), nr = nav.getBoundingClientRect();
          const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(Math.max(r.y + r.height / 2, 0), nr.top - 5));
          return {capture: r.bottom <= nr.top + 1 && !!hit && (hit === e || e.contains(hit)), need: Math.max(0, Math.round(r.bottom - nr.top))};
        });
        let captureReach = await captureProbe();
        for (let k = 0; k < 2 && !captureReach.capture; k++) {
          await page.evaluate(n => window.scrollBy(0, n), captureReach.need || 80);
          await page.waitForTimeout(250);
          captureReach = await captureProbe();
        }
        check('folded: compact scroll reaches capture without nav overlap', captureReach.capture, JSON.stringify(captureReach));
        await page.evaluate(() => window.scrollTo(0, 0));
        // 验收3：本周战报不再出现在今日，进入复盘后可见
        const moved = await page.evaluate(() => ({
          inToday: !!document.querySelector('#view-today #mini-week-chart'),
          inReview: !!document.querySelector('#view-review #mini-week-chart'),
        }));
        check('folded: week report card moved out of today', !moved.inToday && moved.inReview, JSON.stringify(moved));
        await page.click('.mn-item[data-view="review"]');
        await waitView(page, 'view-review');
        const reviewChart = await page.evaluate(() => {
          const cv = document.querySelector('#view-review #mini-week-chart');
          return {present: !!cv, visible: !!cv && !!cv.offsetParent};
        });
        check('folded: week report renders in review', reviewChart.present && reviewChart.visible, JSON.stringify(reviewChart));
        // 验收4：神谕默认折叠，展开/收起不串到其他 view
        await page.click('.mn-item[data-view="today"]');
        await waitView(page, 'view-today');
        await page.waitForFunction(() => {
          const s = document.getElementById('oracle-summary'), p = document.getElementById('oracle-panel');
          return (p && p.style.display !== 'none' && s && s.textContent.length > 0) || (p && p.style.display === 'none');
        }, {timeout: 20000});
        const oracleState = await page.evaluate(() => {
          const p = document.getElementById('oracle-panel');
          return {shown: !!(p && p.style.display !== 'none'),
            bodyHidden: document.getElementById('oracle-body')?.hidden,
            expanded: document.getElementById('oracle-toggle')?.getAttribute('aria-expanded'),
            summary: document.getElementById('oracle-summary')?.textContent || ''};
        });
        check('folded: oracle collapsed by default', !oracleState.shown || (oracleState.bodyHidden && oracleState.expanded === 'false'), JSON.stringify(oracleState));
        if (oracleState.shown) {
          await page.click('#oracle-toggle');
          const opened = await page.evaluate(() => ({hidden: document.getElementById('oracle-body').hidden, aria: document.getElementById('oracle-toggle').getAttribute('aria-expanded'), view: document.querySelector('.view.active')?.id}));
          await page.click('#oracle-toggle');
          const closed = await page.evaluate(() => ({hidden: document.getElementById('oracle-body').hidden, aria: document.getElementById('oracle-toggle').getAttribute('aria-expanded'), view: document.querySelector('.view.active')?.id}));
          check('folded: oracle expand/collapse stays in today',
            !opened.hidden && opened.aria === 'true' && opened.view === 'view-today' && closed.hidden && closed.aria === 'false' && closed.view === 'view-today', JSON.stringify({opened, closed}));
        }
        // 验收5：今日静止 5 秒 DOM mutation 为 0（先等状态带签名稳定——健康兜底拉取可能晚到重绘一次）
        await page.waitForFunction(() => {
          const b = document.getElementById('today-band');
          return b && b.dataset.rsig;
        }, {timeout: 10000});
        await page.waitForTimeout(1600);
        const sigBefore = await page.evaluate(() => document.getElementById('today-band').dataset.rsig);
        await page.waitForTimeout(1200);
        const sigAfter = await page.evaluate(() => document.getElementById('today-band').dataset.rsig);
        const staticWin = await page.evaluate(wait => new Promise(resolve => {
          const target = document.getElementById('view-today');
          let count = 0;
          const obs = new MutationObserver(muts => { count += muts.length; });
          obs.observe(target, {subtree: true, childList: true, attributes: true, characterData: true});
          setTimeout(() => { obs.disconnect(); resolve(count); }, wait);
        }), 5000);
        check('folded: settled today DOM mutations = 0 (5s)',
          sigBefore === sigAfter && staticWin === 0, `mutations=${staticWin}, bandSig=${sigBefore === sigAfter ? 'stable' : 'changed'}`);
        // 验收6：快速切换 今日/全局 50 次——无错误、无残影（恰好一个 active view、其余不可见）
        const errBeforeToggles = errors.length;
        for (let i = 0; i < 50; i++) {
          await page.click(i % 2 === 0 ? '.mn-item[data-view="global"]' : '.mn-item[data-view="today"]');
        }
        await page.waitForTimeout(600);
        const afterToggles = await page.evaluate(() => ({
          active: document.querySelector('.view.active')?.id,
          activeCount: document.querySelectorAll('.view.active').length,
          visible: [...document.querySelectorAll('.view')].filter(v => getComputedStyle(v).display !== 'none').length,
        }));
        check('folded: 50 fast today/global toggles, no ghosts',
          afterToggles.active === 'view-today' && afterToggles.activeCount === 1 && afterToggles.visible === 1 && errors.length === errBeforeToggles, JSON.stringify(afterToggles));
      }

      // ── 真实点击进入健康视图 ──
      await navigateToHealthByClick(page, vp);
      await page.waitForFunction(() => document.querySelectorAll('#health-summary .hkpi').length > 1, {timeout: 20000});
      await page.waitForTimeout(WAIT_MS);
      const health = await page.evaluate(() => ({
        open: document.querySelector('.view.active')?.id === 'view-health',
        sevenActive: !!document.querySelector('.hr-btn[data-days="7"].active'),
        hasSurface: !!document.querySelector('#view-health') && !!document.querySelector('#health-fetch'),
        populated: document.querySelectorAll('#health-summary .hkpi').length > 1 &&
          !document.querySelector('#health-summary')?.textContent.includes('暂无健康数据'),
      }));
      check(`${vp.name}: health surface`, health.open && health.hasSurface, JSON.stringify(health));
      check(`${vp.name}: seven-day default`, health.sevenActive, JSON.stringify(health));
      check(`${vp.name}: first-open health data`, health.populated, JSON.stringify(health));

      // Once the view has settled, a passive interval must not continuously rebuild it.
      const mutationWindows = await page.evaluate((wait) => {
        const sample = () => new Promise(resolve => {
          const target = document.getElementById('view-health');
          if (!target) return resolve(-1);
          let count = 0;
          const observer = new MutationObserver(() => { count += 1; });
          observer.observe(target, {subtree: true, childList: true, attributes: true, characterData: true});
          setTimeout(() => { observer.disconnect(); resolve(count); }, wait);
        });
        return sample().then(first => sample().then(second => [first, second]));
      }, WAIT_MS);
      const lastWindow = mutationWindows[mutationWindows.length - 1];
      check(`${vp.name}: settled health DOM`, lastWindow >= 0 && lastWindow <= 3, `windows=${mutationWindows.join(',')}`);

      // ── 全局分区条状态（IA-2 A1 延续）：≤860 触屏可见，桌面隐藏且子项可见 ──
      const partition = await page.evaluate(() => {
        const seg = document.getElementById('global-segbar');
        const segRect = seg?.getBoundingClientRect();
        const tabs = [...document.querySelectorAll('.gseg-tab')];
        const sub = document.querySelector('.nav-sub[data-view="health"]');
        const subRect = sub?.getBoundingClientRect();
        return {
          // position:fixed 元素的 offsetParent 合法地为 null；用实际矩形判断可见性。
          segVisible: !!seg && getComputedStyle(seg).display !== 'none' && !!segRect && segRect.width > 0 && segRect.height > 0,
          tabCount: tabs.length,
          activeTab: tabs.find(t => t.classList.contains('active'))?.dataset.view || '',
          ariaSelected: tabs.find(t => t.classList.contains('active'))?.getAttribute('aria-selected') || '',
          tabRole: tabs.every(t => t.getAttribute('role') === 'tab'),
          ariaControls: tabs.every(t => (t.getAttribute('aria-controls') || '').startsWith('view-')),
          subVisible: !!sub && !!subRect && subRect.width > 0 && subRect.height > 0,
          navtop: document.body.dataset.navtop || '',
        };
      });
      if (vp.mobileNav) {
        const uiOk = partition.segVisible && partition.tabCount === 6 && partition.activeTab === 'health' &&
          partition.tabRole && partition.ariaControls && partition.navtop === 'global';
        check(`${vp.name}: global partition bar`, uiOk, JSON.stringify(partition));
      } else {
        const uiOk = !partition.segVisible && partition.subVisible && partition.navtop === 'global';
        check(`${vp.name}: global partition bar`, uiOk, JSON.stringify(partition));
      }

      // ── 移动端套件：高度/热区/逐分区点击/记忆/键盘 ──
      if (vp.mobileNav) {
        // 问题2.3：全局态导航整体高度 ≤120px（不含安全区），主内容 padding-bottom 不残留 188/200px。
        const navGeom = await page.evaluate(() => {
          const nav = document.querySelector('.mobilenav');
          const r = nav.getBoundingClientRect();
          const pb = getComputedStyle(document.querySelector('.main')).paddingBottom;
          return {h: Math.round(r.height * 10) / 10, pb, w: Math.round(r.width)};
        });
        check(`${vp.name}: global nav height ≤120px`, navGeom.h > 0 && navGeom.h <= 120, JSON.stringify(navGeom));
        check(`${vp.name}: main padding-bottom follows nav (no 188/200 residue)`,
          navGeom.pb !== '' && parseFloat(navGeom.pb) >= navGeom.h + 20 && parseFloat(navGeom.pb) <= 140, JSON.stringify(navGeom));

        // 问题2.6/测试6：六个分区按钮逐个检查热区，不得只查第一个。
        const boxes = await page.evaluate(() => [...document.querySelectorAll('.gseg-tab')].map(t => {
          const r = t.getBoundingClientRect();
          return {v: t.dataset.view, w: Math.round(r.width), h: Math.round(r.height)};
        }));
        check(`${vp.name}: every tab ≥44px hot zone`,
          boxes.length === 6 && boxes.every(b => b.w >= 44 && b.h >= 44), JSON.stringify(boxes));

        // 测试5：六个分区逐个真实点击，全部激活正确 view，且无水平溢出。
        const loop = [];
        for (const v of GLOBAL_VIEWS) {
          await page.click(`.gseg-tab[data-view="${v}"]`);
          await waitView(page, `view-${v}`);
          loop.push(await page.evaluate(view => ({
            v: view,
            active: document.querySelector('.view.active')?.id === 'view-' + view,
            selected: document.querySelector(`.gseg-tab[data-view="${view}"]`)?.getAttribute('aria-selected') || '',
            overflow: document.documentElement.scrollWidth <= window.innerWidth + 1,
          }), v));
        }
        check(`${vp.name}: six partitions activate by real clicks`,
          loop.length === 6 && loop.every(x => x.active && x.selected === 'true' && x.overflow), JSON.stringify(loop));

        // 测试9：激活最右「积极主动」后，该按钮完整进入可视区域（smooth 滚动落定后再量）。
        await page.waitForTimeout(900);
        const rightmost = await page.evaluate(() => {
          const st = document.querySelector('.gseg-tabs'), tab = document.querySelector('.gseg-tab[data-view="proactive"]');
          const sr = st.getBoundingClientRect(), tr = tab.getBoundingClientRect();
          return {left: Math.round(tr.left - sr.left), right: Math.round(sr.right - tr.right), w: Math.round(tr.width), scrollLeft: Math.round(st.scrollLeft)};
        });
        check(`${vp.name}: rightmost tab fully visible after activation`,
          rightmost.w > 0 && rightmost.left >= -1 && rightmost.right >= -1, JSON.stringify(rightmost));

        if (vp.name === 'folded') {
          // 测试2：健康 → 今日 → 全局 ⇒ 回到健康（dash_last_global_view 不被非全局视图覆盖）。
          await page.click('.gseg-tab[data-view="health"]');
          await waitView(page, 'view-health');
          await page.click('.mn-item[data-view="today"]');
          await waitView(page, 'view-today');
          await page.click('.mn-item[data-view="global"]');
          await waitView(page, 'view-health');
          const mem1 = await activeId(page);
          check('folded: health → today → global returns to health', mem1 === 'view-health', `active=${mem1}`);

          // 测试3：关系 → 复盘 → 全局 ⇒ 回到关系。
          await page.click('.gseg-tab[data-view="relations"]');
          await waitView(page, 'view-relations');
          await page.click('.mn-item[data-view="review"]');
          await waitView(page, 'view-review');
          await page.click('.mn-item[data-view="global"]');
          await waitView(page, 'view-relations');
          const mem2 = await activeId(page);
          check('folded: relations → review → global returns to relations', mem2 === 'view-relations', `active=${mem2}`);

          // 问题3.5/测试10：键盘操作——Tab 可达、Enter/Space 可触发（button 原生行为 + navBus 委托）。
          await page.focus('.gseg-tab[data-view="actions"]');
          await page.keyboard.press('Enter');
          await waitView(page, 'view-actions');
          await page.focus('.mn-item[data-view="review"]');
          await page.keyboard.press(' ');
          await waitView(page, 'view-review');
          await page.focus('.mn-item[data-view="today"]');
          await page.keyboard.press('Tab');
          const kb = await page.evaluate(() => {
            const ae = document.activeElement;
            return {
              next: ae && ae.classList ? (ae.classList.contains('mn-item') || ae.classList.contains('gseg-tab') ? ae.dataset.view || ae.dataset.action : '') : '',
              tabbable: [...document.querySelectorAll('.mn-item,.gseg-tab')].every(b => b.tabIndex === 0),
            };
          });
          check('folded: keyboard Tab/Enter/Space operate nav', kb.next === 'global' && kb.tabbable, JSON.stringify(kb));
        }
      }

      // ── 桌面套件：六个子项逐个真实点击并验证（测试7）──
      if (!vp.mobileNav && (vp.name === 'mac-narrow' || vp.name === 'mac-wide')) {
        const loop = [];
        for (const v of GLOBAL_VIEWS) {
          await page.click(`.nav-sub[data-view="${v}"]`);
          await waitView(page, `view-${v}`);
          loop.push(await page.evaluate(view => ({
            v: view,
            active: document.querySelector('.view.active')?.id === 'view-' + view,
            subActive: !!document.querySelector(`.nav-sub[data-view="${view}"].active`),
            navtop: document.body.dataset.navtop,
          }), v));
        }
        check(`${vp.name}: six desktop subs activate by real clicks`,
          loop.length === 6 && loop.every(x => x.active && x.subActive && x.navtop === 'global'), JSON.stringify(loop));
      }
    } catch (error) {
      check(`${vp.name}: scenario`, false, error?.stack || error?.message || error);
    } finally {
      check(`${vp.name}: no runtime errors`, errors.length === 0, errors.slice(0, 5).join(' | '));
      await context.close();
    }
  }

  // ── 折叠态专属：独立会话两条（不污染上面主上下文的存储状态）──
  const foldedVp = VIEWPORTS[0];
  const ctxOpts = {
    viewport: {width: foldedVp.width, height: foldedVp.height},
    hasTouch: true, isMobile: true, serviceWorkers: 'block',
  };
  if (AUTH_USER && AUTH_PASS) ctxOpts.httpCredentials = {username: AUTH_USER, password: AUTH_PASS};

  // 测试4：新会话没有 dash_last_global_view 时，全局默认落到「行动」。
  {
    const ctx = await browser.newContext(ctxOpts);
    const page = await ctx.newPage();
    const errors = [];
    watchErrors(page, errors);
    try {
      await boot(page);
      await page.click('.mn-item[data-view="global"]');
      await waitView(page, 'view-actions');
      const got = await activeId(page);
      check('folded: fresh session without dash_last_global_view defaults to actions', got === 'view-actions', `active=${got}`);
    } catch (error) {
      check('folded: fresh-session default', false, error?.stack || error?.message || error);
    } finally {
      check('folded: fresh session no runtime errors', errors.length === 0, errors.slice(0, 5).join(' | '));
      await ctx.close();
    }
  }

  // 测试12：冷启动恢复 health 时，「全局」与「健康」高亮一致（head 恢复仍走 dash_last_view）。
  {
    const ctx = await browser.newContext(ctxOpts);
    await ctx.addInitScript(() => { try { localStorage.setItem('dash_last_view', 'health'); } catch (e) {} });
    const page = await ctx.newPage();
    const errors = [];
    watchErrors(page, errors);
    try {
      await boot(page);
      await page.waitForTimeout(600); // 等 boot 异步对齐（navHighlight）完成
      const st = await page.evaluate(() => ({
        view: document.querySelector('.view.active')?.id || '',
        mnGlobal: document.querySelector('.mn-item[data-view="global"]')?.classList.contains('active') || false,
        gsegHealth: document.querySelector('.gseg-tab[data-view="health"]')?.getAttribute('aria-selected') || '',
        gsegHealthActive: document.querySelector('.gseg-tab[data-view="health"]')?.classList.contains('active') || false,
        navtop: document.body.dataset.navtop || '',
      }));
      const ok = st.view === 'view-health' && st.mnGlobal && st.gsegHealth === 'true' && st.gsegHealthActive && st.navtop === 'global';
      check('folded: cold restore health highlights global+health consistently', ok, JSON.stringify(st));
    } catch (error) {
      check('folded: cold restore', false, error?.stack || error?.message || error);
    } finally {
      check('folded: cold restore no runtime errors', errors.length === 0, errors.slice(0, 5).join(' | '));
      await ctx.close();
    }
  }
} finally {
  await browser.close();
}

for (const result of results) console.log(`${result.ok ? '✅' : '❌'} ${result.name}${result.detail ? ' | ' + result.detail : ''}`);
const failed = results.filter(result => !result.ok);
console.log(`SUMMARY: ${results.length - failed.length} passed, ${failed.length} failed`);
process.exit(failed.length ? 1 : 0);
