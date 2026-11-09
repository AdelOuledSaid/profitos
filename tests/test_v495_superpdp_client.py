"""v495 — Client SUPER PDP : OAuth 2.1 avec PKCE, rotation des jetons, dépôt, cycle de vie,
réception, annuaire. Le réseau est simulé : ces tests valident le dialogue tel que la
documentation le décrit ; seule une session sur le bac à sable SUPER PDP valide l'intégration."""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from profitos import superpdp as SP


class Resp:
    def __init__(self, status=200, body=None, content=None, ctype='application/json'):
        self.status_code = status
        self._body = body
        self.content = content if content is not None else (json.dumps(body).encode() if body is not None else b'')
        self.headers = {'content-type': ctype}
        self.text = self.content.decode('utf-8', 'replace')

    def json(self):
        if self._body is None:
            raise ValueError('pas de JSON')
        return self._body


class FakeHttp:
    """Simule l'API : file de réponses par (méthode, chemin) ; garde chaque appel."""

    def __init__(self):
        self.calls, self.routes, self.token_n = [], {}, 0

    def post(self, url, data=None, headers=None, timeout=None):          # point /oauth2/token
        self.calls.append(('TOKEN', url, dict(data)))
        self.token_n += 1
        return Resp(200, {'access_token': f'acc-{self.token_n}', 'refresh_token': f'ref-{self.token_n}',
                          'expires_in': 1800})

    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None):
        path = urlparse(url).path
        self.calls.append((method, path, {'params': params, 'json': json, 'data': data, 'headers': headers}))
        queue = self.routes.get((method, path)) or [Resp(404, {'message': 'introuvable'})]
        return queue.pop(0) if len(queue) > 1 else queue[0]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('SUPERPDP_CLIENT_ID', 'veqalio-app')
    monkeypatch.setenv('SUPERPDP_CLIENT_SECRET', 'secret-app')
    monkeypatch.setenv('APP_BASE_URL', 'https://veqalio.com')
    monkeypatch.delenv('SUPERPDP_REDIRECT_URI', raising=False)
    monkeypatch.delenv('SUPERPDP_BASE_URL', raising=False)


def fresh(**extra):
    return {'access_token': 'acc-0', 'refresh_token': 'ref-0', 'expires_at': int(time.time()) + 1800, **extra}


def test_pkce_et_adresse_d_autorisation():
    verifier, challenge = SP.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    url = SP.authorize_url('etat-123', challenge, siren='123456789')
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.netloc == 'api.superpdp.tech' and u.path == '/oauth2/authorize'
    assert q['client_id'] == 'veqalio-app' and q['code_challenge_method'] == 'S256' and q['state'] == 'etat-123'
    assert q['redirect_uri'] == 'https://veqalio.com/facture-electronique/superpdp/retour'
    assert q['superpdp_company_number'] == '123456789' and q['superpdp_company_number_scheme'] == 'fr_siren'
    assert 'secret-app' not in url                        # le secret ne passe jamais par le navigateur


def test_echange_du_code_puis_rotation_enregistree():
    http = FakeHttp()
    tokens = SP.exchange_code('code-xyz', 'verif', http=http)
    assert tokens['access_token'] == 'acc-1' and tokens['refresh_token'] == 'ref-1'
    form = http.calls[0][2]
    assert form['grant_type'] == 'authorization_code' and form['code_verifier'] == 'verif'
    saved = []
    client = SP.Client({**tokens, 'expires_at': 0}, on_refresh=saved.append, http=http)    # jeton expiré
    http.routes[('GET', '/v1.beta/oauth2_sessions/me')] = [Resp(200, {'company_verification_status': 'verified'})]
    assert client.is_verified()
    assert http.calls[1][2]['grant_type'] == 'refresh_token' and http.calls[1][2]['refresh_token'] == 'ref-1'
    assert saved and saved[-1]['refresh_token'] == 'ref-2'   # le NOUVEAU jeton est enregistré


def test_401_rafraichit_une_fois_puis_rejoue():
    http = FakeHttp()
    http.routes[('GET', '/v1.beta/companies/me')] = [Resp(401, {'error': 'expired'}), Resp(200, {'id': 7, 'name': 'Atelier'})]
    saved = []
    assert SP.Client(fresh(), on_refresh=saved.append, http=http).company()['id'] == 7
    assert len(saved) == 1


def test_403_entreprise_non_verifiee():
    http = FakeHttp()
    http.routes[('POST', '/v1.beta/invoices')] = [Resp(403, {'message': 'company not verified'})]
    with pytest.raises(SP.NotVerifiedError):
        SP.Client(fresh(), http=http).send_invoice(b'%PDF-1.7', 'FA-1')


def test_depot_factur_x_et_cycle_de_vie():
    http = FakeHttp()
    http.routes[('POST', '/v1.beta/invoices')] = [Resp(201, {'id': 42, 'direction': 'out', 'external_id': 'FA-2026-001'})]
    client = SP.Client(fresh(), http=http)
    inv = client.send_invoice(b'%PDF-1.7 factur-x', 'FA-2026-001')
    method, path, req = http.calls[-1]
    assert inv['id'] == 42 and req['params'] == {'external_id': 'FA-2026-001'}
    assert req['data'] == b'%PDF-1.7 factur-x' and req['headers']['Content-Type'] == 'application/pdf'
    http.routes[('GET', '/v1.beta/invoices/42')] = [Resp(200, {'id': 42, 'events': [
        {'id': 3, 'status_code': 'fr:202', 'created_at': '2026-11-09T10:00:02Z'},
        {'id': 1, 'status_code': 'fr:200', 'created_at': '2026-11-09T10:00:00Z'},
        {'id': 2, 'status_code': 'fr:201', 'created_at': '2026-11-09T10:00:01Z'}]})]
    code, label, reason = SP.current_status(client.get_invoice(42))
    assert (code, label, reason) == ('fr:202', 'Reçue par la plateforme du destinataire', None) and SP.is_terminal(code)


def test_rejet_avec_motif():
    code, label, reason = SP.current_status({'events': [
        {'status_code': 'fr:200', 'created_at': '2026-11-09T10:00:00Z'},
        {'status_code': 'fr:213', 'created_at': '2026-11-09T10:00:01Z', 'data': {'reason': 'BR-FR-05 : mention manquante'}}]})
    assert code == 'fr:213' and label == 'Rejetée' and 'BR-FR-05' in reason


def test_factures_recues_telechargement_et_statut_pousse():
    http = FakeHttp()
    http.routes[('GET', '/v1.beta/invoices')] = [Resp(200, {'data': [{'id': 9, 'direction': 'in'}], 'has_after': False})]
    http.routes[('GET', '/v1.beta/invoices/9')] = [Resp(200, None, content=b'%PDF-1.7 original', ctype='application/pdf')]
    http.routes[('POST', '/v1.beta/invoice_events')] = [Resp(201, {'id': 1, 'status_code': 'fr:204'})]
    client = SP.Client(fresh(), http=http)
    assert client.received(starting_after_id=5)['data'][0]['id'] == 9
    assert http.calls[-1][2]['params'] == {'direction': 'in', 'limit': 50, 'starting_after_id': 5, 'order': 'asc'}
    content, ctype = client.download(9)
    assert content.startswith(b'%PDF') and ctype == 'application/pdf'
    client.push_status(9, 'fr:204')
    assert http.calls[-1][2]['json'] == {'invoice_id': 9, 'status_code': 'fr:204'}
    with pytest.raises(ValueError):
        client.push_status(9, 'fr:202')                    # statut réservé aux plateformes


def test_annuaire():
    http = FakeHttp()
    http.routes[('GET', '/v1.beta/french_directory/entries')] = [Resp(200, {'data': [{'identifier': '123456789'}]})]
    assert SP.Client(fresh(), http=http).directory('123456789') == [{'identifier': '123456789'}]


def test_sans_identifiants_erreur_claire(monkeypatch):
    monkeypatch.delenv('SUPERPDP_CLIENT_ID')
    assert not SP.is_configured()
    with pytest.raises(SP.SuperPdpConfigError):
        SP.authorize_url('s', 'c')
