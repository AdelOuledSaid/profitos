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


def _clamp(value, low=0, high=100):
    return max(low, min(high, int(round(value))))


def _intelligence(kind, amount, confidence, days_overdue=0, deadline=None):
    """Score explicable V1.6.1 fondé uniquement sur les données ProfitOS."""
    amount = max(0.0, _safe_float(amount))
    confidence = _confidence(confidence)

    if kind == 'RECOVER':
        urgency = min(100, max(0, int(days_overdue or 0)) * 2)
        impact = min(100, amount / 100.0)  # 10 k€ atteint le plafond d'impact.
        priority = _clamp(0.45 * confidence + 0.35 * urgency + 0.20 * impact)
        if priority >= 80:
            level, action = 'URGENT', 'Préparer une relance aujourd’hui'
        elif priority >= 60:
            level, action = 'ÉLEVÉE', 'Relancer rapidement'
        elif priority >= 40:
            level, action = 'MOYENNE', 'Planifier une relance'
        else:
            level, action = 'FAIBLE', 'Surveiller la créance'
        why = f"{int(days_overdue or 0)} jours de retard · {amount:,.0f} € exposés · confiance {confidence}/100"
        return priority, level, action, why

    if kind == 'SAVE':
        # La valeur SAVE est annualisée : on compare son impact mensuel.
        monthly = amount / 12.0
        impact = min(100, monthly / 50.0)  # 5 k€/mois atteint le plafond.
        priority = _clamp(0.65 * confidence + 0.35 * impact)
        level = 'ÉLEVÉE' if priority >= 70 else ('MOYENNE' if priority >= 45 else 'FAIBLE')
        action = 'Vérifier la dépense et décider' if priority >= 45 else 'Garder sous surveillance'
        why = f"{amount:,.0f} € / an potentiels · confiance {confidence}/100"
        return priority, level, action, why

    # GROW : sans montant fiable, aucune valeur financière n'est fabriquée.
    priority = _clamp(confidence)
    level = 'ÉLEVÉE' if priority >= 75 else ('MOYENNE' if priority >= 50 else 'FAIBLE')
    action = 'Évaluer le GO / NO-GO'
    why = f"Match {confidence}/100" + (f" · échéance {deadline}" if deadline else '')
    return priority, level, action, why



def _payment_intelligence(customer, customer_stats, action_stats, base_confidence, days_overdue):
    """Indicateurs prédictifs prudents fondés sur l'historique réellement observé.

    Le score de risque n'est pas présenté comme une probabilité statistique : sans
    dates de paiement historiques complètes, V1.6.2 fournit un indicateur explicable.
    """
    stats = customer_stats.get(customer, {})
    total = int(stats.get('total') or 0)
    overdue_open = int(stats.get('overdue_open') or 0)
    paid = int(stats.get('paid') or 0)
    avg_overdue = _safe_float(stats.get('avg_overdue'))

    if total >= 5:
        evidence = 'OBSERVÉ'
        evidence_note = f'{total} factures historiques analysées'
    elif total >= 2:
        evidence = 'ESTIMÉ · PEU DE DONNÉES'
        evidence_note = f'{total} factures seulement : prudence'
    else:
        evidence = 'INSUFFISANT'
        evidence_note = 'historique client insuffisant'

    open_ratio = overdue_open / max(total, 1)
    lateness = min(100.0, max(0.0, _safe_float(days_overdue)) * 2.0)
    history = min(100.0, open_ratio * 100.0 + min(30.0, avg_overdue))
    risk = _clamp(0.60 * lateness + 0.40 * history)

    a = action_stats.get(customer, {})
    sent = int(a.get('sent') or 0)
    done = int(a.get('done') or 0)
    cancelled = int(a.get('cancelled') or 0)
    resolved = done + cancelled
    observed_success = (done / resolved) if resolved else None

    confidence_factor = _confidence(base_confidence) / 100.0
    if evidence == 'OBSERVÉ':
        data_factor = 1.0
    elif evidence.startswith('ESTIMÉ'):
        data_factor = 0.85
    else:
        data_factor = 0.70
    if observed_success is not None:
        learning_factor = 0.75 + 0.50 * observed_success
    else:
        learning_factor = 1.0
    recovery_factor = max(0.10, min(1.0, confidence_factor * data_factor * learning_factor))

    if risk >= 75:
        risk_level = 'ÉLEVÉ'
        risk_class = 'high'
        next_action = 'Relancer aujourd’hui et demander une date de paiement ferme'
    elif risk >= 50:
        risk_level = 'MODÉRÉ'
        risk_class = 'medium'
        next_action = 'Relancer sous 48 h et confirmer le statut de la facture'
    elif risk >= 30:
        risk_level = 'À SURVEILLER'
        risk_class = 'medium'
        next_action = 'Programmer un rappel et surveiller la créance'
    else:
        risk_level = 'FAIBLE'
        risk_class = 'low'
        next_action = 'Surveiller sans escalade immédiate'

    # Un score numérique ne doit pas être présenté comme fiable lorsque
    # l'historique client est insuffisant. Le moteur le conserve en interne
    # pour le classement, tandis que l'interface affiche d'abord un niveau.
    show_risk_score = evidence != 'INSUFFISANT'

    learning_note = f'{sent} relance(s) envoyée(s)'
    if resolved:
        learning_note += f' · {done}/{resolved} action(s) clôturée(s) positivement'
    else:
        learning_note += ' · pas encore assez de résultats clôturés'

    return {
        'payment_risk_score': risk,
        'payment_risk_level': risk_level,
        'payment_risk_class': risk_class,
        'show_risk_score': show_risk_score,
        'evidence_level': evidence,
        'evidence_note': evidence_note,
        'next_best_action': next_action,
        'recovery_factor': recovery_factor,
        'learning_note': learning_note,
        'history_total': total,
        'history_paid': paid,
        'history_overdue_open': overdue_open,
    }

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
        customer_rows = c.execute(
            "SELECT customer,COUNT(*) total,"
            "SUM(CASE WHEN LOWER(COALESCE(status,''))='paid' THEN 1 ELSE 0 END) paid,"
            "SUM(CASE WHEN LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 THEN 1 ELSE 0 END) overdue_open,"
            "AVG(CASE WHEN days_overdue>0 THEN days_overdue ELSE NULL END) avg_overdue "
            "FROM invoices GROUP BY customer"
        ).fetchall()
        action_rows = c.execute(
            "SELECT i.customer,"
            "SUM(CASE WHEN a.status='SENT' THEN 1 ELSE 0 END) sent,"
            "SUM(CASE WHEN a.status='DONE' THEN 1 ELSE 0 END) done,"
            "SUM(CASE WHEN a.status='CANCELLED' THEN 1 ELSE 0 END) cancelled "
            "FROM actions a JOIN invoices i ON i.id=a.opportunity_id "
            "WHERE a.kind='RECOVER' GROUP BY i.customer"
        ).fetchall()
    finally:
        c.close()

    customer_stats = {r['customer']: dict(r) for r in customer_rows}
    action_stats = {r['customer']: dict(r) for r in action_rows}

    recommendations = []
    recover_total = 0.0
    recover_expected = 0.0
    save_total = 0.0
    save_expected = 0.0
    grow_pipeline = 0.0

    for row in invoices:
        amount = _safe_float(row['outstanding'])
        score = _confidence(row['score'])
        predictive = _payment_intelligence(
            row['customer'], customer_stats, action_stats, score, row['days_overdue']
        )
        expected = amount * predictive['recovery_factor']
        recover_total += amount
        recover_expected += expected
        priority_score, urgency_level, action, why = _intelligence(
            'RECOVER', amount, score, days_overdue=row['days_overdue']
        )
        recommendations.append({
            'kind': 'RECOVER',
            'title': f"Relancer {row['customer']}",
            'detail': f"Facture #{row['invoice_number']} · {row['days_overdue']} jours de retard",
            'amount': amount,
            'confidence': score,
            'expected': expected,
            'priority': priority_score,
            'priority_score': priority_score,
            'urgency_level': urgency_level,
            'why': why,
            'url_kind': 'RECOVER',
            'item_id': row['id'],
            'action': action,
            **predictive,
        })

    for row in saves:
        amount = _safe_float(row['value'])
        score = _confidence(row['score'])
        expected = amount * score / 100.0
        save_total += amount
        save_expected += expected
        priority_score, urgency_level, action, why = _intelligence('SAVE', amount, score)
        recommendations.append({
            'kind': 'SAVE',
            'title': row['title'],
            'detail': row['details'] or 'Économie potentielle détectée par ProfitOS',
            'amount': amount,
            'confidence': score,
            'expected': expected,
            'priority': priority_score,
            'priority_score': priority_score,
            'urgency_level': urgency_level,
            'why': why,
            'url_kind': 'SAVE',
            'item_id': row['id'],
            'action': action,
        })

    for row in grows:
        amount = _safe_float(row['value'])
        score = _confidence(row['score'])
        if amount > 0:
            grow_pipeline += amount
        priority_score, urgency_level, action, why = _intelligence(
            'GROW', amount, score, deadline=row['deadline']
        )
        recommendations.append({
            'kind': 'GROW',
            'title': row['title'],
            'detail': (row['buyer'] or 'Acheteur non précisé') + (f" · échéance {row['deadline']}" if row['deadline'] else ''),
            'amount': amount,
            'confidence': score,
            'expected': 0.0,
            # Sans valeur de marché fiable, le score reste un signal et non un montant inventé.
            'priority': priority_score,
            'priority_score': priority_score,
            'urgency_level': urgency_level,
            'why': why,
            'url_kind': 'GROW',
            'item_id': row['id'],
            'action': action,
        })

    recommendations.sort(key=lambda x: (x['priority'], x['confidence']), reverse=True)
    top = recommendations[:8]
    today_actions = [r for r in recommendations if r['priority_score'] >= 70][:5]
    if not today_actions and recommendations:
        today_actions = recommendations[:1]

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
        'today_actions': today_actions,
        'recommendation_count': len(recommendations),
        'predictive_recover_count': len(invoices),
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
