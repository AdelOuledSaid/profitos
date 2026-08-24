from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from .cash_intelligence import build_cash_intelligence


def _num(value, default=0.0):
    try:
        return max(0.0, float(str(value or '').replace(' ', '').replace(',', '.')))
    except (TypeError, ValueError):
        return default


def _day(value, default=0):
    try:
        return max(0, min(90, int(value)))
    except (TypeError, ValueError):
        return default


def _simulate_decision(cash, kind, amount, decision_day=0, monthly_cost=0.0,
                       expected_inflow=0.0, inflow_day=60):
    baseline=next((x for x in cash.get('curves', []) if x['mode']=='probable'), None)
    if not baseline:
        return None
    values=list(baseline['values'])
    adjusted=[]
    for day,base in enumerate(values):
        impact=0.0
        if kind in ('investment','expense','market') and day>=decision_day:
            impact-=amount
        if kind=='hire':
            if day>=decision_day:
                impact-=amount
                impact-=monthly_cost*((day-decision_day)/30.0)
        elif monthly_cost and day>=decision_day:
            impact-=monthly_cost*((day-decision_day)/30.0)
        if expected_inflow and day>=inflow_day:
            impact+=expected_inflow
        adjusted.append(round(base+impact,2))
    minimum=min(adjusted); min_day=adjusted.index(minimum); end_90=adjusted[-1]
    baseline_min=baseline['minimum']
    deterioration=round(minimum-baseline_min,2)
    financing_gap=round(max(0.0,-minimum),2)
    if minimum < 0:
        level='RISQUÉ'; tone='danger'
        recommendation=(f"Décision non soutenable avec les données actuelles : il manque au moins "
                        f"{financing_gap:,.0f} € pour éviter une trésorerie négative.")
    elif minimum < max(cash['monthly_burn']*0.5, 1000):
        level='VIGILANCE'; tone='warning'
        recommendation="Décision possible mais avec une marge de sécurité faible. Sécurisez un encaissement ou un financement avant engagement."
    else:
        level='SOUTENABLE'; tone='good'
        recommendation="La décision reste soutenable sur l'horizon de 90 jours avec les données actuellement connues."
    return {
        'kind':kind,'amount':amount,'decision_day':decision_day,'monthly_cost':monthly_cost,
        'expected_inflow':expected_inflow,'inflow_day':inflow_day,'values':adjusted,
        'minimum':round(minimum,2),'min_day':min_day,'end_90':round(end_90,2),
        'baseline_min':round(baseline_min,2),'baseline_end_90':round(baseline['end_90'],2),
        'deterioration':deterioration,'financing_gap':financing_gap,'level':level,
        'tone':tone,'recommendation':recommendation,
    }


def _simulate_strategy(cash, kind, amount, decision_day=0, monthly_cost=0.0,
                       expected_inflow=0.0, inflow_day=60, installments=1):
    baseline=next((x for x in cash.get('curves', []) if x['mode']=='probable'), None)
    if not baseline:
        return None
    values=list(baseline['values'])
    installments=max(1, min(4, int(installments or 1)))
    payment_days=[min(90, decision_day + 30*i) for i in range(installments)]
    payment=amount/installments if installments else amount
    adjusted=[]
    for day,base in enumerate(values):
        impact=0.0
        if kind in ('investment','expense','market'):
            for pday in payment_days:
                if day>=pday:
                    impact-=payment
        elif kind=='hire':
            if day>=decision_day:
                impact-=amount
                impact-=monthly_cost*((day-decision_day)/30.0)
        if kind!='hire' and monthly_cost and day>=decision_day:
            impact-=monthly_cost*((day-decision_day)/30.0)
        if expected_inflow and day>=inflow_day:
            impact+=expected_inflow
        adjusted.append(round(base+impact,2))
    return {
        'values':adjusted, 'minimum':round(min(adjusted),2),
        'min_day':adjusted.index(min(adjusted)), 'end_90':round(adjusted[-1],2),
        'installments':installments, 'payment_days':payment_days,
    }


def _strategy_label(best, financing=0.0):
    parts=[]
    if best['decision_day'] > 0:
        parts.append(f"reporter à J+{best['decision_day']}")
    else:
        parts.append("agir maintenant")
    if best['installments'] > 1:
        parts.append(f"payer en {best['installments']} fois")
    else:
        parts.append("payer en une fois")
    if financing > 0:
        parts.append(f"sécuriser {financing:,.0f} € de financement")
    return ", ".join(parts[:-1]) + (" et " + parts[-1] if len(parts)>1 else parts[0])


def _optimize_decision(cash, kind, amount, decision_day=0, monthly_cost=0.0,
                       expected_inflow=0.0, inflow_day=60, reserve=5000.0):
    reserve=max(0.0, float(reserve or 0.0))
    candidates=[]
    # Search every 5 days so the planner can find a materially better date than
    # the coarse J+30/J+60/J+90 choices used by V1.6.7.
    delays=sorted(set([decision_day,30,60,90] + list(range(decision_day,91,5))))
    delays=[d for d in delays if decision_day<=d<=90]
    installment_options=[1] if kind=='hire' else [1,2,3,4]
    for day in delays:
        for installments in installment_options:
            result=_simulate_strategy(cash,kind,amount,day,monthly_cost,expected_inflow,inflow_day,installments)
            if not result:
                continue
            financing=round(max(0.0, reserve-result['minimum']),2)
            candidates.append({
                'decision_day':day, 'installments':installments,
                'minimum':result['minimum'], 'min_day':result['min_day'],
                'end_90':result['end_90'], 'financing':financing,
                'payment_days':result['payment_days'],
            })
    if not candidates:
        return None

    # Primary plan: minimise financing first, then delay, then complexity.
    ranked=sorted(candidates,key=lambda x:(x['financing'],x['decision_day'],x['installments']))
    best=ranked[0]
    best['score']=(best['financing'],best['decision_day'],best['installments'])
    label=_strategy_label(best,best['financing'])
    if best['financing']==0:
        explanation=(f"Ce plan conserve un point bas de {best['minimum']:,.0f} €, "
                     f"au-dessus de la réserve cible de {reserve:,.0f} €, sans financement supplémentaire.")
    else:
        explanation=(f"Le meilleur plan opérationnel testé descend à {best['minimum']:,.0f} €. "
                     f"Pour préserver {reserve:,.0f} € de réserve, il faut sécuriser {best['financing']:,.0f} €.")

    # Debt-free planner: among strategies already preserving the target reserve,
    # prefer the earliest date and then the fewest instalments.
    debt_free=[c for c in candidates if c['financing']==0]
    no_financing=None
    if debt_free:
        nf=min(debt_free,key=lambda x:(x['decision_day'],x['installments'],-x['minimum']))
        no_financing={
            **nf,
            'available':True,
            'label':_strategy_label(nf,0),
            'explanation':(f"Cette option ne nécessite aucun financement et conserve "
                           f"au moins {nf['minimum']:,.0f} € de trésorerie."),
        }
    else:
        closest=max(candidates,key=lambda x:x['minimum'])
        no_financing={
            'available':False,
            'label':'Aucune solution sans financement sur 90 jours',
            'explanation':(f"Avec les encaissements actuellement connus, même la trajectoire la plus favorable "
                           f"atteint {closest['minimum']:,.0f} €. Il manque {max(0.0,reserve-closest['minimum']):,.0f} € "
                           f"pour préserver la réserve cible de {reserve:,.0f} €."),
            'decision_day':closest['decision_day'],'installments':closest['installments'],
            'minimum':closest['minimum'],'financing':closest['financing'],
        }

    return {
        'reserve':reserve,'best':best,'label':label,'explanation':explanation,
        'alternatives':ranked[:5], 'no_financing':no_financing,
    }


def register(app):
    @app.route('/decision-simulator')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    def decision_simulator():
        cash=build_cash_intelligence()
        kind=(request.args.get('kind') or 'investment').strip().lower()
        if kind not in {'investment','hire','expense','market'}:
            kind='investment'
        submitted=request.args.get('simulate')=='1'
        simulation=None
        inputs={
            'kind':kind,
            'amount':_num(request.args.get('amount')),
            'decision_day':_day(request.args.get('decision_day'),0),
            'monthly_cost':_num(request.args.get('monthly_cost')),
            'expected_inflow':_num(request.args.get('expected_inflow')),
            'inflow_day':_day(request.args.get('inflow_day'),60),
            'reserve':_num(request.args.get('reserve'),5000.0),
        }
        if submitted and cash['cash_balance'] is not None:
            sim_args={k:v for k,v in inputs.items() if k!='reserve'}
            simulation=_simulate_decision(cash, **sim_args)
            simulation['optimizer']=_optimize_decision(cash, reserve=inputs['reserve'], **sim_args)
            log_activity('DECISION_SIMULATION', f"Simulation {kind} · {inputs['amount']:.2f} EUR")
        else:
            log_activity('DECISION_SIMULATOR_VIEW','Consultation du Decision Simulator')
        return render_template('decision_simulator.html',cash=cash,simulation=simulation,inputs=inputs)
