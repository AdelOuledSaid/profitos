from profitos import create_app
from profitos.config import DevelopmentConfig


def test_healthz():
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    client = app.test_client()
    r = client.get('/healthz')
    assert r.status_code == 200
    assert r.get_json()['status'] == 'ok'
    assert r.get_json()['version'] == '1.2'


def test_login_page():
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    client = app.test_client()
    r = client.get('/login')
    assert r.status_code == 200


def test_readyz_sqlite():
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    client = app.test_client()
    r = client.get('/readyz')
    assert r.status_code == 200
    assert r.get_json()['status'] == 'ready'
    assert r.get_json()['database'] in ('sqlite','postgresql')
