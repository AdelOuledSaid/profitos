from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from .money_hunter import build_money_brief, _safe_float, _confidence, _clamp


def _margin_risk(c):
    readings=c.execute("SELECT * FROM price_index_readings WHERE index_name='BT01' ORDER BY reading_date ASC").fetchall()
    contracts=c.execute("SELECT * FROM fixed_price_contracts WHERE status='ACTIVE' ORDER BY signed_date DESC").fetchall()
    if len(readings)<2 or not contracts:
        return {'total':0.0,'contracts':0,'available':False,'note':'Ajoutez des contrats et au moins deux relevés BT01 dans Margin Watch.'}
    latest=readings[-1]
    total=0.0; exposed=0
    for ct in contracts:
        baseline=None
        for r in readings:
            if r['reading_date']<=ct['signed_date']:
                baseline=r
        if baseline is None:
            baseline=readings[0]
        if not baseline['value'] or not latest['value'] or baseline['id']==latest['id']:
            continue
        change=(latest['value']-baseline['value'])/baseline['value']
        if change>0:
            risk=_safe_float(ct['amount'])*(_safe_float(ct['materials_share_pct'])/100.0)*change
            total+=max(0.0,risk); exposed+=1
    return {'total':round(total,2),'contracts':exposed,'available':True,'note':'Risque indicatif lié à la hausse BT01 sur contrats à prix fixe.'}


def build_financial_brain():
    money=build_money_brief()
    c=cx()
    try:
        expenses=c.execute("SELECT COALESCE(SUM(amount),0) total,COUNT(*) n FROM expenses").fetchone()
        grows=c.execute("SELECT id,title,value,score,buyer,deadline FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC").fetchall()
        margin=_margin_risk(c)
    finally:
        c.close()

    recover=_safe_float(money['recover_total'])
    expected_recovery=_safe_float(money['recover_expected'])
    save_annual=_safe_float(money['save_total'])
    save_monthly=save_annual/12.0
    margin_risk=_safe_float(margin['total'])

    # Score de contrôle financier, et non solvabilité : uniquement les signaux connus.
    recovery_quality=(expected_recovery/recover*100.0) if recover>0 else 100.0
    overdue_penalty=min(45.0, recover/1000.0)
    margin_penalty=min(25.0, margin_risk/1000.0)
    savings_credit=min(15.0, save_monthly/500.0)
    control_score=_clamp(70 + 0.20*(recovery_quality-50) - overdue_penalty - margin_penalty + savings_credit)
    if control_score>=75: control_level='SOLIDE'
    elif control_score>=55: control_level='À SURVEILLER'
    else: control_level='SOUS TENSION'

    decisions=[]
    for r in money['recommendations']:
        if r['kind']=='RECOVER':
            impact=_safe_float(r['amount'])
            reason=f"{impact:,.0f} € de cash exposé · {r.get('detail','')}"
            action=r.get('next_best_action') or r.get('action')
        elif r['kind']=='SAVE':
            impact=_safe_float(r['amount'])/12.0
            reason=f"Économie potentielle annualisée de {_safe_float(r['amount']):,.0f} €"
            action=r.get('action')
        else:
            impact=0.0
            reason='Opportunité commerciale à qualifier financièrement avant engagement.'
            action='Compléter la valeur, les coûts et le besoin de trésorerie du marché'
        decisions.append({**r,'financial_impact':round(impact,2),'brain_reason':reason,'brain_action':action})
    decisions.sort(key=lambda x:(x['priority_score'],x['financial_impact']),reverse=True)

    grow_checks=[]
    for g in grows[:6]:
        value=_safe_float(g['value']); score=_confidence(g['score'])
        missing=['cash disponible','coûts prévisionnels / marge']
        if value<=0: missing.insert(0,'valeur du marché')
        grow_checks.append({
            'id':g['id'],'title':g['title'],'buyer':g['buyer'],'value':value,'score':score,
            'decision':'À QUALIFIER','missing':missing,
            'reason':'ProfitOS refuse un GO financier tant que les données de capacité financière sont incomplètes.'
        })

    data_gaps=['Cash bancaire disponible non persisté dans ProfitOS.']
    if not margin['available']: data_gaps.append('Risque de marge incomplet : historique BT01/contrats insuffisant.')
    if not grows: data_gaps.append('Aucune opportunité GROW ouverte à tester.')

    return {
        'money':money,
        'control_score':control_score,'control_level':control_level,
        'recover_exposure':round(recover,2),'expected_recovery':round(expected_recovery,2),
        'save_monthly':round(save_monthly,2),'margin_risk':round(margin_risk,2),'margin':margin,
        'expense_total':round(_safe_float(expenses['total']),2),'expense_count':int(expenses['n'] or 0),
        'capital_at_risk':round(recover+margin_risk,2),
        'decisions':decisions[:5],'best_decision':decisions[0] if decisions else None,
        'grow_checks':grow_checks,'data_gaps':data_gaps,
        'is_partial':True,
    }


def register(app):
    @app.route('/financial-brain')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    def financial_brain():
        brain=build_financial_brain()
        log_activity('FINANCIAL_BRAIN_VIEW','Consultation du Financial Brain')
        return render_template('financial_brain.html',brain=brain)
