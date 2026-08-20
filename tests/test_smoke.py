from profitos import create_app
from profitos.config import DevelopmentConfig
from profitos.runtime import safe_next_url, validate_webhook_url, password_error


def make_client():
    app=create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    return app, app.test_client()


def test_healthz():
    app,client=make_client(); r=client.get('/healthz')
    assert r.status_code==200
    assert r.get_json()['status']=='ok'
    assert r.get_json()['version']=='1.4.1'


def test_login_page():
    app,client=make_client(); r=client.get('/login')
    assert r.status_code==200


def test_readyz_sqlite():
    app,client=make_client(); r=client.get('/readyz')
    assert r.status_code==200
    assert r.get_json()['status']=='ready'
    assert r.get_json()['database'] in ('sqlite','postgresql')


def test_security_headers():
    app,client=make_client(); r=client.get('/login')
    assert r.headers['X-Content-Type-Options']=='nosniff'
    assert r.headers['X-Frame-Options']=='DENY'
    assert 'frame-ancestors' in r.headers['Content-Security-Policy']
    assert r.headers['Cache-Control'].startswith('no-store')


def test_open_redirect_blocked():
    assert safe_next_url('/recover')=='/recover'
    assert safe_next_url('https://evil.example') is None
    assert safe_next_url('//evil.example') is None


def test_webhook_allowlist():
    assert validate_webhook_url('https://hooks.slack.com/services/A/B/C')
    assert not validate_webhook_url('http://hooks.slack.com/services/A/B/C')
    assert not validate_webhook_url('https://127.0.0.1/internal')


def test_password_policy():
    assert password_error('short1')
    assert password_error('abcdefghijk')
    assert password_error('12345678901')
    assert password_error('ProfitOS2026') is None
