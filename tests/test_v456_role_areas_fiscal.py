"""v456 — Toute page de la comptabilité et de la fiscalité est rattachée à un module de rôle :
un rôle sans accès à la comptabilité (commercial) ne peut ouvrir ni la DAS2, ni l'IFU, ni
les déclarations."""
import pytest

import profitos.runtime as rt
from profitos import create_app
from profitos.config import DevelopmentConfig


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    return a


def test_every_accounting_route_has_a_role_area(app):
    missing = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith('/comptabilite'):
            continue
        fn = app.view_functions[rule.endpoint]
        if not (rt.ENDPOINT_AREAS.get(rule.endpoint) or rt.MODULE_AREAS.get(fn.__module__)):
            missing.append(rule.rule)
    assert not missing, missing


def test_commercial_role_blocked_from_fiscal_pages(app):
    import uuid
    cl = app.test_client()
    csrf = 'c' * 64
    with cl.session_transaction() as s:
        s['csrf_token'] = csrf
    cl.post('/signup', data={'csrf_token': csrf, 'accept_terms': '1', 'full_name': 'Alice Martin',
                             'email': f'{uuid.uuid4().hex[:10]}@example.com', 'password': 'Brume-Cactus-Velo-48',
                             'company_name': 'Exemple SARL'})
    with cl.session_transaction() as s:
        org_id, uid = s['org_id'], s['user_id']
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='MULTI',status='ACTIVE_PAID' WHERE id=?", (org_id,))
    ac.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,))
    ac.execute("UPDATE memberships SET role='COMMERCIAL' WHERE organization_id=?", (org_id,))
    ac.commit(); ac.close()
    for url in ('/comptabilite/das2', '/comptabilite/ifu', '/comptabilite/dividendes-2777', '/comptabilite/cfe',
                '/comptabilite/teledeclarations', '/comptabilite/profil-fiscal', '/comptabilite/tva/ca12'):
        r = cl.get(url)
        assert r.status_code == 302 and '/comptabilite' not in r.headers['Location'], url
