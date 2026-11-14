"""v498 — Mode bac à sable : démontrer et tester le produit avant immatriculation.

Le mode découle uniquement de la configuration du raccordement à la plateforme agréée : il est
actif quand le numéro d'entreprise imposé relève du format « sandbox ». Les quatre mentions qui
supposent une entreprise immatriculée cessent alors de bloquer, les autres restent exigées, et
les factures émises portent un filigrane. Le point critique vérifié ici : dès qu'un véritable
SIREN est configuré, tous les contrôles redeviennent bloquants sans intervention.
"""
import json

import pytest

from profitos import superpdp as SP
from profitos.routes import invoicing as I


# Profil d'une entreprise non encore immatriculée : dénomination, adresse et forme juridique
# connues, mais ni SIRET, ni RCS, ni numéro de TVA.
UNREGISTERED = {'name': 'Veqalio', 'legal_form': 'SAS', 'share_capital': '1 000',
                'address': '12 rue de Paris', 'postal_code': '75011', 'city': 'Paris',
                'siret': '', 'vat_number': '', 'rcs_city': '', 'rm_number': '', 'country': 'FR'}

ITEMS = [{'designation': 'Prestation de test', 'quantity': 1, 'unit_price': 1000.0,
          'vat_rate': 20.0, 'line_total': 1000.0}]


def invoice(**extra):
    inv = {'status': 'sent', 'invoice_number': 'FA-2026-001', 'issue_date': '2026-11-13',
           'due_date': '2026-12-13', 'client_name': 'test', 'client_address': '12 rue Paris\n75001 Paris',
           'client_siren': '', 'client_country': 'FR', 'client_vat_number': '',
           'line_items': json.dumps(ITEMS), 'subtotal': 1000.0, 'vat_amount': 200.0, 'total': 1200.0,
           'operation_nature': 'services', 'notes': ''}
    inv.update(extra)
    return inv


@pytest.fixture()
def sandbox(monkeypatch):
    monkeypatch.setenv('SUPERPDP_COMPANY_NUMBER', '000000001')
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '1')
    monkeypatch.delenv('SUPERPDP_COMPANY_NUMBER_SCHEME', raising=False)


@pytest.fixture()
def production(monkeypatch):
    monkeypatch.setenv('SUPERPDP_COMPANY_NUMBER', '732829320')        # SIREN réellement valide
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '0')
    monkeypatch.delenv('SUPERPDP_COMPANY_NUMBER_SCHEME', raising=False)


# ------------------------------------------------------------------ détection du mode

def test_le_mode_suit_le_numero_configure(monkeypatch):
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '1')
    monkeypatch.delenv('SUPERPDP_COMPANY_NUMBER', raising=False)
    monkeypatch.delenv('SUPERPDP_COMPANY_NUMBER_SCHEME', raising=False)
    assert SP.is_sandbox() is False                  # aucun numéro imposé : production
    monkeypatch.setenv('SUPERPDP_COMPANY_NUMBER', '000000001')
    assert SP.is_sandbox() is True
    monkeypatch.setenv('SUPERPDP_COMPANY_NUMBER', '732829320')
    assert SP.is_sandbox() is False                  # vrai SIREN : le mode s'éteint seul


def test_l_ouverture_commerciale_eteint_le_mode_a_elle_seule(monkeypatch):
    """v498.2 — Le garde-fou qui compte : même avec le numéro de test resté configuré, la
    sortie du pré-lancement suffit à rétablir tous les contrôles pour toutes les
    organisations. Un oubli de configuration ne peut donc pas exposer de vrais clients."""
    monkeypatch.setenv('SUPERPDP_COMPANY_NUMBER', '000000001')
    monkeypatch.delenv('SUPERPDP_COMPANY_NUMBER_SCHEME', raising=False)
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '1')
    assert SP.is_sandbox() is True
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '0')
    assert SP.is_sandbox() is False
    _checks, missing = I._check_mandatory_mentions(invoice(), UNREGISTERED)
    assert "SIRET de l'émetteur (14 chiffres)" in missing          # redevenu bloquant
    assert b'000000001' not in I.generate_facturx_xml(invoice(), ITEMS, UNREGISTERED)


def test_la_verification_d_ouverture_signale_le_numero_de_test():
    """Le code se protège, mais la configuration doit être corrigée : go_live_check doit le
    dire, et comme point bloquant."""
    from scripts import go_live_check as G
    env = {'SUPERPDP_CLIENT_ID': 'x', 'SUPERPDP_CLIENT_SECRET': 'y',
           'APP_BASE_URL': 'https://veqalio.com', 'SUPERPDP_COMPANY_NUMBER': '000000001'}
    faults = [it for it in G.check_superpdp(env) if it['level'] == G.FAIL]
    assert any('bac à sable' in it['detail'] for it in faults)
    env['SUPERPDP_COMPANY_NUMBER'] = '732829320'
    assert [it for it in G.check_superpdp(env) if it['level'] == G.FAIL] == []


def test_le_mode_ne_depend_pas_de_la_facture(sandbox):
    """Aucun champ de facture ni de profil ne peut activer le mode : il vient du serveur."""
    assert I._sandbox_mentions() is True


# ------------------------------------------------------------------ mentions obligatoires

def test_sans_bac_a_sable_l_immatriculation_bloque(production):
    _checks, missing = I._check_mandatory_mentions(invoice(), UNREGISTERED)
    assert "SIRET de l'émetteur (14 chiffres)" in missing
    assert "Immatriculation (RCS + ville, ou répertoire des métiers)" in missing
    assert "N° TVA intracommunautaire de l'émetteur" in missing
    assert 'SIREN du client (9 chiffres)' in missing


def test_en_bac_a_sable_ces_quatre_mentions_ne_bloquent_plus(sandbox):
    checks, missing = I._check_mandatory_mentions(invoice(), UNREGISTERED)
    assert missing == []                                   # la facture peut être émise
    waived = {c['label'] for c in checks if c.get('waived')}
    assert len(waived) == 4
    assert all('non exigé en bac à sable' in label for label in waived)


def test_en_bac_a_sable_les_autres_mentions_restent_exigees(sandbox):
    """Ce qui ne demande pas d'immatriculation continue de bloquer : sinon le mode servirait
    à émettre n'importe quoi."""
    _checks, missing = I._check_mandatory_mentions(
        invoice(client_name='', line_items='[]'), {**UNREGISTERED, 'name': '', 'address': ''})
    assert "Nom (dénomination) de l'émetteur" in missing
    assert "Adresse de l'émetteur" in missing
    assert 'Nom du client' in missing
    assert 'Au moins une ligne avec désignation, quantité, prix unitaire et taux de TVA' in missing


def test_un_profil_complet_ne_porte_aucune_tolerance(sandbox):
    """Avec un SIRET et un SIREN client valides, rien n'est « levé » : les mentions sont
    satisfaites pour de vrai, le mode ne masque donc aucune anomalie."""
    complete = {**UNREGISTERED, 'siret': '73282932000074', 'vat_number': 'FR44732829320',
                'rcs_city': 'Paris'}
    checks, missing = I._check_mandatory_mentions(invoice(client_siren='732829320'), complete)
    assert missing == []
    assert [c for c in checks if c.get('waived')] == []


# ------------------------------------------------------------------ marquage des documents

def test_filigrane_des_factures_de_test(sandbox):
    assert I._invoice_watermark(invoice()) == I.SANDBOX_WATERMARK
    assert 'sans valeur de facture' in I.SANDBOX_WATERMARK


def test_le_filigrane_brouillon_ou_annulee_reste_prioritaire(sandbox):
    assert I._invoice_watermark(invoice(status='draft')) == I.DRAFT_WATERMARK
    assert I._invoice_watermark(invoice(status='cancelled')) == I.CANCELLED_WATERMARK


def test_hors_bac_a_sable_aucun_filigrane(production):
    assert I._invoice_watermark(invoice()) is None


# ------------------------------------------------------------------ Factur-X

def test_le_vendeur_porte_l_identifiant_du_bac_a_sable(sandbox):
    """EN16931 exige un identifiant légal du vendeur (BT-30). Faute de SIRET, le XML reprend
    le numéro de l'entreprise de test : l'identité du document correspond à celle du dépôt."""
    xml = I.generate_facturx_xml(invoice(), ITEMS, UNREGISTERED)
    assert b'<ram:ID schemeID="0002">000000001</ram:ID>' in xml
    assert b'<ram:URIID schemeID="0225">000000001</ram:URIID>' in xml


def test_hors_bac_a_sable_aucun_identifiant_n_est_invente(production):
    xml = I.generate_facturx_xml(invoice(), ITEMS, UNREGISTERED)
    assert b'SpecifiedLegalOrganization' not in xml
    assert b'732829320' not in xml


def test_un_vrai_siret_prime_sur_le_bac_a_sable(sandbox):
    complete = {**UNREGISTERED, 'siret': '73282932000074', 'vat_number': 'FR44732829320',
                'rcs_city': 'Paris'}
    xml = I.generate_facturx_xml(invoice(client_siren='732829320'), ITEMS, complete)
    assert b'<ram:ID schemeID="0002">732829320</ram:ID>' in xml
    assert b'000000001' not in xml
