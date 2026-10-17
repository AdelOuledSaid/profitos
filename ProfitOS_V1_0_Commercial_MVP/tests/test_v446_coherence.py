"""v446 — incohérences : menu selon le rôle et la formule, lecture seule, emprunts déjà
en cours, part des factures importées, libellés du tableau de bord, titres d'onglet."""
import io
from datetime import date
from pathlib import Path

from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db)

ROOT = Path(__file__).resolve().parents[1]


def _role(org_id, role):
    from profitos import runtime as rt
    c = rt.auth_cx()
    c.execute('UPDATE memberships SET role=? WHERE organization_id=?', (role, org_id))
    c.commit(); c.close()


def _expert(client):
    from profitos import runtime as rt
    with client.session_transaction() as s:
        uid = s['user_id']
    c = rt.auth_cx(); c.execute("UPDATE users SET ui_mode='expert' WHERE id=?", (uid,)); c.commit(); c.close()


def test_menu_only_shows_pages_the_role_can_open(app):
    client, org_id = _signup(app, plan='MULTI')
    _expert(client)
    _role(org_id, 'COMMERCIAL')
    html = client.get('/').get_data(as_text=True)
    for href in ('/comptabilite/grand-livre', '/cash-intelligence', '/notes-de-frais', '/entites', '/team',
                 '/upload/invoices', '/organizations/new'):
        assert f'href="{href}' not in html and f'action="{href}' not in html, href
    _role(org_id, 'COMPTABLE')
    html = client.get('/').get_data(as_text=True)
    assert 'href="/comptabilite/grand-livre' in html
    for href in ('/entites', '/integrations/ecommerce', '/team', '/organizations/new'):
        assert f'href="{href}' not in html and f'action="{href}' not in html, href


def test_read_only_role_banner_and_own_name(app):
    client, org_id = _signup(app)
    _role(org_id, 'LECTURE')
    html = client.get('/facturation').get_data(as_text=True)
    assert 'readonly-banner' in html and 'data-readonly="1"' in html
    r = _post(client, '/compte/nom', {'first_name': 'Léa', 'last_name': 'Martin', 'full_name': 'Léa Martin'})
    assert not any('Lecture seule' in m for m in _flashes(client)), r
    assert 'data-readonly' in (ROOT / 'static/app.js').read_text(encoding='utf-8')


def test_plan_menu_hides_api_on_starter_and_forecast_links(app):
    client, _ = _signup(app, plan='STARTER')
    _expert(client)
    assert 'href="/settings/api-keys' not in client.get('/').get_data(as_text=True)
    client, _ = _signup(app, plan='PRO')
    html = client.get('/forecast').get_data(as_text=True)
    assert 'href="/cash-intelligence' not in html


def test_existing_loan_past_installments_marked_paid(app):
    from profitos.accounting import paris_today
    client, org_id = _signup(app, plan='BUSINESS')
    start = date(paris_today().year - 1, 1, 5).isoformat()
    _post(client, '/comptabilite/emprunts/nouveau', {'lender_name': 'Banque', 'principal_amount': '12 000',
                                                     'annual_rate': '3', 'duration_months': '60',
                                                     'start_date': start, 'past_paid': '1'})
    c = _db(org_id)
    past_unpaid = c.execute('SELECT COUNT(*) FROM loan_installments WHERE paid=0 AND due_date<?',
                            (paris_today().isoformat(),)).fetchone()[0]
    paid = c.execute('SELECT COUNT(*) FROM loan_installments WHERE paid=1').fetchone()[0]
    c.close()
    assert past_unpaid == 0 and paid >= 12
    for tpl in ('loan_new.html', 'finance_lease_new.html'):
        assert 'name="past_paid"' in (ROOT / 'templates' / tpl).read_text(encoding='utf-8')


def test_invoice_tiles_imported_share_never_exceeds_tile(app):
    client, _ = _signup(app)
    csv = ("Numéro;Client;Montant TTC;Montant payé;Date facture;Échéance\r\n"
           "FA-1;Alpha;1 200,00;0;01/01/2026;01/02/2026\r\nFA-2;Beta;540,50;200;15/09/2026;15/11/2099\r\n")
    _post(client, '/upload/invoices', {'file': (io.BytesIO(csv.encode()), 'f.csv')}, content_type='multipart/form-data')
    html = client.get('/facturation').get_data(as_text=True).replace('\xa0', ' ')
    assert 'dont 1 540,50' not in html
    assert 'dont 340,50' in html and 'dont 1 200,00' in html


def test_dashboard_overdue_label_and_page_titles(app):
    t = (ROOT / 'templates/dashboard.html').read_text(encoding='utf-8')
    assert 'En retard de paiement' in t
    from profitos.ui_labels import PAGE_TITLES
    for ep in ('accounting_general_ledger', 'accounting_trial_balance', 'accounting_entry_new',
               'accounting_ca3', 'banking_reconciliation_statement', 'accounting_entry_detail'):
        assert ep in PAGE_TITLES
