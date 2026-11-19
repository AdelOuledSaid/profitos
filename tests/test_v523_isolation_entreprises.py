"""v523 — Isolation des données entre entreprises : l'attaque par identifiant direct.

Les contrôles existants portent sur le mécanisme multi-locataire (schéma par entreprise,
connexions séparées). Aucun ne tentait ce que tenterait un curieux : être connecté à son
entreprise et demander, à la main, l'adresse d'une ressource d'une autre entreprise.

Comme chaque entreprise numérote ses pièces à partir de 1, la facture n° 1 existe dans
les deux : la bonne question n'est donc pas « obtient-on une erreur ? » mais « obtient-on
les données du voisin ? ». Chaque contrôle ci-dessous plante donc un marqueur unique dans
l'entreprise B et vérifie qu'il n'apparaît jamais chez A — ni dans une page, ni dans un
PDF, ni dans un export, ni dans un document joint.
"""
import io
from datetime import date, timedelta

import pytest

from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db, _create_invoice, _invoice, FULL_PROFILE)

SECRET_B = 'CLIENT-CONFIDENTIEL-DE-B-7f3a9c'
SECRET_A = 'Client Lumière SA'


def _maison(app, secret_client=None):
    """Une entreprise avec une facture émise, un achat et une écriture manuelle."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id, **({'client_name': secret_client} if secret_client else {}),
                             label_1='Prestation', qty_1='1', price_1='1000', vat_1='20')
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    today = date.today()
    _post(client, '/facturation/achats/nouvelle', {
        'supplier_name': secret_client or 'Fournisseur', 'invoice_number': 'FB-001',
        'issue_date': today.isoformat(), 'due_date': (today + timedelta(days=30)).isoformat(),
        'subtotal': '100', 'vat_amount': '20'})
    _post(client, '/comptabilite/ecritures/nouvelle', {
        'journal': 'OD', 'entry_date': today.isoformat(), 'label': secret_client or 'Écriture',
        'compte_0': '606300', 'debit_0': '50', 'credit_0': '',
        'compte_1': '401000', 'debit_1': '', 'credit_1': '50'})
    return client, org_id, inv_id


def _ids(org_id, table):
    c = _db(org_id)
    rows = [r[0] for r in c.execute(f'SELECT id FROM {table}')]
    c.close()
    return rows


def _no_leak(reponse, secret=SECRET_B):
    """Une réponse ne doit jamais contenir le marqueur de l'autre entreprise."""
    corps = reponse.get_data()
    texte = corps.decode('utf-8', 'replace')
    assert secret not in texte, f'fuite de données : {secret} visible (HTTP {reponse.status_code})'
    return reponse


# ---------------------------------------------------------------------------
# Lecture directe par identifiant
# ---------------------------------------------------------------------------

def test_invoice_of_another_company_is_never_served(app):
    client_a, org_a, inv_a = _maison(app)
    client_b, org_b, inv_b = _maison(app, SECRET_B)
    for inv_id in set(_ids(org_b, 'outgoing_invoices') + [inv_b, inv_a, 1, 2]):
        _no_leak(client_a.get(f'/facturation/{inv_id}'))
    # contre-épreuve : A voit bien sa propre facture, le contrôle n'est pas vide
    page = client_a.get(f'/facturation/{inv_a}').get_data(as_text=True)
    assert SECRET_A in page, "A ne voit plus ses propres données : le test ne prouverait rien"


def test_invoice_pdf_of_another_company_is_never_served(app):
    client_a, org_a, inv_a = _maison(app)
    client_b, org_b, inv_b = _maison(app, SECRET_B)
    for inv_id in set(_ids(org_b, 'outgoing_invoices') + [1, 2]):
        r = client_a.get(f'/facturation/{inv_id}/pdf')
        if r.status_code == 200 and r.data[:4] == b'%PDF':
            from pypdf import PdfReader
            texte = '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(r.data)).pages)
            assert SECRET_B not in texte, 'le PDF d’une autre entreprise a été servi'
        else:
            _no_leak(r)


def test_purchase_of_another_company_is_never_served(app):
    client_a, org_a, _ = _maison(app)
    client_b, org_b, _ = _maison(app, SECRET_B)
    for pid in set(_ids(org_b, 'purchase_invoices') + [1, 2]):
        _no_leak(client_a.get(f'/facturation/achats/{pid}'))


def test_accounting_entry_of_another_company_is_never_served(app):
    client_a, org_a, _ = _maison(app)
    client_b, org_b, _ = _maison(app, SECRET_B)
    for eid in set(_ids(org_b, 'accounting_entries') + [1, 2, 3]):
        _no_leak(client_a.get(f'/comptabilite/ecritures/{eid}'))


# ---------------------------------------------------------------------------
# Listes, exports et recherche
# ---------------------------------------------------------------------------

# Chemins vérifiés comme existants : un contrôle de fuite qui porterait sur une page
# absente passerait à vide et ne prouverait rien. Le statut est donc exigé explicitement.
PAGES = ['/facturation', '/facturation/achats', '/comptabilite/journaux',
         '/comptabilite/grand-livre', '/comptabilite/balance', '/comptabilite/tva',
         '/', '/mission-control/banque']


@pytest.mark.parametrize('chemin', PAGES)
def test_lists_never_show_another_company_data(app, chemin):
    client_a, org_a, _ = _maison(app)
    client_b, org_b, _ = _maison(app, SECRET_B)
    r = client_a.get(chemin)
    assert r.status_code == 200, f'{chemin} introuvable ({r.status_code}) : contrôle sans valeur'
    _no_leak(r)


def test_fec_export_contains_only_own_entries(app):
    """L'export FEC part en clair chez le comptable : il ne doit pas mélanger deux
    entreprises."""
    client_a, org_a, _ = _maison(app)
    client_b, org_b, _ = _maison(app, SECRET_B)
    annee = date.today().year
    r = client_a.get(f'/comptabilite/export-fec?date_from={annee}-01-01&date_to={annee}-12-31')
    assert r.status_code in (200, 302), f'export FEC inaccessible ({r.status_code})'
    _no_leak(r)


def test_personal_data_export_contains_only_own_company(app):
    """L'export RGPD rassemble beaucoup de données d'un coup : la fuite y serait totale."""
    client_a, org_a, _ = _maison(app)
    client_b, org_b, _ = _maison(app, SECRET_B)
    _no_leak(client_a.get('/settings/gdpr-export'))


def test_purchase_export_contains_only_own_company(app):
    client_a, org_a, _ = _maison(app)
    client_b, org_b, _ = _maison(app, SECRET_B)
    _no_leak(client_a.get('/facturation/achats/export'))


# ---------------------------------------------------------------------------
# Écriture : une entreprise ne doit pas pouvoir modifier les pièces d'une autre
# ---------------------------------------------------------------------------

def test_another_company_invoice_cannot_be_paid_or_credited(app):
    client_a, org_a, _ = _maison(app)
    client_b, org_b, inv_b = _maison(app, SECRET_B)
    c = _db(org_b)
    avant = c.execute('SELECT status FROM outgoing_invoices WHERE id=?', (inv_b,)).fetchone()[0]
    c.close()

    _post(client_a, f'/facturation/{inv_b}/reglement',
          {'amount': '1200,00', 'payment_date': date.today().isoformat()})
    _post(client_a, f'/facturation/{inv_b}/avoir', {'reason': 'Tentative', 'amount_ttc': '1200,00'})

    c = _db(org_b)
    apres = c.execute('SELECT status FROM outgoing_invoices WHERE id=?', (inv_b,)).fetchone()[0]
    paiements = c.execute('SELECT COUNT(*) FROM outgoing_invoice_payments WHERE invoice_id=?',
                          (inv_b,)).fetchone()[0]
    c.close()
    assert apres == avant, "le statut d'une facture d'une autre entreprise a changé"
    assert paiements == 0, "un règlement a été enregistré chez une autre entreprise"


def test_another_company_entry_cannot_be_reversed(app):
    """La contrepassation d'une écriture d'autrui fausserait sa comptabilité."""
    client_a, org_a, _ = _maison(app)
    client_b, org_b, _ = _maison(app, SECRET_B)
    c = _db(org_b)
    avant = c.execute('SELECT COUNT(*) FROM accounting_entries').fetchone()[0]
    c.close()
    for eid in _ids(org_b, 'accounting_entries'):
        _post(client_a, f'/comptabilite/ecritures/{eid}/contrepasser', {})
    c = _db(org_b)
    apres = c.execute('SELECT COUNT(*) FROM accounting_entries').fetchone()[0]
    c.close()
    assert apres == avant, "une écriture d'une autre entreprise a été contrepassée"


# ---------------------------------------------------------------------------
# Déconnexion
# ---------------------------------------------------------------------------

def test_nothing_is_served_without_a_session(app):
    """Sans session, aucune de ces adresses ne doit rendre de données."""
    client_b, org_b, inv_b = _maison(app, SECRET_B)
    anonyme = app.test_client()
    for chemin in (f'/facturation/{inv_b}', f'/facturation/{inv_b}/pdf', '/facturation',
                   '/comptabilite/grand-livre', '/comptabilite/tva'):
        r = anonyme.get(chemin)
        assert r.status_code in (301, 302, 303, 401, 403, 404), f'{chemin} → {r.status_code}'
        _no_leak(r)
