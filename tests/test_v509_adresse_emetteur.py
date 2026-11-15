"""v509 — L'identifiant d'annuaire de l'émetteur est saisissable.

Refus constaté quand le destinataire tente de renvoyer un statut : « L'adresse électronique
(MDT-73) est invalide ». Le document déclarait comme adresse de l'émetteur (BT-34) son SIREN,
alors que l'adresse réelle de l'entreprise à l'annuaire porte un suffixe de service. Tant que
personne ne s'en servait, le dépôt passait ; mais pour répondre, la plateforme s'y adresse —
et elle n'existait pas. Aucun statut du cycle de vie ne pouvait donc revenir.
"""
import json

import pytest

from profitos.company_profile import parse_company_form
from profitos.routes import invoicing as I

ITEMS = [{'designation': 'Prestation', 'quantity': 1, 'unit_price': 1000.0,
          'vat_rate': 20.0, 'line_total': 1000.0}]

SELLER = {'name': 'Tricatel', 'legal_form': 'SAS', 'share_capital': '1 000',
          'address': 'Avenue de la République', 'postal_code': '37170', 'city': 'Chambray-lès-Tours',
          'siret': '73282932000074', 'vat_number': '', 'rcs_city': 'Tours', 'rm_number': '',
          'country': 'FR', 'electronic_address': ''}


def invoice(**extra):
    inv = {'status': 'sent', 'invoice_number': 'FA-2026-009', 'issue_date': '2026-11-15',
           'due_date': '2026-12-15', 'client_name': 'Burger Queen',
           'client_address': "809 avenue du Languedoc\n12100 Millau", 'client_siren': '732829320',
           'client_country': 'FR', 'client_vat_number': '', 'client_electronic_address': '',
           'line_items': json.dumps(ITEMS), 'subtotal': 1000.0, 'vat_amount': 200.0,
           'total': 1200.0, 'operation_nature': 'services', 'notes': ''}
    inv.update(extra)
    return inv


@pytest.fixture(autouse=True)
def production(monkeypatch):
    monkeypatch.delenv('SUPERPDP_COMPANY_NUMBER', raising=False)
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '0')


def seller_uri(xml):
    import re
    seller = xml.split(b'BuyerTradeParty>', 1)[0]
    m = re.search(rb'<ram:URIID schemeID="([^"]+)">([^<]*)</ram:URIID>', seller)
    return (m.group(1).decode(), m.group(2).decode()) if m else None


def test_l_adresse_saisie_prime_sur_le_siren():
    seller = {**SELLER, 'electronic_address': '0225:315143296_121167'}
    assert seller_uri(I.generate_facturx_xml(invoice(), ITEMS, seller)) == ('0225', '315143296_121167')


def test_sans_adresse_saisie_le_siren_sert_comme_avant():
    assert seller_uri(I.generate_facturx_xml(invoice(), ITEMS, SELLER)) == ('0225', '732829320')


def test_le_schema_prefixe_est_repris_tel_quel():
    seller = {**SELLER, 'electronic_address': '0088:5790000435975'}
    assert seller_uri(I.generate_facturx_xml(invoice(), ITEMS, seller)) == ('0088', '5790000435975')


def test_sans_prefixe_la_convention_francaise_s_applique():
    seller = {**SELLER, 'electronic_address': '315143296_121167'}
    assert seller_uri(I.generate_facturx_xml(invoice(), ITEMS, seller)) == ('0225', '315143296_121167')


def test_la_saisie_est_assainie():
    v, _errors = parse_company_form({'name': 'Tricatel',
                                     'electronic_address': ' 0225:315<script>_121167 '})
    assert v['electronic_address'] == '0225:315script_121167'


def test_le_champ_reste_facultatif():
    v, errors = parse_company_form({'name': 'Tricatel'})
    assert v['electronic_address'] == ''
    assert not [e for e in errors if 'annuaire' in e.lower()]
