"""État de rapprochement bancaire : solde du relevé comparé au solde du compte 512.

Pour un compte comptable de banque (512000, 512100…) et une date :
  solde banque (relevé ou dernier solde synchronisé, ramené à la date)
  solde comptable du compte 512 à la date
  + mouvements bancaires non encore comptabilisés (ni rapprochés, ni catégorisés)
  = solde comptable rapproché ; l'écart restant s'explique par les écritures 512 qui ne
    correspondent à aucun mouvement bancaire (chèque non encaissé, règlement saisi à la
    main, erreur de saisie…), listées à part.

Lecture seule : aucune écriture n'est passée ici.
"""
from datetime import date, timedelta
from profitos.accounting import paris_today

PROVIDERS = ('powens', 'swan', 'import')
LINKED_SOURCES = ('bank_fee', 'bank_categorization')


def _ef(column, entity_id):
    return (f'{column}=?', (entity_id,)) if entity_id else (f'{column} IS NULL', ())


def default_ledger_code(conn, entity_id):
    row = conn.execute("""SELECT code FROM accounting_chart_of_accounts WHERE code LIKE '512%%'
                          AND (entity_id IS ? OR entity_id IS NULL) AND COALESCE(is_active,1)=1
                          ORDER BY code LIMIT 1""", (entity_id,)).fetchone()
    return row['code'] if row else '512000'


def ledger_accounts(conn, entity_id):
    """Comptes de trésorerie bancaire proposables (51…) du plan comptable."""
    return conn.execute("""SELECT code,label FROM accounting_chart_of_accounts WHERE code LIKE '51%%'
                           AND (entity_id IS ? OR entity_id IS NULL) AND COALESCE(is_active,1)=1
                           ORDER BY code""", (entity_id,)).fetchall()


def bank_accounts_for(conn, entity_id, ledger_code=None):
    ef, ep = _ef('entity_id', entity_id)
    default = default_ledger_code(conn, entity_id)
    rows = conn.execute(
        f"SELECT * FROM bank_accounts WHERE provider IN ('powens','swan','import') AND {ef} ORDER BY id", ep).fetchall()
    out = []
    for a in rows:
        code = (a['accounting_code'] or '').strip() or default
        if ledger_code is None or code == ledger_code:
            out.append((a, code))
    return out


def _tx_filter():
    return ("COALESCE(t.raw_status,'')<>'deleted' "
            "AND (t.provider<>'swan' OR COALESCE(t.raw_status,'')='Booked')")


def bank_balance_at(conn, account, as_of):
    """Solde du compte bancaire à la date, à partir du dernier solde connu (synchronisé
    ou lu sur le relevé importé) corrigé des mouvements entre les deux dates. None si
    aucun solde n'est connu."""
    if account['balance'] is None:
        return None
    ref = str(account['statement_balance_date'] or account['last_synced_at'] or '')[:10]
    if not ref:
        return None
    as_of = str(as_of)[:10]
    lo, hi, sign = (as_of, ref, -1) if as_of < ref else (ref, as_of, 1)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(t.amount),0) n FROM bank_transactions t
            WHERE t.provider=? AND t.provider_account_id=? AND t.transaction_date>? AND t.transaction_date<=?
              AND {_tx_filter()}""",
        (account['provider'], account['provider_account_id'], lo, hi)).fetchone()
    return round(float(account['balance']) + sign * float(row['n'] or 0), 2)


def ledger_balance(conn, code, as_of, entity_id):
    ef, ep = _ef('e.entity_id', entity_id)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(l.debit-l.credit),0) n FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            WHERE l.account_code=? AND e.entry_date<=? AND {ef}""",
        (code, str(as_of)[:10], *ep)).fetchone()
    return round(float(row['n'] or 0), 2)


def _booked_amount(conn, tx_id, entity_id):
    q = lambda sql: float(conn.execute(sql, (entity_id, tx_id)).fetchone()['x'] or 0)
    customer = q("SELECT COALESCE(SUM(matched_amount),0) x FROM bank_invoice_allocations WHERE entity_id IS ? AND bank_transaction_id=?")
    supplier = q("SELECT COALESCE(SUM(matched_amount),0) x FROM bank_purchase_allocations WHERE entity_id IS ? AND bank_transaction_id=?")
    # Écritures BQ créées depuis le mouvement (frais, catégorisation) : la part portée
    # sur le compte de banque.
    entries = q("""SELECT COALESCE(SUM(l.debit+l.credit),0) x FROM accounting_entries e
                   JOIN accounting_entry_lines l ON l.entry_id=e.id
                   WHERE e.entity_id IS ? AND e.source_id=? AND e.source_type IN ('bank_fee','bank_categorization')
                     AND l.account_code LIKE '51%%'""")
    return round(customer + supplier + entries, 2)


def reconciliation_statement(conn, entity_id, ledger_code, as_of, statement_balance=None):
    as_of = str(as_of or paris_today().isoformat())[:10]
    accounts = bank_accounts_for(conn, entity_id, ledger_code)
    account_rows = [a for a, _ in accounts]

    # 1. Solde banque
    computed = [bank_balance_at(conn, a, as_of) for a in account_rows]
    computed_total = None if (not account_rows or any(v is None for v in computed)) else round(sum(computed), 2)
    bank_total = statement_balance if statement_balance is not None else computed_total

    # 2. Mouvements bancaires non comptabilisés
    unbooked = []
    tx_ids = []
    first_tx_date = None
    for a in account_rows:
        rows = conn.execute(
            f"""SELECT t.* FROM bank_transactions t
                WHERE t.provider=? AND t.provider_account_id=? AND t.transaction_date<=? AND {_tx_filter()}
                ORDER BY t.transaction_date,t.id""",
            (a['provider'], a['provider_account_id'], as_of)).fetchall()
        for t in rows:
            tx_ids.append(t['id'])
            if t['transaction_date'] and (first_tx_date is None or t['transaction_date'] < first_tx_date):
                first_tx_date = t['transaction_date']
            amount = round(float(t['amount'] or 0), 2)
            remaining = round(abs(amount) - _booked_amount(conn, t['id'], entity_id), 2)
            if remaining > 0.005:
                ignored = conn.execute(
                    "SELECT 1 FROM bank_transaction_workflow WHERE entity_id IS ? AND bank_transaction_id=? AND state='ignored'",
                    (entity_id, t['id'])).fetchone()
                unbooked.append({'id': t['id'], 'date': t['transaction_date'], 'label': t['label'],
                                 'account': a['name'], 'amount': amount,
                                 'remaining': remaining if amount > 0 else -remaining,
                                 'ignored': bool(ignored)})
    unbooked_total = round(sum(u['remaining'] for u in unbooked), 2)

    # 3. Écritures 512 sans mouvement bancaire rapproché
    ef, ep = _ef('e.entity_id', entity_id)
    linked_payments_in = {r['payment_id'] for r in conn.execute(
        "SELECT payment_id FROM bank_invoice_allocations WHERE entity_id IS ? AND payment_id IS NOT NULL", (entity_id,)).fetchall()}
    linked_payments_out = {r['payment_id'] for r in conn.execute(
        "SELECT payment_id FROM bank_purchase_allocations WHERE entity_id IS ? AND payment_id IS NOT NULL", (entity_id,)).fetchall()}
    tx_set = set(tx_ids)
    lines = conn.execute(
        f"""SELECT e.id entry_id,e.entry_date,e.label,e.journal_code,e.piece_number,e.source_type,e.source_id,
                   l.debit,l.credit
            FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
            WHERE l.account_code=? AND e.entry_date<=? AND {ef}
            ORDER BY e.entry_date,e.id""",
        (ledger_code, as_of, *ep)).fetchall()
    unmatched, older = [], 0
    for l in lines:
        st = l['source_type'] or ''
        if st in LINKED_SOURCES and l['source_id'] in tx_set:
            continue
        if st == 'outgoing_invoice_payment_v2' and l['source_id'] in linked_payments_in:
            continue
        if st == 'purchase_invoice_payment_v2' and l['source_id'] in linked_payments_out:
            continue
        if st == 'opening_balance' or l['journal_code'] == 'AN':
            continue  # à-nouveau : solde d'ouverture, pas un mouvement de la période
        if first_tx_date and str(l['entry_date']) < first_tx_date:
            older += 1  # antérieure au premier mouvement bancaire connu
            continue
        amount = round(float(l['debit'] or 0) - float(l['credit'] or 0), 2)
        unmatched.append({'entry_id': l['entry_id'], 'date': l['entry_date'], 'label': l['label'],
                          'journal': l['journal_code'], 'number': l['piece_number'], 'amount': amount})
    unmatched_total = round(sum(u['amount'] for u in unmatched), 2)

    ledger = ledger_balance(conn, ledger_code, as_of, entity_id)
    adjusted = round(ledger + unbooked_total, 2)
    gap = None if bank_total is None else round(bank_total - adjusted, 2)
    # Écart attendu si toutes les écritures sans mouvement bancaire étaient des
    # décaissements / encaissements pas encore passés en banque.
    unexplained = None if gap is None else round(gap + unmatched_total, 2)
    # Solde de la banque la veille du premier mouvement connu : si l'écart lui est égal,
    # c'est le solde d'ouverture qui manque en comptabilité (à-nouveaux non repris).
    opening_bank = None
    if first_tx_date and computed_total is not None:
        day_before = (date.fromisoformat(first_tx_date[:10]) - timedelta(days=1)).isoformat()
        opening_bank = round(sum(bank_balance_at(conn, a, day_before) for a in account_rows), 2)
    missing_opening = (gap is not None and opening_bank is not None and abs(opening_bank) > 0.005
                       and abs(gap - opening_bank) < 0.005 and not unmatched)
    return {
        'opening_bank': opening_bank, 'missing_opening': missing_opening,
        'as_of': as_of, 'ledger_code': ledger_code, 'accounts': account_rows,
        'bank_balance': bank_total, 'computed_bank_balance': computed_total,
        'statement_balance_given': statement_balance is not None,
        'ledger_balance': ledger, 'unbooked': unbooked, 'unbooked_total': unbooked_total,
        'adjusted_ledger': adjusted, 'gap': gap,
        'unmatched': unmatched, 'unmatched_total': unmatched_total, 'older_unmatched': older,
        'unexplained': unexplained,
        'reconciled': gap is not None and abs(gap) < 0.005,
    }
