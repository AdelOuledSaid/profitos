"""v43x — E1 : une seule vue des créances clients (factures émises + factures importées).

Avant : le tableau de bord ne lisait que les factures importées, la page Créances et la
prévision que les factures émises. Les montants divergeaient (105 150 € / 0 € / 34 483 €).
"""
import re
from datetime import date, timedelta

from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db, _create_invoice, FULL_PROFILE)


def _txt(r):
    return r.get_data(as_text=True).replace(' ', ' ').replace('&nbsp;', ' ').replace(' ', ' ')


def _import_invoice(org_id, number, customer, amount, due, paid=0, status='unpaid', entity_id=None):
    c = _db(org_id)
    c.execute("INSERT INTO invoices(invoice_number,customer,amount,paid_amount,issue_date,due_date,status,days_overdue,score,created_at,kind,entity_id)"
              " VALUES(?,?,?,?,?,?,?,0,0,'2026-01-01','STANDARD',?)",
              (number, customer, amount, paid, (due - timedelta(days=30)).isoformat(), due.isoformat(), status, entity_id))
    c.commit(); c.close()


def _emit_overdue_invoice(client, org_id, total_ht='1000', days_late=20, number_hint=None):
    inv_id = _create_invoice(client, org_id, price_1=total_ht, vat_1='20')
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    c = _db(org_id)
    late = (date.today() - timedelta(days=days_late)).isoformat()
    c.execute('UPDATE outgoing_invoices SET due_date=?, issue_date=? WHERE id=?',
              (late, (date.today() - timedelta(days=days_late + 30)).isoformat(), inv_id))
    c.commit()
    number = c.execute('SELECT invoice_number FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()[0]
    c.close()
    return inv_id, number


def test_helper_merges_both_sources_deduplicates_and_nets_payments(app):
    from profitos import receivables as rec
    from profitos import runtime as rt
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id, number = _emit_overdue_invoice(client, org_id)          # 1 200 € TTC, 20 j de retard
    _post(client, f'/facturation/{inv_id}/reglement', {'amount': '200', 'payment_date': date.today().isoformat()})
    today = date.today()
    _import_invoice(org_id, 'MO-1', 'Groupe Habitat', 33300, today - timedelta(days=45))
    _import_invoice(org_id, 'MO-2', 'Boulangerie', 500, today + timedelta(days=10))
    _import_invoice(org_id, 'MO-3', 'Déjà payée', 900, today - timedelta(days=5), status='paid')
    _import_invoice(org_id, number, 'Doublon du même numéro', 1200, today - timedelta(days=20))
    c = rt.tenant_cx_direct(org_id)
    try:
        rows = rec.open_receivables(c, None, today=today)
    finally:
        c.close()
    by_number = {r['invoice_number']: r for r in rows}
    assert set(by_number) == {number, 'MO-1', 'MO-2'}  # payée exclue, doublon compté une fois
    assert by_number[number]['source_label'] == 'Facture ProfitOS'
    assert by_number[number]['outstanding'] == 1000.0  # 1 200 − 200 de règlement partiel
    assert by_number[number]['days_overdue'] == 20
    assert by_number['MO-1']['source_label'] == 'Importée' and by_number['MO-1']['days_overdue'] == 45
    assert not by_number['MO-2']['overdue']
    totals = rec.summarize(rows)
    assert totals['total_due'] == 34800.0 and totals['overdue_due'] == 34300.0
    assert totals['imported_due'] == 33800.0 and totals['overdue_count'] == 2


def test_credit_note_reduces_receivable(app):
    from profitos import receivables as rec
    from profitos import runtime as rt
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id, _ = _emit_overdue_invoice(client, org_id)
    c = _db(org_id)
    c.execute("INSERT INTO outgoing_credit_notes(credit_number,original_invoice_id,original_invoice_number,status,total,created_at,entity_id) VALUES('AV-1',?,'X','issued',300,'2026-01-01',NULL)", (inv_id,))
    c.commit(); c.close()
    c = rt.tenant_cx_direct(org_id)
    try:
        rows = rec.open_receivables(c, None)
    finally:
        c.close()
    assert rows[0]['outstanding'] == 900.0 and rows[0]['credited'] == 300.0


def test_dashboard_receivables_page_recover_and_forecast_agree(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    _emit_overdue_invoice(client, org_id)  # 1 200 € échus
    today = date.today()
    _import_invoice(org_id, 'MO-9', 'Résidences Bellecour', 33300, today - timedelta(days=30))
    expected = '34 500'
    dash = _txt(client.get('/'))
    assert f'{expected} €' in dash and 'restent à encaisser' in dash
    page = _txt(client.get('/facturation/creances'))
    assert 'Importée' in page and 'Facture ProfitOS' in page and 'MO-9' in page
    m = re.search(r'En retard</div><div class="purchase-stat-value">([^<]+)<', page)
    assert m and m.group(1).strip() == '34 500,00 €'
    recover = _txt(client.get('/recover'))
    assert f'{expected} € récupérables' in recover and 'MO-9' in recover
    from profitos.routes.cash_intelligence import build_cash_intelligence
    with app.test_request_context('/'):
        from flask import session
        session['org_id'] = org_id
        ci = build_cash_intelligence()
    assert round(sum(r['amount'] for r in ci['receivables']), 2) == 34500.0
    assert {r['source'] for r in ci['receivables']} == {'outgoing_invoice', 'imported_invoice'}


def test_forecast_page_deducts_supplier_invoices_and_never_shows_minus_zero(app):
    client, org_id = _signup(app)
    _post(client, '/cash-intelligence', {'cash_balance': '10000'})
    _post(client, '/facturation/achats/nouvelle', {
        'supplier_name': 'Fournisseur Alpha', 'invoice_number': 'F-1', 'issue_date': date.today().isoformat(),
        'due_date': (date.today() + timedelta(days=10)).isoformat(), 'subtotal': '1000', 'vat_amount': '200'})
    _import_invoice(org_id, 'MO-1', 'Client', 5000, date.today() + timedelta(days=40))
    html = _txt(client.get('/forecast'))
    assert '−1 200 €' in html  # facture fournisseur déduite sur 0–30 jours
    assert '5 000 €' in html
    assert '−0 €' not in html and '-0 €' not in html
    assert '3 800 € de flux nets' in html or '+3 800' in html or '3 800 €' in html


def test_consolidated_view_shows_receivables(app):
    from profitos import runtime as rt
    client, org_id = _signup(app, plan='MULTI')
    _import_invoice(org_id, 'MO-5', 'Client', 2500, date.today() - timedelta(days=3))
    html = _txt(client.get('/entites/consolide'))
    assert 'À ENCAISSER GROUPE' in html and '2 500,00 €' in html


def test_pluralize_and_french_formats():
    from profitos.runtime import pluralize, fr_month, fr_signed, fr_datetime_paris
    assert pluralize(1, 'facture') == '1 facture'
    assert pluralize(0, 'facture') == '0 facture'
    assert pluralize(3, 'signal', 'signaux') == '3 signaux'
    assert fr_month('2026-10') == 'octobre 2026' and fr_month('2026-09-01') == 'septembre 2026'
    assert fr_signed(-1200) == '−1 200' and fr_signed(-0.2) == '0' and fr_signed(5, plus=True) == '+5'
    assert fr_datetime_paris('2026-10-08T22:30:00+00:00') == '09/10/2026 00:30'
