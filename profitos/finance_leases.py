"""Module Crédit-bail — traitement français (hors bilan pour le preneur,
contrairement à IFRS 16). Chaque redevance est une charge (612000), pas une
dette. L'engagement restant (total des redevances futures non réglées) doit
être mentionné en annexe des comptes annuels — voir remaining_commitment().

Si l'option d'achat est levée en fin de contrat, le bien devient alors une
vraie immobilisation ordinaire (liée à fixed_assets via source_type=
'finance_lease'), à amortir normalement à partir de cette date.
"""
from datetime import datetime

from profitos.accounting import create_entry, AccountingError
from profitos.loans import _add_months

LEASE_EXPENSE_ACCOUNT = '612000'
BANK_ACCOUNT = '512000'

PERIODICITY_MONTHS = {'monthly': 1, 'quarterly': 3, 'annual': 12}


def create_finance_lease(conn, lessor_name, asset_description, asset_value, redevance_amount,
                          periodicity, start_date, duration_months, purchase_option_amount=0,
                          entity_id=None, notes='', created_by=None):
    """Crée un contrat de crédit-bail et son échéancier de redevances.
    periodicity : 'monthly', 'quarterly' ou 'annual' — détermine
    l'intervalle entre deux redevances, pas la durée totale du contrat
    (toujours exprimée en mois)."""
    if periodicity not in PERIODICITY_MONTHS:
        raise AccountingError(f"Périodicité inconnue : {periodicity!r} (attendu monthly, quarterly ou annual).")
    step = PERIODICITY_MONTHS[periodicity]
    if duration_months % step != 0:
        raise AccountingError(
            f"La durée ({duration_months} mois) doit être un multiple de la périodicité choisie ({step} mois)."
        )
    if isinstance(start_date, str):
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
    else:
        start_date_obj = start_date

    conn.execute(
        """INSERT INTO finance_leases(entity_id,lessor_name,asset_description,asset_value,
           redevance_amount,periodicity,start_date,duration_months,purchase_option_amount,
           status,notes,created_at,created_by)
           VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
        (entity_id, lessor_name, asset_description, asset_value, redevance_amount, periodicity,
         start_date_obj.isoformat(), duration_months, purchase_option_amount, notes,
         datetime.utcnow().isoformat(), created_by),
    )
    conn.commit()
    lease_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    n_payments = duration_months // step
    for k in range(1, n_payments + 1):
        due_date = _add_months(start_date_obj, k * step)
        conn.execute(
            'INSERT INTO finance_lease_payments(lease_id,payment_number,due_date,amount) VALUES(?,?,?,?)',
            (lease_id, k, due_date.isoformat(), redevance_amount),
        )
    conn.commit()
    return lease_id


def pay_lease_payment(conn, payment_id, entity_id=None, created_by=None):
    """Enregistre le règlement d'une redevance de crédit-bail : débite la
    charge (612000), crédite la banque (512000). Idempotent — refuse si
    déjà réglée."""
    payment = conn.execute('SELECT * FROM finance_lease_payments WHERE id=?', (payment_id,)).fetchone()
    if not payment:
        raise AccountingError(f"Redevance introuvable : id={payment_id!r}.")
    if payment['paid']:
        raise AccountingError("Cette redevance a déjà été réglée.")
    lease = conn.execute('SELECT * FROM finance_leases WHERE id=?', (payment['lease_id'],)).fetchone()

    entry_id = create_entry(
        conn, 'BQ', datetime.utcnow().date(),
        f"Redevance {payment['payment_number']} — crédit-bail {lease['lessor_name']} ({lease['asset_description']})",
        [
            {'account_code': LEASE_EXPENSE_ACCOUNT, 'debit': payment['amount']},
            {'account_code': BANK_ACCOUNT, 'credit': payment['amount']},
        ],
        source_type='finance_lease_payment', source_id=payment_id,
        entity_id=entity_id, created_by=created_by,
    )
    conn.execute(
        "UPDATE finance_lease_payments SET paid=1,entry_id=?,paid_at=? WHERE id=?",
        (entry_id, datetime.utcnow().isoformat(), payment_id),
    )
    conn.commit()

    remaining = conn.execute(
        "SELECT COUNT(*) n FROM finance_lease_payments WHERE lease_id=? AND paid=0", (lease['id'],)
    ).fetchone()['n']
    if remaining == 0 and lease['status'] == 'active':
        conn.execute("UPDATE finance_leases SET status='completed' WHERE id=?", (lease['id'],))
        conn.commit()
    return entry_id


def remaining_commitment(conn, lease_id):
    """Total des redevances futures non réglées — l'engagement hors bilan
    à mentionner dans l'annexe des comptes annuels pour ce contrat."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM finance_lease_payments WHERE lease_id=? AND paid=0",
        (lease_id,),
    ).fetchone()
    return row['t']


def total_remaining_commitment(conn, entity_id=None):
    """Total des engagements hors bilan de crédit-bail, tous contrats actifs
    confondus, pour l'entité donnée — la ligne à faire figurer en annexe."""
    ef = 'l.entity_id=?' if entity_id else 'l.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    row = conn.execute(
        f"""SELECT COALESCE(SUM(p.amount),0) t FROM finance_lease_payments p
            JOIN finance_leases l ON l.id=p.lease_id
            WHERE p.paid=0 AND l.status!='cancelled' AND {ef}""",
        ep,
    ).fetchone()
    return row['t']


def exercise_purchase_option(conn, lease_id, exercise_date, useful_life_years,
                              asset_account, depreciation_account, expense_account,
                              entity_id=None):
    """Lève l'option d'achat en fin de contrat : le bien devient une
    immobilisation ordinaire à part entière, amortie normalement à partir
    de cette date — pour la valeur de l'option d'achat, pas la valeur
    d'origine du bien (déjà couverte par les redevances passées). Refuse
    si le contrat a encore des redevances impayées ou si l'option a déjà
    été levée."""
    lease = conn.execute('SELECT * FROM finance_leases WHERE id=?', (lease_id,)).fetchone()
    if not lease:
        raise AccountingError(f"Contrat de crédit-bail introuvable : id={lease_id!r}.")
    if lease['status'] == 'option_exercised':
        raise AccountingError("L'option d'achat a déjà été levée sur ce contrat.")
    unpaid = conn.execute(
        "SELECT COUNT(*) n FROM finance_lease_payments WHERE lease_id=? AND paid=0", (lease_id,)
    ).fetchone()['n']
    if unpaid > 0:
        raise AccountingError(f"{unpaid} redevance(s) encore impayée(s) — règle-les avant de lever l'option d'achat.")
    if not lease['purchase_option_amount'] or lease['purchase_option_amount'] <= 0:
        raise AccountingError("Aucun montant de levée d'option n'a été renseigné pour ce contrat.")

    conn.execute(
        """INSERT INTO fixed_assets(label,asset_account,depreciation_account,expense_account,
           purchase_date,purchase_amount,useful_life_years,method,source_type,source_id,status,created_at)
           VALUES(?,?,?,?,?,?,?,'linear','finance_lease',?,'active',?)""",
        (f"{lease['asset_description']} (option levée — {lease['lessor_name']})",
         asset_account, depreciation_account, expense_account,
         str(exercise_date), lease['purchase_option_amount'], useful_life_years,
         lease_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    asset_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.execute("UPDATE finance_leases SET status='option_exercised' WHERE id=?", (lease_id,))
    conn.commit()
    return asset_id
