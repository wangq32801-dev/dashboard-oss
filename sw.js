/* 个人驾驶舱 · Service Worker
   缓存版本由页面注册时的构建指纹自动传入，不再手工维护 vN。 */
const VERSION = new URL(self.location.href).searchParams.get('v') || 'runtime';
const CACHE = `dash-${VERSION}`;
const ASSETS = [
  './',
  './hermes-dashboard.html',
  './chart.umd.min.js',
  './quotes.js',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png',
  './frontend-modules/data-client.js',
  './frontend-modules/build-status.js',
  './frontend-modules/task-mutations.js',
  './frontend-modules/api-client.js',
  './frontend-modules/sse-client.js',
  './frontend-modules/sync-ui.js',
  './frontend-modules/write-coordinator.js',
  './frontend-modules/health-view.js',
  './frontend-modules/today-view.js',
  './frontend-modules/habits-view.js',
  './frontend-modules/view-lifecycle.js',
  './frontend-modules/health-render.js',
  './frontend-modules/dialog-a11y.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
      .then(() => self.clients.matchAll({type:'window'}).then(clients => {
        clients.forEach(c => c.postMessage({type:'DASH_SW_UPDATED', version: CACHE}));
      }))
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // API 请求与跨域请求不拦截
  if (url.pathname.includes('/api/') || url.origin !== self.location.origin) return;

  if (e.request.mode === 'navigate') {
    // 有网络时始终取最新页面，离线才回退缓存，避免升级后仍显示旧页面。
    e.respondWith(
      fetch(e.request, {cache:'no-store'}).then((resp) => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(e.request, copy));
          }
          return resp;
        }).catch(() => caches.match(e.request))
    );
    return;
  }

  // JS 模块需要及时更新；网络失败时回退到 SW 缓存，兼顾在线一致性与离线可用。
  if (url.pathname.endsWith('.js')) {
    e.respondWith(
      fetch(e.request, {cache:'no-store'}).then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return resp;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // 静态资源：缓存优先，失败回网络（fetch 加 no-store 绕过 nginx immutable，SW Cache API 唯一缓存）
  e.respondWith(
    caches.match(e.request).then((cached) => cached || fetch(e.request, {cache:'no-store'}))
  );
});
