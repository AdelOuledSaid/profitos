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


def test_l_identite_de_l_emetteur_transporte_l_adresse(tmp_path, monkeypatch):
    """v509.1 — L'identité de l'émetteur est reconstruite champ par champ avant de fabriquer
    le document. Un champ absent de cette liste est silencieusement perdu : l'adresse était
    enregistrée, puis remplacée par le SIREN au moment de générer le XML."""
    import profitos.runtime as rt
    from profitos import create_app, storage
    from profitos.config import DevelopmentConfig
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    import uuid as _uuid
    cl = app.test_client()
    with cl.session_transaction() as s:
        s['csrf_token'] = 'c' * 64
    cl.post('/signup', data={'csrf_token': 'c' * 64, 'accept_terms': '1', 'first_name': 'A',
                             'last_name': 'M', 'email': f'{_uuid.uuid4().hex[:10]}@example.com',
                             'password': 'Brume-Cactus-Velo-48', 'company_name': 'Tricatel'})
    with cl.session_transaction() as s:
        org = s['org_id']
        s['csrf_token'] = 'c' * 64                 # la session est régénérée à l'inscription
    # par le formulaire : c'est toute la chaîne saisie → base → identité qui est éprouvée
    cl.post('/company', data={'csrf_token': 'c' * 64, 'name': 'Tricatel', 'legal_form': 'SAS',
                              'siret': '73282932000074', 'address': 'Avenue de la République',
                              'postal_code': '37170', 'city': 'Chambray-lès-Tours',
                              'share_capital': '1000', 'rcs_city': 'Tours',
                              'payment_terms': '30 jours', 'late_penalty_rate': '10',
                              'electronic_address': '0225:315143296_121167'})
    import profitos.db as dbmod
    c = dbmod.connect_tenant(org, rt.tenant_db(org))
    stored = c.execute('SELECT electronic_address FROM company WHERE id=1').fetchone()
    ident = I._seller_identity(c, None)
    c.close()
    assert stored['electronic_address'] == '0225:315143296_121167'     # bien enregistré…
    assert ident['electronic_address'] == '0225:315143296_121167'      # …et bien transporté
