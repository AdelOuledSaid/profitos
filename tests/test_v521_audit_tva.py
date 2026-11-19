"""v521 — Audit de la TVA : les cas qui font habituellement dérailler un logiciel.

Avant de confier le module fiscal à un expert-comptable, on cherche ici les défauts par
les invariants plutôt que par la lecture : une écriture doit être équilibrée au centime,
la TVA comptabilisée doit égaler celle de la facture, un encaissement fractionné ne doit
ni perdre ni créer de centime, et la CA3 doit refléter exactement ce qui est en compte.

Chaque contrôle porte sur un piège connu :
  * arrondis : des lignes dont la TVA unitaire tombe sur une demi-unité ;
  * plusieurs taux sur une même facture ;
  * encaissements partiels successifs d'une TVA exigible à l'encaissement ;
  * dernier règlement : le reliquat doit solder 445740 exactement ;
  * avoir : il doit annuler, au centime, ce que la facture avait produit ;
  * taux 0 (exonération) : aucune ligne de TVA parasite.
"""
from datetime import date

import pytest

from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db, _create_invoice, _invoice, FULL_PROFILE)

VAT_PENDING = '445740'      # TVA sur encaissements, pas encore exigible
VAT_DUE = '445710'          # TVA collectée exigible


# ---------------------------------------------------------------------------
# Outils de lecture comptable
# ---------------------------------------------------------------------------

def _all_lines(org_id):
    c = _db(org_id)
    rows = c.execute("""SELECT e.id entry_id, e.source_type, e.source_id, l.account_code,
                               l.debit, l.credit FROM accounting_entry_lines l
                        JOIN accounting_entries e ON e.id=l.entry_id""").fetchall()
    c.close()
    return rows


def _balance(org_id, account):
    c = _db(org_id)
    n = c.execute('SELECT ROUND(COALESCE(SUM(credit-debit),0),2) FROM accounting_entry_lines '
                  'WHERE account_code=?', (account,)).fetchone()[0]
    c.close()
    return n


def _assert_every_entry_balances(org_id):
    """Invariant premier de la partie double : aucune écriture ne peut être déséquilibrée."""
    per_entry = {}
    for r in _all_lines(org_id):
        d, c = per_entry.get(r['entry_id'], (0.0, 0.0))
        per_entry[r['entry_id']] = (d + (r['debit'] or 0), c + (r['credit'] or 0))
    faulty = {eid: v for eid, v in per_entry.items() if round(v[0] - v[1], 2) != 0}
    assert not faulty, f'écriture(s) déséquilibrée(s) : {faulty}'


def _emit(client, org_id, **extra):
    inv_id = _create_invoice(client, org_id, **extra)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    return inv_id


def _emit_on(client, org_id, day, **extra):
    """Facture antidatée pour éprouver les périodes.

    L'émission fixe toujours la date du jour — c'est le comportement voulu, une facture
    est émise le jour où elle l'est. Les dates sont donc reculées après coup, écriture de
    vente comprise, pour reconstituer une facture de mois passé ; sans quoi un règlement
    antérieur à l'émission serait refusé, à juste titre.
    """
    inv_id = _create_invoice(client, org_id, **extra)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    c = _db(org_id)
    c.execute('UPDATE outgoing_invoices SET issue_date=? WHERE id=?', (day.isoformat(), inv_id))
    c.execute("UPDATE accounting_entries SET entry_date=? WHERE source_type='outgoing_invoice' "
              'AND source_id=?', (day.isoformat(), inv_id))
    c.commit()
    c.close()
    return inv_id


def _pay(client, inv_id, amount, day=None):
    return _post(client, f'/facturation/{inv_id}/reglement',
                 {'amount': amount, 'payment_date': (day or date.today()).isoformat()})


# ---------------------------------------------------------------------------
# Arrondis
# ---------------------------------------------------------------------------

def test_rounding_three_lines_at_3333_keeps_books_balanced_and_vat_exact(app):
    """1 000 / 3 : le cas classique où un centime se perd entre les lignes et le total."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id,
                   label_1='Lot A', qty_1='3', price_1='33.33', vat_1='20',
                   label_2='Lot B', qty_2='1', price_2='0.01', vat_2='20')
    inv = _invoice(org_id, inv_id)
    _assert_every_entry_balances(org_id)
    # la TVA comptabilisée doit égaler, au centime, celle portée sur la facture
    comptabilisee = _balance(org_id, VAT_PENDING) + _balance(org_id, VAT_DUE)
    assert round(comptabilisee, 2) == round(float(inv['vat_amount']), 2)
    # et le total TTC doit rester base + TVA
    assert round(float(inv['subtotal']) + float(inv['vat_amount']), 2) == round(float(inv['total']), 2)


def test_rounding_half_cent_per_line_does_not_drift(app):
    """Des montants dont la TVA unitaire tombe exactement sur un demi-centime."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Unité', qty_1='7', price_1='0.025', vat_1='20')
    inv = _invoice(org_id, inv_id)
    _assert_every_entry_balances(org_id)
    assert round(_balance(org_id, VAT_PENDING) + _balance(org_id, VAT_DUE), 2) == round(float(inv['vat_amount']), 2)
    assert round(float(inv['subtotal']) + float(inv['vat_amount']), 2) == round(float(inv['total']), 2)


# ---------------------------------------------------------------------------
# Plusieurs taux
# ---------------------------------------------------------------------------

def test_three_vat_rates_on_one_invoice_are_split_and_total_matches(app):
    """20 %, 10 % et 5,5 % sur la même facture : chaque taux doit vivre sa vie."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id,
                   label_1='Conseil', qty_1='1', price_1='1000', vat_1='20',
                   label_2='Restauration', qty_2='1', price_2='200', vat_2='10',
                   label_3='Alimentaire', qty_3='1', price_3='100', vat_3='5.5')
    inv = _invoice(org_id, inv_id)
    _assert_every_entry_balances(org_id)
    attendu = round(1000 * 0.20 + 200 * 0.10 + 100 * 0.055, 2)
    assert round(float(inv['vat_amount']), 2) == attendu
    assert round(_balance(org_id, VAT_PENDING) + _balance(org_id, VAT_DUE), 2) == attendu
    # la ventilation par taux doit retrouver les trois bases
    from profitos.accounting import ca3_collected_by_rate
    c = _db(org_id)
    _pay(client, inv_id, f"{float(inv['total']):.2f}".replace('.', ','))
    rates = {r['rate']: r for r in ca3_collected_by_rate(c, '2000-01-01', date.today().isoformat())}
    c.close()
    assert round(rates[20.0]['base'], 2) == 1000.0
    assert round(rates[10.0]['base'], 2) == 200.0
    assert round(rates[5.5]['base'], 2) == 100.0


# ---------------------------------------------------------------------------
# Encaissements fractionnés (TVA sur encaissements)
# ---------------------------------------------------------------------------

def test_three_partial_payments_transfer_vat_without_losing_a_cent(app):
    """Trois règlements inégaux : le cumul transféré doit égaler la TVA de la facture,
    et 445740 doit être exactement soldé à la fin — ni débit ni crédit résiduel."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Mission', qty_1='1', price_1='999.99', vat_1='20')
    inv = _invoice(org_id, inv_id)
    ttc = round(float(inv['total']), 2)
    tva = round(float(inv['vat_amount']), 2)
    assert _balance(org_id, VAT_PENDING) == tva and _balance(org_id, VAT_DUE) == 0

    premier = round(ttc / 3, 2)
    second = round(ttc / 7, 2)
    for montant in (premier, second):
        _pay(client, inv_id, f'{montant:.2f}'.replace('.', ','))
    reste = round(ttc - premier - second, 2)
    _pay(client, inv_id, f'{reste:.2f}'.replace('.', ','))

    _assert_every_entry_balances(org_id)
    assert _balance(org_id, VAT_PENDING) == 0, 'la TVA en attente doit être intégralement soldée'
    assert _balance(org_id, VAT_DUE) == tva, 'la TVA exigible doit égaler celle de la facture'


def test_overpayment_does_not_push_more_vat_than_the_invoice_carries(app):
    """Un client qui paie trop ne doit pas rendre exigible plus de TVA qu'il n'y en a."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Mission', qty_1='1', price_1='500', vat_1='20')
    inv = _invoice(org_id, inv_id)
    tva = round(float(inv['vat_amount']), 2)
    _pay(client, inv_id, f"{float(inv['total']) + 100:.2f}".replace('.', ','))
    _assert_every_entry_balances(org_id)
    assert _balance(org_id, VAT_DUE) <= tva, 'TVA exigible supérieure à la TVA facturée'
    assert _balance(org_id, VAT_PENDING) >= 0, 'TVA en attente devenue négative'


# ---------------------------------------------------------------------------
# Avoirs et exonérations
# ---------------------------------------------------------------------------

def test_zero_rate_invoice_creates_no_vat_line(app):
    """Exonération ou autoliquidation : aucune ligne de TVA ne doit apparaître."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Export hors UE', qty_1='1', price_1='1000', vat_1='0')
    inv = _invoice(org_id, inv_id)
    _assert_every_entry_balances(org_id)
    assert round(float(inv['vat_amount']), 2) == 0
    assert _balance(org_id, VAT_PENDING) == 0 and _balance(org_id, VAT_DUE) == 0
    assert round(float(inv['total']), 2) == round(float(inv['subtotal']), 2)


def test_full_credit_note_cancels_the_vat_to_the_cent(app):
    """Un avoir total doit ramener la TVA à zéro, sans laisser de résidu d'arrondi."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Mission', qty_1='3', price_1='33.33', vat_1='20')
    inv = _invoice(org_id, inv_id)
    ttc = round(float(inv['total']), 2)
    _pay(client, inv_id, f'{ttc:.2f}'.replace('.', ','))   # TVA devenue exigible
    tva_avant = _balance(org_id, VAT_DUE)
    assert tva_avant > 0

    _post(client, f'/facturation/{inv_id}/avoir',
          {'reason': 'Annulation commande', 'amount_ttc': f'{ttc:.2f}'.replace('.', ',')})
    _assert_every_entry_balances(org_id)
    assert _balance(org_id, VAT_DUE) == 0, "l'avoir total doit annuler la TVA exigible"
    assert _balance(org_id, VAT_PENDING) == 0


def test_partial_credit_note_leaves_exactly_the_remaining_vat(app):
    """Avoir partiel : la TVA restante doit correspondre à la part non annulée."""
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit(client, org_id, label_1='Mission', qty_1='1', price_1='1000', vat_1='20')
    inv = _invoice(org_id, inv_id)
    _pay(client, inv_id, f"{float(inv['total']):.2f}".replace('.', ','))
    _post(client, f'/facturation/{inv_id}/avoir', {'reason': 'Remise', 'amount_ttc': '300,00'})
    _assert_every_entry_balances(org_id)
    # 300 € TTC annulés à 20 % = 50 € de TVA ; il doit rester 200 - 50 = 150 €
    assert _balance(org_id, VAT_DUE) == 150.0


# ---------------------------------------------------------------------------
# Bornes de période : le piège le plus fréquent d'une déclaration
# ---------------------------------------------------------------------------

def test_period_boundaries_include_both_ends(app):
    """Une TVA exigible le premier et le dernier jour de la période doit y figurer :
    une borne exclue, et c'est une déclaration fausse tous les mois."""
    from profitos.accounting import ca3_collected_by_rate
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    premier = _emit_on(client, org_id, date(2026, 5, 20),
                       label_1='Début de mois', qty_1='1', price_1='100', vat_1='20')
    dernier = _emit_on(client, org_id, date(2026, 5, 20),
                       label_1='Fin de mois', qty_1='1', price_1='200', vat_1='20')
    _pay(client, premier, '120,00', day=date(2026, 6, 1))
    _pay(client, dernier, '240,00', day=date(2026, 6, 30))

    c = _db(org_id)
    dedans = {r['rate']: r for r in ca3_collected_by_rate(c, '2026-06-01', '2026-06-30')}
    avant = ca3_collected_by_rate(c, '2026-05-01', '2026-05-31')
    apres = ca3_collected_by_rate(c, '2026-07-01', '2026-07-31')
    c.close()
    assert round(dedans[20.0]['base'], 2) == 300.0, 'les deux bornes doivent être incluses'
    assert round(dedans[20.0]['vat'], 2) == 60.0
    assert not avant and not apres, 'rien ne doit déborder sur les mois voisins'


def test_vat_falls_in_the_month_of_payment_not_of_invoice(app):
    """Services sans option pour les débits : facture en janvier, paiement en février,
    la TVA appartient à février. L'inverse ferait déclarer trop tôt."""
    from profitos.accounting import ca3_collected_by_rate
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _emit_on(client, org_id, date(2026, 1, 15),
                      label_1='Mission', qty_1='1', price_1='1000', vat_1='20')
    _pay(client, inv_id, '1200,00', day=date(2026, 2, 3))
    c = _db(org_id)
    janvier = ca3_collected_by_rate(c, '2026-01-01', '2026-01-31')
    fevrier = {r['rate']: r for r in ca3_collected_by_rate(c, '2026-02-01', '2026-02-28')}
    c.close()
    assert not janvier, "la TVA ne doit pas être exigible avant l'encaissement"
    assert round(fevrier[20.0]['vat'], 2) == 200.0


# ---------------------------------------------------------------------------
# Achats : TVA déductible
# ---------------------------------------------------------------------------

def test_purchase_with_two_rates_splits_deductible_vat(app):
    """v511 a introduit une ligne 445660 par taux : la somme doit égaler la TVA saisie."""
    from profitos.accounting import _purchase_vat_parts
    parts = _purchase_vat_parts({'vat_amount': 30.0, 'subtotal': 300.0,
                                 'vat_breakdown_json': '[{"rate":20,"base":100,"vat":20},'
                                                       '{"rate":5.5,"base":200,"vat":10}]'})
    assert round(sum(p['vat'] for p in parts), 2) == 30.0
    assert round(sum(p['base'] for p in parts), 2) == 300.0


def test_purchase_breakdown_is_refused_when_it_does_not_add_up(app):
    """Garde-fou v511 : une ventilation qui ne retombe pas sur la TVA de la facture est
    écartée (retour à une ligne unique) plutôt que de produire une écriture fausse."""
    from profitos.accounting import _purchase_vat_parts
    fausse = _purchase_vat_parts({'vat_amount': 30.0, 'subtotal': 300.0,
                                  'vat_breakdown_json': '[{"rate":20,"base":100,"vat":20}]'})
    assert fausse == [], 'une ventilation incomplète ne doit pas être utilisée'
    sans = _purchase_vat_parts({'vat_amount': 17.42, 'subtotal': 87.1, 'vat_breakdown_json': None})
    assert sans == [], "sans ventilation, l'appelant retombe sur une ligne unique"
