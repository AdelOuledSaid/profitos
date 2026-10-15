"""v447 — audit de sécurité et parcours métier : 2FA à compteur serveur, actions selon le
module du rôle, API selon la formule, avoirs au centime, devis acceptés hors e-mail,
saisies françaises, logo téléversé, dépendances corrigées."""
import io
import re
from pathlib import Path

from support_v4xx import PASSWORD, app, auth, flashes, login, post, signup, tenant  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _enable_2fa(client):
    from profitos import totp
    page = client.get('/compte/2fa').get_data(as_text=True)
    secret = re.search(r'secret=([A-Z2-7]+)', page).group(1)
    post(client, '/compte/2fa', {'code': totp.totp(secret)})
    return secret


def test_2fa_failures_counted_server_side_and_survive_relogin(app, monkeypatch):
    from profitos.runtime import limiter
    from profitos import account_security as acsec
    monkeypatch.setattr(limiter, 'enabled', False, raising=False)
    monkeypatch.setattr(acsec, 'notify_user', lambda *a, **k: None)
    client, email, _, uid = signup(app, 'Deux Facteurs Serveur')
    _enable_2fa(client)
    # Deux séries de 4 mauvais codes, séparées par une reconnexion au mot de passe :
    # le compteur du compte n'est pas remis à zéro par le mot de passe seul.
    for _ in range(2):
        c2, r = login(app, email)
        assert '/login/2fa' in r.headers['Location']
        for _ in range(4):
            post(c2, '/login/2fa', {'code': '000000'})
    row = auth('SELECT failed_login_count FROM users WHERE id=?', (uid,), 'one')
    assert int(row['failed_login_count'] or 0) >= 8
    for _ in range(3):
        c3, _ = login(app, email)
        post(c3, '/login/2fa', {'code': '000000'})
    assert auth('SELECT locked_until FROM users WHERE id=?', (uid,), 'one')['locked_until']


def test_commercial_cannot_touch_recovery_actions(app):
    client, _, org_id, uid = signup(app, 'Actions SAS', plan='BUSINESS')
    t = tenant(org_id)
    t.execute("INSERT INTO actions(kind,opportunity_id,title,draft,status,created_at) VALUES('RECOVER',1,'Relance secrète','x','PENDING','2026-10-01')")
    t.commit(); aid = t.execute('SELECT max(id) FROM actions').fetchone()[0]; t.close()
    auth('UPDATE memberships SET role=? WHERE organization_id=?', ('COMMERCIAL', org_id))
    assert 'Relance secrète' not in client.get('/actions').get_data(as_text=True)
    post(client, f'/actions/{aid}/status', {'status': 'CANCELLED'})
    t = tenant(org_id)
    assert t.execute('SELECT status FROM actions WHERE id=?', (aid,)).fetchone()[0] == 'PENDING'
    t.close()
    for url in ('/reports/weekly/send', '/integrations/ecommerce/shopify/synchroniser'):
        post(client, url)
        assert any('rôle' in m for m in flashes(client)), url


def test_api_key_refused_when_plan_has_no_api(app):
    from profitos.runtime import hash_api_key
    client, _, org_id, _ = signup(app, 'Api SAS', plan='PRO')
    raw = 'pk_test_' + 'a' * 40
    auth("INSERT INTO api_keys(organization_id,key_hash,scope,scopes,created_at) VALUES(?,?,?,?,?)",
         (org_id, hash_api_key(raw), 'read', 'read', '2026-10-01'))
    anon = app.test_client()
    assert anon.get('/api/v1/summary', headers={'Authorization': f'Bearer {raw}'}).status_code == 200
    auth("UPDATE organizations SET plan='STARTER' WHERE id=?", (org_id,))
    r = anon.get('/api/v1/summary', headers={'Authorization': f'Bearer {raw}'})
    assert r.status_code == 403 and r.get_json()['error'] in ('plan_without_api', 'plan_feature_unavailable')


def test_partial_credit_note_is_exact_with_several_vat_rates():
    from profitos.routes import invoicing as I
    src = [{'label': 'Conseil', 'vat_rate': 20, 'line_total': 300},
           {'label': 'Formation', 'vat_rate': 5.5, 'line_total': 50}]
    items = I._adjust_credit_items(I.credit_note_items(src, 12.75 / 412.75), 12.75)
    assert I._totals(items)[2] == 12.75


def _company(client):
    post(client, '/company', {'name': 'Flux SARL', 'legal_form': 'SARL', 'siret': '73282932000074',
                              'vat_number': 'FR44732829320', 'address': '1 rue Test', 'postal_code': '75001',
                              'city': 'Paris', 'share_capital': '1000', 'rcs_city': 'Paris', 'vat_regime': 'normal',
                              'payment_terms': '30 jours', 'late_penalty_rate': '10',
                              'iban': 'FR7630006000011234567890189', 'bic': 'AGRIFRPP'})


def test_quote_accepted_without_email_then_invoiced_and_settled(app):
    client, _, org_id, _ = signup(app, 'Devis SAS', plan='BUSINESS', verified=True)
    _company(client)
    lines = {'label_1': 'Conseil', 'qty_1': '3', 'price_1': '100', 'vat_1': '20',
             'label_2': 'Formation', 'qty_2': '1', 'price_2': '50', 'vat_2': '5.5'}
    post(client, '/facturation/devis/nouveau', {'client_name': 'Client A', 'client_address': '2 rue X\n75002 Paris',
                                                'client_siren': '732829320', 'valid_until': '2099-11-30',
                                                'operation_nature': 'services', **lines})
    t = tenant(org_id); qid = t.execute('SELECT max(id) FROM outgoing_quotes').fetchone()[0]; t.close()
    post(client, f'/facturation/devis/{qid}/accepter')
    post(client, f'/facturation/devis/{qid}/convertir')
    t = tenant(org_id); iid = t.execute('SELECT max(id) FROM outgoing_invoices').fetchone()[0]; t.close()
    assert iid
    post(client, f'/facturation/{iid}/marquer-emise')
    from profitos.accounting import paris_today
    today = paris_today().isoformat()
    post(client, f'/facturation/{iid}/reglement', {'amount': '200,00', 'payment_date': today, 'payment_method': 'virement'})
    post(client, f'/facturation/{iid}/avoir', {'reason': 'Remise', 'amount_ttc': '12,75'})
    post(client, f'/facturation/{iid}/reglement', {'amount': '200,00', 'payment_date': today, 'payment_method': 'virement'})
    t = tenant(org_id)
    assert t.execute('SELECT status FROM outgoing_invoices WHERE id=?', (iid,)).fetchone()[0] == 'paid'
    bal = t.execute("SELECT COALESCE(SUM(debit),0)-COALESCE(SUM(credit),0) FROM accounting_entry_lines WHERE account_code LIKE '411%'").fetchone()[0]
    t.close()
    assert abs(bal) < 0.005


def test_expense_line_accepts_french_amount(app):
    client, _, org_id, _ = signup(app, 'Frais SAS', plan='BUSINESS')
    post(client, '/notes-de-frais/nouvelle', {'period_label': 'Octobre'})
    t = tenant(org_id); rid = t.execute('SELECT max(id) FROM expense_reports').fetchone()[0]; t.close()
    post(client, f'/notes-de-frais/{rid}/ligne', {'expense_date': '2026-10-03', 'description': 'Taxi',
                                                  'category': 'autre', 'amount': '45,50'})
    t = tenant(org_id)
    assert t.execute('SELECT amount FROM expense_report_lines WHERE report_id=?', (rid,)).fetchone()[0] == 45.5
    t.close()


PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
       b'\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7V\xbd\xfa\x00\x00\x00\x00IEND\xaeB`\x82')


def test_logo_upload_validated_and_rendered_inline(app):
    client, _, org_id, _ = signup(app, 'Logo SAS', plan='BUSINESS')
    post(client, '/settings', {'logo_file': (io.BytesIO(b'<svg onload=alert(1)>'), 'x.svg')},
         content_type='multipart/form-data')
    assert any('PNG, JPEG ou WebP' in m for m in flashes(client))
    post(client, '/settings', {'logo_file': (io.BytesIO(PNG), 'logo.png')}, content_type='multipart/form-data')
    html = client.get('/').get_data(as_text=True)
    assert 'src="data:image/png;base64,' in html
    t = tenant(org_id)
    t.execute("UPDATE app_settings SET logo_url='https://tracker.example/x.png' WHERE id=1"); t.commit(); t.close()
    assert 'tracker.example' not in client.get('/').get_data(as_text=True)


def test_vulnerable_dependency_pins_upgraded():
    req = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
    pins = dict(re.findall(r'^([A-Za-z0-9_.-]+)==([0-9.]+)', req, re.M))
    ver = lambda s: tuple(int(x) for x in s.split('.'))
    assert ver(pins['Flask']) >= (3, 1, 3)
    assert ver(pins['pypdf']) >= (6, 15, 0)
    assert ver(pins['requests']) >= (2, 33, 0)
    assert ver(pins['Werkzeug']) >= (3, 1, 5)
