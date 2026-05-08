/* 影视资源管理器 — Service Worker */
const CACHE = 'media-manager-v1';

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
  // API 请求不缓存
  if (e.request.url.includes('/api/')) return;
  // 仅缓存 GET 请求
  if (e.request.method !== 'GET') return;

  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request))
  );
});
