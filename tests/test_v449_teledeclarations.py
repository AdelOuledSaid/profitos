"""v449 — Production réglementaire, lot 2 : télédéclarations (préparation contrôlée,
dépôt figé et horodaté, dépôt unique par période, rectificative, accusés de réception,
connecteur simulation et connecteur partenaire EDI)."""
import json

from support_v4xx import app, auth, flashes, post, signup, tenant  # noqa: F401
from profitos.accounting import create_entry
from profitos import teledeclaration as T

COMPANY = {'name': 'Telede SARL', 'legal_form': 'SARL', 'siret': '73282932000074', 'vat_number': 'FR44732829320',
           'address': '1 rue Test', 'postal_code': '75001', 'city': 'Paris', 'share_capital': '1000',
           'rcs_city': 'Paris', 'vat_regime': 'normal', 'payment_terms': '30 jours', 'late_penalty_rate': '10',
           'iban': 'FR7630006000011234567890189', 'bic': 'AGRIFRPP'}


def _setup(app, role=None):
    client, _, org, _ = signup(app, 'Telede SARL', plan='BUSINESS', verified=True)
    post(client, '/company', COMPANY)
    c = tenant(org)
    create_entry(c, 'BQ', '2025-01-05', 'Capital', [{'account_code': '512000', 'debit': 20000}, {'account_code': '101300', 'credit': 20000}])
    create_entry(c, 'VE', '2025-03-01', 'Vente', [{'account_code': '411000', 'debit': 120000, 'auxiliary_name': 'C'},
                                                 {'account_code': '706000', 'credit': 100000},
                                                 {'account_code': '445710', 'credit': 20000, 'vat_rate': 20.0, 'vat_base': 100000}])
    create_entry(c, 'AC', '2025-03-02', 'Achat', [{'account_code': '606400', 'debit': 30000}, {'account_code': '445660', 'debit': 6000},
                                                 {'account_code': '401000', 'credit': 36000, 'auxiliary_name': 'F'}])
    c.close()
    if role:
        auth('UPDATE memberships SET role=? WHERE organization_id=?', (role, org))
    return client, org


CA3 = {'type': 'TVA_CA3', 'du': '2025-03-01', 'au': '2025-03-31', 'regime': 'normal', 'certify': '1'}


def test_build_ca3_and_liasse_payloads(app):
    _client, org = _setup(app)
    c = tenant(org)
    ident = {'name': 'Telede SARL', 'siret': '73282932000074'}
    b = T.build_declaration(c, 'TVA_CA3', '2025-03-01', '2025-03-31', identity=ident)
    boxes = {(x['code'], x.get('kind')): x['amount'] for x in b['payload']['boxes']}
    assert boxes[('16', None)] == 20000 and boxes[('20', None)] == 6000 and boxes[('28', None)] == 14000
    assert b['payload']['payment'] == {'amount': 14000, 'method': 'hors_edi'} and not b['errors']
    l = T.build_declaration(c, 'LIASSE', '2025-01-01', '2025-12-31', identity=ident, regime='normal')
    cases = {(x['form'], x['code']): x['amount'] for x in l['payload']['boxes']}
    assert cases[('2053', 'HN')] == 70000 and cases[('2050', 'BX')] == 120000 and not l['errors']
    bad = T.build_declaration(c, 'TVA_CA3', '2025-03-01', '2025-03-31', identity={'name': 'X', 'siret': '12345678900000'})
    assert any('SIREN' in e for e in bad['errors'])
    future = T.build_declaration(c, 'TVA_CA3', '2099-01-01', '2099-01-31', identity=ident)
    assert 'Période non terminée.' in future['errors']
    c.close()
    assert T.payload_hash(b['payload']) == T.payload_hash(json.loads(json.dumps(b['payload'])))


def test_submit_simulation_lifecycle_and_single_active(app):
    client, org = _setup(app)
    r = post(client, '/comptabilite/teledeclarations/deposer', CA3)
    assert r.status_code == 302 and '/comptabilite/teledeclarations/' in r.headers['Location']
    did = int(r.headers['Location'].rstrip('/').split('/')[-1])
    post(client, '/comptabilite/teledeclarations/deposer', CA3)
    assert any('déjà déposée' in m for m in flashes(client))
    c = tenant(org)
    assert c.execute('SELECT COUNT(*) FROM teledeclarations').fetchone()[0] == 1
    row = c.execute('SELECT * FROM teledeclarations WHERE id=?', (did,)).fetchone()
    assert row['mode'] == 'simulation' and row['status'] == 'deposited' and len(row['payload_hash']) == 64
    c.close()
    post(client, f'/comptabilite/teledeclarations/{did}/actualiser')
    post(client, f'/comptabilite/teledeclarations/{did}/actualiser')
    c = tenant(org)
    row = c.execute('SELECT * FROM teledeclarations WHERE id=?', (did,)).fetchone()
    c.close()
    assert row['status'] == 'accepted' and row['ack_number'].startswith('AR-')
    assert len(json.loads(row['history_json'])) == 3
    # Rectificative possible une fois la déclaration acceptée.
    post(client, '/comptabilite/teledeclarations/deposer', dict(CA3, corrective='1'))
    c = tenant(org)
    rows = c.execute('SELECT corrective,replaces_id FROM teledeclarations ORDER BY id').fetchall()
    c.close()
    assert len(rows) == 2 and rows[1]['corrective'] == 1 and rows[1]['replaces_id'] == did
    html = client.get(f'/comptabilite/teledeclarations/{did}').get_data(as_text=True)
    assert 'Acceptée' in html and 'simulation' in html.lower()
    j = client.get(f'/comptabilite/teledeclarations/{did}/contenu.json')
    assert j.status_code == 200 and json.loads(j.data)['form'] == '3310-CA3'


def test_submit_requires_certification_and_role(app):
    client, org = _setup(app)
    post(client, '/comptabilite/teledeclarations/deposer', dict(CA3, certify=''))
    assert any('certification' in m for m in flashes(client))
    client2, org2 = _setup(app, role='MEMBER')
    post(client2, '/comptabilite/teledeclarations/deposer', CA3)
    assert any('propriétaire' in m for m in flashes(client2))
    c = tenant(org2)
    assert c.execute('SELECT COUNT(*) FROM teledeclarations').fetchone()[0] == 0
    c.close()


def test_pages_render(app):
    client, _org = _setup(app)
    html = client.get('/comptabilite/teledeclarations').get_data(as_text=True)
    assert 'Mode simulation' in html and 'Préparer' in html
    for q in ('type=TVA_CA3&du=2025-03-01&au=2025-03-31', 'type=LIASSE&du=2025-01-01&au=2025-12-31',
              'type=LIASSE&du=2025-01-01&au=2025-12-31&regime=simplifie', 'type=IS_2572&du=2025-01-01&au=2025-12-31',
              'type=IS_2571&du=2025-01-01&au=2025-12-31', 'type=CVAE_1330&du=2025-01-01&au=2025-12-31'):
        r = client.get('/comptabilite/teledeclarations/preparer?' + q)
        assert r.status_code == 200, q
    assert client.get('/comptabilite/teledeclarations/preparer?type=XXX').status_code == 404


def test_partner_connector_contract(app, monkeypatch):
    calls = []

    class Resp:
        def __init__(self, code, data):
            self.status_code, self._d = code, data

        def json(self):
            return self._d

    import requests
    monkeypatch.setattr(requests, 'post', lambda url, json=None, headers=None, timeout=None:
                        calls.append(('POST', url, headers)) or Resp(201, {'id': 'EDI-42', 'status': 'deposited'}))
    monkeypatch.setattr(requests, 'get', lambda url, headers=None, timeout=None:
                        calls.append(('GET', url, headers)) or Resp(200, {'status': 'rejected', 'rejection_reasons': ['Case 08 incohérente']}))
    monkeypatch.setenv('EDI_PARTNER_ENABLED', '1')
    monkeypatch.setenv('EDI_PARTNER_API_URL', 'https://edi.example/api')
    monkeypatch.setenv('EDI_PARTNER_API_KEY', 'secret-key')
    assert T.partner_configured() and isinstance(T.get_connector(), T.PartnerConnector)
    client, org = _setup(app)
    assert 'Partenaire EDI connecté' in client.get('/comptabilite/teledeclarations').get_data(as_text=True)
    r = post(client, '/comptabilite/teledeclarations/deposer', CA3)
    did = int(r.headers['Location'].rstrip('/').split('/')[-1])
    assert calls[0][0] == 'POST' and calls[0][1] == 'https://edi.example/api/declarations'
    assert calls[0][2]['Authorization'] == 'Bearer secret-key' and len(calls[0][2]['Idempotency-Key']) == 64
    post(client, f'/comptabilite/teledeclarations/{did}/actualiser')
    c = tenant(org)
    row = c.execute('SELECT * FROM teledeclarations WHERE id=?', (did,)).fetchone()
    c.close()
    assert row['mode'] == 'production' and row['remote_id'] == 'EDI-42' and row['status'] == 'rejected'
    assert json.loads(row['rejection_json']) == ['Case 08 incohérente']
    # Une déclaration rejetée ne bloque pas un nouveau dépôt.
    monkeypatch.setattr(requests, 'post', lambda url, json=None, headers=None, timeout=None: Resp(201, {'id': 'EDI-43'}))
    post(client, '/comptabilite/teledeclarations/deposer', CA3)
    c = tenant(org)
    assert c.execute('SELECT COUNT(*) FROM teledeclarations').fetchone()[0] == 2
    c.close()


def test_partner_not_enabled_without_https_or_flag(monkeypatch):
    monkeypatch.setenv('EDI_PARTNER_API_URL', 'http://edi.example')
    monkeypatch.setenv('EDI_PARTNER_API_KEY', 'k')
    monkeypatch.setenv('EDI_PARTNER_ENABLED', '1')
    assert not T.partner_configured()
    monkeypatch.setenv('EDI_PARTNER_API_URL', 'https://edi.example')
    monkeypatch.delenv('EDI_PARTNER_ENABLED')
    assert not T.partner_configured() and isinstance(T.get_connector(), T.SimulationConnector)
