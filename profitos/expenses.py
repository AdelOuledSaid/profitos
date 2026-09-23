"""Notes de frais et indemnités kilométriques.

Réutilise les fondations déjà construites pour les achats fournisseurs :
même principe d'extraction OCR (texte natif d'abord, IA en repli), même
logique de workflow de validation, même moteur d'écritures comptables
(profitos/accounting.py). Ce module ne réinvente que ce qui est vraiment
spécifique aux notes de frais : les catégories de dépense employé et le
calcul du barème kilométrique.
"""
from datetime import datetime, date

from profitos.accounting import create_entry, AccountingError

# --- Catégories de notes de frais -> compte de charge par défaut ----------
DEFAULT_EXPENSE_REPORT_CATEGORIES = [
    ('repas', 'Repas', '625700'),
    ('transport', 'Transport', '625100'),
    ('hebergement', 'Hébergement', '625100'),
    ('fournitures', 'Fournitures', '606800'),
    ('kilometrique', 'Indemnité kilométrique', '625100'),
    ('autre', 'Autre', '625600'),
]
EXPENSE_REPORT_CATEGORY_LABELS = {k: label for k, label, _ in DEFAULT_EXPENSE_REPORT_CATEGORIES}
EXPENSE_REPORT_CATEGORY_ACCOUNTS = {k: acct for k, _, acct in DEFAULT_EXPENSE_REPORT_CATEGORIES}

# --- Barème kilométrique fiscal (structure officielle à 3 tranches) -------
# Le barème français réel n'est pas un taux fixe : pour chaque puissance
# fiscale, la distance parcourue dans l'année est découpée en 3 tranches
# avec des formules différentes :
#   - jusqu'à 5 000 km  : distance × taux_a
#   - de 5 001 à 20 000 km : (distance × taux_b) + montant_fixe
#   - au-delà de 20 000 km : distance × taux_c
# ProfitOS fournit la structure ; les taux réels (taux_a/b/c et montant
# fixe) sont saisis par l'utilisateur, jamais devinés — voir seed ci-dessous.
MILEAGE_FISCAL_POWERS = ['3', '4', '5', '6', '7+']
MILEAGE_BRACKETS = ['up_to_5000', '5001_to_20000', 'over_20000']
MILEAGE_BRACKET_LABELS = {
    'up_to_5000': "Jusqu'à 5 000 km",
    '5001_to_20000': "De 5 001 à 20 000 km",
    'over_20000': "Au-delà de 20 000 km",
}


def seed_mileage_rate_table(conn):
    """Crée une ligne (taux à 0, à configurer) pour chaque combinaison
    puissance fiscale × tranche, si elle n'existe pas déjà. Idempotent."""
    for fp in MILEAGE_FISCAL_POWERS:
        for bracket in MILEAGE_BRACKETS:
            conn.execute(
                'INSERT OR IGNORE INTO mileage_rate_table(fiscal_power,bracket,rate_per_km,fixed_amount) VALUES(?,?,0,0)',
                (fp, bracket),
            )
    conn.commit()


class MileageRateNotConfigured(ValueError):
    """Levée quand le taux kilométrique demandé n'a pas encore été
    configuré (toujours à 0) — pour ne jamais calculer silencieusement un
    remboursement à 0 € qui aurait l'air d'un vrai résultat."""


def compute_mileage_allowance(conn, fiscal_power, km):
    """Calcule l'indemnité kilométrique pour une puissance fiscale et une
    distance données, selon la structure officielle à 3 tranches. Lève
    MileageRateNotConfigured si le taux de la tranche concernée est encore
    à 0 (non configuré) — jamais de calcul silencieux à 0 €."""
    km = float(km or 0)
    if km <= 0:
        raise ValueError("La distance parcourue doit être positive.")
    if km <= 5000:
        bracket = 'up_to_5000'
    elif km <= 20000:
        bracket = '5001_to_20000'
    else:
        bracket = 'over_20000'
    row = conn.execute(
        'SELECT rate_per_km,fixed_amount FROM mileage_rate_table WHERE fiscal_power=? AND bracket=?',
        (str(fiscal_power), bracket),
    ).fetchone()
    if not row or not row['rate_per_km']:
        raise MileageRateNotConfigured(
            f"Le barème kilométrique pour {fiscal_power} CV / {MILEAGE_BRACKET_LABELS[bracket]} "
            "n'est pas encore configuré (taux à 0)."
        )
    amount = km * row['rate_per_km'] + (row['fixed_amount'] or 0)
    return round(amount, 2)


def _entry_already_exists(conn, source_type, source_id):
    row = conn.execute(
        'SELECT id FROM accounting_entries WHERE source_type=? AND source_id=? LIMIT 1',
        (source_type, source_id),
    ).fetchone()
    return row is not None


def generate_expense_report_entry(conn, report):
    """Génère l'écriture comptable (journal OD) quand une note de frais est
    approuvée : un débit par compte de charge concerné (regroupé par
    catégorie des lignes de la note), au crédit du compte 421000 (Personnel
    - rémunérations dues) pour le total — l'entreprise doit cette somme à
    l'employé tant qu'elle n'est pas remboursée. Idempotent."""
    if _entry_already_exists(conn, 'expense_report', report['id']):
        return None
    lines = conn.execute(
        'SELECT category,SUM(amount) as total FROM expense_report_lines WHERE report_id=? GROUP BY category',
        (report['id'],),
    ).fetchall()
    if not lines:
        raise AccountingError("Cette note de frais n'a aucune ligne de dépense.")
    total = 0.0
    entry_lines = []
    for l in lines:
        account = EXPENSE_REPORT_CATEGORY_ACCOUNTS.get(l['category'], '606800')
        entry_lines.append({'account_code': account, 'debit': l['total'],
                             'label': EXPENSE_REPORT_CATEGORY_LABELS.get(l['category'], l['category'])})
        total += l['total']
    entry_lines.append({'account_code': '421000', 'credit': total, 'auxiliary_name': report['employee_email']})
    return create_entry(
        conn, 'OD', date.today(),
        f"Note de frais {report['employee_email']} — {report['period_label'] or ''}".strip(),
        entry_lines, source_type='expense_report', source_id=report['id'],
    )


def generate_expense_reimbursement_entry(conn, report):
    """Génère l'écriture de remboursement (journal BQ) quand une note de
    frais est marquée remboursée : Personnel (421) au débit (la dette envers
    l'employé est soldée), Banque (512) au crédit. Idempotent."""
    source_type = 'expense_report_reimbursement'
    if _entry_already_exists(conn, source_type, report['id']):
        return None
    total = conn.execute(
        'SELECT SUM(amount) t FROM expense_report_lines WHERE report_id=?', (report['id'],)
    ).fetchone()['t'] or 0
    return create_entry(
        conn, 'BQ', date.today(),
        f"Remboursement note de frais {report['employee_email']}",
        [
            {'account_code': '421000', 'debit': total, 'auxiliary_name': report['employee_email']},
            {'account_code': '512000', 'credit': total},
        ],
        source_type=source_type, source_id=report['id'],
    )
