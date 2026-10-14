"""v439 — Facture électronique : règles françaises et EN 16931 vérifiées contre les
artefacts officiels (XSD CII D16B + schématron EN 16931 du CEN, validés avec Saxon
hors CI). Régressions couvertes :
- BT-23 « cadre de facturation » obligatoire (BR-FR-08) : B/S/M + 1, ou 4 si la facture
  définitive est entièrement couverte par les acomptes (BR-FR-CO-09) ;
- livraison intracommunautaire : date de livraison (BR-IC-11) et pays de livraison
  (BR-IC-12) toujours présents ;
- facture d'acompte : type 386 (et IBAN conservé) ;
- XML embarqué avec date de modification (/Params /ModDate)."""
import xml.etree.ElementTree as ET

from tests.test_v421_invoice_compliance import FULL_PROFILE

RSM = '{urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100}'
RAM = '{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}'
ITEMS = [{'label': 'Prestation', 'qty': 1, 'unit_price': 1000, 'vat_rate': 20, 'line_total': 1000.0}]


def _inv(**kw):
    inv = {'invoice_number': 'FA-2026-100', 'issue_date': '2026-10-02', 'due_date': '2026-11-01',
           'client_name': 'Client SA', 'client_address': '1 rue A\n69001 Lyon', 'client_siren': '552100554',
           'subtotal': 1000, 'vat_amount': 200, 'total': 1200, 'operation_nature': 'services'}
    inv.update(kw)
    return inv


def _xml(inv, items=ITEMS, **kw):
    from profitos.routes import invoicing as I
    return ET.fromstring(I.generate_facturx_xml(inv, items, dict(FULL_PROFILE), **kw).split(b'\n', 1)[1])


def _mode(root):
    ctx = root.find(f'{RSM}ExchangedDocumentContext')
    tags = [c.tag.replace(RAM, '') for c in ctx]
    assert tags.index('BusinessProcessSpecifiedDocumentContextParameter') < tags.index('GuidelineSpecifiedDocumentContextParameter')
    return ctx.find(f'{RAM}BusinessProcessSpecifiedDocumentContextParameter/{RAM}ID').text


def test_bt23_billing_mode_follows_operation_nature():
    assert _mode(_xml(_inv())) == 'S1'
    assert _mode(_xml(_inv(operation_nature='biens'))) == 'B1'
    assert _mode(_xml(_inv(operation_nature='mixte'))) == 'M1'


def test_bt23_mode_4_only_when_fully_covered_by_deposits():
    assert _mode(_xml(_inv(), prepaid=1200.0)) == 'S4'
    assert _mode(_xml(_inv(), prepaid=300.0)) == 'S1'
    assert _mode(_xml(_inv(), type_code='386', prepaid=1200.0)) == 'S1'  # BR-FR-CO-08


def test_intra_community_supply_has_delivery_date_and_country():
    items = [{'label': 'Machine', 'qty': 1, 'unit_price': 8000, 'vat_rate': 0, 'line_total': 8000.0}]
    inv = _inv(vat_treatment='intra_eu_goods', operation_nature='biens', client_name='Cliente SRL',
               client_address='Via Roma 1\n00100 Roma', counterparty_country='IT', client_vat_number='IT12345678901',
               client_siren='', subtotal=8000, vat_amount=0, total=8000)
    delivery = _xml(inv, items).find(f'.//{RAM}ApplicableHeaderTradeDelivery')
    assert delivery.find(f'{RAM}ShipToTradeParty/{RAM}PostalTradeAddress/{RAM}CountryID').text == 'IT'
    assert delivery.find(f'{RAM}ActualDeliverySupplyChainEvent/{RAM}OccurrenceDateTime')[0].text == '20261002'
    # Une prestation classique n'invente ni date ni adresse de livraison.
    plain = _xml(_inv()).find(f'.//{RAM}ApplicableHeaderTradeDelivery')
    assert plain.find(f'{RAM}ShipToTradeParty') is None and plain.find(f'{RAM}ActualDeliverySupplyChainEvent') is None


def test_deposit_invoice_keeps_iban_payment_means():
    root = _xml(_inv(), type_code='386')
    assert root.find(f'{RSM}ExchangedDocument/{RAM}TypeCode').text == '386'
    assert root.find(f'.//{RAM}SpecifiedTradeSettlementPaymentMeans/{RAM}PayeePartyCreditorFinancialAccount/{RAM}IBANID') is not None


def test_render_facturx_uses_386_for_deposits_and_sets_moddate(monkeypatch):
    from profitos.routes import invoicing as I
    seen = {}
    import fpdf.enums as fenums  # fpdf2 < 2.8 en local : énumération simulée
    if not hasattr(fenums, 'DocumentCompliance'):
        monkeypatch.setattr(fenums, 'DocumentCompliance', type('DC', (), {'PDFA_3B': 'PDFA_3B'}), raising=False)
    monkeypatch.setattr(I._pdf, 'pdfa_supported', lambda: True)
    monkeypatch.setattr(I._pdf, 'render_document', lambda doc, **kw: seen.update(kw) or b'%PDF')

    def fake_xml(inv, items, company, type_code='380', **kw):
        seen['type_code'] = type_code
        return b'<?xml version="1.0"?>\n<x/>'
    monkeypatch.setattr(I, 'generate_facturx_xml', fake_xml)
    monkeypatch.setattr(I, 'build_invoice_document', lambda inv, company: {'title': 'Facture'})
    I.render_facturx_pdf(dict(_inv(), invoice_kind='deposit', line_items='[]'), dict(FULL_PROFILE))
    assert seen['type_code'] == '386'
    att = seen['attachments'][0]
    assert att['basename'] == 'factur-x.xml' and att['associated_file_relationship'] == 'Alternative'
    assert att['modification_date'] is not None
    I.render_facturx_pdf(dict(_inv(), invoice_kind='standard', line_items='[]'), dict(FULL_PROFILE))
    assert seen['type_code'] == '380'


def test_french_mandatory_notes_and_identifiers_present():
    root = _xml(_inv())
    subjects = [n.find(f'{RAM}SubjectCode').text for n in root.iter(f'{RAM}IncludedNote')]
    assert sorted(s for s in subjects if s in ('PMT', 'PMD', 'AAB')) == ['AAB', 'PMD', 'PMT']   # BR-FR-05/06
    seller = root.find(f'.//{RAM}SellerTradeParty')
    buyer = root.find(f'.//{RAM}BuyerTradeParty')
    assert seller.find(f'{RAM}SpecifiedLegalOrganization/{RAM}ID').attrib['schemeID'] == '0002'      # BR-FR-10
    assert buyer.find(f'{RAM}SpecifiedLegalOrganization/{RAM}ID').text == '552100554'               # BR-FR-11
    uri = buyer.find(f'{RAM}URIUniversalCommunication/{RAM}URIID')
    assert uri.attrib['schemeID'] == '0225' and uri.text == '552100554'                             # BR-FR-21
