const CACHE = '9ty5-news-v2';
const STATIC = ['./', './index.html'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = e.request.url;
  if (url.includes('api.rss2json') || url.includes('reddit.com') || url.includes('allorigins')) {
    // Network only for data fetches
    e.respondWith(fetch(e.request).catch(() => new Response('[]')));
    return;
  }
  if (e.request.mode === 'navigate' || url.endsWith('/index.html') || url.endsWith('.js') || url.endsWith('.json')) {
    // Network-first for the app shell so pushed updates are picked up immediately;
    // cache is only a fallback when offline.
    e.respondWith(
      fetch(e.request).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request))
  );
});