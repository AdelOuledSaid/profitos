/* ProfitOS native bridge for Capacitor shells (Android/iOS).
   No-op in a normal browser. Push registration uses the authenticated
   ProfitOS session and the existing CSRF-protected mobile API. */
(function () {
  'use strict';

  function capacitor() { return window.Capacitor || null; }
  function nativePlatform() {
    var c = capacitor();
    if (!c || typeof c.isNativePlatform !== 'function' || !c.isNativePlatform()) return null;
    var p = typeof c.getPlatform === 'function' ? c.getPlatform() : null;
    return (p === 'ios' || p === 'android') ? p : null;
  }
  async function jsonFetch(url, options) {
    var response = await fetch(url, Object.assign({credentials: 'same-origin'}, options || {}));
    if (!response.ok) throw new Error('HTTP ' + response.status + ' on ' + url);
    return response.json();
  }
  async function csrfToken() {
    var payload = await jsonFetch('/api/mobile/csrf-token');
    if (!payload.csrf_token) throw new Error('CSRF token unavailable');
    return payload.csrf_token;
  }
  async function storeToken(token, platform) {
    var csrf = await csrfToken();
    return jsonFetch('/api/mobile/register-push-token', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify({token: token, platform: platform})
    });
  }
  function safeNotificationTarget(notification) {
    var data = notification && notification.notification && notification.notification.data;
    var target = data && data.url;
    if (typeof target !== 'string' || !target.startsWith('/') || target.startsWith('//')) return null;
    return target;
  }
  async function initPush() {
    var platform = nativePlatform();
    if (!platform) return;
    var plugins = capacitor().Plugins || {};
    var push = plugins.PushNotifications;
    if (!push) return;

    await push.addListener('registration', function (event) {
      if (event && event.value) {
        storeToken(event.value, platform).catch(function (err) { console.warn('ProfitOS push token:', err); });
      }
    });
    await push.addListener('registrationError', function (err) { console.warn('ProfitOS push registration:', err); });
    await push.addListener('pushNotificationActionPerformed', function (event) {
      var target = safeNotificationTarget(event);
      if (target) window.location.assign(target);
    });

    var permission = await push.checkPermissions();
    if (permission.receive === 'prompt' || permission.receive === 'prompt-with-rationale') {
      permission = await push.requestPermissions();
    }
    if (permission.receive === 'granted') await push.register();
  }

  window.ProfitOSMobile = {nativePlatform: nativePlatform, initPush: initPush};
  document.addEventListener('DOMContentLoaded', function () {
    initPush().catch(function (err) { console.warn('ProfitOS mobile bridge:', err); });
  });
}());
