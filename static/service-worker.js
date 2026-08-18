const CACHE_NAME = 'profitos-shell-v1';
const SHELL_ASSETS = [
  '/static/style.css',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

const OFFLINE_HTML = `<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Hors ligne - ProfitOS</title>
<style>body{margin:0;background:#081020;color:#f6f8fc;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px}
button{margin-top:16px;background:#f5f8ff;color:#081020;border:0;border-radius:10px;padding:12px 20px;font-weight:900;cursor:pointer}</style>
</head><body><div><h1>Vous etes hors ligne</h1><p>ProfitOS a besoin d'une connexion pour afficher vos donnees financieres a jour.</p>
<button onclick="location.reload()">Reessayer</button></div></body></html>`;

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Assets statiques uniquement (CSS, icones, manifest) : cache-first, jamais
  // les pages applicatives qui contiennent des donnees financieres liees a la session.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          const resClone = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, resClone));
          return res;
        }).catch(() => cached);
      })
    );
    return;
  }

  // Pages applicatives : toujours reseau (jamais de cache de donnees sensibles).
  // En cas d'echec (hors ligne), un ecran simple plutot que des donnees perimees.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() =>
        new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })
      )
    );
  }
});
