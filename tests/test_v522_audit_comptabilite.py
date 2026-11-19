"""v522 — Audit de la comptabilité : les invariants qu'un expert-comptable vérifierait.

Complément de l'audit TVA (v521). On ne relit pas le code ligne à ligne : on fait vivre
un exercice complet — ventes, achats, règlements, avoir — puis on contrôle ce qui ne peut
jamais être faux dans une comptabilité en partie double :

  * toute écriture est équilibrée au centime, et le grand livre l'est globalement ;
  * ce que la balance affiche est ce que les écritures contiennent ;
  * un compte de tiers soldé l'est réellement (le client qui a tout payé ne doit rien) ;
  * une écriture manuelle déséquilibrée est refusée, pas rattrapée en silence ;
  * la TVA déductible d'un achat correspond à la facture reçue ;
  * une facture payée puis avoirée ne laisse ni produit ni TVA fantôme.
"""
from datetime import date, timedelta

import pytest

from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db, _create_invoice, _invoice, FULL_PROFILE)


# ---------------------------------------------------------------------------
# Lecture du grand livre
# ---------------------------------------------------------------------------

def _entries(org_id):
    c = _db(org_id)
    rows = c.execute("""SELECT e.id, e.journal_code, e.entry_date, e.source_type,
                               ROUND(COALESCE(SUM(l.debit),0),2) d,
                               ROUND(COALESCE(SUM(l.credit),0),2) cr
                        FROM accounting_entries e
                        LEFT JOIN accounting_entry_lines l ON l.entry_id=e.id
                        GROUP BY e.id""").fetchall()
    c.close()
    return rows


def _ledger_totals(org_id):
    c = _db(org_id)
    row = c.execute('SELECT ROUND(COALESCE(SUM(debit),0),2) d, ROUND(COALESCE(SUM(credit),0),2) cr '
                    'FROM accounting_entry_lines').fetchone()
    c.close()
    return float(row['d']), float(row['cr'])


def _account(org_id, prefix):
    c = _db(org_id)
    n = c.execute('SELECT ROUND(COALESCE(SUM(debit-credit),0),2) FROM accounting_entry_lines '
                  'WHERE account_code LIKE ?', (prefix + '%',)).fetchone()[0]
    c.close()
    return float(n or 0)


def _emit(client, org_id, **extra):
    inv_id = _create_invoice(client, org_id, **extra)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    return inv_id


def _purchase(client, subtotal, vat, number='FB-2026-01'):
    today = date.today()
    return _post(client, '/facturation/achats/nouvelle', {
        'supplier_name': 'Fournisseur Bureau', 'invoice_number': number,
        'issue_date': today.isoformat(), 'due_date': (today + timedelta(days=30)).isoformat(),
        'subtotal': subtotal, 'vat_amount': vat})


def _full_exercise(client, org_id):
    """Un petit exercice complet, sur lequel porteront les invariants."""
    _post(client, '/company', FULL_PROFILE)
    vente = _emit(client, org_id, label_1='Conseil', qty_1='1', price_1='1000', vat_1='20',
                  label_2='Fourniture', qty_2='2', price_2='49.99', vat_2='5.5')
    inv = _invoice(org_id, vente)
    _post(client, f'/facturation/{vente}/reglement',
          {'amount': f"{float(inv['total']):.2f}".replace('.', ','),
           'payment_date': date.today().isoformat()})
    _purchase(client, '100', '20')
    autre = _emit(client, org_id, label_1='Mission courte', qty_1='1', price_1='300', vat_1='20')
    return vente, autre


# ---------------------------------------------------------------------------
# Invariants de la partie double
# ---------------------------------------------------------------------------

def test_every_entry_balances_and_the_ledger_balances(app):
    client, org_id = _signup(app)
    _full_exercise(client, org_id)
    boiteuses = [dict(r) for r in _entries(org_id) if round(r['d'] - r['cr'], 2) != 0]
    assert not boiteuses, f'écriture(s) déséquilibrée(s) : {boiteuses}'
    debits, credits = _ledger_totals(org_id)
    assert round(debits - credits, 2) == 0, 'le grand livre ne s’équilibre pas'
    assert debits > 0, "l'exercice doit avoir produit des écritures"


def test_no_entry_is_left_without_lines(app):
    """Une écriture sans ligne est invisible en balance mais fausse les contrôles."""
    client, org_id = _signup(app)
    _full_exercise(client, org_id)
    vides = [dict(r) for r in _entries(org_id) if r['d'] == 0 and r['cr'] == 0]
    assert not vides, f'écriture(s) sans ligne : {vides}'


def test_every_entry_has_a_journal_and_a_date(app):
    """Sans code journal ni date, une écriture n'est pas présentable à un contrôle."""
    client, org_id = _signup(app)
    _full_exercise(client, org_id)
    for r in _entries(org_id):
        assert r['journal_code'], f"écriture {r['id']} sans journal"
        assert r['entry_date'] and len(str(r['entry_date'])) >= 10, f"écriture {r['id']} sans date"


def test_fully_paid_customer_account_is_settled(app):
    """Le client qui a tout réglé ne doit plus rien : 411 soldé pour cette facture."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Mission', qty_1='1', price_1='1000', vat_1='20')
    inv = _invoice(org_id, inv_id)
    _post(client, f'/facturation/{inv_id}/reglement',
          {'amount': f"{float(inv['total']):.2f}".replace('.', ','),
           'payment_date': date.today().isoformat()})
    c = _db(org_id)
    solde = c.execute("""SELECT ROUND(COALESCE(SUM(l.debit-l.credit),0),2) FROM accounting_entry_lines l
                         JOIN accounting_entries e ON e.id=l.entry_id
                         WHERE l.account_code='411000'""").fetchone()[0]
    c.close()
    assert float(solde or 0) == 0, 'le compte client devrait être soldé'


def test_purchase_vat_matches_the_supplier_invoice(app):
    """La TVA déductible comptabilisée est celle de la facture reçue, pas un recalcul."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    _purchase(client, '87.10', '17.42')
    assert _account(org_id, '445660') == 17.42
    assert round(_account(org_id, '401') * -1, 2) == round(87.10 + 17.42, 2)


# ---------------------------------------------------------------------------
# Garde-fous
# ---------------------------------------------------------------------------

def _manual_entry(client, label, debit, credit):
    """Saisie manuelle par le formulaire réel (champs compte_i / debit_i / credit_i)."""
    return _post(client, '/comptabilite/ecritures/nouvelle', {
        'journal': 'OD', 'entry_date': date.today().isoformat(), 'label': label,
        'compte_0': '606300', 'debit_0': debit, 'credit_0': '',
        'compte_1': '401000', 'debit_1': '', 'credit_1': credit})


def test_the_manual_entry_form_actually_records_a_balanced_entry(app):
    """Contrôle de sensibilité : sans lui, le test suivant passerait même si le
    formulaire n'enregistrait jamais rien — un piège de nommage de champs."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    avant = _ledger_totals(app and org_id)
    _manual_entry(client, 'Saisie équilibrée', '100', '100')
    apres = _ledger_totals(org_id)
    assert apres[0] == avant[0] + 100, "le formulaire de saisie n'a rien enregistré"
    assert apres[1] == avant[1] + 100


def test_manual_unbalanced_entry_is_refused(app):
    """Une saisie déséquilibrée doit être refusée, jamais corrigée en silence."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    avant = _ledger_totals(org_id)
    r = _manual_entry(client, 'Saisie bancale', '100', '90')
    assert _ledger_totals(org_id) == avant, "une écriture déséquilibrée a été enregistrée"
    # La page est re-rendue directement (200) : le message y est donc consommé, et c'est
    # dans le HTML qu'il faut le chercher, non dans la session.
    assert r.status_code == 200
    page = r.get_data(as_text=True).lower()
    assert 'non enregistrée' in page or 'équilibr' in page, \
        "l'utilisateur doit être averti du refus, pas laissé croire que c'est passé"


def test_paid_then_credited_invoice_leaves_no_phantom_revenue(app):
    """Facture payée puis annulée par avoir : ni produit ni TVA ne doivent subsister."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Mission', qty_1='1', price_1='1000', vat_1='20')
    inv = _invoice(org_id, inv_id)
    ttc = round(float(inv['total']), 2)
    _post(client, f'/facturation/{inv_id}/reglement',
          {'amount': f'{ttc:.2f}'.replace('.', ','), 'payment_date': date.today().isoformat()})
    _post(client, f'/facturation/{inv_id}/avoir',
          {'reason': 'Annulation', 'amount_ttc': f'{ttc:.2f}'.replace('.', ',')})
    boiteuses = [dict(r) for r in _entries(org_id) if round(r['d'] - r['cr'], 2) != 0]
    assert not boiteuses, f'écriture(s) déséquilibrée(s) : {boiteuses}'
    assert round(_account(org_id, '706') * -1, 2) == 0, 'produit résiduel après avoir total'
    assert round(_account(org_id, '4457') * -1, 2) == 0, 'TVA résiduelle après avoir total'


def test_entries_are_idempotent_when_the_same_payment_is_posted_twice(app):
    """Un double envoi du formulaire ne doit pas comptabiliser deux fois le règlement."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Mission', qty_1='1', price_1='100', vat_1='20')
    data = {'amount': '120,00', 'payment_date': date.today().isoformat()}
    _post(client, f'/facturation/{inv_id}/reglement', data)
    banque_apres_un = _account(org_id, '512')
    _post(client, f'/facturation/{inv_id}/reglement', data)
    # un second règlement identique est soit refusé, soit un vrai second encaissement :
    # dans les deux cas la banque ne doit jamais dépasser le total de la facture.
    assert _account(org_id, '512') <= 120.0 + 0.005, 'encaissement comptabilisé en double'
    assert banque_apres_un == 120.0


# ---------------------------------------------------------------------------
# Amortissements
# ---------------------------------------------------------------------------

class _FakeConn:
    """Aucune dotation déjà comptabilisée : on éprouve la formule, pas la base."""
    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return {'t': 0.0}


def _asset(purchase_date, amount=10000.0, years=5):
    return {'id': 1, 'purchase_date': purchase_date, 'purchase_amount': amount,
            'useful_life_years': years, 'keys': lambda: ('method',), 'method': 'linear'}


def test_first_year_depreciation_never_exceeds_a_full_annuity(app):
    """Défaut relevé en v522 : le prorata divisait par 365 même les années bissextiles.
    Un bien acquis le 1er janvier 2024 recevait 2 005,48 € au lieu de 2 000 €."""
    from profitos.accounting import compute_depreciation_for_year
    asset = dict(_asset('2024-01-01'))
    asset_row = type('R', (), {'__getitem__': lambda s, k: asset[k], 'keys': lambda s: asset.keys()})()
    dotation = compute_depreciation_for_year(_FakeConn(), asset_row, 2024)
    assert dotation == 2000.0, 'la première annuité ne peut pas dépasser une annuité pleine'


def test_leap_year_prorata_uses_366_days(app):
    """Acquis le 1er mars 2024 : 306 jours sur 366, pas sur 365."""
    from profitos.accounting import compute_depreciation_for_year
    asset = dict(_asset('2024-03-01'))
    asset_row = type('R', (), {'__getitem__': lambda s, k: asset[k], 'keys': lambda s: asset.keys()})()
    assert compute_depreciation_for_year(_FakeConn(), asset_row, 2024) == 1672.13


def test_common_year_prorata_is_unchanged(app):
    """Contre-épreuve : sur une année ordinaire, le calcul ne bouge pas."""
    from profitos.accounting import compute_depreciation_for_year
    asset = dict(_asset('2025-03-01'))
    asset_row = type('R', (), {'__getitem__': lambda s, k: asset[k], 'keys': lambda s: asset.keys()})()
    assert compute_depreciation_for_year(_FakeConn(), asset_row, 2025) == round(2000 * 306 / 365, 2)


# ---------------------------------------------------------------------------
# Clôture d'exercice : une erreur ici fausse tout l'exercice suivant
# ---------------------------------------------------------------------------

def _class_balance(org_id, classes, journal=None, upto=None):
    """Solde net (débit - crédit) des comptes d'une ou plusieurs classes."""
    c = _db(org_id)
    sql = ("SELECT ROUND(COALESCE(SUM(l.debit-l.credit),0),2) FROM accounting_entry_lines l "
           "JOIN accounting_entries e ON e.id=l.entry_id "
           "WHERE SUBSTR(l.account_code,1,1) IN (%s)" % ','.join('?' * len(classes)))
    params = list(classes)
    if journal:
        sql += ' AND e.journal_code=?'
        params.append(journal)
    if upto:
        sql += ' AND e.entry_date<=?'
        params.append(upto)
    n = c.execute(sql, params).fetchone()[0]
    c.close()
    return float(n or 0)


def _close(client, start, end):
    return _post(client, '/comptabilite/cloture/a-nouveaux',
                 {'exercice_start': start, 'exercice_end': end})


def test_opening_entries_are_balanced_and_carry_only_balance_sheet_accounts(app):
    """Les à-nouveaux reprennent le bilan (classes 1 à 5) et le résultat, jamais les
    charges ni les produits : les reprendre doublerait le résultat de l'exercice suivant."""
    client, org_id = _signup(app)
    _full_exercise(client, org_id)
    today = date.today()
    _close(client, today.replace(month=1, day=1).isoformat(), today.isoformat())

    an = [dict(r) for r in _entries(org_id) if r['journal_code'] == 'AN']
    assert an, "aucun à-nouveau n'a été généré"
    for r in an:
        assert round(r['d'] - r['cr'], 2) == 0, f'à-nouveau déséquilibré : {r}'
        assert r['d'] > 0, 'à-nouveau sans aucune ligne'
    # aucune charge ni aucun produit repris en AN : on compte les lignes, car une somme
    # nulle ne prouverait rien (deux lignes opposées se compenseraient).
    c = _db(org_id)
    nb = c.execute("""SELECT COUNT(*) FROM accounting_entry_lines l
                      JOIN accounting_entries e ON e.id=l.entry_id
                      WHERE e.journal_code='AN' AND SUBSTR(l.account_code,1,1) IN ('6','7')""").fetchone()[0]
    c.close()
    assert nb == 0, 'charges ou produits repris dans les à-nouveaux'


def _balances_by_account(org_id, upto=None, journal=None, exclude_journal=None):
    c = _db(org_id)
    sql = ("SELECT l.account_code, ROUND(COALESCE(SUM(l.debit-l.credit),0),2) n "
           "FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id WHERE 1=1")
    params = []
    if upto:
        sql += ' AND e.entry_date<=?'
        params.append(upto)
    if journal:
        sql += ' AND e.journal_code=?'
        params.append(journal)
    if exclude_journal:
        sql += ' AND e.journal_code<>?'
        params.append(exclude_journal)
    sql += ' GROUP BY l.account_code'
    out = {r['account_code']: float(r['n'] or 0) for r in c.execute(sql, params)}
    c.close()
    return {k: v for k, v in out.items() if round(v, 2)}


def test_opening_entries_preserve_each_balance_sheet_account(app):
    """Continuité du bilan, compte par compte : le solde repris en à-nouveau doit égaler
    le solde de clôture. Une somme globale ne prouverait rien — l'écriture étant
    équilibrée, elle vaut zéro quoi qu'il arrive."""
    client, org_id = _signup(app)
    _full_exercise(client, org_id)
    today = date.today()
    cloture = {k: v for k, v in _balances_by_account(org_id, upto=today.isoformat()).items()
               if k[0] in '12345'}
    assert cloture, "l'exercice devrait laisser des soldes de bilan"
    _close(client, today.replace(month=1, day=1).isoformat(), today.isoformat())
    repris = _balances_by_account(org_id, journal='AN')
    for compte, solde in cloture.items():
        assert round(repris.get(compte, 0), 2) == round(solde, 2), \
            f'compte {compte} : {solde} à la clôture, {repris.get(compte, 0)} en à-nouveau'


def test_result_carried_forward_equals_revenue_minus_expenses(app):
    """Le résultat porté en 120000 (bénéfice) ou 129000 (perte) doit être exactement
    celui de l'exercice : produits moins charges."""
    client, org_id = _signup(app)
    _full_exercise(client, org_id)
    today = date.today()
    debut = today.replace(month=1, day=1).isoformat()
    soldes = _balances_by_account(org_id, upto=today.isoformat())
    charges = sum(v for k, v in soldes.items() if k.startswith('6'))
    produits = -sum(v for k, v in soldes.items() if k.startswith('7'))
    attendu = round(produits - charges, 2)
    _close(client, debut, today.isoformat())
    an = _balances_by_account(org_id, journal='AN')
    porte = round(-an.get('120000', 0) + an.get('129000', 0), 2)
    assert porte == attendu, f'résultat porté {porte}, attendu {attendu}'


def test_closing_twice_is_refused(app):
    """Deux clôtures à la même date doubleraient tous les soldes d'ouverture."""
    client, org_id = _signup(app)
    _full_exercise(client, org_id)
    today = date.today()
    debut = today.replace(month=1, day=1).isoformat()
    _close(client, debut, today.isoformat())
    apres_une = _ledger_totals(org_id)
    _close(client, debut, today.isoformat())
    assert _ledger_totals(org_id) == apres_une, 'une seconde clôture a été enregistrée'


def test_closing_with_inverted_dates_is_refused(app):
    """Une clôture antérieure à son ouverture n'a pas de sens : elle doit être refusée."""
    client, org_id = _signup(app)
    _full_exercise(client, org_id)
    avant = _ledger_totals(org_id)
    _close(client, '2026-12-31', '2026-01-01')
    assert _ledger_totals(org_id) == avant, 'une clôture aux dates inversées a été acceptée'


# ---------------------------------------------------------------------------
# CA3 et liquidation : le chiffre que l'entreprise paiera réellement
# ---------------------------------------------------------------------------

def test_ca3_net_due_equals_collected_minus_deductible(app):
    """La TVA nette due doit être exactement collectée − déductible : c'est le montant
    que l'entreprise virera au Trésor."""
    from profitos.vat_return import ca3_form
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Conseil', qty_1='1', price_1='1000', vat_1='20')
    inv = _invoice(org_id, inv_id)
    _post(client, f'/facturation/{inv_id}/reglement',
          {'amount': f"{float(inv['total']):.2f}".replace('.', ','),
           'payment_date': date.today().isoformat()})
    _purchase(client, '300', '60')
    today = date.today()
    c = _db(org_id)
    form = ca3_form(c, today.replace(month=1, day=1).isoformat(), today.isoformat())
    c.close()
    assert form['l16'] == 200.0, 'TVA collectée'
    assert round(form['l19'] + form['l20'], 2) == 60.0, 'TVA déductible'
    assert form['l28'] == 140.0, 'TVA nette à payer'
    assert form['l25'] == 0.0, 'aucun crédit de TVA attendu ici'


def test_ca3_shows_a_credit_when_deductible_exceeds_collected(app):
    """Plus d'achats que de ventes : un crédit de TVA, pas un montant négatif à payer."""
    from profitos.vat_return import ca3_form
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    _purchase(client, '1000', '200')
    today = date.today()
    c = _db(org_id)
    form = ca3_form(c, today.replace(month=1, day=1).isoformat(), today.isoformat())
    c.close()
    assert form['l28'] == 0.0, 'rien à payer'
    assert form['l25'] == 200.0, 'le crédit doit apparaître en crédit, jamais en dû négatif'


def test_vat_liquidation_clears_the_vat_accounts(app):
    """Après liquidation, les comptes de TVA de la période sont soldés et le montant dû
    se retrouve en 445510 — sinon la TVA resterait comptée deux fois le mois suivant."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Conseil', qty_1='1', price_1='1000', vat_1='20')
    inv = _invoice(org_id, inv_id)
    _post(client, f'/facturation/{inv_id}/reglement',
          {'amount': f"{float(inv['total']):.2f}".replace('.', ','),
           'payment_date': date.today().isoformat()})
    _purchase(client, '300', '60')
    today = date.today()
    _post(client, '/comptabilite/tva/ca3/liquider',
          {'date_from': today.replace(month=1, day=1).isoformat(), 'date_to': today.isoformat()})

    soldes = _balances_by_account(org_id)
    assert round(soldes.get('445710', 0), 2) == 0, 'TVA collectée non soldée'
    assert round(soldes.get('445660', 0), 2) == 0, 'TVA déductible non soldée'
    assert round(-soldes.get('445510', 0), 2) == 140.0, 'TVA à décaisser attendue en 445510'
    boiteuses = [dict(r) for r in _entries(org_id) if round(r['d'] - r['cr'], 2) != 0]
    assert not boiteuses, f'écriture(s) déséquilibrée(s) : {boiteuses}'


def test_vat_cannot_be_liquidated_twice(app):
    """Une double liquidation doublerait la dette fiscale."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Conseil', qty_1='1', price_1='1000', vat_1='20')
    inv = _invoice(org_id, inv_id)
    _post(client, f'/facturation/{inv_id}/reglement',
          {'amount': f"{float(inv['total']):.2f}".replace('.', ','),
           'payment_date': date.today().isoformat()})
    today = date.today()
    data = {'date_from': today.replace(month=1, day=1).isoformat(), 'date_to': today.isoformat()}
    _post(client, '/comptabilite/tva/ca3/liquider', data)
    apres_une = _ledger_totals(org_id)
    _post(client, '/comptabilite/tva/ca3/liquider', data)
    assert _ledger_totals(org_id) == apres_une, 'la TVA a été liquidée deux fois'
