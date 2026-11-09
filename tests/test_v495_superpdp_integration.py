"""v495 — SUPER PDP branchée dans Veqalio : raccordement (parcours hébergé), dépôt d'une facture
émise, suivi du cycle de vie, suspension des relances sur refus, tâche planifiée.
Le réseau est simulé (voir test_v495_superpdp_client.py)."""
import time
from urllib.parse import parse_qs, urlparse

import pytest

from profitos import superpdp as SP
from test_v421_invoice_compliance import FULL_PROFILE, _flashes, _post, _signup, app  # noqa: F401
from datetime import date, timedelta


def _db(org_id):
    from profitos import runtime as rt
    return rt.tenant_cx_direct(org_id)


def _invoice(org_id, inv_id):
    c = _db(org_id)
    row = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()
    c.close()
    return row


def _create_invoice(client, org_id):
    _post(client, '/facturation/nouvelle', {
        'client_name': 'Client Lumière SA', 'client_email': 'compta@client.example', 'client_address': '1 rue A\n69001 Lyon',
        'client_siren': '552100554', 'due_date': (date.today() + timedelta(days=30)).isoformat(),
        'operation_nature': 'services', 'label_1': 'Prestation de conseil', 'qty_1': '1', 'price_1': '1000', 'vat_1': '20'})
    c = _db(org_id)
    inv_id = c.execute('SELECT MAX(id) AS m FROM outgoing_invoices').fetchone()['m']
    c.close()
    return inv_id
from test_v495_superpdp_client import FakeHttp, Resp


@pytest.fixture(autouse=True)
def _no_boamp(monkeypatch):
    from profitos.routes import main as main_routes
    monkeypatch.setattr(main_routes, 'sync_grow', lambda: 0)


@pytest.fixture
def pdp(monkeypatch):
    """SUPER PDP configurée, réseau simulé : la vérification de l'entreprise est acquise."""
    monkeypatch.setenv('SUPERPDP_CLIENT_ID', 'veqalio-app')
    monkeypatch.setenv('SUPERPDP_CLIENT_SECRET', 'secret-app')
    monkeypatch.setenv('APP_BASE_URL', 'https://veqalio.com')
    monkeypatch.delenv('EINVOICE_PROVIDER', raising=False)
    http = FakeHttp()
    http.routes[('GET', '/v1.beta/oauth2_sessions/me')] = [Resp(200, {'company_verification_status': 'verified'})]
    http.routes[('GET', '/v1.beta/companies/me')] = [Resp(200, {'id': 7, 'name': 'Atelier Dupont SAS', 'number': '732829320'})]

    class Shim:                                # remplace le module requests vu par superpdp.py
        RequestException = Exception
        post = staticmethod(http.post)
        request = staticmethod(http.request)
    monkeypatch.setattr(SP, 'requests', Shim)
    return http


def connect(client):
    r = _post(client, '/facture-electronique/superpdp/relier')
    assert r.status_code == 302
    url = urlparse(r.headers['Location'])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == 'api.superpdp.tech' and q['superpdp_company_number'] == '732829320'
    return client.get(f"/facture-electronique/superpdp/retour?state={q['state']}&code=code-ok")


def test_raccordement_complet(app, pdp):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    page = client.get('/company').get_data(as_text=True)
    assert 'Relier la facture électronique' in page and 'Plateforme agréée : SUPER PDP' in page
    assert connect(client).status_code == 302
    assert any('reliée et vérifiée' in m for m in _flashes(client))
    page = client.get('/company').get_data(as_text=True)
    assert 'Reliée et vérifiée' in page and 'SIREN 732829320' in page
    c = _db(org_id)
    row = c.execute('SELECT * FROM superpdp_connections').fetchone()
    c.close()
    assert row['verified'] == 1 and row['refresh_token'] and 'ref-' not in row['refresh_token']   # chiffré


def test_retour_avec_etat_falsifie_refuse(app, pdp):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    _post(client, '/facture-electronique/superpdp/relier')
    client.get('/facture-electronique/superpdp/retour?state=pirate&code=volé')
    assert any('non reconnu' in m for m in _flashes(client))
    assert not [c for c in pdp.calls if c[0] == 'TOKEN']      # aucun échange de code


def test_depot_puis_cycle_de_vie_jusqu_au_refus(app, pdp):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    connect(client)
    inv_id = _create_invoice(client, org_id)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    inv = _invoice(org_id, inv_id)
    assert inv['status'] == 'sent'
    pdp.routes[('POST', '/v1.beta/invoices')] = [Resp(201, {'id': 42, 'direction': 'out',
                                                            'events': [{'id': 1, 'status_code': 'fr:200'}]})]
    _post(client, f'/facturation/{inv_id}/weinvoice')
    assert any('transmise à la plateforme agréée (identifiant 42)' in m for m in _flashes(client))
    sent = [c for c in pdp.calls if c[:2] == ('POST', '/v1.beta/invoices')][0][2]
    assert sent['data'][:4] == b'%PDF' and sent['params'] == {'external_id': inv['invoice_number']}
    inv = _invoice(org_id, inv_id)
    assert inv['weinvoice_invoice_id'] == '42' and inv['einvoice_provider'] == 'superpdp' and inv['weinvoice_status'] == 'SUBMITTED'
    # un second clic ne redépose rien
    _post(client, f'/facturation/{inv_id}/weinvoice')
    assert len([c for c in pdp.calls if c[:2] == ('POST', '/v1.beta/invoices')]) == 1
    # la plateforme du client a reçu, puis le client refuse : relances suspendues
    pdp.routes[('GET', '/v1.beta/invoices/42')] = [Resp(200, {'id': 42, 'events': [
        {'id': 1, 'status_code': 'fr:200', 'created_at': '2026-11-09T10:00:00Z'},
        {'id': 2, 'status_code': 'fr:202', 'created_at': '2026-11-09T10:00:01Z'},
        {'id': 3, 'status_code': 'fr:210', 'created_at': '2026-11-10T09:00:00Z', 'data': {'reason': 'Prix non conforme au devis'}}]})]
    _post(client, f'/facturation/{inv_id}/weinvoice/synchroniser')
    assert any('Refusée' in m and 'Prix non conforme' in m for m in _flashes(client))
    inv = _invoice(org_id, inv_id)
    assert inv['weinvoice_status'] == 'REFUSED' and inv['weinvoice_regulatory_code'] == '210'
    page = client.get(f'/facturation/{inv_id}').get_data(as_text=True)
    assert 'Le client a refusé cette facture' in page
    c = _db(org_id)
    evs = [r['regulatory_code'] for r in c.execute("SELECT * FROM einvoice_events WHERE provider='superpdp' AND event_type='status_sync' ORDER BY id").fetchall()]
    c.close()
    assert evs == ['200', '202', '210']
    # une resynchronisation ne duplique pas le journal
    _post(client, f'/facturation/{inv_id}/weinvoice/synchroniser')
    c = _db(org_id)
    assert c.execute("SELECT COUNT(*) AS n FROM einvoice_events WHERE provider='superpdp' AND event_type='status_sync'").fetchone()['n'] == 3
    c.close()


def test_envoi_refuse_tant_que_l_entreprise_n_est_pas_verifiee(app, pdp):
    pdp.routes[('GET', '/v1.beta/oauth2_sessions/me')] = [Resp(200, {'company_verification_status': 'pending'})]
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    connect(client)
    assert any('vérification' in m for m in _flashes(client))
    inv_id = _create_invoice(client, org_id)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    _post(client, f'/facturation/{inv_id}/weinvoice')
    assert any('pas encore vérifiée' in m for m in _flashes(client))
    assert not [c for c in pdp.calls if c[:2] == ('POST', '/v1.beta/invoices')]


def test_tache_planifiee_et_rotation_des_jetons(app, pdp):
    from profitos import einvoice_superpdp as ES, runtime as rt
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    connect(client)
    inv_id = _create_invoice(client, org_id)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    pdp.routes[('POST', '/v1.beta/invoices')] = [Resp(201, {'id': 77, 'events': []})]
    _post(client, f'/facturation/{inv_id}/weinvoice')
    c = rt.tenant_cx_direct(org_id)
    c.execute('UPDATE superpdp_connections SET expires_at=0')        # jeton d'accès expiré
    c.commit()
    before = c.execute('SELECT refresh_token FROM superpdp_connections').fetchone()['refresh_token']
    pdp.routes[('GET', '/v1.beta/invoices/77')] = [Resp(200, {'id': 77, 'events': [
        {'id': 9, 'status_code': 'fr:212', 'created_at': '2026-11-20T08:00:00Z'}]})]
    done, errors = ES.sync_pending(c, org_id)
    assert (done, errors) == (1, [])
    row = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()
    assert row['weinvoice_status'] == 'CASHED_IN'
    after = c.execute('SELECT refresh_token FROM superpdp_connections').fetchone()['refresh_token']
    assert after != before                                            # nouveau jeton enregistré
    assert ES.sync_pending(c, org_id) == (0, [])                       # statut final : plus interrogée
    c.close()


def test_weinvoice_reste_disponible_par_reglage(app, pdp, monkeypatch):
    monkeypatch.setenv('EINVOICE_PROVIDER', 'weinvoice')
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    page = client.get('/company').get_data(as_text=True)
    assert 'Relier la facture électronique' not in page and 'Tester la connexion' in page


def test_controle_de_mise_en_ligne_suit_la_plateforme_choisie():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('gl', Path(__file__).resolve().parent.parent / 'scripts' / 'go_live_check.py')
    gl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gl)
    base = {'APP_BASE_URL': 'https://veqalio.com', 'WEINVOICE_CLIENT_ID': 'w', 'WEINVOICE_CLIENT_SECRET': 'w'}
    assert gl.check_einvoice(base)[0]['section'] == 'Facture électronique (WeInvoice)'
    pdp = gl.check_einvoice({**base, 'SUPERPDP_CLIENT_ID': 'a', 'SUPERPDP_CLIENT_SECRET': 'b'})
    assert pdp[0]['section'] == 'Facture électronique (SUPER PDP)' and not [i for i in pdp if i['level'] == gl.FAIL]
    assert 'https://veqalio.com/facture-electronique/superpdp/retour' in pdp[1]['detail']
    bad = gl.check_einvoice({**base, 'SUPERPDP_CLIENT_ID': 'a', 'SUPERPDP_CLIENT_SECRET': 'b',
                             'SUPERPDP_REDIRECT_URI': 'https://ailleurs.example/retour'})
    assert [i for i in bad if i['level'] == gl.FAIL]
    assert gl.check_einvoice({'APP_BASE_URL': 'https://veqalio.com'})[0]['level'] == gl.WARN
