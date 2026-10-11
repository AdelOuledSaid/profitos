"""v421 — achats (date obligatoire, échéance, doublon, tuiles), formats français,
export CSV « ; » + virgule, statuts « Partiellement payée »."""
from datetime import date, timedelta

from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db, _create_invoice, _invoice, FULL_PROFILE)


def _purchase(client, **kw):
    data = {'supplier_name': 'Fournisseur Alpha', 'invoice_number': 'F-100', 'issue_date': date.today().isoformat(),
            'due_date': (date.today() + timedelta(days=5)).isoformat(), 'subtotal': '100', 'vat_amount': '20'}
    data.update(kw)
    return _post(client, '/facturation/achats/nouvelle', data)


def test_purchase_requires_issue_date_blocks_due_before_issue_and_warns_duplicate(app):
    client, org_id = _signup(app)
    r = _purchase(client, issue_date='')
    assert r.status_code == 400 and 'La date de facture est obligatoire' in r.get_data(as_text=True)
    r = _purchase(client, due_date=(date.today() - timedelta(days=1)).isoformat())
    assert r.status_code == 400 and "ne peut pas être antérieure" in r.get_data(as_text=True)
    assert 'value="F-100"' in r.get_data(as_text=True)  # saisie conservée
    assert _purchase(client).status_code == 302
    r = _purchase(client, invoice_number='f-100')
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and 'Doublon probable' in html and 'confirm_duplicate' in html
    assert _purchase(client, invoice_number='f-100', confirm_duplicate='1').status_code == 302
    c = _db(org_id)
    assert c.execute('SELECT COUNT(*) FROM purchase_invoices').fetchone()[0] == 2
    c.close()


def test_purchase_tiles_7_30_days_and_supplier_count(app):
    client, org_id = _signup(app)
    today = date.today()
    _purchase(client, invoice_number='A1', due_date=(today + timedelta(days=3)).isoformat(), subtotal='100', vat_amount='0')
    _purchase(client, invoice_number='A2', due_date=(today + timedelta(days=20)).isoformat(), subtotal='200', vat_amount='0')
    _purchase(client, supplier_name='Beta SARL', invoice_number='B1', issue_date=(today - timedelta(days=40)).isoformat(),
              due_date=(today - timedelta(days=10)).isoformat(), subtotal='50', vat_amount='0')
    html = client.get('/facturation/achats').get_data(as_text=True).replace('\u00a0', ' ').replace('&nbsp;', ' ')
    import re
    def tile(label):
        m = re.search(label + r'</div><div class="purchase-stat-value">([^<]+)<', html)
        return m.group(1).strip()
    assert tile('À payer') == '350,00 €' and tile('En retard') == '50,00 €'
    assert tile('À 7 jours') == '100,00 €' and tile('À 30 jours') == '300,00 €'
    assert tile('Fournisseurs') == '2'


def test_purchase_payment_date_cannot_be_future(app):
    client, org_id = _signup(app)
    _purchase(client)
    c = _db(org_id)
    pid = c.execute('SELECT id FROM purchase_invoices').fetchone()[0]
    c.close()
    _post(client, f'/facturation/achats/{pid}/payer', {'payment_date': (date.today() + timedelta(days=2)).isoformat()})
    assert any('futur' in m for m in _flashes(client))
    _post(client, f'/facturation/achats/{pid}/payer', {'payment_date': date.today().isoformat()})
    c = _db(org_id)
    row = c.execute('SELECT payment_date,amount FROM purchase_invoice_payments').fetchone()
    c.close()
    assert (row['payment_date'], row['amount']) == (date.today().isoformat(), 120.0)


def test_purchase_csv_export_uses_semicolon_and_decimal_comma(app):
    client, org_id = _signup(app)
    _purchase(client, subtotal='1234,5', vat_amount='246,9')
    r = client.get('/facturation/achats/export?format=csv')
    text = r.data.decode('utf-8-sig')
    header, first = text.splitlines()[:2]
    assert header.startswith('Fournisseur;N° facture;')
    assert '1234,5;246,9;1481,4' in first and date.today().strftime('%d/%m/%Y') in first


def test_invoice_detail_formats_quantities_rates_and_partial_status(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id, qty_1='1', price_1='100', vat_1='5,5', label_2='B', qty_2='2,5', price_2='10', vat_2='10')
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    _post(client, f'/facturation/{inv_id}/reglement', {'amount': '10', 'payment_date': date.today().isoformat()})
    html = client.get(f'/facturation/{inv_id}').get_data(as_text=True).replace(' ', ' ')
    assert '<td>1</td>' in html and '<td>2,5</td>' in html and '5,5 %' in html and '10 %' in html
    assert '1.0' not in html and 'Partiellement payée' in html
    lst = client.get('/facturation').get_data(as_text=True)
    assert 'Partiellement payée' in lst
