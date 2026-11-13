// Service worker do quadro: só notificações (o quadro não funciona sem o servidor, então não há cache offline).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// O servidor manda {title, body, tag, url} cifrado; tag = id da sessão, então uma notificação nova da mesma sessão substitui a anterior
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "YourTurn", {
    body: d.body || "", tag: d.tag, renotify: !!d.tag, icon: "/icon-192.png", badge: "/icon-192.png", data: { url: d.url || "/" },
  }));
});

// Tocar na notificação: abre (ou traz para frente) o quadro já no terminal da sessão
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "/", self.location.origin);
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    const c = cs.find((c) => new URL(c.url).origin === url.origin);
    if (c) { c.postMessage({ abrir: url.searchParams.get("abrir") }); return c.focus(); }
    return self.clients.openWindow(url.href);
  }));
});
