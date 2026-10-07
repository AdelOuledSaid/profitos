from datetime import date, datetime, timedelta

from profitos.runtime import *
from profitos.feature_access import requires_paid_plan, requires_feature
from .money_hunter import _safe_float, _confidence


def _iso_date(value):
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _cash_settings(c):
    from profitos.entities import get_cash_balance, current_entity_id
    balance=get_cash_balance(c, current_entity_id())
    return {'cash_balance':balance['cash_balance'],'cash_as_of':balance['cash_as_of'],'updated_at':None}


def _curve_points(values, width=920, height=220, pad=18):
    """Transforme une série journalière en points SVG, sans JavaScript inline."""
    if not values:
        return ''
    lo=min(values); hi=max(values)
    span=max(hi-lo,1.0)
    usable_w=width-2*pad; usable_h=height-2*pad
    pts=[]
    for i,value in enumerate(values):
        x=pad + usable_w*(i/max(len(values)-1,1))
        y=pad + usable_h*(hi-value)/span
        pts.append(f"{x:.1f},{y:.1f}")
    return ' '.join(pts)


def _simulate_curve(cash, daily_burn, receivables, scheduled_outflows=None, mode='probable', top_delay=None):
    """Courbe 0..90 j. Les hypothèses restent explicites et déterministes."""
    factors={'prudent':0.55,'probable':1.0,'optimiste':1.12}
    factor=factors.get(mode,1.0)
    events={}
    outflow_events={}
    for item in (scheduled_outflows or []):
        day=max(0,min(90,int(item.get('day',0))))
        if day>0:
            outflow_events[day]=outflow_events.get(day,0.0)+_safe_float(item.get('amount'))
    top=receivables[0] if receivables else None
    for idx,r in enumerate(receivables):
        if idx==0 and top_delay is not None:
            if top_delay < 0: # -1 = jamais sur l'horizon
                continue
            day=max(0,min(90,int(top_delay)))
            amount=r['amount'] if mode!='prudent' else r['amount']*0.75
        else:
            base=(_iso_date(r['expected_date'])-date.today()).days
            shift=15 if mode=='prudent' else (-10 if mode=='optimiste' else 0)
            day=max(0,min(90,base+shift))
            amount=r['expected_amount']*factor
        events[day]=events.get(day,0.0)+amount
    values=[round(cash,2)]; running=cash
    minimum=cash; min_day=0
    for day in range(1,91):
        running-=daily_burn
        running-=outflow_events.get(day,0.0)
        running+=events.get(day,0.0)
        running=round(running,2)
        values.append(running)
        if running<minimum:
            minimum=running; min_day=day
    return {
        'mode':mode,'values':values,'points':_curve_points(values),
        'minimum':round(minimum,2),'min_day':min_day,'end_90':round(running,2),
    }


def build_cash_intelligence():
    """Prévision 90 j fondée sur les soldes réellement ouverts de l'entité active."""
    c=cx()
    try:
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        ef='entity_id=?' if eid else 'entity_id IS NULL'; ep=(eid,) if eid else ()
        settings=_cash_settings(c)
        sales=c.execute(
            "SELECT oi.*,COALESCE((SELECT SUM(p.amount) FROM outgoing_invoice_payments p WHERE p.invoice_id=oi.id AND COALESCE(CAST(p.entity_id AS TEXT),'') = COALESCE(CAST(oi.entity_id AS TEXT),'')),0) paid_total "
            "FROM outgoing_invoices oi WHERE oi.entity_id IS ? AND oi.status IN ('sent','partially_paid') ORDER BY oi.due_date,oi.id",(eid,)).fetchall()
        purchases=c.execute(
            "SELECT pi.*,COALESCE((SELECT SUM(p.amount) FROM purchase_invoice_payments p WHERE p.purchase_invoice_id=pi.id AND COALESCE(CAST(p.entity_id AS TEXT),'') = COALESCE(CAST(pi.entity_id AS TEXT),'')),0) paid_total "
            "FROM purchase_invoices pi WHERE pi.entity_id IS ? AND COALESCE(pi.status,'unpaid')!='paid' ORDER BY pi.due_date,pi.id",(eid,)).fetchall()
        expenses=c.execute(f"SELECT vendor,description,amount,expense_date,category FROM expenses WHERE expense_date IS NOT NULL AND {ef} ORDER BY expense_date DESC",ep).fetchall()
    finally: c.close()
    today=date.today(); cash=None if settings['cash_balance'] is None else _safe_float(settings['cash_balance'])
    recent=[]; scheduled_outflows=[]; supplier_payables=[]
    # Fournisseurs : le solde réel restant dû prime sur les dépenses futures importées.
    purchase_signatures=set()
    for inv in purchases:
        amount=max(0.0,round(_safe_float(inv['total'])-_safe_float(inv['paid_total']),2))
        if amount<=.005: continue
        due=_iso_date(inv['due_date']) or _iso_date(inv['issue_date']) or today
        day=max(0,(due-today).days)
        item={'date':due.isoformat(),'day':min(day,90),'amount':amount,'vendor':inv['supplier_name'],
              'description':f"Facture {inv['invoice_number']}",'category':'facture fournisseur','source':'purchase_invoice','id':inv['id']}
        supplier_payables.append(item)
        if day<=90: scheduled_outflows.append(item)
        purchase_signatures.add(((inv['supplier_name'] or '').strip().lower(),round(amount,2),due.isoformat()))
    for e in expenses:
        d=_iso_date(e['expense_date']); amount=_safe_float(e['amount'])
        if not d or amount<=0: continue
        delta=(d-today).days
        if -90<=delta<=0: recent.append(amount)
        elif 1<=delta<=90:
            sig=((e['vendor'] or '').strip().lower(),round(amount,2),d.isoformat())
            if sig in purchase_signatures: continue
            scheduled_outflows.append({'date':d.isoformat(),'day':delta,'amount':round(amount,2),'vendor':e['vendor'],
                'description':e['description'],'category':e['category'],'source':'expense'})
    observed_90=sum(recent); daily_burn=observed_90/90.0 if recent else 0.0; monthly_burn=daily_burn*30.0
    planned_outflows_90=round(sum(x['amount'] for x in scheduled_outflows),2)
    receivables=[]
    for inv in sales:
        amount=max(0.0,round(_safe_float(inv['total'])-_safe_float(inv['paid_total']),2))
        if amount<=.005: continue
        due=_iso_date(inv['due_date']) or _iso_date(inv['issue_date']) or today
        overdue=max(0,(today-due).days)
        # Facture commerciale émise : 100% du solde est connu; seul le timing varie selon le scénario.
        expected=due if due>=today else today+timedelta(days=7 if overdue<=30 else 21 if overdue<=60 else 45)
        receivables.append({'id':inv['id'],'invoice_number':inv['invoice_number'],'customer':inv['client_name'],
            'amount':amount,'confidence':100,'expected_amount':amount,'expected_date':expected.isoformat(),
            'days_overdue':overdue,'source':'outgoing_invoice'})
    receivables.sort(key=lambda r:r['amount'],reverse=True)
    horizons={30:0.0,60:0.0,90:0.0}
    if cash is not None:
        for h in horizons:
            inflow=sum(r['expected_amount'] for r in receivables if max(0,(_iso_date(r['expected_date'])-today).days)<=h)
            planned=sum(x['amount'] for x in scheduled_outflows if x['day']<=h)
            horizons[h]=round(cash+inflow-daily_burn*h-planned,2)
    min_cash=None; min_day=None
    if cash is not None:
        running=cash; events={}; outs={}
        for r in receivables:
            day=max(1,min(90,(_iso_date(r['expected_date'])-today).days)); events[day]=events.get(day,0)+r['expected_amount']
        for x in scheduled_outflows:
            day=max(1,min(90,x['day'])); outs[day]=outs.get(day,0)+x['amount']
        min_cash=running; min_day=0
        for day in range(1,91):
            running-=daily_burn+outs.get(day,0); running+=events.get(day,0)
            if running<min_cash: min_cash=running; min_day=day
        min_cash=round(min_cash,2)
    top=receivables[0] if receivables else None; scenarios=[]
    if cash is not None and top:
        for delay in (7,30,60):
            curve=_simulate_curve(cash,daily_burn,receivables,scheduled_outflows=scheduled_outflows,mode='probable',top_delay=delay)
            scenarios.append({'delay':delay,'minimum':curve['minimum'],'end_90':curve['end_90']})
    curves=[]
    if cash is not None:
        for mode in ('prudent','probable','optimiste'):
            curves.append(_simulate_curve(cash,daily_burn,receivables,scheduled_outflows=scheduled_outflows,mode=mode))
    risk_day=(today+timedelta(days=min_day)).isoformat() if min_cash is not None and min_cash<0 else None
    if cash is None: alert_level='INCOMPLET'; alert='Renseignez le solde bancaire actuel pour activer la prévision.'
    elif min_cash is not None and min_cash<0: alert_level='ALERTE'; alert=f"Tension de trésorerie projetée autour du {risk_day}."
    elif horizons[30] < max(monthly_burn*.5,1000): alert_level='VIGILANCE'; alert='Marge de sécurité de trésorerie faible à 30 jours.'
    else: alert_level='STABLE'; alert='Aucune tension détectée sur les données actuellement connues.'
    return {'cash_balance':cash,'cash_as_of':settings['cash_as_of'],'monthly_burn':round(monthly_burn,2),'observed_90':round(observed_90,2),
        'expense_rows':len(recent),'horizons':horizons,'scheduled_outflows':scheduled_outflows,'supplier_payables':supplier_payables,
        'planned_outflows_90':planned_outflows_90,'receivables':receivables,'top_receivable':top,'scenarios':scenarios,
        'min_cash':min_cash,'min_day':min_day,'risk_day':risk_day,'alert_level':alert_level,'alert':alert,'curves':curves,
        'method_note':"Prévision calculée par entité à partir du solde disponible, des factures clients émises restant à encaisser, des factures fournisseurs restant à payer et des dépenses futures enregistrées. Les paiements partiels sont déduits. Les scénarios modifient le timing des encaissements, jamais les écritures comptables."}


def register(app):
    @app.route('/cash-intelligence',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @requires_feature('advanced_ai')
    def cash_intelligence():
        if request.method=='POST':
            raw=(request.form.get('cash_balance') or '').strip().replace(' ','').replace(',','.')
            try:
                balance=float(raw)
            except ValueError:
                flash('Solde bancaire invalide.')
                return redirect(url_for('cash_intelligence'))
            c=cx()
            from profitos.entities import set_cash_balance, current_entity_id
            set_cash_balance(c, current_entity_id(), balance, date.today().isoformat(), now())
            c.close()
            log_activity('CASH_BALANCE_UPDATE','Mise à jour manuelle du solde de trésorerie')
            flash('Solde de trésorerie mis à jour.')
            return redirect(url_for('cash_intelligence'))
        cash=build_cash_intelligence()
        raw_delay=(request.args.get('payment_delay') or '').strip()
        custom=None
        if cash['cash_balance'] is not None and cash['top_receivable'] and raw_delay:
            mapping={'today':0,'7':7,'30':30,'60':60,'never':-1}
            if raw_delay in mapping:
                daily_burn=cash['monthly_burn']/30.0
                custom=_simulate_curve(cash['cash_balance'],daily_burn,cash['receivables'],scheduled_outflows=cash.get('scheduled_outflows',[]),mode='probable',top_delay=mapping[raw_delay])
                custom['label']={'today':"Aujourd'hui",'7':'Sous 7 jours','30':'Sous 30 jours','60':'Sous 60 jours','never':"Pas d'encaissement sur 90 j"}[raw_delay]
                custom['choice']=raw_delay
        log_activity('CASH_INTELLIGENCE_VIEW','Consultation de Cash Intelligence')
        return render_template('cash_intelligence.html',cash=cash,custom=custom)
