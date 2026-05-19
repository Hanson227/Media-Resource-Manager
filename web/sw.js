/* 影视资源管理器 — Service Worker */
const CACHE = 'media-manager-v2';

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then((cache) =>
      cache.addAll(['/', '/manifest.json'])
    )
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(clients.claim());
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

  // 其他静态资源缓存优先
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request))
  );
});
