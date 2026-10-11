"""v421 — TVA sur encaissements (44574 -> 44571), CA3 par taux, 706/707, avoirs.

Tests fonctionnels avec montants : une prestation de services sans option pour les
débits met sa TVA en attente (445740) à l'émission ; chaque règlement en rend exigible
la part correspondante (445710) ; la CA3 est ventilée par taux depuis 445710.
"""
import sqlite3
from datetime import date, timedelta

import pytest

from tests.test_v421_invoice_compliance import (  # noqa: F401  (fixtures réutilisées)
    _no_boamp, app, _post, _flashes, _signup, _db, _create_invoice, _invoice, FULL_PROFILE)


def _lines(org_id, source_type, source_id):
    c = _db(org_id)
    rows = c.execute(
        """SELECT l.account_code, ROUND(SUM(l.debit),2) d, ROUND(SUM(l.credit),2) cr FROM accounting_entry_lines l
           JOIN accounting_entries e ON e.id=l.entry_id WHERE e.source_type=? AND e.source_id=?
           GROUP BY l.account_code""", (source_type, source_id)).fetchall()
    c.close()
    return {r['account_code']: (r['d'] or 0, r['cr'] or 0) for r in rows}


def _balance(org_id, account):
    c = _db(org_id)
    n = c.execute('SELECT ROUND(COALESCE(SUM(credit-debit),0),2) FROM accounting_entry_lines WHERE account_code=?',
                  (account,)).fetchone()[0]
    c.close()
    return n


def test_services_vat_pending_until_payment_and_ca3_by_rate(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id, price_1='1000', vat_1='20')
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    sale = _lines(org_id, 'outgoing_invoice', inv_id)
    assert sale == {'411000': (1200.0, 0), '706000': (0, 1000.0), '445740': (0, 200.0)}
    assert _balance(org_id, '445710') == 0
    today = date.today().isoformat()
    _post(client, f'/facturation/{inv_id}/reglement', {'amount': '600,00', 'payment_date': today})
    assert _balance(org_id, '445740') == 100.0 and _balance(org_id, '445710') == 100.0
    page = client.get(f'/comptabilite/tva?date_from={today[:4]}-01-01&date_to={today}').get_data(as_text=True)
    assert 'TVA collectée par taux (CA3)' in page and '500,00' in page and '100,00' in page
    _post(client, f'/facturation/{inv_id}/reglement', {'amount': '600', 'payment_date': today})
    assert _balance(org_id, '445740') == 0 and _balance(org_id, '445710') == 200.0
    from profitos.accounting import ca3_collected_by_rate
    c = _db(org_id)
    rates = ca3_collected_by_rate(c, f'{today[:4]}-01-01', today)
    c.close()
    assert rates == [{'line': '08', 'label': 'Taux normal 20 %', 'rate': 20.0, 'base': 1000.0, 'vat': 200.0}]


def test_goods_and_debits_option_book_vat_at_invoice_and_mixed_lines_split_accounts(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    goods = _create_invoice(client, org_id, operation_nature='biens', delivery_address='3 rue C\n69003 Lyon',
                            price_1='100', vat_1='5,5')
    _post(client, f'/facturation/{goods}/marquer-emise')
    assert _lines(org_id, 'outgoing_invoice', goods) == {'411000': (105.5, 0), '707000': (0, 100.0), '445710': (0, 5.5)}
    debits = _create_invoice(client, org_id, vat_on_debits='on', price_1='200', vat_1='10')
    _post(client, f'/facturation/{debits}/marquer-emise')
    assert _lines(org_id, 'outgoing_invoice', debits) == {'411000': (220.0, 0), '706000': (0, 200.0), '445710': (0, 20.0)}
    mixed = _create_invoice(client, org_id, operation_nature='mixte', delivery_address='3 rue C\n69003 Lyon',
                            label_1='Pose', price_1='300', vat_1='20', nature_1='services',
                            label_2='Matériel', price_2='700', vat_2='20', nature_2='biens')
    _post(client, f'/facturation/{mixed}/marquer-emise')
    # TVA par taux (200) répartie au prorata : 60 en attente (services), 140 exigible (biens).
    assert _lines(org_id, 'outgoing_invoice', mixed) == {
        '411000': (1200.0, 0), '706000': (0, 300.0), '707000': (0, 700.0), '445740': (0, 60.0), '445710': (0, 140.0)}


def test_credit_note_follows_original_accounts_and_cancels_pending_vat(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id, operation_nature='mixte', delivery_address='3 rue C\n69003 Lyon',
                             label_1='Pose', price_1='300', vat_1='20', nature_1='services',
                             label_2='Matériel', price_2='700', vat_2='20', nature_2='biens')
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    _post(client, f'/facturation/{inv_id}/avoir', {'reason': 'Annulation commande', 'amount_ttc': '1200'})
    c = _db(org_id)
    credit = c.execute('SELECT * FROM outgoing_credit_notes WHERE original_invoice_id=?', (inv_id,)).fetchone()
    c.close()
    assert (credit['subtotal'], credit['vat_amount'], credit['total']) == (1000.0, 200.0, 1200.0)
    assert credit['credit_number'].startswith('AV-')
    assert _lines(org_id, 'outgoing_credit_note', credit['id']) == {
        '706000': (300.0, 0), '707000': (700.0, 0), '445740': (60.0, 0), '445710': (140.0, 0), '411000': (0, 1200.0)}
    assert _balance(org_id, '445740') == 0 and _balance(org_id, '445710') == 0
    assert _invoice(org_id, inv_id)['status'] == 'paid'
    pdf = client.get(f"/facturation/avoir/{credit['id']}/pdf")
    from tests.test_v421_invoice_compliance import _pdf_text, _norm
    text = _norm(_pdf_text(pdf.data))
    issue = _invoice(org_id, inv_id)['issue_date']
    assert (f"Facture d'avoir n° {credit['credit_number']} — rectifie la facture n° "
            f"{_invoice(org_id, inv_id)['invoice_number']} du {issue[8:10]}/{issue[5:7]}/{issue[:4]}") in text
    assert "Total TTC de l'avoir 1 200,00 €" in text


def test_payment_date_in_future_refused_and_cancelled_draft_does_not_block_vat_lock(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    _post(client, f'/facturation/{inv_id}/reglement', {'amount': '100', 'payment_date': tomorrow})
    assert any('futur' in m for m in _flashes(client))
    c = _db(org_id)
    assert c.execute('SELECT COUNT(*) FROM outgoing_invoice_payments').fetchone()[0] == 0
    c.close()
    draft = _create_invoice(client, org_id)
    _post(client, f'/facturation/{draft}/annuler')
    today = date.today().isoformat()
    _post(client, '/comptabilite/tva', {'date_from': f'{today[:4]}-01-01', 'date_to': today, 'action': 'lock'})
    c = _db(org_id)
    status = c.execute('SELECT status FROM vat_declarations').fetchone()
    c.close()
    assert status is not None and status['status'] == 'locked'
