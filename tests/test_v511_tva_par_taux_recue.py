"""v511 — La ventilation de TVA d'une facture reçue est conservée et comptabilisée.

Seul le total de TVA était retenu. Pour le montant déductible, cela suffit ; mais les
contrôles fiscaux de Veqalio vérifient base × taux, et cette vérification était impossible
sur toute facture reçue par voie électronique — y compris celles à plusieurs taux, où une
erreur de ventilation est précisément la plus probable. La facture de test de la plateforme
en portait deux, 5,5 % et 20 %.
"""
import json

from profitos.accounting import _purchase_vat_parts
from profitos import einvoice_superpdp_inbound as IN

CII = b"""<?xml version="1.0" encoding="UTF-8"?>
<CrossIndustryInvoice xmlns="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100">
  <ExchangedDocument><ID>F-1</ID><TypeCode>380</TypeCode>
    <IssueDateTime><DateTimeString>20261014</DateTimeString></IssueDateTime></ExchangedDocument>
  <SupplyChainTradeTransaction>
   <ApplicableHeaderTradeAgreement><SellerTradeParty><Name>Burger Queen</Name></SellerTradeParty>
   </ApplicableHeaderTradeAgreement>
   <ApplicableHeaderTradeSettlement>
    <ApplicableTradeTax><CalculatedAmount>3.33</CalculatedAmount><BasisAmount>60.46</BasisAmount>
      <RateApplicablePercent>5.50</RateApplicablePercent></ApplicableTradeTax>
    <ApplicableTradeTax><CalculatedAmount>300.00</CalculatedAmount><BasisAmount>1500.00</BasisAmount>
      <RateApplicablePercent>20.00</RateApplicablePercent></ApplicableTradeTax>
    <SpecifiedTradeSettlementHeaderMonetarySummation>
      <TaxBasisTotalAmount>1560.46</TaxBasisTotalAmount><TaxTotalAmount>303.33</TaxTotalAmount>
      <GrandTotalAmount>1863.79</GrandTotalAmount>
    </SpecifiedTradeSettlementHeaderMonetarySummation>
   </ApplicableHeaderTradeSettlement>
  </SupplyChainTradeTransaction></CrossIndustryInvoice>"""

UBL = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>F-2</cbc:ID><cbc:IssueDate>2026-10-15</cbc:IssueDate><cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cac:AccountingSupplierParty><cac:Party><cac:PartyLegalEntity>
    <cbc:RegistrationName>Burger Queen</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party>
  </cac:AccountingSupplierParty>
  <cac:TaxTotal><cbc:TaxAmount>303.33</cbc:TaxAmount>
    <cac:TaxSubtotal><cbc:TaxableAmount>60.46</cbc:TaxableAmount><cbc:TaxAmount>3.33</cbc:TaxAmount>
      <cac:TaxCategory><cbc:Percent>5.50</cbc:Percent></cac:TaxCategory></cac:TaxSubtotal>
    <cac:TaxSubtotal><cbc:TaxableAmount>1500.00</cbc:TaxableAmount><cbc:TaxAmount>300.00</cbc:TaxAmount>
      <cac:TaxCategory><cbc:Percent>20.00</cbc:Percent></cac:TaxCategory></cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount>1560.46</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount>1863.79</cbc:TaxInclusiveAmount></cac:LegalMonetaryTotal>
</Invoice>"""


def rates(breakdown):
    return [(b['rate'], b['base'], b['vat']) for b in breakdown]


def test_les_deux_taux_sont_lus_en_cii():
    assert rates(IN.from_xml(CII)['vat_breakdown']) == [(5.5, 60.46, 3.33), (20.0, 1500.0, 300.0)]


def test_les_deux_taux_sont_lus_en_ubl():
    assert rates(IN.from_xml(UBL)['vat_breakdown']) == [(5.5, 60.46, 3.33), (20.0, 1500.0, 300.0)]


def test_une_ventilation_complete_est_conservee():
    data = IN.from_xml(CII)
    stored = json.loads(IN._breakdown_json(data, 1560.46, 303.33))
    assert [p['rate'] for p in stored] == [5.5, 20.0]


def test_une_ventilation_partielle_est_ecartee():
    """Mieux vaut le total seul qu'une ventilation qui ne rend pas compte de la facture :
    elle déséquilibrerait l'écriture et fausserait les contrôles."""
    data = {'vat_breakdown': [{'rate': 20.0, 'base': 1500.0, 'vat': 300.0}]}
    assert IN._breakdown_json(data, 1560.46, 303.33) is None      # il manque le taux à 5,5 %
    assert IN._breakdown_json({'vat_breakdown': []}, 1560.46, 303.33) is None


# ---------------------------------------------------------------- écriture comptable

def purchase(**extra):
    row = {'vat_breakdown_json': json.dumps([{'rate': 5.5, 'base': 60.46, 'vat': 3.33},
                                             {'rate': 20.0, 'base': 1500.0, 'vat': 300.0}]),
           'vat_amount': 303.33}
    row.update(extra)
    return row


def test_l_ecriture_porte_une_ligne_par_taux():
    parts = _purchase_vat_parts(purchase())
    assert [(p['rate'], p['base'], p['vat']) for p in parts] == [(5.5, 60.46, 3.33), (20.0, 1500.0, 300.0)]


def test_une_ventilation_incoherente_est_ignoree():
    """La somme doit retrouver la TVA de la facture, sinon l'écriture serait déséquilibrée."""
    assert _purchase_vat_parts(purchase(vat_amount=400.0)) == []


def test_sans_ventilation_le_comportement_est_inchange():
    assert _purchase_vat_parts(purchase(vat_breakdown_json=None)) == []
    assert _purchase_vat_parts(purchase(vat_breakdown_json='pas du json')) == []
    assert _purchase_vat_parts({'vat_amount': 303.33}) == []      # base antérieure à la migration
