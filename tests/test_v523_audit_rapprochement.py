"""v523 — Audit des rapprochements bancaires : doublons et débordements.

Un rapprochement rattache un mouvement bancaire à une facture. Les deux fautes possibles
sont symétriques et toutes deux graves : rattacher deux fois le même encaissement (la
facture paraît payée en double, le compte client devient créditeur) ou rattacher plus que
ce que le mouvement porte (la banque comptabilisée ne correspond plus au relevé).

Les contrôles portent directement sur la fonction de rapprochement, là où vit la règle.
"""
from datetime import date

import pytest

from profitos.routes.bank_sync import reconcile_invoice, reconcile_purchase
from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db, _create_invoice, _invoice, FULL_PROFILE)


def _compte_bancaire(conn, eid=None):
    conn.execute("""INSERT INTO bank_accounts(provider,provider_account_id,name,iban,currency,entity_id)
                    VALUES('swan','acc-1','Compte courant','FR7630001007941234567890185','EUR',?)""", (eid,))
    conn.commit()


def _mouvement(conn, montant, ref, jour=None):
    cur = conn.execute("""INSERT INTO bank_transactions(provider,provider_transaction_id,
                              provider_account_id,transaction_date,label,amount,raw_status)
                          VALUES('swan',?, 'acc-1', ?, ?, ?, 'Booked')""",
                       (ref, (jour or date.today()).isoformat(), 'Virement ' + ref, montant))
    conn.commit()
    return cur.lastrowid


def _facture(client, org_id, prix='1000', vat='20'):
    inv_id = _create_invoice(client, org_id, label_1='Prestation', qty_1='1',
                             price_1=prix, vat_1=vat)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    return inv_id


def _maison(app):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    c = _db(org_id)
    _compte_bancaire(c)
    return client, org_id, c


def _solde(conn, compte):
    n = conn.execute('SELECT ROUND(COALESCE(SUM(debit-credit),0),2) FROM accounting_entry_lines '
                     'WHERE account_code LIKE ?', (compte + '%',)).fetchone()[0]
    return float(n or 0)


def _ecritures_equilibrees(conn):
    rows = conn.execute("""SELECT e.id, ROUND(COALESCE(SUM(l.debit),0),2) d,
                                  ROUND(COALESCE(SUM(l.credit),0),2) cr
                           FROM accounting_entries e
                           LEFT JOIN accounting_entry_lines l ON l.entry_id=e.id
                           GROUP BY e.id""").fetchall()
    return [dict(r) for r in rows if round(r['d'] - r['cr'], 2) != 0]


# ---------------------------------------------------------------------------
# Doublons
# ---------------------------------------------------------------------------

def test_the_same_transaction_cannot_pay_the_same_invoice_twice(app):
    """Le cas le plus courant : on clique deux fois. Le second rattachement doit être
    refusé, sans quoi la facture est payée deux fois et le compte client devient créditeur."""
    client, org_id, c = _maison(app)
    inv = _facture(client, org_id)
    tx = _mouvement(c, 1200.0, 'tx-1')
    montant, _ = reconcile_invoice(c, None, tx, inv)
    assert montant == 1200.0
    with pytest.raises((ValueError, LookupError)):
        reconcile_invoice(c, None, tx, inv)
    paiements = c.execute('SELECT COUNT(*) FROM outgoing_invoice_payments WHERE invoice_id=?',
                          (inv,)).fetchone()[0]
    assert paiements == 1, 'le règlement a été enregistré deux fois'
    assert _solde(c, '512') == 1200.0, 'la banque a été débitée deux fois'
    assert _solde(c, '411') == 0.0, 'le compte client devrait être soldé, pas créditeur'
    c.close()


def test_a_transaction_cannot_be_spread_beyond_its_own_amount(app):
    """Un encaissement de 1 200 € peut régler deux factures de 600 €, pas trois."""
    client, org_id, c = _maison(app)
    a, b, d = _facture(client, org_id, '500'), _facture(client, org_id, '500'), _facture(client, org_id, '500')
    tx = _mouvement(c, 1200.0, 'tx-2')
    assert reconcile_invoice(c, None, tx, a)[0] == 600.0
    assert reconcile_invoice(c, None, tx, b)[0] == 600.0
    with pytest.raises((ValueError, LookupError)):
        reconcile_invoice(c, None, tx, d)
    assert _solde(c, '512') == 1200.0, 'la banque ne doit jamais dépasser le mouvement réel'
    c.close()


def test_partial_reconciliation_then_the_rest(app):
    """Rattachement en deux fois : 400 puis le solde. La facture passe à « payée »."""
    client, org_id, c = _maison(app)
    inv = _facture(client, org_id)
    tx1, tx2 = _mouvement(c, 400.0, 'tx-3a'), _mouvement(c, 800.0, 'tx-3b')
    reconcile_invoice(c, None, tx1, inv, amount=400.0)
    statut = c.execute('SELECT status FROM outgoing_invoices WHERE id=?', (inv,)).fetchone()[0]
    assert statut == 'partially_paid'
    reconcile_invoice(c, None, tx2, inv)
    statut = c.execute('SELECT status FROM outgoing_invoices WHERE id=?', (inv,)).fetchone()[0]
    assert statut == 'paid'
    assert _solde(c, '411') == 0.0
    assert not _ecritures_equilibrees(c)
    c.close()


# ---------------------------------------------------------------------------
# Débordements
# ---------------------------------------------------------------------------

def test_cannot_reconcile_more_than_the_invoice_balance(app):
    """Un virement de 5 000 € ne peut pas solder une facture de 1 200 € à hauteur de
    5 000 € : le surplus n'appartient pas à cette facture."""
    client, org_id, c = _maison(app)
    inv = _facture(client, org_id)
    tx = _mouvement(c, 5000.0, 'tx-4')
    with pytest.raises(ValueError):
        reconcile_invoice(c, None, tx, inv, amount=5000.0)
    montant, _ = reconcile_invoice(c, None, tx, inv)
    assert montant == 1200.0, 'le rattachement automatique se limite au solde dû'
    c.close()


def test_negative_or_zero_amounts_are_refused(app):
    client, org_id, c = _maison(app)
    inv = _facture(client, org_id)
    tx = _mouvement(c, 1200.0, 'tx-5')
    for mauvais in (0, -100.0):
        with pytest.raises(ValueError):
            reconcile_invoice(c, None, tx, inv, amount=mauvais)
    c.close()


def test_an_outgoing_payment_cannot_settle_a_customer_invoice(app):
    """Une sortie d'argent n'encaisse rien : la rattacher à une facture client
    inverserait le sens du mouvement."""
    client, org_id, c = _maison(app)
    inv = _facture(client, org_id)
    tx = _mouvement(c, -1200.0, 'tx-6')
    with pytest.raises(ValueError):
        reconcile_invoice(c, None, tx, inv)
    c.close()


def test_an_incoming_payment_cannot_settle_a_supplier_invoice(app):
    """Symétrique : une entrée d'argent ne paie pas un fournisseur."""
    client, org_id, c = _maison(app)
    today = date.today()
    _post(client, '/facturation/achats/nouvelle', {
        'supplier_name': 'Fournisseur', 'invoice_number': 'FB-900',
        'issue_date': today.isoformat(), 'due_date': today.isoformat(),
        'subtotal': '100', 'vat_amount': '20'})
    pid = c.execute('SELECT MAX(id) FROM purchase_invoices').fetchone()[0]
    tx = _mouvement(c, 120.0, 'tx-7')      # une entrée, pas une sortie
    with pytest.raises(ValueError):
        reconcile_purchase(c, None, tx, pid)
    c.close()


def test_an_already_paid_invoice_is_not_available_anymore(app):
    """Une facture soldée doit sortir des rapprochements possibles."""
    client, org_id, c = _maison(app)
    inv = _facture(client, org_id)
    tx1, tx2 = _mouvement(c, 1200.0, 'tx-8a'), _mouvement(c, 1200.0, 'tx-8b')
    reconcile_invoice(c, None, tx1, inv)
    with pytest.raises(ValueError):
        reconcile_invoice(c, None, tx2, inv)
    assert _solde(c, '512') == 1200.0
    c.close()


# ---------------------------------------------------------------------------
# Cohérence comptable d'ensemble
# ---------------------------------------------------------------------------

def test_reconciliation_keeps_the_books_balanced_and_the_bank_faithful(app):
    """Après plusieurs rapprochements, la banque comptabilisée doit égaler la somme des
    mouvements rattachés, et toutes les écritures rester équilibrées."""
    client, org_id, c = _maison(app)
    a, b = _facture(client, org_id, '500'), _facture(client, org_id, '250')
    tx1 = _mouvement(c, 600.0, 'tx-9a')
    tx2 = _mouvement(c, 300.0, 'tx-9b')
    reconcile_invoice(c, None, tx1, a)
    reconcile_invoice(c, None, tx2, b)
    assert _solde(c, '512') == 900.0
    assert _solde(c, '411') == 0.0
    assert not _ecritures_equilibrees(c)
    alloue = c.execute('SELECT ROUND(COALESCE(SUM(matched_amount),0),2) '
                       'FROM bank_invoice_allocations').fetchone()[0]
    assert float(alloue) == 900.0, 'les allocations ne correspondent pas aux écritures'
    c.close()
