"""v435 — Prêt pour les clients : menu simplifié, prénom/nom, imports
français (dates jj/mm/aaaa, montants « 1 234,56 », CSV « ; »), modèles CSV, SIREN réel."""
import io
from pathlib import Path

from support_v4xx import app, auth, login, post, signup  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ parsing français
def test_parse_date_reads_french_day_first():
    from profitos.runtime import parse_date
    assert str(parse_date('05/03/2026')) == '2026-03-05'
    assert str(parse_date('31/12/2026')) == '2026-12-31'
    assert str(parse_date('05.03.26')) == '2026-03-05'
    assert str(parse_date('2026-03-05')) == '2026-03-05'
    assert parse_date('pas une date') is None


def test_amounts_in_french_format_are_not_zeroed():
    from profitos.runtime import money, parse_amount
    assert parse_amount('1 234,56 €') == 1234.56
    assert parse_amount('1 000,00') == 1000.0
    assert parse_amount('1.234,56') == 1234.56
    assert parse_amount('1,234.56') == 1234.56
    assert parse_amount('-12,5') == -12.5
    assert parse_amount('(45,00)') == -45.0
    assert parse_amount('abc') is None and parse_amount('') is None and parse_amount(float('nan')) is None
    assert money('1 234,56') == 1234.56 and money(None) == 0.0 and money(12) == 12.0


def test_read_csv_any_accepts_french_excel_export(tmp_path):
    from profitos.routes.imports import read_csv_any
    p = tmp_path / 'f.csv'
    p.write_bytes('Numéro;Client;Montant TTC;Échéance\r\nFA-1;Dupont;1 200,50;15/10/2026\r\nFA-2;Durand;99,90;01/11/2026\r\n'.encode('cp1252'))
    df = read_csv_any(p)
    assert list(df.columns) == ['Numéro', 'Client', 'Montant TTC', 'Échéance']
    from profitos.runtime import parse_amount
    assert len(df) == 2 and parse_amount(df['Montant TTC'][0]) == 1200.5 and parse_amount(df['Montant TTC'][1]) == 99.9
    p.write_text('invoice_number,customer,amount,due_date\nA1,X,10.5,2026-10-01\n', encoding='utf-8')
    assert float(read_csv_any(p)['amount'][0]) == 10.5


def test_import_templates_match_importer_aliases():
    from profitos.routes.imports import IMPORT_TEMPLATES, import_template_csv
    from profitos.runtime import norm
    required = {'invoices': ['numero', 'client', 'montant ttc', 'echeance'],
                'expenses': ['fournisseur', 'montant', 'date'],
                'bank-statement': ['date', 'montant']}
    for kind, cols in required.items():
        filename, data = import_template_csv(kind)
        assert filename.endswith('.csv') and data.startswith(b'\xef\xbb\xbf')
        header = [norm(h) for h in data.decode('utf-8-sig').splitlines()[0].split(';')]
        for col in cols:
            assert col in header, (kind, col)
    assert set(IMPORT_TEMPLATES) == set(required)


def test_import_template_download_requires_login_and_serves_csv(app):
    client = app.test_client()
    assert client.get('/import/modele/invoices.csv').status_code in (301, 302)
    c, email, _, _ = signup(app, 'Modele SARL')
    client, _ = login(app, email)
    r = client.get('/import/modele/invoices.csv')
    assert r.status_code == 200 and 'attachment' in r.headers['Content-Disposition']
    assert 'Montant TTC' in r.get_data().decode('utf-8-sig')
    assert client.get('/import/modele/inconnu.csv').status_code == 404


# ------------------------------------------------------------------ inscription / nom
def test_signup_with_first_and_last_name(app):
    import uuid
    from support_v4xx import PASSWORD
    client = app.test_client()
    email = f'{uuid.uuid4().hex[:10]}@example.com'
    post(client, '/signup', {'accept_terms': '1', 'first_name': ' Adel ', 'last_name': 'Ouled  Said',
                             'email': email, 'password': PASSWORD, 'company_name': 'Prenom SAS'})
    row = auth('SELECT full_name FROM users WHERE email=?', (email,), 'one')
    assert row and row['full_name'] == 'Adel Ouled Said'


def test_signup_requires_both_names(app):
    import uuid
    from support_v4xx import PASSWORD
    client = app.test_client()
    email = f'{uuid.uuid4().hex[:10]}@example.com'
    post(client, '/signup', {'accept_terms': '1', 'first_name': 'Adel', 'last_name': '',
                             'email': email, 'password': PASSWORD, 'company_name': 'Prenom SAS'})
    assert auth('SELECT 1 FROM users WHERE email=?', (email,), 'one') is None
    html = (ROOT / 'templates/signup.html').read_text(encoding='utf-8')
    assert 'name="first_name"' in html and 'name="last_name"' in html and 'name="full_name"' not in html


def test_user_can_change_display_name(app):
    _, email, _, uid = signup(app, 'Nom SARL')
    client, _ = login(app, email)
    assert 'Mon nom' in client.get('/compte/securite').get_data(as_text=True)
    post(client, '/compte/nom', {'first_name': 'Adel', 'last_name': 'Ouled Said'})
    assert auth('SELECT full_name FROM users WHERE id=?', (uid,), 'one')['full_name'] == 'Adel Ouled Said'
    post(client, '/compte/nom', {'first_name': 'Adel', 'last_name': ''})
    assert auth('SELECT full_name FROM users WHERE id=?', (uid,), 'one')['full_name'] == 'Adel Ouled Said'


# ------------------------------------------------------------------ menu simplifié
def test_simple_menu_by_default_and_toggle(app):
    _, email, _, uid = signup(app, 'Menu SARL')
    client, _ = login(app, email)
    html = client.get('/').get_data(as_text=True)
    assert 'Afficher toutes les fonctionnalités' in html
    nav = html[html.find('<nav aria-label="Navigation principale">'):html.find('</nav>')]
    assert 'Facturation' in nav and 'Relances clients' in nav
    for hidden in ('/comptabilite/plan-comptable', 'Simulateur de décision', 'Marchés publics', 'Crédit-bail', 'Partenaires'):
        assert hidden not in nav, hidden
    post(client, '/settings/menu', {})
    assert auth('SELECT ui_mode FROM users WHERE id=?', (uid,), 'one')['ui_mode'] == 'expert'
    html = client.get('/').get_data(as_text=True)
    assert 'Revenir au menu simplifié' in html
    post(client, '/settings/menu', {})
    assert auth('SELECT ui_mode FROM users WHERE id=?', (uid,), 'one')['ui_mode'] == 'simple'


# ------------------------------------------------------------------ SIREN
def test_siren_luhn_validation():
    from profitos.legal import siren_valid
    assert siren_valid('732829320') and siren_valid('732 829 320') and siren_valid('356000000')
    for bad in ('123456789', '000000000', '111111111', '12345678', 'abcdefghi', ''):
        assert not siren_valid(bad), bad


def test_preflight_rejects_placeholder_legal_identity():
    import importlib.util
    spec = importlib.util.spec_from_file_location('cloud_preflight', ROOT / 'scripts/cloud_preflight.py')
    pf = importlib.util.module_from_spec(spec); spec.loader.exec_module(pf)
    env = {'LEGAL_COMPANY_NAME': 'ProfitOS', 'LEGAL_FORM': 'SAS', 'LEGAL_SIREN': '123456789',
           'LEGAL_ADDRESS': '15 RUE Roger Paris', 'LEGAL_PUBLICATION_DIRECTOR': 'Jean',
           'LEGAL_CONTACT_EMAIL': 'contact@profitos.fr'}
    errors = ' '.join(pf.check_legal(env))
    assert 'LEGAL_SIREN invalide' in errors and 'LEGAL_ADDRESS incomplète' in errors
    env.update(LEGAL_SIREN='732829320', LEGAL_ADDRESS='15 rue Roger, 75001 Paris')
    assert pf.check_legal(env) == []
