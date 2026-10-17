"""Aides partagées par les tests de sécurité v41x (client Flask réel, SQLite locale)."""
import uuid

import pytest

CSRF = 's' * 64
PASSWORD = 'Brume-Cactus-Velo-48'


def make_app():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    return app


@pytest.fixture()
def app():
    return make_app()


def post(client, url, data=None, **kw):
    with client.session_transaction() as s:
        s['csrf_token'] = CSRF
    payload = dict(data or {})
    payload.setdefault('csrf_token', CSRF)
    return client.post(url, data=payload, **kw)


def flashes(client):
    with client.session_transaction() as s:
        return [m for _, m in s.get('_flashes', [])]


def auth(sql, params=(), fetch=None):
    from profitos import runtime as rt
    c = rt.auth_cx()
    try:
        cur = c.execute(sql, params)
        result = cur.fetchone() if fetch == 'one' else cur.fetchall() if fetch == 'all' else None
        c.commit()
        return result
    finally:
        c.close()


def tenant(org_id):
    from profitos import runtime as rt
    return rt.tenant_cx_direct(org_id)


def signup(app, company='Sécurité SAS', email=None, verified=False, plan=None):
    client = app.test_client()
    email = email or f'{uuid.uuid4().hex[:10]}@example.com'
    r = post(client, '/signup', {'accept_terms': '1', 'full_name': 'Alice Martin', 'email': email, 'password': PASSWORD,
                                 'company_name': company})
    assert r.status_code in (302, 303), r.status_code
    with client.session_transaction() as s:
        org_id, user_id = s['org_id'], s['user_id']
    if verified:
        auth('UPDATE users SET email_verified=1 WHERE id=?', (user_id,))
    if plan:
        auth("UPDATE organizations SET plan=?,status='ACTIVE_PAID' WHERE id=?", (plan, org_id))
    return client, email, org_id, user_id


def login(app, email, password=PASSWORD):
    client = app.test_client()
    r = post(client, '/login', {'email': email, 'password': password})
    return client, r
