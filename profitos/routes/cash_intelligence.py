from datetime import date, datetime, timedelta

from profitos.runtime import *
from profitos.feature_access import requires_paid_plan, requires_feature
from .money_hunter import _safe_float, _confidence


HORIZON_DAYS=90
# Âge maximal du solde déclaré avant de signaler que la prévision part d'une base périmée.
STALE_BALANCE_DAYS=7
# Au-delà d'un an, une échéance de facture client est très probablement une erreur de saisie.
SUSPICIOUS_DUE_DAYS=365
# En dessous de ce nombre de dépenses sur 90 j, la dépense mensuelle observée est peu représentative.
MIN_RELIABLE_EXPENSES=6

# Règles des scénarios, appliquées UNIQUEMENT aux encaissements clients connus.
# - shift  : décalage en jours de la date d'encaissement attendue ;
# - factor : part du solde restant dû effectivement comptée (jamais > 100 % :
#            on ne peut pas encaisser plus que ce qui a été facturé).
# Par construction : optimiste >= probable >= prudent, à chaque jour de la courbe.
SCENARIO_RULES={
    'prudent':{'shift':15,'factor':0.55,'label':"encaissements 15 j plus tard, 55 % du restant dû compté"},
    'probable':{'shift':0,'factor':1.0,'label':"encaissements à la date attendue, 100 % compté"},
    'optimiste':{'shift':-10,'factor':1.0,'label':"encaissements 10 j plus tôt, 100 % compté"},
}


def _iso_date(value):
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _cash_settings(c):
    from profitos.entities import get_cash_balance, current_entity_id
    balance=get_cash_balance(c, current_entity_id())
    return {'cash_balance':balance['cash_balance'],'cash_as_of':balance['cash_as_of'],'updated_at':None}


def _horizon_day(offset):
    """Jour de la courbe (1..90) où un flux est appliqué, ou None s'il tombe après l'horizon.

    Le jour 0 est le solde déclaré. Un flux échu ou dû aujourd'hui est appliqué à J+1 : il n'est
    jamais perdu (auparavant les flux du jour 0 étaient ignorés). Un flux après J+90 est EXCLU,
    et non plus ramené à J+90 (ce qui créait un pic artificiel en fin de courbe)."""
    offset=int(offset)
    if offset>HORIZON_DAYS:
        return None
    return max(1,offset)


def _chart_scale(curves):
    """Échelle commune à toutes les courbes : sans elle, chaque courbe était normalisée sur son
    propre min/max et les trois trajectoires n'étaient pas comparables visuellement."""
    values=[v for curve in curves for v in curve['values']]
    if not values:
        return 0.0,1.0
    lo=min(values); hi=max(values)
    if lo==hi:
        lo-=1.0; hi+=1.0
    margin=(hi-lo)*0.06
    return lo-margin,hi+margin


def _curve_points(values, width=920, height=220, pad=18, lo=None, hi=None, left=None):
    """Transforme une série journalière en points SVG, sans JavaScript inline.
    lo/hi : bornes de l'échelle verticale (communes à plusieurs courbes si fournies)."""
    if not values:
        return ''
    lo=min(values) if lo is None else lo
    hi=max(values) if hi is None else hi
    span=max(hi-lo,1.0)
    left=pad if left is None else left
    usable_w=width-left-pad; usable_h=height-2*pad
    pts=[]
    for i,value in enumerate(values):
        x=left + usable_w*(i/max(len(values)-1,1))
        y=pad + usable_h*(hi-value)/span
        pts.append(f"{x:.1f},{y:.1f}")
    return ' '.join(pts)


def _simulate_curve(cash, daily_burn, receivables, scheduled_outflows=None, mode='probable', top_delay=None, today=None):
    """Courbe 0..90 j. Les hypothèses restent explicites et déterministes.

    top_delay : délai simulé (jours) pour la plus grosse créance (receivables[0]) ; -1 = jamais
    sur l'horizon. Les autres créances suivent la règle du scénario."""
    today=today or date.today()
    rule=SCENARIO_RULES.get(mode,SCENARIO_RULES['probable'])
    events={}
    outflow_events={}
    for item in (scheduled_outflows or []):
        day=_horizon_day(item.get('day',0))
        if day is not None:
            outflow_events[day]=outflow_events.get(day,0.0)+_safe_float(item.get('amount'))
    for idx,r in enumerate(receivables):
        if idx==0 and top_delay is not None:
            if top_delay < 0: # -1 = jamais sur l'horizon
                continue
            offset=int(top_delay)
        else:
            expected=_iso_date(r['expected_date'])
            if expected is None:
                continue
            offset=(expected-today).days+rule['shift']
        day=_horizon_day(offset)
        if day is None:
            continue
        events[day]=events.get(day,0.0)+_safe_float(r['expected_amount'])*rule['factor']
    values=[round(cash,2)]; running=cash
    minimum=cash; min_day=0
    for day in range(1,HORIZON_DAYS+1):
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


def compute_cash_forecast(cash_balance, cash_as_of, sales, purchases, expenses, einvoice_hold=None, today=None):
    """Calcul pur (sans base de données) de la prévision 90 j.

    Une seule fonction de projection (_simulate_curve) alimente TOUS les chiffres de la page :
    KPI 30/60/90 j, point bas, alerte, courbes et What-if. Les KPI correspondent au scénario
    probable, ce qui garantit leur cohérence avec la courbe bleue."""
    today=today or date.today()
    _einvoice_hold=einvoice_hold or (lambda inv: None)
    cash=None if cash_balance is None else _safe_float(cash_balance)
    recent=[]; scheduled_outflows=[]; supplier_payables=[]
    # Fournisseurs : le solde réel restant dû prime sur les dépenses futures importées.
    purchase_signatures=set()
    for inv in purchases:
        paid=round(_safe_float(inv['paid_total']),2)
        amount=max(0.0,round(_safe_float(inv['total'])-_safe_float(inv['paid_total']),2))
        if amount<=.005: continue
        due=_iso_date(inv['due_date']) or _iso_date(inv['issue_date']) or today
        day=max(0,(due-today).days)
        item={'date':due.isoformat(),'day':min(day,HORIZON_DAYS),'amount':amount,'vendor':inv['supplier_name'],
              'description':f"Facture {inv['invoice_number']}",'category':'facture fournisseur','source':'purchase_invoice','id':inv['id'],
              'paid':paid if paid>.005 else 0.0,'overdue_days':max(0,(today-due).days),'in_horizon':day<=HORIZON_DAYS}
        supplier_payables.append(item)
        if day<=HORIZON_DAYS: scheduled_outflows.append(item)
        purchase_signatures.add(((inv['supplier_name'] or '').strip().lower(),round(amount,2),due.isoformat()))
    for e in expenses:
        d=_iso_date(e['expense_date']); amount=_safe_float(e['amount'])
        if not d or amount<=0: continue
        delta=(d-today).days
        if -90<=delta<=0: recent.append(amount)
        elif 1<=delta<=HORIZON_DAYS:
            sig=((e['vendor'] or '').strip().lower(),round(amount,2),d.isoformat())
            if sig in purchase_signatures: continue
            scheduled_outflows.append({'date':d.isoformat(),'day':delta,'amount':round(amount,2),'vendor':e['vendor'],
                'description':e['description'],'category':e['category'],'source':'expense'})
    observed_90=sum(recent); daily_burn=observed_90/90.0 if recent else 0.0; monthly_burn=daily_burn*30.0
    planned_outflows_90=round(sum(x['amount'] for x in scheduled_outflows),2)
    receivables=[]
    held_receivables=[]
    for inv in sales:
        amount=max(0.0,round(_safe_float(inv['total'])-_safe_float(inv['paid_total']),2))
        if amount<=.005: continue
        # Une facture refusée, en litige, suspendue ou rejetée côté facturation électronique n'est pas
        # un encaissement fiable : elle sort de la prévision et reste signalée à part.
        hold=_einvoice_hold(inv)
        if hold:
            held_receivables.append({'id':inv['id'],'invoice_number':inv['invoice_number'],
                'customer':inv['client_name'],'amount':amount,'reason':hold})
            continue
        due=_iso_date(inv['due_date']) or _iso_date(inv['issue_date']) or today
        overdue=max(0,(today-due).days)
        # Facture commerciale émise : 100% du solde est connu; seul le timing varie selon le scénario.
        expected=due if due>=today else today+timedelta(days=7 if overdue<=30 else 21 if overdue<=60 else 45)
        offset=(expected-today).days
        receivables.append({'id':inv['id'],'invoice_number':inv['invoice_number'],'customer':inv['client_name'],
            'amount':amount,'expected_amount':amount,'expected_date':expected.isoformat(),'due_date':due.isoformat(),
            'days_overdue':overdue,'in_horizon':offset<=HORIZON_DAYS,'suspicious_date':offset>SUSPICIOUS_DUE_DAYS,
            'source':'outgoing_invoice'})
    receivables.sort(key=lambda r:r['amount'],reverse=True)
    receivables_in_horizon=[r for r in receivables if r['in_horizon']]
    receivables_beyond=sorted((r for r in receivables if not r['in_horizon']),key=lambda r:r['expected_date'])

    horizons={30:0.0,60:0.0,90:0.0}
    min_cash=None; min_day=None
    curves=[]; chart=None
    if cash is not None:
        for mode in ('prudent','probable','optimiste'):
            curves.append(_simulate_curve(cash,daily_burn,receivables,scheduled_outflows=scheduled_outflows,mode=mode,today=today))
        probable=curves[1]
        for h in horizons:
            horizons[h]=probable['values'][h]
        min_cash=probable['minimum']; min_day=probable['min_day']
        lo,hi=_chart_scale(curves)
        chart_left=74
        for curve in curves:
            curve['points']=_curve_points(curve['values'],lo=lo,hi=hi,left=chart_left)
            curve['rule']=SCENARIO_RULES[curve['mode']]['label']
        # Graduations de l'axe Y (positions dans le viewBox 920x220, pad=18).
        ticks=[]
        for i in range(5):
            value=hi-(hi-lo)*i/4
            ticks.append({'y':round(18+184*i/4,1),'value':round(value,-1) if abs(hi-lo)>200 else round(value,2)})
        zero_y=round(18+184*(hi-0)/max(hi-lo,1.0),1) if lo<0<hi else None
        chart={'lo':lo,'hi':hi,'ticks':ticks,'zero_y':zero_y,'left':chart_left,
               'x30':round(chart_left+(920-chart_left-18)*30/90,1),'x60':round(chart_left+(920-chart_left-18)*60/90,1)}

    top=receivables[0] if receivables else None; scenarios=[]
    if cash is not None and top:
        for delay in (7,30,60):
            curve=_simulate_curve(cash,daily_burn,receivables,scheduled_outflows=scheduled_outflows,mode='probable',top_delay=delay,today=today)
            scenarios.append({'delay':delay,'minimum':curve['minimum'],'min_day':curve['min_day'],'end_90':curve['end_90']})

    as_of=_iso_date(cash_as_of)
    balance_age_days=(today-as_of).days if as_of else None
    balance_stale=cash is not None and (balance_age_days is None or balance_age_days>STALE_BALANCE_DAYS)
    overdue_payables=[p for p in supplier_payables if p['overdue_days']>0]

    risk_day=(today+timedelta(days=min_day)).isoformat() if min_cash is not None and min_cash<0 else None
    if cash is None: alert_level='INCOMPLET'; alert='Renseignez le solde bancaire actuel pour activer la prévision.'
    elif min_cash is not None and min_cash<0: alert_level='ALERTE'; alert=f"Tension de trésorerie projetée autour du {risk_day}."
    elif horizons[30] < max(monthly_burn*.5,1000): alert_level='VIGILANCE'; alert='Marge de sécurité de trésorerie faible à 30 jours.'
    elif balance_stale:
        alert_level='VIGILANCE'
        alert=(f"Aucune tension détectée, mais le solde de départ date de {balance_age_days} jours : mettez-le à jour pour fiabiliser la prévision."
               if balance_age_days is not None else "Aucune tension détectée, mais la date du solde de départ est inconnue : mettez-le à jour.")
    else: alert_level='STABLE'; alert='Aucune tension détectée sur les données actuellement connues.'
    return {'cash_balance':cash,'cash_as_of':cash_as_of,'balance_age_days':balance_age_days,'balance_stale':balance_stale,
        'monthly_burn':round(monthly_burn,2),'daily_burn':daily_burn,'observed_90':round(observed_90,2),
        'expense_rows':len(recent),'burn_reliable':len(recent)>=MIN_RELIABLE_EXPENSES,
        'horizons':horizons,'scheduled_outflows':scheduled_outflows,'supplier_payables':supplier_payables,
        'overdue_payables_total':round(sum(p['amount'] for p in overdue_payables),2),'overdue_payables_count':len(overdue_payables),
        'planned_outflows_90':planned_outflows_90,'receivables':receivables,
        'receivables_in_horizon':receivables_in_horizon,'receivables_beyond':receivables_beyond,
        'receivables_beyond_total':round(sum(r['amount'] for r in receivables_beyond),2),
        'suspicious_receivables':[r for r in receivables if r['suspicious_date']],
        'held_receivables':held_receivables,'held_receivables_total':round(sum(h['amount'] for h in held_receivables),2),
        'top_receivable':top,'scenarios':scenarios,
        'min_cash':min_cash,'min_day':min_day,'risk_day':risk_day,'alert_level':alert_level,'alert':alert,'curves':curves,'chart':chart,
        'method_note':"Prévision calculée par entité à partir du solde disponible, des factures clients émises restant à encaisser, des factures fournisseurs restant à payer et des dépenses futures enregistrées. Les paiements partiels sont déduits. Les montants à 30 / 60 / 90 jours correspondent au scénario probable. Les flux attendus après 90 jours sont exclus ; les échéances déjà dépassées sont comptées dès J+1. Les scénarios modifient le timing des encaissements (et, en prudent, la part comptée), jamais les écritures comptables."}


def build_cash_intelligence():
    """Prévision 90 j fondée sur les soldes réellement ouverts de l'entité active."""
    # Import tardif : évite une dépendance circulaire au chargement des routes.
    from profitos.routes.invoicing import _einvoice_hold
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
    return compute_cash_forecast(settings['cash_balance'],settings['cash_as_of'],sales,purchases,expenses,einvoice_hold=_einvoice_hold)


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
                daily_burn=cash['daily_burn']
                custom=_simulate_curve(cash['cash_balance'],daily_burn,cash['receivables'],scheduled_outflows=cash.get('scheduled_outflows',[]),mode='probable',top_delay=mapping[raw_delay])
                custom['label']={'today':"Aujourd'hui",'7':'Sous 7 jours','30':'Sous 30 jours','60':'Sous 60 jours','never':"Pas d'encaissement sur 90 j"}[raw_delay]
                custom['choice']=raw_delay
        log_activity('CASH_INTELLIGENCE_VIEW','Consultation de Cash Intelligence')
        return render_template('cash_intelligence.html',cash=cash,custom=custom)
