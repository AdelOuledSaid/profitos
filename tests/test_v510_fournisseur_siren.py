"""v510 — Les factures d'un même fournisseur se rejoignent par son numéro d'entreprise.

Le rapprochement se faisait sur le nom seul. « Burger Queen », « BURGER QUEEN SAS » et
« Burger Queen SARL » devenaient trois fournisseurs pour une même entreprise — faussant les
totaux par fournisseur, les relances et le lettrage. Constaté en séance : deux fournisseurs à
l'écran pour une seule entreprise réelle.
"""
from profitos import einvoice_superpdp_inbound as IN

CII = b"""<?xml version="1.0" encoding="UTF-8"?>
<CrossIndustryInvoice xmlns="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100">
  <ExchangedDocument><ID>F-1</ID><TypeCode>380</TypeCode>
    <IssueDateTime><DateTimeString>20261014</DateTimeString></IssueDateTime></ExchangedDocument>
  <SupplyChainTradeTransaction><ApplicableHeaderTradeAgreement>
    <SellerTradeParty><Name>Burger Queen</Name>
      <SpecifiedLegalOrganization><ID schemeID="0002">732829320</ID></SpecifiedLegalOrganization>
    </SellerTradeParty>
  </ApplicableHeaderTradeAgreement>
  <ApplicableHeaderTradeSettlement><SpecifiedTradeSettlementHeaderMonetarySummation>
    <TaxBasisTotalAmount>100.00</TaxBasisTotalAmount><TaxTotalAmount>20.00</TaxTotalAmount>
    <GrandTotalAmount>120.00</GrandTotalAmount>
  </SpecifiedTradeSettlementHeaderMonetarySummation></ApplicableHeaderTradeSettlement>
  </SupplyChainTradeTransaction></CrossIndustryInvoice>"""

UBL = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>F-2</cbc:ID><cbc:IssueDate>2026-10-15</cbc:IssueDate><cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyLegalEntity><cbc:RegistrationName>BURGER QUEEN SAS</cbc:RegistrationName>
      <cbc:CompanyID schemeID="0002">732829320</cbc:CompanyID></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:TaxTotal><cbc:TaxAmount>20.00</cbc:TaxAmount></cac:TaxTotal>
  <cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount>100.00</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount>120.00</cbc:TaxInclusiveAmount></cac:LegalMonetaryTotal>
</Invoice>"""


def test_le_siren_est_lu_dans_les_deux_grammaires():
    assert IN.from_xml(CII)['supplier_siren'] == '732829320'
    assert IN.from_xml(UBL)['supplier_siren'] == '732829320'


def test_les_formes_d_identifiant_sont_ramenees_au_siren():
    """Préfixe de schéma, espaces, ou SIRET complet : seul le SIREN est commun à tous les
    documents d'un même émetteur."""
    assert IN._siren('0002:732829320') == '732829320'
    assert IN._siren('732 829 320') == '732829320'
    assert IN._siren('73282932000074') == '732829320'      # SIRET → son SIREN
    assert IN._siren('12345') is None
    assert IN._siren(None) is None


def test_le_resume_de_la_plateforme_peut_aussi_le_porter():
    en = {'number': 'F-3', 'issue_date': '2026-10-14', 'seller': {'name': 'Burger Queen', 'company_number': '732829320'},
          'totals': {'total_without_vat': 100.0, 'total_vat_amount': 20.0, 'total_with_vat': 120.0}}
    assert IN.from_en_invoice(en)['supplier_siren'] == '732829320'


class FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """Base minimale : renvoie le nom déjà enregistré pour un SIREN connu."""

    def __init__(self, known=None, broken=False):
        self.known, self.broken = known, broken

    def execute(self, sql, params=()):
        if self.broken:
            raise RuntimeError('colonne supplier_siren absente')
        siren = params[1]
        return FakeCursor({'supplier_name': self.known} if self.known and siren == '732829320' else None)


def test_un_fournisseur_connu_garde_son_nom():
    c = FakeConn(known='Burger Queen')
    assert IN._known_supplier_name(c, None, '732829320', 'BURGER QUEEN SAS') == 'Burger Queen'


def test_un_fournisseur_inconnu_prend_le_nom_du_document():
    assert IN._known_supplier_name(FakeConn(), None, '732829320', 'Burger Queen') == 'Burger Queen'


def test_sans_siren_le_nom_du_document_s_applique():
    """Rien à rapprocher : on n'invente pas de correspondance sur un nom approchant."""
    assert IN._known_supplier_name(FakeConn(known='Autre'), None, None, 'Burger Queen') == 'Burger Queen'


def test_une_base_sans_la_colonne_ne_fait_pas_echouer_l_import():
    """Avant migration, l'import doit continuer de fonctionner comme avant."""
    assert IN._known_supplier_name(FakeConn(broken=True), None, '732829320', 'Burger Queen') == 'Burger Queen'
