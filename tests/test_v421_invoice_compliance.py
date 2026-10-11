"""v421 — Factures : mentions obligatoires, numérotation à l'émission, PDF, Factur-X.

Tests fonctionnels (client Flask réel, SQLite locale) et unitaires, avec assertions
chiffrées : B1 (mentions imprimées), B2 (blocage + contrôles de profil), B3 (seules
les factures émises sont transmises), B4 (numéro attribué à l'émission, série
verrouillée par entité/année), B5 (filigrane brouillon, lien public), B7 (TVA par
taux), B8/B9 (XML Factur-X).
"""
import io
import json
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pytest

CSRF = 'f' * 64
PASSWORD = 'Sup3r-Secret-Pass!2026'
RAM = '{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}'
RSM = '{urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100}'

FULL_PROFILE = {
    'name': 'Atelier Dupont SAS', 'legal_form': 'SAS', 'share_capital': '10 000', 'rcs_city': 'Lyon',
    'address': '18 rue de la Charité', 'postal_code': '69002', 'city': 'Lyon', 'siret': '73282932000074',
    'vat_number': 'FR44732829320', 'iban': 'FR7630006000011234567890189', 'bic': 'AGRIFRPP',
    'payment_terms': 'Paiement à 30 jours par virement',
}


@pytest.fixture(autouse=True)
def _no_boamp(monkeypatch):
    # Le profil entreprise déclenche une actualisation BOAMP (réseau) : neutralisée.
    from profitos.routes import main as main_routes
    monkeypatch.setattr(main_routes, 'sync_grow', lambda: 0)


@pytest.fixture()
def app():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    return app


def _post(client, url, data=None, **kw):
    with client.session_transaction() as s:
        s['csrf_token'] = CSRF
    payload = dict(data or {})
    payload['csrf_token'] = CSRF
    return client.post(url, data=payload, **kw)


def _flashes(client):
    with client.session_transaction() as s:
        return [m for _, m in s.get('_flashes', [])]


def _signup(app, plan='BUSINESS'):
    from profitos import runtime as rt
    client = app.test_client()
    r = _post(client, '/signup', {'accept_terms': '1', 'full_name': 'Claire Dupuis', 'email': f'{uuid.uuid4().hex[:10]}@example.com',
                                  'password': PASSWORD, 'company_name': 'Atelier Dupont SAS'})
    assert r.status_code in (302, 303)
    with client.session_transaction() as s:
        org_id = s['org_id']
    c = rt.auth_cx()
    c.execute("UPDATE organizations SET plan=?,status='ACTIVE_PAID' WHERE id=?", (plan, org_id))
    c.commit(); c.close()
    return client, org_id


def _db(org_id):
    from profitos import runtime as rt
    c = sqlite3.connect(rt.tenant_db(org_id))
    c.row_factory = sqlite3.Row
    return c


def _create_invoice(client, org_id, **extra):
    data = {'client_name': 'Client Lumière SA', 'client_email': 'compta@client.example',
            'client_address': '1 rue A\n69001 Lyon', 'client_siren': '552100554',
            'due_date': (date.today() + timedelta(days=30)).isoformat(), 'operation_nature': 'services',
            'label_1': 'Prestation de conseil', 'qty_1': '1', 'price_1': '1000', 'vat_1': '20'}
    data.update(extra)
    r = _post(client, '/facturation/nouvelle', data)
    assert r.status_code in (302, 303), r.get_data(as_text=True)[:500]
    c = _db(org_id)
    inv_id = c.execute('SELECT MAX(id) FROM outgoing_invoices').fetchone()[0]
    c.close()
    return inv_id


def _invoice(org_id, inv_id):
    c = _db(org_id)
    row = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()
    c.close()
    return row


def _pdf_text(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return '\n'.join(p.extract_text() for p in reader.pages)


def _norm(text):
    return re.sub(r'\s+', ' ', text.replace(' ', ' ').replace(' ', ' '))


# --- B2 : identifiants -------------------------------------------------------------------

def test_identifier_checks_luhn_vat_key_iban_postcode():
    from profitos.fr_identifiers import (siren_is_valid, siret_is_valid, fr_vat_is_valid, iban_is_valid,
                                         fr_postal_code_is_valid)
    assert siren_is_valid('552100554') and not siren_is_valid('552100555')
    assert siret_is_valid('73282932000074') and not siret_is_valid('73282932000075')
    # La Poste : somme des chiffres multiple de 5 (35600000000010 -> 3+5+6+1 = 15).
    assert siret_is_valid('35600000000010') and not siret_is_valid('35600000000011')
    # Clé TVA : (12 + 3 × (732829320 mod 97)) mod 97 = 44.
    assert fr_vat_is_valid('FR44732829320') and not fr_vat_is_valid('FR45732829320')
    assert not fr_vat_is_valid('FR44732829320', siren='552100554')
    assert iban_is_valid('FR76 3000 6000 0112 3456 7890 189') and not iban_is_valid('FR7630006000011234567890188')
    assert fr_postal_code_is_valid('69002') and not fr_postal_code_is_valid('6900')


def test_profile_rejects_invalid_identifiers_keeps_values_and_stays_on_page(app):
    client, org_id = _signup(app)
    bad = dict(FULL_PROFILE, siret='73282932000075', vat_number='FR45732829320', iban='FR7630006000011234567890188',
               postal_code='6900', rcs_city='Villeurbanne')
    r = _post(client, '/company', bad)
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert 'SIRET invalide' in html and 'IBAN invalide' in html and 'Code postal invalide' in html
    assert "TVA intracommunautaire invalide" in html
    assert 'value="Villeurbanne"' in html  # valeurs conservées
    c = _db(org_id)
    assert c.execute('SELECT siret FROM company WHERE id=1').fetchone() is None or \
        c.execute('SELECT siret FROM company WHERE id=1').fetchone()['siret'] != '73282932000075'
    c.close()


def test_profile_saves_legal_fields_and_hides_boamp_errors(app, monkeypatch):
    from profitos.routes import main as main_routes
    client, org_id = _signup(app)

    def boom():
        raise RuntimeError('Tunnel connection failed: 403 secret-host')
    monkeypatch.setattr(main_routes, 'sync_grow', boom)
    r = _post(client, '/company', FULL_PROFILE)
    assert r.status_code == 302 and r.headers['Location'].endswith('/company')
    msgs = ' '.join(_flashes(client))
    assert 'Profil entreprise enregistré' in msgs and 'secret-host' not in msgs and 'Tunnel' not in msgs
    c = _db(org_id)
    p = c.execute('SELECT * FROM company WHERE id=1').fetchone()
    c.close()
    assert (p['legal_form'], p['share_capital'], p['rcs_city'], p['siret']) == ('SAS', '10000', 'Lyon', '73282932000074')


# --- B4 / B2 / B5 : brouillon, blocage, numérotation ------------------------------------

def test_draft_has_no_number_and_issue_is_blocked_until_mentions_complete(app):
    client, org_id = _signup(app)
    _post(client, '/company', {'name': 'Atelier Dupont SAS', 'address': '18 rue de la Charité'})
    inv_id = _create_invoice(client, org_id)
    inv = _invoice(org_id, inv_id)
    assert inv['invoice_number'] is None and inv['status'] == 'draft'
    page = client.get(f'/facturation/{inv_id}').get_data(as_text=True)
    assert f'Brouillon n°{inv_id}' in page
    # Émission refusée : mentions manquantes listées avec un lien vers le profil.
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    assert any('mention(s) obligatoire(s) manquante(s)' in m for m in _flashes(client))
    inv = _invoice(org_id, inv_id)
    assert inv['status'] == 'draft' and inv['invoice_number'] is None
    page = client.get(f'/facturation/{inv_id}').get_data(as_text=True)
    assert "Forme juridique de l&#39;émetteur" in page and 'Compléter le profil' in page
    # Envoi et transmission également bloqués.
    _post(client, f'/facturation/{inv_id}/envoyer')
    assert _invoice(org_id, inv_id)['status'] == 'draft'
    _post(client, f'/facturation/{inv_id}/weinvoice')
    assert any('Seule une facture émise' in m for m in _flashes(client))


def test_numbering_at_issue_cancelled_draft_consumes_nothing_and_series_per_entity(app):
    from profitos.accounting import paris_today
    client, org_id = _signup(app, plan='MULTI')
    _post(client, '/company', FULL_PROFILE)
    year = paris_today().year
    cancelled = _create_invoice(client, org_id)
    _post(client, f'/facturation/{cancelled}/annuler')
    first = _create_invoice(client, org_id)
    second = _create_invoice(client, org_id, price_1='250')
    # Émission dans l'ordre inverse de création : le numéro suit l'émission.
    _post(client, f'/facturation/{second}/marquer-emise')
    _post(client, f'/facturation/{first}/marquer-emise')
    assert _invoice(org_id, cancelled)['invoice_number'] is None
    assert _invoice(org_id, second)['invoice_number'] == f'FA-{year}-001'
    assert _invoice(org_id, first)['invoice_number'] == f'FA-{year}-002'
    assert _invoice(org_id, first)['issue_date'] == paris_today().isoformat()
    c = _db(org_id)
    seq = c.execute("SELECT last_sequence FROM invoice_number_sequences WHERE entity_key=0 AND fiscal_year=?",
                    (f'invoice:{year}',)).fetchone()['last_sequence']
    entry = c.execute("SELECT piece_ref FROM accounting_entries WHERE source_type='outgoing_invoice' AND source_id=?",
                      (first,)).fetchone()
    c.close()
    assert seq == 2 and entry['piece_ref'] == f'FA-{year}-002'
    # Une filiale a sa propre série.
    from profitos.invoice_numbering import next_document_number
    c = _db(org_id)
    assert next_document_number(c, 'invoice', 7, f'{year}-03-01') == f'FA-E7-{year}-001'
    assert next_document_number(c, 'invoice', None, f'{year + 1}-01-01') == f'FA-{year + 1}-001'
    c.rollback(); c.close()


def test_issue_is_atomic_when_accounting_entry_is_refused(app, monkeypatch):
    from profitos.routes import invoicing as inv_routes
    from profitos.accounting import AccountingError
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id)

    def refuse(*a, **k):
        raise AccountingError('Période clôturée')
    monkeypatch.setattr(inv_routes, 'generate_sale_entry', refuse)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    inv = _invoice(org_id, inv_id)
    assert inv['status'] == 'draft' and inv['invoice_number'] is None
    c = _db(org_id)
    assert c.execute("SELECT COUNT(*) FROM invoice_number_sequences").fetchone()[0] == 0
    c.close()
    assert any('Période clôturée' in m for m in _flashes(client))


def test_send_issues_then_emails_and_email_failure_keeps_issued_invoice(app, monkeypatch):
    from profitos.routes import invoicing as inv_routes
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id)
    calls = []

    def failing_send(to, subject, html, **kw):
        calls.append((to, subject))
        return {'sent': False, 'error': 'SMTP 550 internal relay detail'}
    monkeypatch.setattr(inv_routes, 'send_email', failing_send)
    _post(client, f'/facturation/{inv_id}/envoyer')
    inv = _invoice(org_id, inv_id)
    assert inv['status'] == 'sent' and inv['invoice_number'].startswith('FA-')
    assert calls and inv['invoice_number'] in calls[0][1]
    msgs = ' '.join(_flashes(client))
    assert "L'e-mail n'a pas pu être envoyé" in msgs and 'SMTP 550' not in msgs


def test_public_link_only_for_issued_invoice_and_draft_pdf_watermark(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id)
    token = _invoice(org_id, inv_id)['public_token']
    anon = app.test_client()
    assert anon.get(f'/facture/{token}').status_code == 404
    assert anon.get(f'/facture/{token}/pdf').status_code == 404
    draft_pdf = client.get(f'/facturation/{inv_id}/pdf')
    assert draft_pdf.status_code == 200
    text = _norm(_pdf_text(draft_pdf.data))
    assert 'BROUILLON — document sans valeur de facture' in text and f'Brouillon n°{inv_id}' in text
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    page = anon.get(f'/facture/{token}')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'FR44732829320' in html and 'À régler' in html and 'cancelled' not in html


def test_quote_conversion_copies_fields_and_activates_public_link_at_issue(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    _post(client, '/facturation/devis/nouveau', {
        'client_name': 'Client Devis', 'client_email': 'a@b.example', 'client_address': '5 rue D\n75002 Paris',
        'client_siren': '552100554', 'operation_nature': 'biens', 'delivery_address': 'Dépôt\n93200 Saint-Denis',
        'notes': 'Ligne 1\nLigne 2', 'label_1': 'Chaise', 'qty_1': '2', 'price_1': '45,50', 'vat_1': '20'})
    c = _db(org_id)
    q = c.execute('SELECT * FROM outgoing_quotes ORDER BY id DESC').fetchone()
    c.execute("UPDATE outgoing_quotes SET status='accepted' WHERE id=?", (q['id'],)); c.commit(); c.close()
    _post(client, f"/facturation/devis/{q['id']}/convertir")
    c = _db(org_id)
    inv = c.execute('SELECT * FROM outgoing_invoices ORDER BY id DESC').fetchone()
    c.close()
    assert inv['invoice_number'] is None
    assert (inv['client_siren'], inv['operation_nature']) == ('552100554', 'biens')
    assert inv['delivery_address'] == 'Dépôt\n93200 Saint-Denis'
    assert '\\n' not in inv['notes'] and 'Ligne 1\nLigne 2' in inv['notes']
    assert inv['total'] == 109.2
    _post(client, f"/facturation/{inv['id']}/marquer-emise")
    from profitos import runtime as rt
    ac = rt.auth_cx()
    mapping = ac.execute('SELECT * FROM outgoing_invoice_tokens WHERE token=?', (inv['public_token'],)).fetchone()
    ac.close()
    assert mapping is not None
    assert app.test_client().get(f"/facture/{inv['public_token']}").status_code == 200


# --- B1 / B7 : PDF et montants ----------------------------------------------------------

def test_issued_pdf_prints_all_mandatory_mentions_and_vat_per_rate(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id, operation_nature='mixte', delivery_address='Entrepôt\n3 rue C\n69003 Lyon',
                             client_vat_number='FR96552100554', service_date=date.today().isoformat(),
                             label_1="Main d'œuvre", qty_1='3', price_1='12,345', vat_1='20', nature_1='services',
                             label_2='Livre technique', qty_2='1', price_2='10', vat_2='5,5', nature_2='biens',
                             vat_on_debits='on')
    inv = _invoice(org_id, inv_id)
    items = json.loads(inv['line_items'])
    assert items[0]['line_total'] == 37.04          # 3 × 12,345 = 37,035 -> 37,04
    assert (inv['subtotal'], inv['vat_amount'], inv['total']) == (47.04, 7.96, 55.0)  # 7,41 + 0,55
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    inv = _invoice(org_id, inv_id)
    text = _norm(_pdf_text(client.get(f'/facturation/{inv_id}/pdf').data))
    today = date.today().strftime('%d/%m/%Y')
    for expected in (f"Facture n° {inv['invoice_number']}", '69002 Lyon', 'SAS au capital de 10 000 €',
                     'RCS Lyon 732 829 320', 'SIREN 552100554', 'FR96552100554', 'Entrepôt',
                     "Livraison de biens et prestation de services", f"Date d'émission : {today}",
                     "Option pour le paiement de la TVA d'après les débits", '5,5 % 10,00 € 0,55 €',
                     '20 % 37,04 € 7,41 €', 'Total TTC 55,00 €', '40 €', "Pénalités de retard",
                     "Pas d'escompte pour paiement anticipé", "Main d'œuvre", '12,345 €'):
        assert expected in text, expected
    assert 'BROUILLON' not in text


def test_franchise_seller_prints_293b_and_xml_category_e(app):
    from profitos.routes import invoicing as I
    seller = dict(FULL_PROFILE, vat_regime='franchise_293B', vat_number='')
    items = [{'label': 'Cours', 'qty': 2, 'unit_price': 50, 'vat_rate': 0, 'line_total': 100.0}]
    inv = {'id': 1, 'invoice_number': 'FA-2026-010', 'status': 'sent', 'issue_date': '2026-10-01',
           'due_date': '2026-10-31', 'client_name': 'Mme Martin', 'client_address': '2 rue E\n75011 Paris',
           'client_siren': None, 'ereporting_scope': 'b2c', 'line_items': json.dumps(items), 'subtotal': 100.0,
           'vat_amount': 0.0, 'total': 100.0, 'operation_nature': 'services', 'notes': ''}
    _, missing = I._check_mandatory_mentions(inv, seller)
    assert missing == []
    with app.app_context():
        text = _norm(_pdf_text(I._render_invoice_pdf(inv, seller)))
    assert 'TVA non applicable, art. 293 B du CGI' in text and '40 €' not in text  # B2C : pas d'indemnité
    root = ET.fromstring(I.generate_facturx_xml(inv, items, seller).split(b'\n', 1)[1])
    tax = root.find(f'.//{RAM}ApplicableHeaderTradeSettlement/{RAM}ApplicableTradeTax')
    assert tax.find(f'{RAM}CategoryCode').text == 'E'
    assert tax.find(f'{RAM}ExemptionReason').text == 'TVA non applicable, art. 293 B du CGI'
    assert tax.find(f'{RAM}ExemptionReasonCode').text == 'VATEX-FR-FRANCHISE'


def test_vat_breakdown_rounds_per_rate_and_validator_checks_br_co_14():
    from profitos import invoice_amounts as amounts
    from profitos.routes import invoicing as I
    items = [{'label': 'A', 'qty': 1, 'unit_price': 2.75, 'vat_rate': 20, 'line_total': 2.75},
             {'label': 'B', 'qty': 1, 'unit_price': 2.75, 'vat_rate': 20, 'line_total': 2.75}]
    # TVA ligne à ligne : 0,55 + 0,55 = 1,10 ; par taux : 5,50 × 20 % = 1,10. Cas piégeux :
    items2 = [{'label': 'A', 'qty': 1, 'unit_price': 0.125, 'vat_rate': 20, 'line_total': 0.13},
              {'label': 'B', 'qty': 1, 'unit_price': 0.125, 'vat_rate': 20, 'line_total': 0.13}]
    assert amounts.totals(items) == (5.5, 1.1, 6.6)
    assert amounts.vat_breakdown(items2) == [{'rate': 20.0, 'base': 0.26, 'vat': 0.05}]  # 0,026 + 0,026 -> 0,05
    inv = {'invoice_number': 'X', 'issue_date': '2026-01-01', 'client_name': 'C', 'client_address': 'a',
           'subtotal': 0.26, 'vat_amount': 0.06, 'total': 0.32, 'due_date': '2026-02-01'}
    seller = dict(FULL_PROFILE)
    _, failures = I.validate_facturx_business_rules(inv, items2, seller)
    assert [f['code'] for f in failures] == ['BR-CO-14']
    inv.update(vat_amount=0.05, total=0.31)
    assert I.validate_facturx_business_rules(inv, items2, seller)[1] == []


# --- B8 / B9 : XML Factur-X --------------------------------------------------------------

def _xml(I, inv, items, seller, **kw):
    return ET.fromstring(I.generate_facturx_xml(inv, items, seller, **kw).split(b'\n', 1)[1])


def test_facturx_xml_identity_notes_reverse_charge_and_totals():
    from profitos.routes import invoicing as I
    seller = dict(FULL_PROFILE)
    items = [{'label': 'Audit', 'qty': 1, 'unit_price': 1200, 'vat_rate': 0, 'line_total': 1200.0}]
    inv = {'invoice_number': 'FA-2026-020', 'issue_date': '2026-10-02', 'due_date': '2026-11-01',
           'client_name': 'Kunde GmbH', 'client_address': 'Hauptstraße 5\n10115 Berlin', 'counterparty_country': 'DE',
           'client_vat_number': 'DE123456789', 'vat_treatment': 'intra_eu_services', 'subtotal': 1200, 'vat_amount': 0,
           'total': 1200, 'operation_nature': 'services'}
    root = _xml(I, inv, items, seller)
    notes = {n.find(f'{RAM}SubjectCode').text for n in root.iter(f'{RAM}IncludedNote')}
    assert {'PMT', 'PMD', 'AAB'} <= notes
    seller_el = root.find(f'.//{RAM}SellerTradeParty')
    addr = seller_el.find(f'{RAM}PostalTradeAddress')
    assert addr.find(f'{RAM}PostcodeCode').text == '69002' and addr.find(f'{RAM}CityName').text == 'Lyon'
    assert seller_el.find(f'{RAM}SpecifiedTaxRegistration/{RAM}ID').text == 'FR44732829320'
    assert 'SAS au capital de 10 000' in seller_el.find(f'{RAM}Description').text.replace(' ', ' ')
    buyer = root.find(f'.//{RAM}BuyerTradeParty')
    assert buyer.find(f'{RAM}PostalTradeAddress/{RAM}CountryID').text == 'DE'
    assert buyer.find(f'{RAM}PostalTradeAddress/{RAM}PostcodeCode').text == '10115'
    assert buyer.find(f'{RAM}SpecifiedTaxRegistration/{RAM}ID').text == 'DE123456789'
    tax = root.find(f'.//{RAM}ApplicableHeaderTradeSettlement/{RAM}ApplicableTradeTax')
    assert tax.find(f'{RAM}CategoryCode').text == 'AE' and 'Autoliquidation' in tax.find(f'{RAM}ExemptionReason').text
    assert root.find(f'.//{RAM}SpecifiedTradePaymentTerms/{RAM}DueDateDateTime')[0].text == '20261101'
    summ = root.find(f'.//{RAM}SpecifiedTradeSettlementHeaderMonetarySummation')
    assert summ.find(f'{RAM}DuePayableAmount').text == '1200.00'
    _, failures = I.validate_facturx_business_rules(inv, items, seller)
    assert failures == []


def test_credit_note_xml_reference_order_and_lines_match_header():
    from profitos.routes import invoicing as I
    seller = dict(FULL_PROFILE)
    original = {'id': 3, 'invoice_number': 'FA-2026-005', 'issue_date': '2026-09-15', 'client_name': 'Client SA',
                'client_address': '1 rue A\n69001 Lyon', 'client_siren': '552100554', 'operation_nature': 'services',
                'line_items': '[]'}
    src = [{'label': 'Mission', 'qty': 1, 'unit_price': 999.99, 'vat_rate': 20, 'line_total': 999.99},
           {'label': 'Frais', 'qty': 1, 'unit_price': 33.33, 'vat_rate': 10, 'line_total': 33.33}]
    items = I.credit_note_items(src, ratio=0.37)
    ht, vat, ttc = I._totals(items)
    credit = {'credit_number': 'AV-2026-001', 'original_invoice_number': 'FA-2026-005', 'issue_date': '2026-10-05',
              'line_items': json.dumps(items), 'subtotal': ht, 'vat_amount': vat, 'total': ttc}
    root = ET.fromstring(I.generate_credit_facturx_xml(credit, original, seller).split(b'\n', 1)[1])
    assert root.find(f'{RSM}ExchangedDocument/{RAM}TypeCode').text == '381'
    settlement = root.find(f'.//{RAM}ApplicableHeaderTradeSettlement')
    tags = [child.tag.replace(RAM, '') for child in settlement]
    assert tags.index('InvoiceReferencedDocument') == tags.index('SpecifiedTradeSettlementHeaderMonetarySummation') + 1
    assert settlement.find(f'{RAM}InvoiceReferencedDocument/{RAM}IssuerAssignedID').text == 'FA-2026-005'
    line_sum = round(sum(float(e.text) for e in root.iter(f'{RAM}LineTotalAmount')
                         if e is not settlement.find(f'.//{RAM}SpecifiedTradeSettlementHeaderMonetarySummation/{RAM}LineTotalAmount')), 2)
    assert line_sum == ht == round(370.0 + 12.33, 2)
    assert settlement.find(f'{RAM}SpecifiedTradePaymentTerms/{RAM}Description') is not None


def test_final_invoice_deduction_uses_negative_quantity(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    _post(client, '/facturation/devis/nouveau', {
        'client_name': 'Client Chantier', 'client_address': '5 rue D\n75002 Paris', 'client_siren': '552100554',
        'label_1': 'Travaux', 'qty_1': '1', 'price_1': '1000', 'vat_1': '20'})
    c = _db(org_id)
    qid = c.execute('SELECT MAX(id) FROM outgoing_quotes').fetchone()[0]
    c.execute("UPDATE outgoing_quotes SET status='accepted' WHERE id=?", (qid,)); c.commit(); c.close()
    _post(client, f'/facturation/devis/{qid}/acompte', {'deposit_percent': '30'})
    c = _db(org_id)
    dep = c.execute("SELECT * FROM outgoing_invoices WHERE invoice_kind='deposit'").fetchone()
    c.close()
    assert (dep['subtotal'], dep['vat_amount'], dep['total']) == (300.0, 60.0, 360.0)
    _post(client, f"/facturation/{dep['id']}/marquer-emise")
    _post(client, f'/facturation/devis/{qid}/facture-finale')
    c = _db(org_id)
    final = c.execute("SELECT * FROM outgoing_invoices WHERE invoice_kind='final'").fetchone()
    c.close()
    deduction = [it for it in json.loads(final['line_items']) if it.get('deposit_deduction')][0]
    assert deduction['qty'] == -1.0 and deduction['unit_price'] == 300.0 and deduction['line_total'] == -300.0
    assert (final['subtotal'], final['vat_amount'], final['total']) == (700.0, 140.0, 840.0)
    _post(client, f"/facturation/{final['id']}/marquer-emise")
    c = _db(org_id)
    entry = c.execute("SELECT id FROM accounting_entries WHERE source_type='customer_final_invoice' AND source_id=?",
                      (final['id'],)).fetchone()
    lines = {r['account_code']: (r['debit'], r['credit']) for r in c.execute(
        'SELECT account_code,SUM(debit) debit,SUM(credit) credit FROM accounting_entry_lines WHERE entry_id=? GROUP BY account_code',
        (entry['id'],))}
    c.close()
    # 411 = 840 ; 4191 apuré de 300 ; 706 = 1 000 HT ; TVA nette 140 (en attente : services sans débits).
    assert lines['411000'] == (840.0, 0) and lines['419100'] == (300.0, 0) and lines['706000'] == (0, 1000.0)
    assert lines['445740'] == (0, 140.0)
