"""v411 — Cabinet : le rôle est vérifié dans l'organisation de l'URL.

Un utilisateur PROPRIÉTAIRE de A mais seulement COMMERCIAL de B ne doit ni lister, ni
télécharger, ni déposer de documents de B depuis A, ni voir B dans le portefeuille.
"""
import io

from support_v4xx import app, auth, post, signup  # noqa: F401


def _two_orgs(app, role_in_b='COMMERCIAL'):
    # Le cabinet (portefeuille, documents) fait partie de la formule Multi-sociétés.
    client, _, org_a, uid = signup(app, 'Cabinet A', plan='MULTI')
    _, _, org_b, _ = signup(app, 'Client B', plan='MULTI')
    auth("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,?,'2026-01-01')", (uid, org_b, role_in_b))
    auth("INSERT INTO cabinet_documents(organization_id,uploaded_by,filename,category,stored_name,uploaded_at) "
         "VALUES(?,?,?,?,?,'2026-01-01')", (org_b, uid, 'bilan.pdf', 'Autre', 'x.pdf'))
    doc_id = auth('SELECT MAX(id) id FROM cabinet_documents', (), 'one')['id']
    return client, org_a, org_b, doc_id


def test_commercial_in_url_org_cannot_reach_cabinet_documents(app):
    client, org_a, org_b, doc_id = _two_orgs(app)
    assert client.get(f'/cabinet/documents/{org_b}').status_code == 404
    assert client.get(f'/cabinet/documents/{org_b}/{doc_id}/telecharger').status_code == 404
    assert client.get(f'/cabinet/temps/{org_b}').status_code == 404
    r = post(client, f'/cabinet/documents/{org_b}', {'file': (io.BytesIO(b'%PDF-1.4'), 'x.pdf')},
             content_type='multipart/form-data')
    assert r.status_code == 404
    assert auth('SELECT COUNT(*) n FROM cabinet_documents WHERE organization_id=?', (org_b,), 'one')['n'] == 1
    # Sa propre organisation reste accessible.
    assert client.get(f'/cabinet/documents/{org_a}').status_code == 200


def test_portfolio_and_cabinet_list_only_cabinet_roles(app):
    client, org_a, org_b, _ = _two_orgs(app)
    cab = client.get('/cabinet/portefeuille').get_data(as_text=True)
    assert f'/cabinet/documents/{org_a}' in cab and f'/cabinet/documents/{org_b}' not in cab
    assert f'/cabinet/client/{org_b}/ouvrir' not in cab
    from profitos.routes import cabinet
    with app.test_request_context('/'):
        from flask import session
        with client.session_transaction() as s:
            uid = s['user_id']
        session['user_id'] = uid
        assert [o['id'] for o in cabinet.cabinet_organizations()] == [org_a]


def test_accountant_role_in_url_org_is_allowed(app):
    client, org_a, org_b, doc_id = _two_orgs(app, role_in_b='COMPTABLE')
    assert client.get(f'/cabinet/documents/{org_b}').status_code == 200
    assert 'Client B' in client.get('/cabinet/portefeuille').get_data(as_text=True)


def test_entity_restricted_accountant_has_no_org_wide_documents(app):
    client, org_a, org_b, _ = _two_orgs(app, role_in_b='COMPTABLE')
    uid = auth('SELECT user_id FROM memberships WHERE organization_id=?', (org_a,), 'one')['user_id']
    from support_v4xx import tenant
    tc = tenant(org_b)
    tc.execute("INSERT INTO user_entity_access(user_id,entity_id,created_at) VALUES(?,NULL,'2026-01-01')", (uid,))
    tc.commit(); tc.close()
    assert client.get(f'/cabinet/documents/{org_b}').status_code == 404
