"""Module Emprunts — échéancier d'amortissement à échéances constantes
(méthode française standard) et comptabilisation des remboursements.

Compte 164000 (Emprunts auprès des établissements de crédit) pour le
capital restant dû, 661000 (Charges d'intérêts) pour la part d'intérêts,
512000 (Banque) au crédit lors du remboursement effectif.
"""
from datetime import date

from profitos.accounting import create_entry, AccountingError

LOAN_ACCOUNT = '164000'
INTEREST_ACCOUNT = '661000'
BANK_ACCOUNT = '512000'


def _add_months(d, months):
    """Ajoute un nombre de mois à une date, sans dépendance externe
    (python-dateutil n'est qu'une dépendance transitive non déclarée de
    pandas — pas fiable à utiliser directement après l'incident d'import
    manquant de cette session). Gère la fin de mois correctement (ex: 31
    janvier + 1 mois -> 28/29 février, jamais une date invalide)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    day = min(d.day, last_day)
    return date(year, month, day)


def compute_monthly_payment(principal, annual_rate, duration_months):
    """Mensualité constante, méthode française standard. Un taux à 0 (prêt
    sans intérêt, ex. prêt d'honneur) est géré à part — division simple du
    capital sur la durée, pas de formule actuarielle."""
    if duration_months <= 0:
        raise ValueError("La durée doit être d'au moins un mois.")
    if annual_rate <= 0:
        return round(principal / duration_months, 2)
    i = annual_rate / 12 / 100
    payment = principal * i / (1 - (1 + i) ** (-duration_months))
    return round(payment, 2)


def build_amortization_schedule(principal, annual_rate, start_date, duration_months):
    """Construit l'échéancier complet : une ligne par mois avec la
    répartition capital/intérêts et le solde restant dû. La dernière
    échéance absorbe l'écart d'arrondi (quelques centimes) pour que le
    solde final tombe exactement à zéro — jamais un reliquat qui traîne."""
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    monthly_payment = compute_monthly_payment(principal, annual_rate, duration_months)
    i = annual_rate / 12 / 100 if annual_rate > 0 else 0
    balance = round(principal, 2)
    schedule = []
    for k in range(1, duration_months + 1):
        due_date = _add_months(start_date, k)
        interest = round(balance * i, 2) if i else 0.0
        capital = round(monthly_payment - interest, 2)
        if k == duration_months:
            # Absorbe l'écart d'arrondi cumulé sur la dernière échéance.
            capital = balance
        balance = round(balance - capital, 2)
        schedule.append({
            'installment_number': k, 'due_date': due_date.isoformat(),
            'capital_amount': capital, 'interest_amount': interest,
            'remaining_balance': max(balance, 0.0),
        })
    return schedule, monthly_payment


def create_loan(conn, lender_name, principal_amount, annual_rate, start_date, duration_months,
                 entity_id=None, notes='', created_by=None):
    """Crée un emprunt et son échéancier complet en une fois."""
    schedule, monthly_payment = build_amortization_schedule(
        principal_amount, annual_rate, start_date, duration_months
    )
    conn.execute(
        """INSERT INTO loans(entity_id,lender_name,principal_amount,annual_rate,start_date,
           duration_months,monthly_payment,status,notes,created_at,created_by)
           VALUES(?,?,?,?,?,?,?,'active',?,?,?)""",
        (entity_id, lender_name, principal_amount, annual_rate,
         start_date if isinstance(start_date, str) else start_date.isoformat(),
         duration_months, monthly_payment, notes, __import__('datetime').datetime.utcnow().isoformat(), created_by),
    )
    conn.commit()
    loan_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    for row in schedule:
        conn.execute(
            """INSERT INTO loan_installments
               (loan_id,installment_number,due_date,capital_amount,interest_amount,remaining_balance)
               VALUES(?,?,?,?,?,?)""",
            (loan_id, row['installment_number'], row['due_date'],
             row['capital_amount'], row['interest_amount'], row['remaining_balance']),
        )
    conn.commit()
    # Écriture de déblocage des fonds — sans elle, 164000 ne serait jamais
    # crédité initialement et ne recevrait que les débits des remboursements,
    # ce qui ferait dériver le compte en négatif au lieu de refléter la
    # dette réelle. Bien réel, comptabilisé dès la création de l'emprunt.
    create_entry(
        conn, 'BQ', start_date,
        f"Déblocage des fonds — emprunt {lender_name}",
        [
            {'account_code': BANK_ACCOUNT, 'debit': principal_amount},
            {'account_code': LOAN_ACCOUNT, 'credit': principal_amount},
        ],
        source_type='loan_drawdown', source_id=loan_id,
        entity_id=entity_id, created_by=created_by,
    )
    return loan_id


def pay_installment(conn, installment_id, entity_id=None, created_by=None):
    """Enregistre le règlement effectif d'une échéance : débite le capital
    remboursé (164000) et les intérêts (661000), crédite la banque
    (512000). Idempotent — refuse si déjà payée, jamais une double
    écriture pour la même échéance."""
    installment = conn.execute('SELECT * FROM loan_installments WHERE id=?', (installment_id,)).fetchone()
    if not installment:
        raise AccountingError(f"Échéance introuvable : id={installment_id!r}.")
    if installment['paid']:
        raise AccountingError("Cette échéance a déjà été réglée.")
    loan = conn.execute('SELECT * FROM loans WHERE id=?', (installment['loan_id'],)).fetchone()

    lines = [{'account_code': LOAN_ACCOUNT, 'debit': installment['capital_amount']}]
    if installment['interest_amount']:
        lines.append({'account_code': INTEREST_ACCOUNT, 'debit': installment['interest_amount']})
    total = installment['capital_amount'] + installment['interest_amount']
    lines.append({'account_code': BANK_ACCOUNT, 'credit': total})

    entry_id = create_entry(
        conn, 'BQ', date.today(),
        f"Échéance {installment['installment_number']}/{loan['duration_months']} — {loan['lender_name']}",
        lines, source_type='loan_installment', source_id=installment_id,
        entity_id=entity_id, created_by=created_by,
    )
    conn.execute(
        "UPDATE loan_installments SET paid=1,entry_id=?,paid_at=? WHERE id=?",
        (entry_id, __import__('datetime').datetime.utcnow().isoformat(), installment_id),
    )
    conn.commit()

    remaining_unpaid = conn.execute(
        "SELECT COUNT(*) n FROM loan_installments WHERE loan_id=? AND paid=0", (loan['id'],)
    ).fetchone()['n']
    if remaining_unpaid == 0:
        conn.execute("UPDATE loans SET status='paid_off' WHERE id=?", (loan['id'],))
        conn.commit()
    return entry_id
