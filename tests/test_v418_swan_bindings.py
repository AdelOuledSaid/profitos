"""v418 — Swan : comptes rattachés à l'organisation, virement refusé depuis un
compte non rattaché, webhook routé sans session, menu masqué sans SWAN_ENABLED."""
import pytest

from support_v4xx import app, auth, post, signup, tenant  # noqa: F401

import uuid
SECRET = 'w' * 40
A, B, W = (f'acc-{uuid.uuid4().hex[:8]}' for _ in range(3))


@pytest.fixture()
def swan_env(monkeypatch):
    monkeypatch.setenv('SWAN_CLIENT_ID', 'client')
    monkeypatch.setenv('SWAN_CLIENT_SECRET', 'secret')
    monkeypatch.setenv('SWAN_ENABLED', '1')
    monkeypatch.setenv('SWAN_WEBHOOK_SECRET', SECRET)


def test_compte_pro_hidden_and_404_without_explicit_enable(app, monkeypatch):
    monkeypatch.setenv('SWAN_CLIENT_ID', 'client')
    monkeypatch.setenv('SWAN_CLIENT_SECRET', 'secret')
    monkeypatch.delenv('SWAN_ENABLED', raising=False)
    client, *_ = signup(app, 'Sans Swan')
    assert client.get('/settings/swan').status_code == 404
    assert 'Compte Pro' not in client.get('/settings').get_data(as_text=True)


def test_accounts_filtered_to_organization_bindings(app, swan_env, monkeypatch):
    from profitos.routes import swan_baas as route
    client, _, org_a, _ = signup(app, 'Swan A')
    _, _, org_b, _ = signup(app, 'Swan B')
    from profitos.swan_baas import bind_account
    with app.app_context():
        bind_account(org_a, A)
        bind_account(org_b, B)
        with pytest.raises(ValueError):
            bind_account(org_a, B)  # déjà à une autre organisation
    monkeypatch.setattr(route, 'get_server_token', lambda: 'tok')
    monkeypatch.setattr(route, 'sync_onboarding_accounts', lambda token, org_id: 0)
    monkeypatch.setattr(route, 'list_accounts', lambda token, environment=None: [
        {'id': A, 'name': 'Compte A', 'IBAN': 'FR76A', 'available_balance': {}, 'booked_balance': {}, 'recent_transactions': []},
        {'id': B, 'name': 'Compte B', 'IBAN': 'FR76B', 'available_balance': {}, 'booked_balance': {}, 'recent_transactions': []}])
    page = client.get('/settings/swan').get_data(as_text=True)
    assert 'FR76A' in page and 'FR76B' not in page

    with client.session_transaction() as s:
        s['swan_user_id'] = 'u1'
    called = []
    monkeypatch.setattr(route, 'initiate_sepa_credit_transfer_sandbox', lambda *a, **k: called.append(a) or {})
    r = post(client, '/settings/swan/transfer/sepa', {'account_id': B, 'beneficiary_name': 'X',
             'beneficiary_iban': 'FR7630006000011234567890189', 'amount': '10', 'confirm_sandbox_transfer': 'yes'})
    assert r.status_code == 403 and not called


def test_webhook_routes_to_bound_org_without_session(app, swan_env):
    _, _, org_id, _ = signup(app, 'Swan Webhook')
    from profitos.swan_baas import bind_account
    with app.app_context():
        bind_account(org_id, W)
    client = app.test_client()
    payload = {'eventId': 'evt-' + W, 'eventType': 'Account.Updated', 'resourceId': W, 'projectId': 'p'}
    r = client.post('/webhooks/swan', json=payload, headers={'x-swan-secret': SECRET})
    assert r.status_code == 200 and r.get_json()['ok']
    tc = tenant(org_id)
    assert tc.execute('SELECT COUNT(*) n FROM swan_webhook_events WHERE event_id=?', ('evt-' + W,)).fetchone()['n'] == 1
    tc.close()
    assert client.post('/webhooks/swan', json=payload, headers={'x-swan-secret': SECRET}).get_json()['duplicate']
    unknown = client.post('/webhooks/swan', json=dict(payload, eventId='evt2-' + W, resourceId='inconnu'),
                          headers={'x-swan-secret': SECRET})
    assert unknown.status_code == 200 and unknown.get_json()['routed'] is False
    assert client.post('/webhooks/swan', json=payload, headers={'x-swan-secret': 'faux'}).status_code == 401
