from datetime import date, datetime, timedelta

from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from .money_hunter import _safe_float, _confidence


def _iso_date(value):
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _cash_settings(c):
    row=c.execute("SELECT * FROM financial_settings WHERE id=1").fetchone()
    return row or {'cash_balance':None,'cash_as_of':None,'updated_at':None}


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


def _simulate_curve(cash, daily_burn, receivables, mode='probable', top_delay=None):
    """Courbe 0..90 j. Les hypothèses restent explicites et déterministes."""
    factors={'prudent':0.55,'probable':1.0,'optimiste':1.12}
    factor=factors.get(mode,1.0)
    events={}
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
    c=cx()
    try:
        settings=_cash_settings(c)
        invoices=c.execute(
            "SELECT id,invoice_number,customer,MAX(amount-paid_amount,0) outstanding,"
            "days_overdue,score,due_date FROM invoices "
            "WHERE LOWER(COALESCE(status,''))!='paid' AND MAX(amount-paid_amount,0)>0 "
            "ORDER BY outstanding DESC"
        ).fetchall()
        expenses=c.execute(
            "SELECT amount,expense_date FROM expenses WHERE expense_date IS NOT NULL ORDER BY expense_date DESC"
        ).fetchall()
    finally:
        c.close()

    today=date.today()
    cash=None if settings['cash_balance'] is None else _safe_float(settings['cash_balance'])

    # Dépense quotidienne observée : moyenne des dépenses des 90 derniers jours disponibles.
    recent=[]
    for e in expenses:
        d=_iso_date(e['expense_date'])
        if d and 0 <= (today-d).days <= 90:
            recent.append(_safe_float(e['amount']))
    observed_90=sum(recent)
    daily_burn=observed_90/90.0 if recent else 0.0
    monthly_burn=daily_burn*30.0

    receivables=[]
    for inv in invoices:
        amount=_safe_float(inv['outstanding'])
        confidence=_confidence(inv['score'])/100.0
        overdue=max(0,int(inv['days_overdue'] or 0))
        # Horizon prudent et explicable, identique à la logique RECOVER existante.
        delay=21 if overdue<=30 else (45 if overdue<=60 else 75)
        expected_date=today+timedelta(days=delay)
        receivables.append({
            'id':inv['id'],'invoice_number':inv['invoice_number'],'customer':inv['customer'],
            'amount':round(amount,2),'confidence':round(confidence*100),
            'expected_amount':round(amount*confidence,2),'expected_date':expected_date.isoformat(),
            'days_overdue':overdue,
        })

    horizons={30:0.0,60:0.0,90:0.0}
    if cash is not None:
        for h in horizons:
            inflow=sum(r['expected_amount'] for r in receivables if (_iso_date(r['expected_date'])-today).days<=h)
            horizons[h]=round(cash + inflow - daily_burn*h,2)

    min_cash=None; min_day=None
    if cash is not None:
        running=cash
        events={}
        for r in receivables:
            day=(_iso_date(r['expected_date'])-today).days
            if 1<=day<=90: events[day]=events.get(day,0.0)+r['expected_amount']
        min_cash=running; min_day=0
        for day in range(1,91):
            running-=daily_burn
            running+=events.get(day,0.0)
            if running<min_cash:
                min_cash=running; min_day=day
        min_cash=round(min_cash,2)

    top=receivables[0] if receivables else None
    scenarios=[]
    if cash is not None and top:
        for delay in (7,30,60):
            # Scénario : la facture principale est payée intégralement à la date testée;
            # les autres créances restent pondérées par leur confiance.
            running=cash; minimum=cash
            other_events={}
            for r in receivables[1:]:
                d=(_iso_date(r['expected_date'])-today).days
                if 1<=d<=90: other_events[d]=other_events.get(d,0.0)+r['expected_amount']
            for day in range(1,91):
                running-=daily_burn
                running+=other_events.get(day,0.0)
                if day==delay: running+=top['amount']
                minimum=min(minimum,running)
            scenarios.append({'delay':delay,'minimum':round(minimum,2),'end_90':round(running,2)})

    curves=[]
    selected_delay=None
    if cash is not None:
        for mode in ('prudent','probable','optimiste'):
            curves.append(_simulate_curve(cash,daily_burn,receivables,mode=mode))

    risk_day=(today+timedelta(days=min_day)).isoformat() if min_cash is not None and min_cash<0 else None
    if cash is None:
        alert_level='INCOMPLET'; alert='Renseignez le solde bancaire actuel pour activer la prévision.'
    elif min_cash is not None and min_cash<0:
        alert_level='ALERTE'; alert=f"Tension de trésorerie projetée autour du {risk_day}."
    elif horizons[30] < monthly_burn*0.5:
        alert_level='VIGILANCE'; alert='Marge de sécurité de trésorerie faible à 30 jours.'
    else:
        alert_level='STABLE'; alert='Aucune tension détectée sur les données actuellement connues.'

    return {
        'cash_balance':cash,'cash_as_of':settings['cash_as_of'],'monthly_burn':round(monthly_burn,2),
        'observed_90':round(observed_90,2),'expense_rows':len(recent),'horizons':horizons,
        'receivables':receivables,'top_receivable':top,'scenarios':scenarios,
        'min_cash':min_cash,'min_day':min_day,'risk_day':risk_day,
        'alert_level':alert_level,'alert':alert,'curves':curves,
        'method_note':'Prévision calculée à partir du solde saisi, des créances RECOVER pondérées par leur score et de la dépense quotidienne observée sur les 90 derniers jours.',
    }


def register(app):
    @app.route('/cash-intelligence',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    def cash_intelligence():
        if request.method=='POST':
            raw=(request.form.get('cash_balance') or '').strip().replace(' ','').replace(',','.')
            try:
                balance=float(raw)
            except ValueError:
                flash('Solde bancaire invalide.')
                return redirect(url_for('cash_intelligence'))
            c=cx()
            c.execute(
                "INSERT INTO financial_settings(id,cash_balance,cash_as_of,updated_at) VALUES(1,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET cash_balance=excluded.cash_balance,cash_as_of=excluded.cash_as_of,updated_at=excluded.updated_at",
                (balance,date.today().isoformat(),now())
            )
            c.commit(); c.close()
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
                custom=_simulate_curve(cash['cash_balance'],daily_burn,cash['receivables'],mode='probable',top_delay=mapping[raw_delay])
                custom['label']={'today':"Aujourd'hui",'7':'Sous 7 jours','30':'Sous 30 jours','60':'Sous 60 jours','never':"Pas d'encaissement sur 90 j"}[raw_delay]
                custom['choice']=raw_delay
        log_activity('CASH_INTELLIGENCE_VIEW','Consultation de Cash Intelligence')
        return render_template('cash_intelligence.html',cash=cash,custom=custom)
