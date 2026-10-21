"""« Votre semaine » (v472) : ce qui demande une action dans les jours qui viennent, en tête
du rapport hebdomadaire du lundi.

- encaissements : factures échues (total et nombre) et factures arrivant à échéance sous
  7 jours ;
- paiements : factures fournisseurs à régler sous 7 jours (reste à payer) ;
- fiscalité : déclarations à déposer sous 14 jours (non encore justifiées) ;
- veille : indicateurs de l'entreprise en alerte.

Chaque bloc est calculé indépendamment : une erreur dans l'un (module non souscrit, donnée
manquante) n'empêche pas les autres d'être envoyés.
"""
import logging
from datetime import date, timedelta

log = logging.getLogger('profitos')


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        log.exception('weekly overview: bloc ignoré')
        return default


def _receivables(c, today):
    from profitos.receivables import open_receivables
    rows = open_receivables(c)
    overdue = [r for r in rows if r['overdue']]
    soon = []
    for r in rows:
        if r['overdue'] or not r.get('due_date'):
            continue
        try:
            due = date.fromisoformat(str(r['due_date'])[:10])
        except ValueError:
            continue
        if today <= due <= today + timedelta(days=7):
            soon.append(r)
    return {'overdue_total': round(sum(r['outstanding'] for r in overdue), 2), 'overdue_count': len(overdue),
            'soon_total': round(sum(r['outstanding'] for r in soon), 2), 'soon_count': len(soon)}


def _payables(c, today):
    rows = c.execute("""SELECT p.id, p.entity_id, p.total, p.due_date,
                               COALESCE((SELECT SUM(amount) FROM purchase_invoice_payments x
                                         WHERE x.purchase_invoice_id=p.id), 0) AS paid
                        FROM purchase_invoices p
                        WHERE p.status NOT IN ('paid','cancelled') AND p.due_date IS NOT NULL AND p.due_date<=?""",
                     ((today + timedelta(days=7)).isoformat(),)).fetchall()
    due = [max(0.0, float(r['total'] or 0) - float(r['paid'] or 0)) for r in rows]
    due = [x for x in due if x > 0.005]
    return {'total': round(sum(due), 2), 'count': len(due)}


def _fiscal(c, today):
    from profitos.automation import upcoming_deadlines
    # Échéances à venir seulement ; une TVA sans montant calculé (aucune écriture) n'est pas rappelée ici.
    items = [i for i in upcoming_deadlines(c, None, today, horizon_days=14)
             if not i.get('deposited') and i['date'] >= today and (i.get('amount') or i.get('type') != 'TVA_CA3')]
    return [{'date': i['date'], 'label': i['label'], 'form': i.get('form'), 'amount': i.get('amount')}
            for i in sorted(items, key=lambda x: x['date'])][:6]


def _watch(c):
    from profitos import market_watch as MW
    from profitos.runtime import auth_cx
    settings = MW.load_settings(c, None)
    if not settings.get('sector') and settings.get('series') is None and not settings.get('custom'):
        return []                                  # veille jamais configurée : rien à signaler
    ac = auth_cx()
    try:
        out = []
        for code in MW.series_for(settings):
            defn = MW.definition(code, settings.get('custom'))
            if not defn:
                continue
            st = MW.stats(MW.observations(ac, code), defn['unit'])
            alerts = MW.alerts(st, settings)
            if alerts:
                out.append({'label': defn['label'], 'alerts': alerts, 'period': st['label']})
        return out
    finally:
        ac.close()


def compute(c, today=None, accounting=True):
    from profitos.accounting import paris_today
    today = today or paris_today()
    data = {
        'receivables': _safe(lambda: _receivables(c, today), None),
        'payables': _safe(lambda: _payables(c, today), None),
        'fiscal': _safe(lambda: _fiscal(c, today), []) if accounting else [],
        'watch': _safe(lambda: _watch(c), []),
        'documents': _safe(lambda: __import__('profitos.document_requests', fromlist=['x']).open_count(c), 0) if accounting else 0,
    }
    r, p = data['receivables'] or {}, data['payables'] or {}
    data['has_signal'] = bool(r.get('overdue_count') or r.get('soon_count') or p.get('count') or data['fiscal'] or data['watch']
                              or data['documents'])
    return data
