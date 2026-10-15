/* 影视资源管理器 — Service Worker */
const CACHE = 'media-manager-v9';

/* 预缓存清单：本地脚本/样式 + 离线 Vue 运行库（vendor/）。
   必须逐个 add（addAll 有一个 404 就整批失败），否则部署时缺了 vendor
   会导致整个 SW 安装失败、离线能力整体失效。 */
const PRECACHE = [
  '/',
  '/manifest.json',
  '/js-core.js',
  '/js-gesture.js',
  '/js-pages.js',
  '/js-app.js',
  '/style.css',
  '/vendor/vue.global.prod.js',
  '/vendor/vue-router.global.prod.js',
  '/vendor/mdi/css/materialdesignicons.min.css',
  '/vendor/mdi/fonts/materialdesignicons-webfont.woff2',
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then((cache) =>
      Promise.all(PRECACHE.map((url) => cache.add(url).catch(() => {})))
    )
  );
});

self.addEventListener('activate', (e) => {
  // 必须删除旧版本缓存：caches.match 按缓存创建顺序查找，
  // 残留的旧缓存会让 bump 版本号后客户端仍拿到旧 index.html（应用壳永不更新）。
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = e.request.url;
  // API 请求不缓存
  if (url.includes('/api/')) return;
  if (e.request.method !== 'GET') return;

  // JS/CSS 走网络优先（开发者改动立即可见），成功后回填缓存 ——
  // 没有回填的话断网首次加载拿不到脚本，整页白屏。
  if (url.endsWith('.js') || url.endsWith('.css')) {
    e.respondWith(
      fetch(e.request).then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        }
        return resp;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // 其他静态资源缓存优先；未命中时回填缓存（否则除预缓存项外永不入缓存）
  e.respondWith(
    caches.match(e.request).then((hit) => {
      if (hit) return hit;
      return fetch(e.request).then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        }
        return resp;
      });
    })
  );
});
