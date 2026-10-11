"""v416 — SSRF : résolution unique, adresses publiques uniquement, connexion à
l'IP validée (SNI/Host = nom d'origine), aucun corps de réponse stocké/affiché."""
import socket

import pytest

from profitos import safe_http


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.1.2.3', '172.16.0.1', '192.168.1.1', '169.254.169.254',
                                '100.64.0.1', '100.127.255.254', '0.0.0.0', '224.0.0.1', '240.0.0.1',
                                '::1', 'fc00::1', 'fd12:3456::1', 'fe80::1', '::ffff:127.0.0.1',
                                '::ffff:10.0.0.1', '64:ff9b::a00:1', '2002:a00:1::1', '2001:db8::1'])
def test_internal_addresses_are_rejected(ip):
    assert not safe_http.is_public_ip(ip)


@pytest.mark.parametrize('ip', ['8.8.8.8', '1.1.1.1', '2606:4700:4700::1111'])
def test_public_addresses_are_accepted(ip):
    assert safe_http.is_public_ip(ip)


def _resolve_to(monkeypatch, *ips):
    def fake(host, port, *a, **k):
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in ips]
    monkeypatch.setattr(socket, 'getaddrinfo', fake)


def test_any_private_answer_rejects_the_host(monkeypatch):
    _resolve_to(monkeypatch, '93.184.216.34', '10.0.0.5')
    with pytest.raises(safe_http.UnsafeDestination):
        safe_http.validate_public_https_url('https://rebind.example.com/hook')


def test_http_and_credentials_rejected():
    for url in ('http://example.com/', 'https://user:pass@example.com/', 'https://127.0.0.1/', 'https://[::1]/'):
        with pytest.raises(safe_http.UnsafeDestination):
            safe_http.validate_public_https_url(url)


class _FakeResp:
    status = 200
    headers = {}

    def read(self, n):
        return b'secret interne du serveur distant'

    def release_conn(self):
        pass


def test_request_connects_to_validated_ip_with_original_hostname(monkeypatch):
    _resolve_to(monkeypatch, '93.184.216.34')
    seen = {}

    class Pool:
        def urlopen(self, method, path, **kw):
            seen.update(method=method, path=path, headers=kw['headers'], redirect=kw['redirect'])
            return _FakeResp()

        def close(self):
            pass

    def fake_pool(ip, port, hostname, timeout):
        seen.update(ip=ip, hostname=hostname)
        return Pool()
    monkeypatch.setattr(safe_http, '_open_pool', fake_pool)
    # Toute seconde résolution (DNS rebinding) ferait échouer le test.
    resp = safe_http.safe_request('POST', 'https://hooks.example.com/x?a=1', data=b'{}')
    assert seen['ip'] == '93.184.216.34' and seen['hostname'] == 'hooks.example.com'
    assert seen['headers']['Host'] == 'hooks.example.com' and seen['redirect'] is False
    assert seen['path'] == '/x?a=1' and resp.status_code == 200


def test_tls_pool_verifies_certificate_for_hostname():
    pool = safe_http._open_pool('93.184.216.34', 443, 'hooks.example.com', 5)
    assert pool.host == '93.184.216.34'
    assert pool.conn_kw.get('server_hostname') == 'hooks.example.com'
    assert pool.assert_hostname == 'hooks.example.com' and pool.cert_reqs == 'CERT_REQUIRED'


def test_webhook_delivery_stores_status_only(monkeypatch):
    from profitos import webhooks_outbound as wo

    class Conn:
        rows = []

        def execute(self, sql, params=()):
            if sql.strip().startswith('INSERT'):
                Conn.rows.append(params)
            return self

        def fetchall(self):
            return [{'id': 1, 'url': 'https://hooks.example.com/x', 'secret': 's', 'events': 'invoice.sent'}]

        def commit(self):
            pass
    monkeypatch.setattr(wo, 'safe_request', lambda *a, **k: safe_http.SafeResponse(500, b'stack trace interne', {}))
    wo.deliver_webhook(Conn(), 'invoice.sent', {'a': 1})
    snippet = Conn.rows[0][-1]
    assert snippet == 'HTTP 500' and 'stack' not in snippet

    def refused(*a, **k):
        raise safe_http.UnsafeDestination('refusée')
    Conn.rows = []
    monkeypatch.setattr(wo, 'safe_request', refused)
    wo.deliver_webhook(Conn(), 'invoice.sent', {'a': 1})
    assert Conn.rows[0][3] is None and 'refusée' in Conn.rows[0][-1]


def test_woocommerce_never_shows_remote_body(monkeypatch):
    from profitos import ecommerce
    monkeypatch.setattr(ecommerce, 'safe_request', lambda *a, **k: safe_http.SafeResponse(401, b'<html>debug token=abc</html>', {}))
    with pytest.raises(ValueError) as e:
        ecommerce.fetch_woocommerce_orders('boutique.example.fr', 'ck', 'cs')
    assert 'HTTP 401' in str(e.value) and 'debug' not in str(e.value)

    def internal(*a, **k):
        raise safe_http.UnsafeDestination('ressource interne')
    monkeypatch.setattr(ecommerce, 'safe_request', internal)
    with pytest.raises(ValueError, match='refusée'):
        ecommerce.fetch_woocommerce_orders('boutique.example.fr', 'ck', 'cs')
