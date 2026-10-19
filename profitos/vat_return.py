"""Déclaration de TVA CA3 pré-remplie case par case et écriture de liquidation.

Les montants viennent des écritures de la période (comptes 4457x, 44562, 44566,
44567). L'écriture de liquidation (source « vat_liquidation ») solde ces comptes vers
44551 « TVA à décaisser » ou 44567 « Crédit de TVA à reporter » ; elle est exclue des
calculs pour que la déclaration reste lisible après liquidation.

ProfitOS ne télédéclare pas : les montants se reportent sur impots.gouv.fr (espace
professionnel) ou sont transmis par l'expert-comptable.
"""
from datetime import date, timedelta

from profitos.accounting import (AccountingError, VAT_COLLECTED_ACCOUNT, ca3_collected_by_rate,
                                 create_entry, _entry_already_exists)

LIQUIDATION_SOURCE = 'vat_liquidation'
DEDUCTIBLE_ASSETS = '445620'
DEDUCTIBLE_OTHER = '445660'
VAT_CREDIT = '445670'
VAT_PAYABLE = '445510'
NOT_LIQUIDATION = "COALESCE(e.source_type,'')<>'vat_liquidation'"


def _ef(entity_id):
    return ('e.entity_id=?', (entity_id,)) if entity_id else ('e.entity_id IS NULL', ())


def _movement(conn, account, date_from, date_to, entity_id):
    ef, ep = _ef(entity_id)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(l.debit-l.credit),0) n FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            WHERE l.account_code=? AND e.entry_date BETWEEN ? AND ? AND {ef} AND {NOT_LIQUIDATION}""",
        (account, str(date_from), str(date_to), *ep)).fetchone()
    return round(float(row['n'] or 0), 2)


def _balance_before(conn, account, day, entity_id):
    ef, ep = _ef(entity_id)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(l.debit-l.credit),0) n FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            WHERE l.account_code=? AND e.entry_date<? AND {ef}""",
        (account, str(day), *ep)).fetchone()
    return round(float(row['n'] or 0), 2)


def unliquidated_before(conn, date_from, entity_id=None):
    """Vrai si des comptes de TVA collectée ou déductible gardent un solde avant la période :
    une période antérieure n'a pas été liquidée et son crédit éventuel ne sera pas reporté."""
    for account in (VAT_COLLECTED_ACCOUNT, DEDUCTIBLE_ASSETS, DEDUCTIBLE_OTHER):
        if abs(_balance_before(conn, account, date_from, entity_id)) >= 0.01:
            return True
    return False


def ca3_form(conn, date_from, date_to, entity_id=None):
    """Cases de la CA3 (formulaire n° 3310-CA3) pour la période."""
    rates = ca3_collected_by_rate(conn, date_from, date_to, entity_id)
    base_taxable = round(sum(r['base'] for r in rates if r['rate'] is not None), 2)
    l16 = round(sum(r['vat'] for r in rates), 2)
    l19 = _movement(conn, DEDUCTIBLE_ASSETS, date_from, date_to, entity_id)
    l20 = _movement(conn, DEDUCTIBLE_OTHER, date_from, date_to, entity_id)
    # Crédit de la déclaration précédente (solde débiteur du 44567 avant la période).
    l22 = max(0.0, _balance_before(conn, VAT_CREDIT, date_from, entity_id))
    l23 = round(l19 + l20 + l22, 2)
    diff = round(l16 - l23, 2)
    l28 = diff if diff > 0 else 0.0
    l25 = -diff if diff < 0 else 0.0
    liquidated = _entry_already_exists(conn, LIQUIDATION_SOURCE, _source_id(date_to), entity_id)
    return {
        'date_from': str(date_from), 'date_to': str(date_to),
        'l01': base_taxable, 'rates': rates, 'l16': l16, 'l19': l19, 'l20': l20, 'l22': l22,
        'l23': l23, 'l25': l25, 'l27': l25, 'l28': l28, 'l32': l28,
        'liquidated': bool(liquidated),
    }


def _source_id(date_to):
    return int(str(date_to)[:10].replace('-', ''))


def generate_liquidation_entry(conn, date_from, date_to, entity_id=None, created_by=None):
    """Écriture de liquidation de la TVA au dernier jour de la période (journal OD) :
    débit 44571 (TVA collectée), crédit 44562 / 44566 (TVA déductible), crédit 44567 du
    crédit antérieur utilisé ; solde en 44551 (à décaisser) ou 44567 (crédit à reporter).
    Une seule liquidation par fin de période."""
    form = ca3_form(conn, date_from, date_to, entity_id)
    if form['liquidated']:
        raise AccountingError("La TVA de cette période a déjà été liquidée.")
    signed = []  # (compte, montant signé : positif = débit)
    signed.append((VAT_COLLECTED_ACCOUNT, form['l16']))
    signed.append((DEDUCTIBLE_ASSETS, -form['l19']))
    signed.append((DEDUCTIBLE_OTHER, -form['l20']))
    signed.append((VAT_CREDIT, -form['l22']))
    signed.append((VAT_PAYABLE, -form['l28']))
    signed.append((VAT_CREDIT, form['l25']))
    lines = []
    for account, amount in signed:
        amount = round(amount, 2)
        if abs(amount) < 0.005:
            continue
        lines.append({'account_code': account, 'debit': amount if amount > 0 else 0,
                      'credit': -amount if amount < 0 else 0})
    if len(lines) < 2:
        raise AccountingError("Aucune TVA à liquider sur cette période.")
    d_to = date.fromisoformat(str(date_to)[:10])
    label = f"Liquidation TVA du {date.fromisoformat(str(date_from)[:10]):%d/%m/%Y} au {d_to:%d/%m/%Y}"
    return create_entry(conn, 'OD', d_to, label, lines, source_type=LIQUIDATION_SOURCE,
                        source_id=_source_id(date_to), created_by=created_by, entity_id=entity_id,
                        piece_ref=f"CA3-{d_to:%Y%m}")


# --- CA12 (3517-S) : déclaration annuelle du régime simplifié ------------------------------
# Taux -> (ligne, code de la base, libellé)
CA12_RATE_LINES = {
    20.0: ('5A', '0207', 'Taux normal 20 %'),
    10.0: ('6C', '0151', 'Taux réduit 10 %'),
    5.5: ('06', '0105', 'Taux réduit 5,5 %'),
    2.1: ('09', '0950', 'Taux particulier 2,1 %'),
    8.5: ('07', '0201', 'Taux normal 8,5 % (DOM)'),
}
VAT_INSTALMENTS_ACCOUNT = '44581'
CA12_INSTALMENT_MIN = 1000


def ca12_form(conn, date_from, date_to, entity_id=None, acomptes=None):
    """Cases de la CA12 (formulaire 3517-S) pour l'exercice, avec la base et le
    calendrier des acomptes semestriels de l'exercice suivant (55 % en juillet, 40 % en
    décembre, dispense si la base est inférieure à 1 000 €)."""
    rates = ca3_collected_by_rate(conn, date_from, date_to, entity_id)
    lines, warnings = [], []
    for r in rates:
        if r['rate'] is None:
            continue
        spec = CA12_RATE_LINES.get(round(float(r['rate']), 1))
        if not spec:
            warnings.append(f"Taux de {r['rate']} % non ventilé automatiquement : à reporter à la main.")
            continue
        lines.append({'line': spec[0], 'base_code': spec[1], 'label': spec[2], 'rate': r['rate'],
                      'base': r['base'], 'vat': r['vat']})
    l16 = round(sum(x['vat'] for x in lines), 2)
    l19 = l16
    l20 = _movement(conn, DEDUCTIBLE_OTHER, date_from, date_to, entity_id)
    l21 = 0.0
    l22 = round(l20 + l21, 2)
    l23 = _movement(conn, DEDUCTIBLE_ASSETS, date_from, date_to, entity_id)
    l24 = max(0.0, _balance_before(conn, VAT_CREDIT, date_from, entity_id))
    l26 = round(l22 + l23 + l24, 2)
    l28 = round(max(0.0, l19 - l26), 2)
    l29 = round(max(0.0, l26 - l19), 2)
    if acomptes is None:
        acomptes = max(0.0, _movement(conn, VAT_INSTALMENTS_ACCOUNT, date_from, date_to, entity_id)) \
            if conn.execute('SELECT 1 FROM accounting_chart_of_accounts WHERE code LIKE ?', (VAT_INSTALMENTS_ACCOUNT + '%',)).fetchone() else 0.0
    l30 = round(float(acomptes or 0), 2)
    l33 = round(max(0.0, l28 - l30), 2)
    l34 = round(max(0.0, l30 - l28), 2)
    l35 = round(l29 + l34, 2)
    l57 = round(max(0.0, l16 - l22), 2)
    end = date.fromisoformat(str(date_to)[:10])
    nxt = end.year + 1
    instalments = []
    if l57 >= CA12_INSTALMENT_MIN:
        instalments = [{'date': date(nxt, 7, 15), 'label': 'Acompte de juillet (55 %)', 'amount': round(l57 * 0.55)},
                       {'date': date(nxt, 12, 15), 'label': 'Acompte de décembre (40 %)', 'amount': round(l57 * 0.40)}]
    from profitos.corporate_tax import second_working_day_after_may_1, liasse_deadline
    deadline = second_working_day_after_may_1(end.year + 1) if (end.month, end.day) == (12, 31) else liasse_deadline(end)[0]
    return {
        'date_from': str(date_from), 'date_to': str(date_to), 'lines': lines, 'warnings': warnings,
        'l16': l16, 'l19': l19, 'l20': l20, 'l21': l21, 'l22': l22, 'l23': l23, 'l24': l24, 'l26': l26,
        'l28': l28, 'l29': l29, 'l30': l30, 'l33': l33, 'l34': l34, 'l35': l35, 'l54': l33, 'l56': l33, 'l57': l57,
        'instalments': instalments, 'deadline': deadline,
    }


CA12_BOXES = [
    ('16', None, 'Total de la taxe due', 'l16'), ('19', None, 'Total de la TVA brute due', 'l19'),
    ('20', '0702', 'Déductions sur factures (biens et services)', 'l20'), ('22', None, 'Total des déductions (20 + 21)', 'l22'),
    ('23', '0703', 'TVA déductible sur immobilisations', 'l23'), ('24', '0058', 'Crédit antérieur non imputé et non remboursé', 'l24'),
    ('26', None, 'Total de la TVA déductible', 'l26'), ('28', '8900', 'TVA due', 'l28'), ('29', '0705', 'Crédit de TVA', 'l29'),
    ('30', '0018', 'Acomptes payés', 'l30'), ('33', None, 'Solde dû', 'l33'), ('34', None, 'Excédent de versement', 'l34'),
    ('35', '0020', 'Solde excédentaire', 'l35'), ('54', '8901', 'TVA nette due', 'l54'), ('56', '9992', 'Total à payer', 'l56'),
    ('57', None, "Base de calcul des acomptes de l'exercice suivant", 'l57'),
]
