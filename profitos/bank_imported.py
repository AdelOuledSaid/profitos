"""Rapprochement bancaire des factures clients importées (v494).

Les factures en cours importées depuis un fichier (table ``invoices``) sont des créances
réelles : Mission Control les relance et les signale. Jusqu'ici, la banque ne savait les
rapprocher que des factures émises dans Veqalio (``outgoing_invoices``) : un virement citant
exactement le numéro et le montant d'une facture importée restait « sans correspondance ».

Ce module ajoute, pour les factures importées :
- la liste des factures encore ouvertes et leur solde ;
- le rapprochement atomique d'un encaissement : montant réglé sur la facture, allocation
  du mouvement (qui sort des mouvements en attente), écriture comptable 512 / 411.
"""
from profitos.accounting import AccountingError, create_entry, paris_today

SOURCE_TYPE = 'imported_invoice_payment'

SCHEMA = """CREATE TABLE IF NOT EXISTS bank_imported_allocations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER,
    bank_transaction_id INTEGER NOT NULL,
    imported_invoice_id INTEGER NOT NULL,
    invoice_number TEXT,
    matched_amount REAL NOT NULL,
    matched_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    UNIQUE(entity_id,idempotency_key)
)"""


def ensure_table(c):
    c.execute(SCHEMA)


def allocated_on_transaction(c, tx_id, eid):
    """Montant d'un mouvement déjà affecté à des factures importées (table créée par la
    migration du schéma tenant)."""
    row = c.execute('SELECT COALESCE(SUM(matched_amount),0) n FROM bank_imported_allocations '
                    'WHERE entity_id IS ? AND bank_transaction_id=?', (eid, tx_id)).fetchone()
    return round(float(row['n'] or 0), 2)


def _outgoing_numbers(c, eid):
    from profitos.receivables import _num_key
    return {_num_key(r['invoice_number']) for r in
            c.execute('SELECT invoice_number FROM outgoing_invoices WHERE entity_id IS ?', (eid,)).fetchall()
            if (r['invoice_number'] or '').strip()}


def balance(row):
    return max(0.0, round(float(row['amount'] or 0) - float(row['paid_amount'] or 0), 2))


def open_imported(c, eid):
    """Factures importées encore ouvertes, sous la forme attendue par le score bancaire
    (``invoice_number``, ``client_name``), avec leur solde. Une facture importée dont le numéro
    existe aussi dans Veqalio est ignorée : c'est la facture émise qui fait foi."""
    from profitos.receivables import _num_key
    known = _outgoing_numbers(c, eid)
    out = []
    for r in c.execute("SELECT * FROM invoices WHERE entity_id IS ? AND LOWER(COALESCE(status,''))!='paid' "
                       "ORDER BY due_date, id", (eid,)).fetchall():
        if _num_key(r['invoice_number']) and _num_key(r['invoice_number']) in known:
            continue
        b = balance(r)
        if b <= .005:
            continue
        out.append({'id': r['id'], 'entity_id': r['entity_id'], 'invoice_number': r['invoice_number'],
                    'client_name': r['customer'], 'issue_date': r['issue_date'], 'due_date': r['due_date'],
                    'total': float(r['amount'] or 0), 'balance': b, 'source': 'imported'})
    return out


def _reconcile(c, eid, tx_id, inv_id, amount=None):
    from profitos.routes.bank_sync import _bank_allocated_total, _bank_ledger_code
    from profitos.runtime import now
    ensure_table(c)
    t = c.execute("""SELECT t.* FROM bank_transactions t JOIN bank_accounts a
                     ON a.provider=t.provider AND a.provider_account_id=t.provider_account_id
                     WHERE t.id=? AND a.entity_id IS ?""", (tx_id, eid)).fetchone()
    inv = c.execute('SELECT * FROM invoices WHERE id=? AND entity_id IS ?', (inv_id, eid)).fetchone()
    if not t or not inv:
        raise LookupError('introuvable')
    if (inv['status'] or '').lower() == 'paid':
        raise ValueError("Cette facture est déjà soldée.")
    tx_amount = float(t['amount'] or 0)
    available = round(tx_amount - _bank_allocated_total(c, tx_id, eid), 2)
    due = balance(inv)
    if tx_amount <= 0 or available <= .005 or due <= .005:
        raise ValueError("Aucun montant restant à rapprocher.")
    amount = round(amount, 2) if amount is not None else min(available, due)
    if amount <= 0 or amount > available + .001 or amount > due + .001:
        raise ValueError("Montant de rapprochement invalide.")
    key = f"bank-imported:{eid}:{tx_id}:{inv_id}:{amount:.2f}"
    at = now()
    cur = c.execute("""INSERT INTO bank_imported_allocations(entity_id,bank_transaction_id,imported_invoice_id,
                       invoice_number,matched_amount,matched_at,idempotency_key) VALUES(?,?,?,?,?,?,?)""",
                    (eid, tx_id, inv_id, inv['invoice_number'], amount, at, key))
    alloc_id = cur.lastrowid
    bank_code = _bank_ledger_code(c, t, eid)
    if not bank_code:
        raise AccountingError("Compte banque 512 absent du plan comptable.")
    customer = (inv['customer'] or 'Client').strip()
    create_entry(c, 'BQ', t['transaction_date'] or paris_today(),
                 f"Règlement facture {inv['invoice_number'] or ''} — {customer}".replace('  ', ' '),
                 [{'account_code': bank_code, 'debit': amount},
                  {'account_code': '411000', 'credit': amount, 'auxiliary_name': customer}],
                 source_type=SOURCE_TYPE, source_id=alloc_id, entity_id=eid,
                 piece_ref=inv['invoice_number'] or (t['label'] or '')[:40], commit=False)
    paid = round(float(inv['paid_amount'] or 0) + amount, 2)
    fully = paid >= float(inv['amount'] or 0) - .005
    c.execute('UPDATE invoices SET paid_amount=?, status=? WHERE id=? AND entity_id IS ?',
              (paid, 'paid' if fully else (inv['status'] or 'unpaid'), inv_id, eid))
    c.commit()
    return amount, inv


def restore_after_reimport(c, eid):
    """Après un nouvel import du fichier de factures (qui remplace toutes les factures
    importées), reporte les encaissements déjà rapprochés en banque sur les nouvelles lignes,
    par numéro de facture : réglé = max(réglé selon le fichier, encaissé en banque). Le
    maximum évite de compter deux fois un paiement que le nouveau fichier indique déjà.
    Ne valide pas : l'appelant fait le commit avec le reste de l'import."""
    ensure_table(c)
    rows = c.execute("""SELECT invoice_number, SUM(matched_amount) total FROM bank_imported_allocations
                        WHERE entity_id IS ? AND COALESCE(invoice_number,'')<>'' GROUP BY invoice_number""",
                     (eid,)).fetchall()
    restored = 0
    for r in rows:
        new = c.execute('SELECT * FROM invoices WHERE entity_id IS ? AND invoice_number=? ORDER BY id LIMIT 1',
                        (eid, r['invoice_number'])).fetchone()
        if not new:
            continue
        bank_paid = round(float(r['total'] or 0), 2)
        paid = max(float(new['paid_amount'] or 0), bank_paid)
        status = 'paid' if paid >= float(new['amount'] or 0) - .005 else new['status']
        c.execute('UPDATE invoices SET paid_amount=?, status=? WHERE id=?', (round(paid, 2), status, new['id']))
        c.execute('UPDATE bank_imported_allocations SET imported_invoice_id=? WHERE entity_id IS ? AND invoice_number=?',
                  (new['id'], eid, r['invoice_number']))
        restored += 1
    return restored


def reconcile_imported(c, eid, tx_id, inv_id, amount=None):
    """Rapprochement atomique d'un encaissement et d'une facture importée : toute erreur annule
    l'ensemble (rollback) avant d'être relayée. Retourne (montant, facture)."""
    try:
        return _reconcile(c, eid, tx_id, inv_id, amount)
    except Exception:
        c.rollback()
        raise
