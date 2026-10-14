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
