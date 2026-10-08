"""Suite de l'audit : émission manuelle, /ops/health restreint, comptable extérieur.

Tests fonctionnels (client Flask réel, base SQLite locale) :
- « Marquer comme émise » : brouillon -> émise + écriture de vente, sans e-mail.
- /ops/health : détail des dépendances uniquement avec OPS_HEALTH_TOKEN.
- Invitation comptable : un cabinet connecté sur SA propre organisation peut accepter ;
  il n'obtient que l'entité invitée ; la révocation lui retire tout accès (avant, elle
  supprimait la seule restriction d'entité et lui ouvrait donc toutes les entités).
"""
import re
import uuid
from datetime import date, timedelta

import pytest

CSRF = 'c' * 64
PASSWORD = 'Sup3r-Secret-Pass!2026'


@pytest.fixture()
def app():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    return app


def _post(client, url, data=None):
    with client.session_transaction() as s:
        s['csrf_token'] = CSRF
    payload = dict(data or {})
    payload['csrf_token'] = CSRF
    return client.post(url, data=payload)


def _flashes(client):
    with client.session_transaction() as s:
        return [m for _, m in s.get('_flashes', [])]


def _signup(app, company, paid=True):
    from profitos import runtime as rt
    client = app.test_client()
    email = f"{uuid.uuid4().hex[:10]}@example.com"
    r = _post(client, '/signup', {'full_name': 'Test', 'email': email, 'password': PASSWORD, 'company_name': company})
    assert r.status_code in (302, 303)
    with client.session_transaction() as s:
        org_id, user_id = s['org_id'], s['user_id']
    if paid:
        c = rt.auth_cx()
        c.execute("UPDATE organizations SET plan='MULTI',status='ACTIVE_PAID' WHERE id=?", (org_id,))
        c.commit(); c.close()
    return client, email, org_id, user_id


def _tenant(org_id):
    from profitos import runtime as rt
    from profitos import db as dbmod
    return dbmod.connect_tenant(org_id, rt.tenant_db(org_id))


def test_mark_issued_issues_and_books_a_draft_without_email(app):
    client, _, org_id, _ = _signup(app, 'Emission SAS')
    _post(client, '/company', {'name': 'Emission SAS', 'siret': '73282932000074', 'address': '1 rue', 'postal_code': '75001', 'city': 'Paris'})
    _post(client, '/facturation/nouvelle', {'client_name': 'Client Courrier', 'due_date': (date.today() + timedelta(days=30)).isoformat(),
                                            'label_1': 'Mission', 'qty_1': '1', 'price_1': '1000', 'vat_1': '20'})
    c = _tenant(org_id)
    inv_id = c.execute("SELECT id FROM outgoing_invoices WHERE client_name='Client Courrier'").fetchone()['id']
    c.close()

    r = _post(client, f'/facturation/{inv_id}/marquer-emise')
    assert r.status_code == 302
    c = _tenant(org_id)
    inv = c.execute('SELECT status,issue_date FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()
    entry = c.execute("SELECT id FROM accounting_entries WHERE source_type='outgoing_invoice' AND source_id=?", (inv_id,)).fetchone()
    c.close()
    assert inv['status'] == 'sent' and inv['issue_date'] == date.today().isoformat()
    assert entry is not None

    # Un règlement peut désormais être saisi.
    _post(client, f'/facturation/{inv_id}/reglement', {'amount': '1200'})
    c = _tenant(org_id)
    assert c.execute('SELECT status FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()['status'] == 'paid'
    c.close()

    # Une facture déjà émise ne peut pas être ré-émise.
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    assert any('brouillon' in m for m in _flashes(client))


def test_ops_health_hides_dependencies_without_token(app, monkeypatch):
    client = app.test_client()
    monkeypatch.setenv('OPS_HEALTH_TOKEN', 'x' * 40)
    assert 'dependencies' not in client.get('/ops/health').get_json()
    assert 'dependencies' not in client.get('/ops/health', headers={'X-Ops-Token': 'wrong'}).get_json()
    assert 'dependencies' in client.get('/ops/health', headers={'X-Ops-Token': 'x' * 40}).get_json()
    monkeypatch.delenv('OPS_HEALTH_TOKEN')
    assert 'dependencies' not in client.get('/ops/health', headers={'X-Ops-Token': ''}).get_json()


def test_external_accountant_can_accept_and_revocation_removes_all_access(app):
    from profitos import runtime as rt
    owner, _, org_a, _ = _signup(app, 'Client SAS')
    accountant, acc_email, org_b, acc_id = _signup(app, 'Cabinet Expert', paid=False)

    _post(owner, '/comptabilite/revision/nouvelle', {'review_type': 'courante', 'period_label': 'Sept 2026'})
    _post(owner, '/comptabilite/revision/1/cabinet/activer', {'accountant_email': acc_email, 'firm_name': 'Cabinet Expert'})
    _post(owner, '/comptabilite/revision/1/collaboration/inviter', {'email': acc_email})
    link = next(m for m in _flashes(owner) if 'Invitation créée' in m)
    path = re.search(r'(/collaboration-comptable/accepter/\S+)', link).group(1)
    assert f'/accepter/{org_a}/' in path

    # Le comptable est connecté sur SA propre organisation (org B).
    assert accountant.get(path).status_code == 200
    r = _post(accountant, path)
    assert r.status_code == 302
    with accountant.session_transaction() as s:
        assert s['org_id'] == org_a and s['role'] == 'COMPTABLE'
    ac = rt.auth_cx()
    assert ac.execute('SELECT role FROM memberships WHERE user_id=? AND organization_id=?', (acc_id, org_a)).fetchone()['role'] == 'COMPTABLE'
    ac.close()
    c = _tenant(org_a)
    assert c.execute('SELECT COUNT(*) n FROM user_entity_access WHERE user_id=?', (acc_id,)).fetchone()['n'] == 1
    c.close()

    # Un tiers avec un autre e-mail ne peut pas utiliser le lien.
    intruder, *_ = _signup(app, 'Intrus SARL', paid=False)
    assert intruder.get(path).status_code == 404  # déjà acceptée

    # Révocation : plus aucune entité -> plus d'adhésion (et non un accès complet).
    _post(owner, '/comptabilite/revision/1/collaboration/invitation/1/revoquer')
    ac = rt.auth_cx()
    assert ac.execute('SELECT 1 FROM memberships WHERE user_id=? AND organization_id=?', (acc_id, org_a)).fetchone() is None
    ac.close()
    accountant.get('/comptabilite/revision')
    with accountant.session_transaction() as s:
        assert s['org_id'] == org_b


def test_invitation_link_cannot_be_used_by_another_email(app):
    owner, _, org_a, _ = _signup(app, 'Client Deux SAS')
    other, *_ = _signup(app, 'Autre Cabinet', paid=False)
    _post(owner, '/comptabilite/revision/nouvelle', {'review_type': 'courante', 'period_label': 'Oct 2026'})
    _post(owner, '/comptabilite/revision/1/cabinet/activer', {'accountant_email': 'vrai@cabinet.example', 'firm_name': 'Cabinet'})
    _post(owner, '/comptabilite/revision/1/collaboration/inviter', {'email': 'vrai@cabinet.example'})
    path = re.search(r'(/collaboration-comptable/accepter/\S+)', next(m for m in _flashes(owner) if 'Invitation' in m)).group(1)
    assert other.get(path).status_code == 403
    assert _post(other, path).status_code == 403
    assert other.get(f'/collaboration-comptable/accepter/{org_a}/faux-jeton').status_code == 404
