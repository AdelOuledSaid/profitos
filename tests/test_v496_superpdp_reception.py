"""v496 — Réception des factures fournisseurs par SUPER PDP : import en facture d'achat (contenu
``en_invoice`` ou XML du fichier d'origine), avoirs, reprise après échec, statuts de l'acheteur.
Le réseau est simulé."""
import pytest

from profitos import einvoice_superpdp_inbound as ESI
from test_v421_invoice_compliance import FULL_PROFILE, _flashes, _post, _signup, app  # noqa: F401
from test_v495_superpdp_client import Resp
from test_v495_superpdp_integration import _db, _no_boamp, connect, pdp  # noqa: F401

PDF = b'%PDF-1.7 facture fournisseur'
CII = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
 xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
 xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
 <rsm:ExchangedDocument><ram:ID>F-CII-7</ram:ID><ram:TypeCode>380</ram:TypeCode>
  <ram:IssueDateTime><udt:DateTimeString format="102">20261102</udt:DateTimeString></ram:IssueDateTime></rsm:ExchangedDocument>
 <rsm:SupplyChainTradeTransaction>
  <ram:ApplicableHeaderTradeAgreement><ram:SellerTradeParty><ram:Name>Quincaillerie Centrale</ram:Name></ram:SellerTradeParty>
   <ram:BuyerTradeParty><ram:Name>Atelier Dupont SAS</ram:Name></ram:BuyerTradeParty></ram:ApplicableHeaderTradeAgreement>
  <ram:ApplicableHeaderTradeSettlement>
   <ram:SpecifiedTradePaymentTerms><ram:DueDateDateTime><udt:DateTimeString format="102">20261202</udt:DateTimeString></ram:DueDateDateTime></ram:SpecifiedTradePaymentTerms>
   <ram:SpecifiedTradeSettlementHeaderMonetarySummation><ram:TaxBasisTotalAmount>250.00</ram:TaxBasisTotalAmount>
    <ram:TaxTotalAmount currencyID="EUR">50.00</ram:TaxTotalAmount><ram:GrandTotalAmount>300.00</ram:GrandTotalAmount>
   </ram:SpecifiedTradeSettlementHeaderMonetarySummation></ram:ApplicableHeaderTradeSettlement>
 </rsm:SupplyChainTradeTransaction></rsm:CrossIndustryInvoice>""".encode()


def en(number, ht, vat, type_code=380, original=None, seller='Bois & Panneaux Durand'):
    data = {'number': number, 'issue_date': '2026-11-03', 'type_code': type_code, 'payment_due_date': '2026-12-03',
            'seller': {'name': seller}, 'buyer': {'name': 'Atelier Dupont SAS'},
            'totals': {'total_without_vat': f'{ht:.2f}', 'total_vat_amount': f'{vat:.2f}', 'total_with_vat': f'{ht + vat:.2f}'}}
    if original:
        data['preceding_invoice_references'] = [{'reference': original}]
    return data


def serve(pdp, items, files=None):
    """Simule la liste des factures reçues (curseur compris) et le téléchargement des fichiers."""
    def listing(req):
        after = int(req['params'].get('starting_after_id') or 0)
        return Resp(200, {'data': [i for i in items if i['id'] > after], 'has_after': False})
    pdp.routes[('GET', '/v1.beta/invoices')] = listing
    for i in items:
        content = (files or {}).get(i['id'], PDF)
        pdp.routes[('GET', f"/v1.beta/invoices/{i['id']}")] = (
            lambda req, i=i, content=content: Resp(200, None, content=content, ctype='application/pdf' if content[:4] == b'%PDF' else 'application/xml')
            if (req['params'] or {}).get('format') == 'original' else Resp(200, i))


@pytest.fixture
def linked(app, pdp):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    connect(client)
    _flashes(client)
    return client, org_id


def sync(client):
    _post(client, '/facturation/achats/weinvoice/synchroniser')
    return ' '.join(_flashes(client))


def purchases(org_id):
    c = _db(org_id)
    rows = c.execute('SELECT * FROM purchase_invoices ORDER BY id').fetchall()
    c.close()
    return rows


def test_import_puis_aucun_doublon(linked, pdp):
    client, org_id = linked
    serve(pdp, [{'id': 11, 'direction': 'in', 'events': [{'id': 1, 'status_code': 'fr:202'}], 'en_invoice': en('F-2026-88', 1000, 200)}])
    assert '1 importée(s)' in sync(client)
    p = purchases(org_id)[0]
    assert (p['supplier_name'], p['invoice_number'], p['subtotal'], p['vat_amount'], p['total']) == \
        ('Bois & Panneaux Durand', 'F-2026-88', 1000.0, 200.0, 1200.0)
    assert p['issue_date'] == '2026-11-03' and p['due_date'] == '2026-12-03' and p['status'] == 'unpaid'
    assert p['einvoice_provider'] == 'superpdp' and p['weinvoice_invoice_id'] == '11' and p['weinvoice_status'] == 'RECEIVED'
    assert p['document_path'].startswith('superpdp_') and p['document_path'].endswith('.pdf')
    c = _db(org_id)
    assert c.execute("SELECT COUNT(*) AS n FROM accounting_entries WHERE source_type='purchase_invoice'").fetchone()['n'] == 1
    assert c.execute('SELECT inbound_cursor FROM superpdp_connections').fetchone()['inbound_cursor'] == 11
    c.close()
    msg = sync(client)                                     # second passage : rien de neuf
    assert '0 importée(s)' in msg and len(purchases(org_id)) == 1
    page = client.get(f"/facturation/achats/{p['id']}").get_data(as_text=True)
    assert 'Reçue par facturation électronique' in page and 'Prendre en charge' in page


def test_contenu_lu_dans_le_xml_quand_en_invoice_manque(linked, pdp):
    client, org_id = linked
    serve(pdp, [{'id': 12, 'direction': 'in', 'events': []}], files={12: CII})
    assert '1 importée(s)' in sync(client)
    p = purchases(org_id)[0]
    assert (p['supplier_name'], p['invoice_number'], p['subtotal'], p['vat_amount'], p['total'], p['due_date']) == \
        ('Quincaillerie Centrale', 'F-CII-7', 250.0, 50.0, 300.0, '2026-12-02')
    assert p['document_path'].endswith('.xml')


def test_avoir_rattache_a_la_facture_d_origine_et_rejet_ignore(linked, pdp):
    client, org_id = linked
    serve(pdp, [
        {'id': 21, 'direction': 'in', 'events': [], 'en_invoice': en('F-100', 500, 100)},
        {'id': 22, 'direction': 'in', 'events': [{'id': 5, 'status_code': 'fr:213'}], 'en_invoice': en('F-101', 10, 2)},
        {'id': 23, 'direction': 'in', 'events': [], 'en_invoice': en('AV-100', 100, 20, type_code=381, original='F-100')}])
    msg = sync(client)
    assert '2 importée(s)' in msg and '1 rejetée(s) ignorée(s)' in msg
    assert [p['invoice_number'] for p in purchases(org_id)] == ['F-100']
    c = _db(org_id)
    credit = c.execute('SELECT * FROM purchase_credit_notes').fetchone()
    c.close()
    assert credit['credit_number'] == 'AV-100' and credit['original_invoice_number'] == 'F-100' and credit['total'] == 120.0


def test_echec_n_avance_pas_le_curseur_puis_reprise(linked, pdp):
    client, org_id = linked
    items = [{'id': 31, 'direction': 'in', 'events': [], 'en_invoice': en('F-A', 100, 20)},
             {'id': 32, 'direction': 'in', 'events': []},                       # ni en_invoice ni XML lisible
             {'id': 33, 'direction': 'in', 'events': [], 'en_invoice': en('F-C', 300, 60)}]
    serve(pdp, items)
    msg = sync(client)
    assert '1 importée(s)' in msg and '1 échec(s)' in msg
    c = _db(org_id)
    assert c.execute('SELECT inbound_cursor FROM superpdp_connections').fetchone()['inbound_cursor'] == 31
    c.close()
    items[1]['en_invoice'] = en('F-B', 200, 40)             # la plateforme renvoie enfin le contenu
    serve(pdp, items)
    assert '2 importée(s)' in sync(client)
    assert [p['invoice_number'] for p in purchases(org_id)] == ['F-A', 'F-B', 'F-C']


def test_statuts_de_l_acheteur(linked, pdp):
    client, org_id = linked
    serve(pdp, [{'id': 41, 'direction': 'in', 'events': [{'id': 1, 'status_code': 'fr:202'}], 'en_invoice': en('F-41', 100, 20)}])
    sync(client)
    pid = purchases(org_id)[0]['id']
    pdp.routes[('POST', '/v1.beta/invoice_events')] = [Resp(201, {'id': 1})]
    _post(client, f'/facturation/achats/{pid}/weinvoice/action', {'action': 'take-in-charge'})
    assert pdp.calls[-1][2]['json'] == {'invoice_id': 41, 'status_code': 'fr:204'}
    assert purchases(org_id)[0]['weinvoice_status'] == 'TAKEN_IN_CHARGE'
    # un refus sans motif n'est pas transmis
    n = len(pdp.calls)
    _post(client, f'/facturation/achats/{pid}/weinvoice/action', {'action': 'refuse'})
    assert any('motif est obligatoire' in m for m in _flashes(client)) and len(pdp.calls) == n
    _post(client, f'/facturation/achats/{pid}/weinvoice/action', {'action': 'refuse', 'reason_code': 'Prix différent du devis'})
    body = pdp.calls[-1][2]['json']
    assert body['status_code'] == 'fr:210' and body['details'] == [{'reason': 'NON_CONFORME', 'notes': 'Prix différent du devis'}]
    assert purchases(org_id)[0]['weinvoice_status'] == 'REFUSED'


def test_lecture_ubl():
    ubl = b"""<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
      xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
      xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
      <cbc:ID>UBL-9</cbc:ID><cbc:IssueDate>2026-11-05</cbc:IssueDate><cbc:DueDate>2026-12-05</cbc:DueDate>
      <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
      <cac:AccountingSupplierParty><cac:Party><cac:PartyLegalEntity><cbc:RegistrationName>Loc Utilitaires Pro</cbc:RegistrationName>
      </cac:PartyLegalEntity></cac:Party></cac:AccountingSupplierParty>
      <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">104.00</cbc:TaxAmount></cac:TaxTotal>
      <cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount>520.00</cbc:TaxExclusiveAmount>
      <cbc:TaxInclusiveAmount>624.00</cbc:TaxInclusiveAmount></cac:LegalMonetaryTotal></Invoice>"""
    d = ESI.from_xml(ubl)
    assert (d['number'], d['supplier'], d['subtotal'], d['vat'], d['total'], d['due_date']) == \
        ('UBL-9', 'Loc Utilitaires Pro', 520.0, 104.0, 624.0, '2026-12-05')
    assert ESI.from_xml(b'pas du xml') is None and ESI.from_en_invoice({'number': 'X'}) is None
