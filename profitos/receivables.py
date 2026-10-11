"""Vue unique des créances clients ouvertes.

ProfitOS connaît deux sources de factures clients :

- les factures émises dans Facturation (table ``outgoing_invoices``) — règlements partiels
  (``outgoing_invoice_payments``) et avoirs émis (``outgoing_credit_notes``) déduits ;
- les factures importées depuis un fichier Excel/CSV (table ``invoices``).

Avant ce module, chaque écran lisait l'une OU l'autre : le tableau de bord ne voyait que les
factures importées, la page Créances que les factures émises, la prévision de trésorerie aussi.
Toutes les pages qui affichent un « reste à encaisser » passent désormais par
:func:`open_receivables`, ce qui garantit des totaux identiques partout.

Règles :
- une facture importée dont le numéro est celui d'une facture émise dans ProfitOS (même entité)
  n'est comptée qu'une fois : la facture ProfitOS fait foi (elle porte les règlements réels) ;
- le retard est calculé à la lecture (Europe/Paris), jamais lu d'une valeur stockée ;
- les retenues contractuelles importées sont échues à leur date de libération.
"""
from datetime import date, datetime

ALL_ENTITIES = object()

SOURCE_OUTGOING = 'outgoing'
SOURCE_IMPORTED = 'imported'
SOURCE_LABELS = {SOURCE_OUTGOING: 'Facture ProfitOS', SOURCE_IMPORTED: 'Importée'}


def paris_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('Europe/Paris')).date()
    except Exception:  # pragma: no cover - zoneinfo toujours présent en 3.9+
        return date.today()


def _f(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _d(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def _num_key(number):
    return str(number or '').strip().upper()


def score_for(remaining, days_overdue):
    """Même barème que pour les factures importées (montant + ancienneté)."""
    if days_overdue <= 0:
        return 0
    return min(100, min(40, int(remaining / 20000 * 40)) + min(45, int(days_overdue / 90 * 45)) + 15)


def _entity_sql(entity_id, column='entity_id'):
    if entity_id is ALL_ENTITIES:
        return '1=1', ()
    if entity_id is None:
        return f'{column} IS NULL', ()
    return f'{column}=?', (entity_id,)


def _hold(row):
    try:
        from profitos.routes.invoicing import _einvoice_hold
    except Exception:  # pragma: no cover
        return None
    try:
        return _einvoice_hold(row)
    except Exception:
        return None


def open_receivables(c, entity_id=ALL_ENTITIES, today=None, current=False):
    """Toutes les créances clients ouvertes (les deux sources), triées par échéance.

    ``entity_id`` : identifiant d'entité, ``None`` pour la société mère, ``ALL_ENTITIES`` pour
    tout le tenant. ``current=True`` : entité active de la session.

    Chaque ligne est un dict avec les clés historiques des factures importées (``customer``,
    ``outstanding``, ``days_overdue``, ``score``, ``kind``, ``due_date``…) plus ``source``,
    ``source_label``, ``total``, ``paid``, ``credited``, ``overdue`` et ``einvoice_hold``.
    """
    if current:
        from profitos.entities import current_entity_id
        entity_id = current_entity_id()
    today = today or paris_today()
    rows = []

    if entity_id is ALL_ENTITIES:
        ef, ep = '1=1', ()
    else:
        ef, ep = 'oi.entity_id IS ?', (entity_id,)
    outgoing = c.execute(
        "SELECT oi.*,"
        " COALESCE((SELECT SUM(p.amount) FROM outgoing_invoice_payments p WHERE p.invoice_id=oi.id"
        "  AND COALESCE(CAST(p.entity_id AS TEXT),'')=COALESCE(CAST(oi.entity_id AS TEXT),'')),0) AS paid_total,"
        " COALESCE((SELECT SUM(cn.total) FROM outgoing_credit_notes cn WHERE cn.original_invoice_id=oi.id"
        "  AND cn.status='issued' AND COALESCE(CAST(cn.entity_id AS TEXT),'')=COALESCE(CAST(oi.entity_id AS TEXT),'')),0) AS credited_total"
        f" FROM outgoing_invoices oi WHERE {ef} AND COALESCE(oi.status,'draft') NOT IN ('draft','cancelled')",
        ep).fetchall()
    known_numbers = set()
    for r in outgoing:
        known_numbers.add((r['entity_id'], _num_key(r['invoice_number'])))
        if r['status'] not in ('sent', 'partially_paid', 'overdue'):
            continue
        total = _f(r['total'])
        paid = _f(r['paid_total'])
        credited = _f(r['credited_total'])
        remaining = round(max(0.0, total - paid - credited), 2)
        if remaining <= 0.01:
            continue
        due = _d(r['due_date']) or _d(r['issue_date'])
        days = (today - due).days if due and due < today else 0
        keys = r.keys()
        rows.append({
            'source': SOURCE_OUTGOING, 'source_label': SOURCE_LABELS[SOURCE_OUTGOING],
            'id': r['id'], 'entity_id': r['entity_id'], 'invoice_number': r['invoice_number'],
            'customer': (r['client_name'] or 'Client sans nom').strip(),
            'customer_email': r['client_email'], 'customer_phone': None,
            'issue_date': r['issue_date'], 'due_date': r['due_date'],
            'retention_release_date': None, 'kind': 'STANDARD',
            'total': round(total, 2), 'amount': round(total, 2), 'paid': round(paid, 2),
            'credited': round(credited, 2), 'paid_amount': round(paid + credited, 2),
            'outstanding': remaining, 'remaining': remaining,
            'days_overdue': days, 'overdue': days > 0, 'score': score_for(remaining, days),
            'status': r['status'], 'created_at': r['created_at'],
            'einvoice_hold': _hold(r),
            'weinvoice_status': r['weinvoice_status'] if 'weinvoice_status' in keys else None,
            'client_name': r['client_name'], 'client_email': r['client_email'],
        })

    ef, ep = _entity_sql(entity_id)
    imported = c.execute(
        f"SELECT * FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND {ef}", ep).fetchall()
    for r in imported:
        if (r['entity_id'], _num_key(r['invoice_number'])) in known_numbers and _num_key(r['invoice_number']):
            continue  # déjà comptée via la facture émise dans ProfitOS
        total = _f(r['amount'])
        paid = _f(r['paid_amount'])
        remaining = round(max(0.0, total - paid), 2)
        if remaining <= 0.01:
            continue
        kind = r['kind'] or 'STANDARD'
        ref = _d(r['retention_release_date']) if kind == 'RETENTION' and r['retention_release_date'] else _d(r['due_date'])
        if ref is None:
            # Fichier importé sans date d'échéance : seul le retard déclaré à l'import est connu.
            days = max(0, int(r['days_overdue'] or 0))
        else:
            days = (today - ref).days if ref < today else 0
        rows.append({
            'source': SOURCE_IMPORTED, 'source_label': SOURCE_LABELS[SOURCE_IMPORTED],
            'id': r['id'], 'entity_id': r['entity_id'], 'invoice_number': r['invoice_number'],
            'customer': (r['customer'] or 'Client sans nom').strip(),
            'customer_email': r['customer_email'], 'customer_phone': r['customer_phone'],
            'issue_date': r['issue_date'], 'due_date': r['due_date'],
            'retention_release_date': r['retention_release_date'], 'kind': kind,
            'total': round(total, 2), 'amount': round(total, 2), 'paid': round(paid, 2),
            'credited': 0.0, 'paid_amount': round(paid, 2),
            'outstanding': remaining, 'remaining': remaining,
            'days_overdue': days, 'overdue': days > 0, 'score': score_for(remaining, days),
            'status': r['status'], 'created_at': r['created_at'],
            'einvoice_hold': None, 'weinvoice_status': None,
            'client_name': r['customer'], 'client_email': r['customer_email'],
        })
    rows.sort(key=lambda x: (x['due_date'] or '9999-12-31', x['invoice_number'] or ''))
    return rows


def overdue_only(rows):
    return [r for r in rows if r['overdue']]


def summarize(rows):
    """Totaux communs à toutes les pages (montants arrondis au centime)."""
    overdue = [r for r in rows if r['overdue']]
    return {
        'total_due': round(sum(r['outstanding'] for r in rows), 2),
        'overdue_due': round(sum(r['outstanding'] for r in overdue), 2),
        'count': len(rows),
        'overdue_count': len(overdue),
        'imported_due': round(sum(r['outstanding'] for r in rows if r['source'] == SOURCE_IMPORTED), 2),
        'outgoing_due': round(sum(r['outstanding'] for r in rows if r['source'] == SOURCE_OUTGOING), 2),
    }


def by_customer(rows):
    agg = {}
    for r in rows:
        a = agg.setdefault(r['customer'], {'client_name': r['customer'], 'total': 0.0, 'overdue': 0.0, 'count': 0})
        a['total'] += r['outstanding']
        a['count'] += 1
        if r['overdue']:
            a['overdue'] += r['outstanding']
    return sorted(agg.values(), key=lambda x: (x['overdue'], x['total']), reverse=True)


def detail_url(row):
    """Lien vers la fiche de la créance (facture ProfitOS ou facture importée)."""
    from flask import url_for
    if row['source'] == SOURCE_OUTGOING:
        return url_for('invoicing_detail', invoice_id=row['id'])
    return url_for('detail', kind='RECOVER', item_id=row['id'])
