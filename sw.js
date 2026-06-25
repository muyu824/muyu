const CACHE = 'ghost-v6'
const API = ['/chat', '/chat/stream', '/messages', '/activity', '/push', '/traces', '/books']

self.addEventListener('install', () => self.skipWaiting())

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  )
  self.clients.claim()
})

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return
  const { pathname } = new URL(e.request.url)
  if (API.some(p => pathname.startsWith(p))) return
  e.respondWith(
    fetch(e.request)
      .then(res => {
        const clone = res.clone()
        caches.open(CACHE).then(c => c.put(e.request, clone))
        return res
      })
      .catch(() => caches.match(e.request))
  )
})

self.addEventListener('push', e => {
  const data = e.data ? e.data.json() : { title: 'Claude', body: '…' }
  e.waitUntil(
    self.registration.showNotification(data.title || 'Claude', {
      body: data.body || '',
      icon: '/icon.svg',
      badge: '/icon.svg',
      vibrate: [150, 80, 150],
      tag: 'claude',
      renotify: true,
    })
  )
})

self.addEventListener('notificationclick', e => {
  e.notification.close()
  e.waitUntil(clients.matchAll({ type: 'window' }).then(wins => {
    for (const w of wins) { if (w.url === '/' && 'focus' in w) return w.focus() }
    if (clients.openWindow) return clients.openWindow('/')
  }))
})
