"""v508 — La connexion rouvre la dernière organisation consultée.

Veqalio ouvrait invariablement la première organisation du compte. Qui n'en a qu'une ne le
voyait pas ; qui en suit plusieurs retrouvait à chaque connexion un autre espace que celui
qu'il venait de quitter — sans ses factures ni son profil entreprise. Constaté en séance :
« tous les factures sont supprimées et même le profil d'entreprise », alors que rien n'avait
été effacé.
"""
import uuid

import pytest

import profitos.runtime as rt
from profitos import account_security as acsec
from profitos import create_app, storage
from profitos.config import DevelopmentConfig

CSRF = 'c' * 64
PWD = 'Brume-Cactus-Velo-48'


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    try:
        rt.limiter.enabled = False
    except Exception:
        pass
    cl = app.test_client()

    def csrf():
        with cl.session_transaction() as s:
            s['csrf_token'] = CSRF

    email = f'{uuid.uuid4().hex[:10]}@example.com'
    csrf()
    cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'first_name': 'Alice',
                             'last_name': 'Martin', 'email': email, 'password': PWD,
                             'company_name': 'Dossier A'})
    with cl.session_transaction() as s:
        uid, org_a = s['user_id'], s['org_id']
    # une seconde organisation pour le même utilisateur, créée après la première
    c = rt.auth_cx()
    c.execute("INSERT INTO organizations(name,plan,status) VALUES('Dossier B','BUSINESS','ACTIVE_PAID')")
    org_b = c.execute('SELECT id FROM organizations WHERE name=?', ('Dossier B',)).fetchone()['id']
    c.execute('INSERT INTO memberships(user_id,organization_id,role) VALUES(?,?,?)', (uid, org_b, 'OWNER'))
    c.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,))
    c.commit()
    c.close()
    yield cl, csrf, email, uid, org_a, org_b


def login(cl, csrf, email):
    csrf()
    cl.post('/login', data={'csrf_token': CSRF, 'email': email, 'password': PWD})
    with cl.session_transaction() as s:
        return s.get('org_id')


def test_la_derniere_organisation_est_rouverte(ctx):
    cl, csrf, email, _uid, org_a, org_b = ctx
    csrf()
    cl.post(f'/organizations/switch/{org_b}', data={'csrf_token': CSRF})
    with cl.session_transaction() as s:
        assert s['org_id'] == org_b
    cl.post('/logout', data={'csrf_token': CSRF})
    assert login(cl, csrf, email) == org_b          # et non org_a, la plus ancienne


def test_sans_preference_la_plus_ancienne_sert_toujours(ctx):
    """Comportement d'origine conservé pour un compte qui n'a jamais basculé."""
    cl, csrf, email, uid, org_a, _org_b = ctx
    c = rt.auth_cx()
    c.execute('UPDATE users SET last_org_id=NULL WHERE id=?', (uid,))
    c.commit()
    c.close()
    cl.post('/logout', data={'csrf_token': CSRF})
    assert login(cl, csrf, email) == org_a


def test_une_preference_caduque_est_ignoree(ctx):
    """Organisation supprimée ou quittée : on retombe sur une organisation valide plutôt que
    de refuser la connexion."""
    cl, csrf, email, uid, org_a, org_b = ctx
    c = rt.auth_cx()
    c.execute('UPDATE users SET last_org_id=? WHERE id=?', (org_b, uid))
    c.execute('DELETE FROM memberships WHERE user_id=? AND organization_id=?', (uid, org_b))
    c.commit()
    c.close()
    cl.post('/logout', data={'csrf_token': CSRF})
    assert login(cl, csrf, email) == org_a


def test_une_organisation_supprimee_n_est_pas_rouverte(ctx):
    cl, csrf, email, uid, org_a, org_b = ctx
    c = rt.auth_cx()
    c.execute('UPDATE users SET last_org_id=? WHERE id=?', (org_b, uid))
    c.execute("UPDATE organizations SET deleted_at='2026-11-15T00:00:00+00:00' WHERE id=?", (org_b,))
    c.commit()
    c.close()
    cl.post('/logout', data={'csrf_token': CSRF})
    assert login(cl, csrf, email) == org_a


def test_memoriser_n_interrompt_jamais_la_navigation(monkeypatch):
    """Cette mémoire est un confort : une base indisponible ne doit pas empêcher d'entrer."""
    def boom():
        raise RuntimeError('base indisponible')
    monkeypatch.setattr(acsec, 'auth_cx', boom, raising=False)
    monkeypatch.setattr(rt, 'auth_cx', boom)
    acsec.remember_org(1, 2)          # ne lève pas
    acsec.remember_org(None, None)
