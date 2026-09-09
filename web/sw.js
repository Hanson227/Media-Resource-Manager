/* 影视资源管理器 — Service Worker */
const CACHE = 'media-manager-v5';

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then((cache) =>
      cache.addAll(['/', '/manifest.json'])
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

  // JS/CSS 走网络优先（确保开发者改动立即可见）
  if (url.endsWith('.js') || url.endsWith('.css')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // 其他静态资源缓存优先；未命中时回填缓存（否则除 / 与 manifest 外永不入缓存）
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
