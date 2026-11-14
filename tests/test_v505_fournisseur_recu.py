"""v505 — Le fournisseur d'une facture reçue porte son nom.

Première réception réelle : la facture est bien créée, avec ses montants et ses dates, mais
sous le libellé « Fournisseur électronique » au lieu de « Burger Queen ». Le résumé normalisé
que fournit la plateforme (``en_invoice``) porte les montants sans toujours nommer le vendeur,
et la lecture s'arrêtait à cette première source. Toutes les factures reçues auraient porté le
même libellé.
"""
from profitos import einvoice_superpdp_inbound as IN

CII = b"""<?xml version="1.0" encoding="UTF-8"?>
<CrossIndustryInvoice xmlns="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100">
  <ExchangedDocument><ID>F-2026-001</ID><TypeCode>380</TypeCode>
    <IssueDateTime><DateTimeString>20261014</DateTimeString></IssueDateTime></ExchangedDocument>
  <SupplyChainTradeTransaction>
    <ApplicableHeaderTradeAgreement>
      <SellerTradeParty><Name>Burger Queen</Name></SellerTradeParty>
    </ApplicableHeaderTradeAgreement>
    <ApplicableHeaderTradeSettlement>
      <SpecifiedTradeSettlementHeaderMonetarySummation>
        <TaxBasisTotalAmount>1560.46</TaxBasisTotalAmount>
        <TaxTotalAmount>303.33</TaxTotalAmount>
        <GrandTotalAmount>1863.79</GrandTotalAmount>
      </SpecifiedTradeSettlementHeaderMonetarySummation>
    </ApplicableHeaderTradeSettlement>
  </SupplyChainTradeTransaction>
</CrossIndustryInvoice>"""

# Résumé tel que la plateforme le renvoie : des montants, mais pas de nom de vendeur.
RESUME_SANS_VENDEUR = {'en_invoice': {
    'number': 'F-2026-001', 'type_code': '380', 'issue_date': '2026-10-14',
    'payment_due_date': '2026-11-14', 'seller': {},
    'totals': {'total_without_vat': 1560.46, 'total_vat_amount': 303.33,
               'total_with_vat': 1863.79}}}


def test_le_nom_manquant_est_pris_dans_le_document():
    data = IN.read_invoice(RESUME_SANS_VENDEUR, CII, 'application/xml')
    assert data['supplier'] == 'Burger Queen'
    assert data['total'] == 1863.79          # les montants restent ceux du résumé


def test_le_resume_garde_la_priorite_sur_ce_qu_il_renseigne():
    """Le résumé est normalisé par la plateforme : là où il répond, il fait foi."""
    item = {'en_invoice': {**RESUME_SANS_VENDEUR['en_invoice'], 'seller': {'name': 'Burger Queen SAS'}}}
    data = IN.read_invoice(item, CII, 'application/xml')
    assert data['supplier'] == 'Burger Queen SAS'


def test_sans_resume_le_document_suffit():
    data = IN.read_invoice({}, CII, 'application/xml')
    assert data['supplier'] == 'Burger Queen'
    assert data['number'] == 'F-2026-001'


def test_sans_document_le_resume_suffit():
    data = IN.read_invoice(RESUME_SANS_VENDEUR, b'', None)
    assert data['total'] == 1863.79
    assert data['supplier'] is None          # le libellé de repli est posé plus loin


def test_une_facture_illisible_reste_illisible():
    assert IN.read_invoice({}, b'ceci n est pas une facture', 'text/plain') is None
    assert IN.read_invoice({}, b'', None) is None
