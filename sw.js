const CACHE_NAME = 'news-v342';
const DATA_PATTERNS = [
  /\/data\/interpreted\//,
  /\/data\/themes\//,
  /\/data\/kiwoom\//,
  /\/data\/calendar\//,
  /\/data\/pm320_history\//,  // DOC-20260603-DSN-001 — PM320 추천/결과 data fetch path
  /\/data\/limit-up-trend\.json/,  // 2026-06-18 상한가 추이 stale fix — /themes/ 하위 아니라 별도 매칭 필요 (network-first 적용)
];
const STATIC_ASSETS = [
  '/pm320.html',  // Q-20260605-104 — PM320 본 페이지 (News 이전 후 주 진입점)
  '/news.html',   // redirect stub 유지 (과거 공유 링크 query 보존 → pm320.html)
  '/news.css',
  '/menu.js',
  '/js/utils.js',
  '/js/data-loader.js',
  '/js/calendar.js',
  '/js/renderer.js',
  '/js/lib/chart-tv/expanded-chart.js',
  '/js/lib/chart-tv/toggle-panel.js',
  '/js/lib/chart-tv/plugins/fibonacci.js',
  '/js/lib/chart-tv/plugins/volume-by-decile.js',
  '/js/lib/chart-tv/plugins/markers.js',
  '/js/lib/chart-tv/plugins/pink-signal.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// REQ-024 §1: 정적 자산도 network-first로 통일.
// stale-while-revalidate은 첫 진입 시 구버전을 즉시 반환해 배포 직후 사용자가
// 폐기된 enum/UI를 그대로 보는 회귀를 유발 (REQ-014~023 누적 사례).
// 네트워크 실패 시에만 캐시 fallback. 캐시 매칭은 query string 무시(ignoreSearch).
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // JSON 데이터 — network-first
  if (DATA_PATTERNS.some(p => p.test(url.pathname))) {
    e.respondWith(
      caches.open(CACHE_NAME).then(async cache => {
        try {
          const response = await fetch(e.request);
          if (response.ok) cache.put(e.request, response.clone());
          return response;
        } catch {
          const cached = await cache.match(e.request, { ignoreSearch: true });
          return cached || new Response('{}', { status: 503 });
        }
      })
    );
    return;
  }

  // 정적 자산 — network-first (배포 즉시 반영)
  if (STATIC_ASSETS.some(a => url.pathname === a || url.pathname.endsWith(a))) {
    e.respondWith(
      caches.open(CACHE_NAME).then(async cache => {
        try {
          const response = await fetch(e.request);
          if (response.ok) cache.put(e.request, response.clone());
          return response;
        } catch {
          const cached = await cache.match(e.request, { ignoreSearch: true });
          return cached || new Response('', { status: 503 });
        }
      })
    );
    return;
  }

  // 나머지 — network-first
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request, { ignoreSearch: true })));
});
