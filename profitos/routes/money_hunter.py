from profitos.runtime import *
from profitos.feature_access import requires_paid_plan


def _safe_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _confidence(score):
    try:
        return max(0, min(100, int(score or 0)))
    except (TypeError, ValueError):
        return 0


def build_money_brief():
    """Construit le brief Money Hunter à partir des données du tenant courant.

    V1.6 reste volontairement déterministe et explicable : aucun montant n'est
    inventé. Les recommandations utilisent uniquement les factures, signaux SAVE
    et opportunités GROW déjà stockés dans ProfitOS.
    """
    c = cx()
    try:
        invoices = c.execute(
            "SELECT id,invoice_number,customer,MAX(amount-paid_amount,0) outstanding,"
            "days_overdue,score,kind,due_date FROM invoices "
            "WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 "
            "ORDER BY score DESC"
        ).fetchall()
        saves = c.execute(
            "SELECT id,title,value,score,details FROM opportunities "
            "WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC"
        ).fetchall()
        grows = c.execute(
            "SELECT id,title,value,score,buyer,deadline FROM opportunities "
            "WHERE type='GROW' AND status='OPEN' ORDER BY score DESC"
        ).fetchall()
    finally:
        c.close()

    recommendations = []
    recover_total = 0.0
    recover_expected = 0.0
    save_total = 0.0
    save_expected = 0.0
    grow_pipeline = 0.0

    for row in invoices:
        amount = _safe_float(row['outstanding'])
        score = _confidence(row['score'])
        expected = amount * score / 100.0
        recover_total += amount
        recover_expected += expected
        recommendations.append({
            'kind': 'RECOVER',
            'title': f"Relancer {row['customer']}",
            'detail': f"Facture #{row['invoice_number']} · {row['days_overdue']} jours de retard",
            'amount': amount,
            'confidence': score,
            'expected': expected,
            'priority': expected,
            'url_kind': 'RECOVER',
            'item_id': row['id'],
            'action': 'Relancer maintenant' if row['days_overdue'] >= 7 else 'Surveiller et préparer la relance',
        })

    for row in saves:
        amount = _safe_float(row['value'])
        score = _confidence(row['score'])
        expected = amount * score / 100.0
        save_total += amount
        save_expected += expected
        recommendations.append({
            'kind': 'SAVE',
            'title': row['title'],
            'detail': row['details'] or 'Économie potentielle détectée par ProfitOS',
            'amount': amount,
            'confidence': score,
            'expected': expected,
            # SAVE est généralement annualisé : mensualisation pour comparer la priorité cash.
            'priority': expected / 12.0,
            'url_kind': 'SAVE',
            'item_id': row['id'],
            'action': 'Vérifier la source et décider',
        })

    for row in grows:
        amount = _safe_float(row['value'])
        score = _confidence(row['score'])
        if amount > 0:
            grow_pipeline += amount
        recommendations.append({
            'kind': 'GROW',
            'title': row['title'],
            'detail': (row['buyer'] or 'Acheteur non précisé') + (f" · échéance {row['deadline']}" if row['deadline'] else ''),
            'amount': amount,
            'confidence': score,
            'expected': 0.0,
            # Sans valeur de marché fiable, le score reste un signal et non un montant inventé.
            'priority': score * 10.0,
            'url_kind': 'GROW',
            'item_id': row['id'],
            'action': 'Évaluer le GO / NO-GO',
        })

    recommendations.sort(key=lambda x: (x['priority'], x['confidence']), reverse=True)
    top = recommendations[:8]

    urgent_recover = sum(1 for r in invoices if _confidence(r['score']) >= 80)
    high_save = sum(1 for r in saves if _confidence(r['score']) >= 80)
    strong_grow = sum(1 for r in grows if _confidence(r['score']) >= 75)

    return {
        'recover_total': round(recover_total, 2),
        'recover_expected': round(recover_expected, 2),
        'save_total': round(save_total, 2),
        'save_expected': round(save_expected, 2),
        'grow_pipeline': round(grow_pipeline, 2),
        'grow_count': len(grows),
        'money_identified': round(recover_total + save_total, 2),
        'expected_value': round(recover_expected + save_expected, 2),
        'urgent_recover': urgent_recover,
        'high_save': high_save,
        'strong_grow': strong_grow,
        'recommendations': top,
        'recommendation_count': len(recommendations),
    }


def register(app):
    @app.route('/money-hunter')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    def money_hunter():
        brief = build_money_brief()
        log_activity('MONEY_HUNTER_VIEW','Consultation du Money Brief')
        return render_template('money_hunter.html', brief=brief)
