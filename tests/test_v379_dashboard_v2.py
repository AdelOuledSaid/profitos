"""Tableau de bord v2 : en-tête personnel, pouls de trésorerie, indicateurs cliquables."""
import uuid

import pytest

CSRF = 'd' * 64
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
    payload = dict(data or {}); payload['csrf_token'] = CSRF
    return client.post(url, data=payload)


def _signup(app, plan=None):
    from profitos import runtime as rt
    client = app.test_client()
    r = _post(client, '/signup', {'full_name': 'Camille Martin', 'email': f'{uuid.uuid4().hex[:10]}@example.com',
                                  'password': PASSWORD, 'company_name': 'Tableau SAS'})
    assert r.status_code in (302, 303)
    if plan:
        with client.session_transaction() as s:
            org_id = s['org_id']
        c = rt.auth_cx()
        c.execute("UPDATE organizations SET plan=?,status='ACTIVE_PAID' WHERE id=?", (plan, org_id))
        c.commit(); c.close()
    return client


def test_trial_dashboard_greets_and_hides_cash_pulse(app):
    html = _signup(app).get('/').get_data(as_text=True)
    assert 'Bonjour Camille.' in html
    assert 'db-kpis' in html
    assert 'db-pulse' not in html  # Trésorerie & scénarios non inclus dans l'essai


def test_paid_dashboard_without_balance_invites_to_enter_it(app):
    html = _signup(app, 'MULTI').get('/').get_data(as_text=True)
    assert 'Renseigner mon solde' in html


def test_paid_dashboard_with_balance_shows_projection(app):
    client = _signup(app, 'MULTI')
    _post(client, '/cash-intelligence', {'cash_balance': '15000'})
    html = client.get('/').get_data(as_text=True)
    assert 'data-count-to="15000"' in html
    assert 'Dans 30 jours' in html and 'Voir les scénarios' in html
