"""v422 — Vérification bout en bout du lot facturation / TVA / comptabilité.

Scénarios réels (client Flask, SQLite locale) avec assertions chiffrées sur les
documents produits : bons de livraison (numérotation par entité, reprise en facture),
TVA sur encaissements du scénario de démonstration (CA3 = TVA exigible seule),
autoliquidation et exonérations (PDF + XML), filiale, acheteur public, verrou de série
SQLite, exports CSV français, page de suppression de compte.
"""
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pytest

from tests.test_v421_invoice_compliance import (FULL_PROFILE, RAM, _create_invoice, _db, _flashes, _invoice, _norm,
                                                _pdf_text, _post, _signup)


@pytest.fixture(autouse=True)
def _no_boamp(monkeypatch):
    from profitos.routes import main as main_routes
    monkeypatch.setattr(main_routes, 'sync_grow', lambda: 0)


@pytest.fixture()
def app():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    return app


def _paris_today():
    from profitos.accounting import paris_today
    return paris_today()


def _xml_root(org_id, inv_id):
    from profitos.routes import invoicing as I
    c = _db(org_id)
    inv = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()
    seller = I._seller_identity(c, inv['entity_id'])
    xml = I.generate_facturx_xml(inv, json.loads(inv['line_items']), seller)
    failures = I.validate_facturx_business_rules(inv, json.loads(inv['line_items']), seller)[1]
    c.close()
    return ET.fromstring(xml.split(b'\n', 1)[1]), failures


# --- Bons de livraison -----------------------------------------------------------------

def test_delivery_notes_numbered_per_entity_and_invoice_copies_nature_delivery_due_date(app):
    client, org_id = _signup(app, plan='MULTI')
    _post(client, '/company', FULL_PROFILE)
    year = _paris_today().year
    due = (_paris_today() + timedelta(days=45)).isoformat()
    note = {'client_name': 'Hôtel Saint-Jean', 'client_address': '1 quai Romain Rolland\n69005 Lyon',
            'billing_address': '2 rue Juiverie\n69005 Lyon', 'client_siren': '552100554', 'operation_nature': 'mixte',
            'due_date': due, 'label_1': 'Fenêtres', 'qty_1': '8', 'price_1': '1150', 'vat_1': '20'}
    r = _post(client, '/facturation/livraisons/nouvelle', note)
    assert r.status_code == 302
    _post(client, '/facturation/livraisons/nouvelle', dict(note, due_date=''))
    c = _db(org_id)
    c.execute("INSERT INTO entities(name,created_at) VALUES('Filiale Menuiserie','2026-01-01')")
    eid = c.execute('SELECT MAX(id) FROM entities').fetchone()[0]
    c.commit(); c.close()
    _post(client, '/entites/basculer', {'entity_id': str(eid)})
    _post(client, '/facturation/livraisons/nouvelle', note)
    _post(client, '/entites/basculer', {'entity_id': ''})
    c = _db(org_id)
    notes = c.execute('SELECT id,delivery_number,entity_id,due_date FROM delivery_notes ORDER BY id').fetchall()
    c.close()
    assert [n['delivery_number'] for n in notes] == [f'BL-{year}-001', f'BL-{year}-002', f'BL-E{eid}-{year}-001']
    assert notes[0]['due_date'] == due and notes[1]['due_date'] is None
    # Échéance antérieure à la livraison refusée.
    _post(client, '/facturation/livraisons/nouvelle', dict(note, due_date=(_paris_today() - timedelta(days=1)).isoformat()))
    assert any("ne peut pas précéder la date de livraison" in m for m in _flashes(client))

    pdf_text = _norm(_pdf_text(client.get(f"/facturation/livraisons/{notes[0]['id']}/pdf").data))
    assert f"Bon de livraison n° BL-{year}-001" in pdf_text
    assert f"Date de livraison : {_paris_today().strftime('%d/%m/%Y')}" in pdf_text
    assert re.search(r'\d{4}-\d{2}-\d{2}', pdf_text) is None

    _post(client, f"/facturation/livraisons/{notes[0]['id']}/facturer")
    c = _db(org_id)
    inv = c.execute('SELECT * FROM outgoing_invoices ORDER BY id DESC LIMIT 1').fetchone()
    c.close()
    assert inv['invoice_number'] is None and inv['status'] == 'draft'
    assert inv['operation_nature'] == 'mixte'
    assert inv['delivery_address'] == '1 quai Romain Rolland\n69005 Lyon'
    assert inv['client_address'] == '2 rue Juiverie\n69005 Lyon'
    assert inv['due_date'] == due and inv['service_date'] == _paris_today().isoformat()
    assert (inv['client_siren'], inv['subtotal'], inv['vat_amount'], inv['total']) == ('552100554', 9200.0, 1840.0, 11040.0)
    # Sans échéance sur le bon : 30 jours.
    _post(client, f"/facturation/livraisons/{notes[1]['id']}/facturer")
    c = _db(org_id)
    inv2 = c.execute('SELECT due_date FROM outgoing_invoices ORDER BY id DESC LIMIT 1').fetchone()
    c.close()
    assert inv2['due_date'] == (_paris_today() + timedelta(days=30)).isoformat()


def test_cancelled_draft_message_uses_draft_label(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id)
    _post(client, f'/facturation/{inv_id}/annuler')
    msgs = _flashes(client)
    assert f"Brouillon n°{inv_id} annulé : aucun numéro de facture n'a été consommé." in msgs
    assert not any('None' in m for m in msgs)
    text = _norm(_pdf_text(client.get(f'/facturation/{inv_id}/pdf').data))
    assert 'ANNULÉE' in text and f'Brouillon n°{inv_id}' in text


# --- B4 : verrou de série SQLite ------------------------------------------------------------

def test_sqlite_series_lock_serializes_concurrent_issuers(tmp_path):
    from profitos import invoice_numbering as N
    path = tmp_path / 'seq.db'
    setup = sqlite3.connect(path)
    setup.executescript("""
        CREATE TABLE invoice_number_sequences(entity_key INTEGER NOT NULL DEFAULT 0, fiscal_year TEXT NOT NULL,
            last_sequence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(entity_key,fiscal_year));
        CREATE TABLE outgoing_invoices(id INTEGER PRIMARY KEY, entity_id INTEGER, invoice_number TEXT);""")
    setup.close()
    first = sqlite3.connect(path, timeout=5); first.row_factory = sqlite3.Row
    second = sqlite3.connect(path, timeout=0.2); second.row_factory = sqlite3.Row
    assert N.next_document_number(first, 'invoice', None, '2026-10-10') == 'FA-2026-001'
    # La première émission n'est pas validée : la seconde ne peut pas lire une séquence périmée.
    with pytest.raises(N.SeriesLockTimeout):
        N.next_document_number(second, 'invoice', None, '2026-10-10')
    first.execute("INSERT INTO outgoing_invoices(entity_id,invoice_number) VALUES(NULL,'FA-2026-001')")
    first.commit()
    assert N.next_document_number(second, 'invoice', None, '2026-10-10') == 'FA-2026-002'
    second.rollback()
    # Annulée : la séquence n'a pas bougé, le numéro 002 reste disponible.
    assert N.next_document_number(first, 'invoice', None, '2026-10-10') == 'FA-2026-002'
    first.rollback(); first.close(); second.close()


# --- B6 : scénario de démonstration -------------------------------------------------------

def test_demo_services_vat_collected_equals_exigible_amount_only(app):
    """Trois factures de prestations de services (sans option débits) émises, une seule
    réglée partiellement (8 000 €) : la CA3 ne déclare que la TVA de ce règlement."""
    from profitos.accounting import ca3_collected_by_rate, pending_vat_balance
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    specs = [
        dict(label_1='Rénovation cage d’escalier', price_1='14800', vat_1='10', label_2='Peinture', price_2='3600', vat_2='10'),
        dict(label_1='Menuiseries chambres', qty_1='8', price_1='1150', vat_1='20'),
        dict(label_1='Cloisons', price_1='6900', vat_1='20', label_2='Faux plafond', qty_2='42', price_2='58', vat_2='20'),
    ]
    ids = []
    for spec in specs:
        inv_id = _create_invoice(client, org_id, **spec)
        _post(client, f'/facturation/{inv_id}/marquer-emise')
        ids.append(inv_id)
    totals = [(_invoice(org_id, i)['subtotal'], _invoice(org_id, i)['vat_amount'], _invoice(org_id, i)['total']) for i in ids]
    assert totals == [(18400.0, 1840.0, 20240.0), (9200.0, 1840.0, 11040.0), (9336.0, 1867.2, 11203.2)]
    _post(client, f'/facturation/{ids[0]}/reglement', {'amount': '8000'})
    year = _paris_today().year
    c = _db(org_id)
    rates = ca3_collected_by_rate(c, f'{year}-01-01', f'{year}-12-31')
    pending = pending_vat_balance(c, f'{year}-12-31')
    c.close()
    # 1 840 × 8 000 / 20 240 = 727,27 € exigibles ; base 18 400 × 8 000 / 20 240 = 7 272,73 €.
    assert rates == [{'line': '9B', 'label': 'Taux réduit 10 %', 'rate': 10.0, 'base': 7272.73, 'vat': 727.27}]
    assert pending == round(1840 - 727.27 + 1840 + 1867.2, 2) == 4819.93
    page = client.get(f'/comptabilite/tva?date_from={year}-01-01&date_to={year}-12-31').get_data(as_text=True)
    assert '727,27' in page and '4 819,93' in page.replace('&nbsp;', ' ').replace(' ', ' ')


# --- B1 / B8 : régimes de TVA (PDF + XML) -------------------------------------------------

def test_foreign_b2b_reverse_charge_requires_buyer_vat_and_prints_mention(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    common = dict(client_name='Bau GmbH', client_address='Hauptstraße 1\n10115 Berlin', client_siren='',
                  counterparty_country='DE', vat_treatment='intra_eu_services', vat_1='0', ereporting_scope='international_b2b')
    blocked = _create_invoice(client, org_id, **common)
    _post(client, f'/facturation/{blocked}/marquer-emise')
    assert any('1 mention(s) obligatoire(s) manquante(s)' in m for m in _flashes(client))
    assert _invoice(org_id, blocked)['invoice_number'] is None
    inv_id = _create_invoice(client, org_id, client_vat_number='DE811569869', **common)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    inv = _invoice(org_id, inv_id)
    assert inv['invoice_number'] and (inv['subtotal'], inv['vat_amount'], inv['total']) == (1000.0, 0.0, 1000.0)
    text = _norm(_pdf_text(client.get(f'/facturation/{inv_id}/pdf').data))
    for expected in ('Autoliquidation', 'N° TVA intracommunautaire : DE811569869', 'Pays : DE',
                     'Autoliquidation 1 000,00 € 0,00 €', 'Total TTC 1 000,00 €'):
        assert expected in text, expected
    root, failures = _xml_root(org_id, inv_id)
    assert failures == []
    tax = root.find(f'.//{RAM}ApplicableHeaderTradeSettlement/{RAM}ApplicableTradeTax')
    assert tax.find(f'{RAM}CategoryCode').text == 'AE' and tax.find(f'{RAM}ExemptionReasonCode').text == 'VATEX-EU-AE'
    assert tax.find(f'{RAM}RateApplicablePercent').text == '0'
    buyer = root.find(f'.//{RAM}BuyerTradeParty')
    assert buyer.find(f'{RAM}SpecifiedTaxRegistration/{RAM}ID').text == 'DE811569869'
    assert buyer.find(f'{RAM}PostalTradeAddress/{RAM}CountryID').text == 'DE'


@pytest.mark.parametrize('treatment,extra,category,code,mention', [
    ('intra_eu_goods', {'client_vat_number': 'DE811569869', 'counterparty_country': 'DE', 'client_siren': '',
                        'operation_nature': 'biens', 'delivery_address': 'Lager 2\n10115 Berlin',
                        'ereporting_scope': 'international_b2b'},
     'K', 'VATEX-EU-IC', 'livraison intracommunautaire, art. 262 ter I du CGI'),
    ('export', {'counterparty_country': 'US', 'client_siren': '', 'operation_nature': 'biens',
                'delivery_address': '1 Main Street\nNew York', 'ereporting_scope': 'international_b2b'},
     'G', 'VATEX-EU-G', 'exportation, art. 262 I du CGI'),
    ('exempt', {'vat_exemption_reason': 'Exonération de TVA, art. 261-4-4° du CGI'},
     'E', None, 'Exonération de TVA, art. 261-4-4° du CGI'),
])
def test_exempt_categories_k_g_e_in_pdf_and_xml(app, treatment, extra, category, code, mention):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id, vat_treatment=treatment, vat_1='0', **extra)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    assert _invoice(org_id, inv_id)['invoice_number'], _flashes(client)
    text = _norm(_pdf_text(client.get(f'/facturation/{inv_id}/pdf').data))
    assert mention in text and 'Total TVA 0,00 €' in text
    root, failures = _xml_root(org_id, inv_id)
    assert failures == []
    tax = root.find(f'.//{RAM}ApplicableHeaderTradeSettlement/{RAM}ApplicableTradeTax')
    assert tax.find(f'{RAM}CategoryCode').text == category
    assert mention in tax.find(f'{RAM}ExemptionReason').text
    code_el = tax.find(f'{RAM}ExemptionReasonCode')
    assert (code_el.text if code_el is not None else None) == code
    assert tax.find(f'{RAM}BasisAmount').text == '1000.00' and tax.find(f'{RAM}CalculatedAmount').text == '0.00'
    line_tax = root.find(f'.//{RAM}SpecifiedLineTradeSettlement/{RAM}ApplicableTradeTax')
    assert line_tax.find(f'{RAM}CategoryCode').text == category


def test_zero_rate_standard_line_is_category_z_in_xml():
    from profitos.routes import invoicing as I
    items = [{'label': 'Prestation', 'qty': 2, 'unit_price': 100, 'vat_rate': 20, 'line_total': 200.0},
             {'label': 'Débours', 'qty': 1, 'unit_price': 50, 'vat_rate': 0, 'line_total': 50.0}]
    inv = {'invoice_number': 'FA-2026-030', 'issue_date': '2026-10-10', 'due_date': '2026-11-09', 'client_name': 'C',
           'client_address': '1 rue A\n69001 Lyon', 'client_siren': '552100554', 'subtotal': 250.0, 'vat_amount': 40.0,
           'total': 290.0, 'operation_nature': 'services'}
    root = ET.fromstring(I.generate_facturx_xml(inv, items, dict(FULL_PROFILE)).split(b'\n', 1)[1])
    taxes = {t.find(f'{RAM}CategoryCode').text: (t.find(f'{RAM}BasisAmount').text, t.find(f'{RAM}CalculatedAmount').text,
                                                 t.find(f'{RAM}RateApplicablePercent').text)
             for t in root.findall(f'.//{RAM}ApplicableHeaderTradeSettlement/{RAM}ApplicableTradeTax')}
    assert taxes == {'S': ('200.00', '40.00', '20'), 'Z': ('50.00', '0.00', '0')}
    assert root.find(f'.//{RAM}TaxTotalAmount').text == '40.00'
    assert root.find(f'.//{RAM}GrandTotalAmount').text == '290.00'
    assert I.validate_facturx_business_rules(inv, items, dict(FULL_PROFILE))[1] == []


# --- B8 : filiale, acheteur public ----------------------------------------------------------

def test_subsidiary_invoice_uses_subsidiary_identity_everywhere(app):
    client, org_id = _signup(app, plan='MULTI')
    _post(client, '/company', FULL_PROFILE)
    _post(client, '/entites/nouvelle', {'name': 'Morel Menuiserie SARL'})
    c = _db(org_id)
    eid = c.execute('SELECT MAX(id) FROM entities').fetchone()[0]
    c.close()
    r = _post(client, f'/entites/{eid}', {
        'name': 'Morel Menuiserie SARL', 'legal_form': 'SARL', 'share_capital': '5000', 'rcs_city': 'Villeurbanne',
        'address': '5 rue Francis de Pressensé', 'postal_code': '69100', 'city': 'Villeurbanne',
        'siret': '35600000000010', 'vat_number': 'FR39356000000'})
    assert r.status_code == 302
    _post(client, '/entites/basculer', {'entity_id': str(eid)})
    inv_id = _create_invoice(client, org_id, entity_id=str(eid))
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    inv = _invoice(org_id, inv_id)
    assert inv['entity_id'] == eid and inv['invoice_number'] == f'FA-E{eid}-{_paris_today().year}-001'
    text = _norm(_pdf_text(client.get(f'/facturation/{inv_id}/pdf').data))
    assert 'Morel Menuiserie SARL' in text and '69100 Villeurbanne' in text and 'SARL au capital de 5 000 €' in text
    assert 'FR39356000000' in text and 'Atelier Dupont' not in text and '73282932000074' not in text
    root, failures = _xml_root(org_id, inv_id)
    assert failures == []
    seller = root.find(f'.//{RAM}SellerTradeParty')
    assert seller.find(f'{RAM}Name').text == 'Morel Menuiserie SARL'
    assert seller.find(f'{RAM}SpecifiedLegalOrganization/{RAM}ID').text == '356000000'
    assert seller.find(f'{RAM}PostalTradeAddress/{RAM}PostcodeCode').text == '69100'
    assert seller.find(f'{RAM}SpecifiedTaxRegistration/{RAM}ID').text == 'FR39356000000'


def test_public_buyer_gets_chorus_pro_notice(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    # SIREN commençant par 2 : collectivité territoriale.
    inv_id = _create_invoice(client, org_id, client_name='Mairie de Villeurbanne', client_siren='216902643')
    page = client.get(f'/facturation/{inv_id}').get_data(as_text=True)
    assert 'Chorus Pro' in page
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    text = _norm(_pdf_text(client.get(f'/facturation/{inv_id}/pdf').data))
    assert 'Acheteur public : facture à déposer sur Chorus Pro' in text


# --- Exports et suppression de compte ------------------------------------------------------

def test_csv_exports_default_to_french_excel_format(app):
    from profitos.runtime import export_response
    with app.test_request_context('/x?format=csv'):
        body = export_response([{'Client': 'Durand', 'Montant': 1234.5}], 'test').get_data().decode('utf-8-sig')
    assert body.splitlines() == ['Client;Montant', 'Durand;1234,5']


def test_account_deletion_warns_about_ten_year_retention_and_offers_fec(app):
    client, org_id = _signup(app)
    page = client.get('/settings/delete-account').get_data(as_text=True)
    assert 'pendant 10 ans' in page and 'L123-22' in page
    assert '/comptabilite/export-fec' in page


def test_reminder_failure_shows_generic_french_message(app, monkeypatch):
    from profitos.routes import invoicing as I
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    c = _db(org_id)
    c.execute('UPDATE outgoing_invoices SET due_date=? WHERE id=?', ((_paris_today() - timedelta(days=5)).isoformat(), inv_id))
    c.commit(); c.close()

    def boom(*a, **k):
        raise RuntimeError('SMTP 535 secret-relay.example')
    monkeypatch.setattr(I, 'send_email', boom)
    r = _post(client, f'/facturation/{inv_id}/relancer')
    assert r.status_code == 302
    msgs = ' '.join(_flashes(client))
    assert "La relance n'a pas pu être envoyée. Réessayez dans quelques minutes." in msgs and 'secret-relay' not in msgs
    c = _db(org_id)
    assert c.execute('SELECT COUNT(*) FROM invoice_reminders WHERE invoice_id=?', (inv_id,)).fetchone()[0] == 0
    c.close()
