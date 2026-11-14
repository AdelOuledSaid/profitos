"""v504 — Identifiant fiscal du vendeur à défaut de numéro de TVA (BT-32).

Rejet constaté au premier dépôt accepté par la plateforme :

    [BR-S-02] An Invoice that contains an Invoice line (BG-25) where the Invoiced item VAT
    category code (BT-151) is "Standard rated" shall contain the Seller VAT Identifier
    (BT-31), the Seller tax registration identifier (BT-32) and/or the Seller tax
    representative VAT identifier (BT-63).

Veqalio n'émettait BT-31 que si le profil portait un numéro de TVA, et rien à défaut. Ce
n'est pas propre au bac à sable : une entreprise en franchise de TVA n'a pas de numéro
intracommunautaire, et ne pouvait donc émettre aucune facture électronique acceptée — soit
l'essentiel des micro-entrepreneurs.
"""
import json

import pytest

from profitos.routes import invoicing as I

ITEMS = [{'designation': 'Prestation', 'quantity': 1, 'unit_price': 1000.0,
          'vat_rate': 20.0, 'line_total': 1000.0}]

SELLER = {'name': 'Atelier Martin', 'legal_form': 'SAS', 'share_capital': '1 000',
          'address': '1 rue Test', 'postal_code': '75001', 'city': 'Paris',
          'siret': '73282932000074', 'rcs_city': 'Paris', 'rm_number': '', 'country': 'FR',
          'vat_number': ''}


def invoice(**extra):
    inv = {'status': 'sent', 'invoice_number': 'FA-2026-007', 'issue_date': '2026-11-14',
           'due_date': '2026-12-12', 'client_name': 'Burger Queen',
           'client_address': "809 avenue du Languedoc\n12100 Millau", 'client_siren': '732829320',
           'client_country': 'FR', 'client_vat_number': '', 'client_electronic_address': '',
           'line_items': json.dumps(ITEMS), 'subtotal': 1000.0, 'vat_amount': 200.0,
           'total': 1200.0, 'operation_nature': 'services', 'notes': ''}
    inv.update(extra)
    return inv


@pytest.fixture(autouse=True)
def production(monkeypatch):
    """Hors bac à sable : le défaut corrigé ici n'en dépend pas."""
    monkeypatch.delenv('SUPERPDP_COMPANY_NUMBER', raising=False)
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '0')


def tax_ids(xml):
    import re
    seller = xml.split(b'BuyerTradeParty>', 1)[0]
    block = re.findall(rb'<ram:SpecifiedTaxRegistration>\s*<ram:ID schemeID="([^"]+)">([^<]*)<', seller)
    return [(s.decode(), v.decode()) for s, v in block]


def test_sans_numero_de_tva_le_siren_sert_d_identifiant_fiscal():
    """Franchise de TVA : pas de BT-31, donc BT-32 sous le schéma FC."""
    assert tax_ids(I.generate_facturx_xml(invoice(), ITEMS, SELLER)) == [('FC', '732829320')]


def test_le_numero_de_tva_reste_prioritaire():
    """BT-31 est l'identifiant attendu quand il existe : BT-32 ne s'y substitue pas."""
    seller = {**SELLER, 'vat_number': 'FR44732829320'}
    assert tax_ids(I.generate_facturx_xml(invoice(), ITEMS, seller)) == [('VA', 'FR44732829320')]


def test_sans_siret_ni_tva_aucun_identifiant_n_est_invente():
    """Rien à déclarer : on n'invente pas un identifiant fiscal. L'émission est de toute façon
    bloquée en amont faute de SIRET."""
    seller = {**SELLER, 'siret': '', 'vat_number': ''}
    assert tax_ids(I.generate_facturx_xml(invoice(), ITEMS, seller)) == []


def test_en_bac_a_sable_l_identifiant_suit_l_entreprise_de_test(monkeypatch):
    monkeypatch.setenv('SUPERPDP_COMPANY_NUMBER', '000000001')
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '1')
    seller = {**SELLER, 'siret': '', 'vat_number': ''}
    assert tax_ids(I.generate_facturx_xml(invoice(), ITEMS, seller)) == [('FC', '000000001')]
