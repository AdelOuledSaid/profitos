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


def _plan_reason(plan, reserve, max_financing, deadline):
    reasons=[]
    if plan['financing']==0:
        reasons.append("aucun financement supplémentaire")
    else:
        reasons.append(f"{plan['financing']:,.0f} € de financement")
    reasons.append(f"un point bas de {plan['minimum']:,.0f} €")
    reasons.append(f"une décision à J+{plan['decision_day']}")
    if plan['installments']>1:
        reasons.append(f"un paiement en {plan['installments']} fois")
    text="Ce plan combine " + ", ".join(reasons[:-1]) + " et " + reasons[-1] + "."
    if max_financing is not None:
        text += f" Il respecte le plafond de financement de {max_financing:,.0f} €."
    if deadline is not None:
        text += f" Il respecte aussi l'échéance maximale J+{deadline}."
    text += f" Réserve cible : {reserve:,.0f} €."
    return text


def _optimize_decision(cash, kind, amount, decision_day=0, monthly_cost=0.0,
                       expected_inflow=0.0, inflow_day=60, reserve=5000.0,
                       max_financing=None, deadline=90):
    reserve=max(0.0, float(reserve or 0.0))
    deadline=max(decision_day, min(90, int(deadline if deadline is not None else 90)))
    if max_financing is not None:
        max_financing=max(0.0,float(max_financing))
    candidates=[]
    delays=sorted(set([decision_day,30,60,90] + list(range(decision_day,91,5))))
    delays=[d for d in delays if decision_day<=d<=deadline]
    installment_options=[1] if kind=='hire' else [1,2,3,4]
    for day in delays:
        for installments in installment_options:
            result=_simulate_strategy(cash,kind,amount,day,monthly_cost,expected_inflow,inflow_day,installments)
            if not result: continue
            financing=round(max(0.0,reserve-result['minimum']),2)
            candidates.append({'decision_day':day,'installments':installments,'minimum':result['minimum'],
                'min_day':result['min_day'],'end_90':result['end_90'],'financing':financing,
                'payment_days':result['payment_days']})
    if not candidates: return None
    feasible=[c for c in candidates if max_financing is None or c['financing']<=max_financing]
    constraints_met=bool(feasible)
    pool=feasible or candidates
    ranked_all=sorted(pool,key=lambda x:(x['financing'],x['decision_day'],x['installments'],-x['minimum']))

    # Collapse financially equivalent plans. If several dates produce the same
    # financing need, minimum cash and installment count, keep the earliest one.
    ranked=[]
    seen=set()
    equivalent_dates={}
    for c in ranked_all:
        key=(round(c['financing'],2),round(c['minimum'],2),c['installments'],round(c['end_90'],2))
        if key in seen:
            equivalent_dates.setdefault(key,[]).append(c['decision_day'])
            continue
        seen.add(key)
        ranked.append(c)
        equivalent_dates.setdefault(key,[c['decision_day']])
    best=dict(ranked[0]); best['score']=(best['financing'],best['decision_day'],best['installments'])
    label=_strategy_label(best,best['financing'])
    if constraints_met:
        explanation=_plan_reason(best,reserve,max_financing,deadline)
        eq_key=(round(best['financing'],2),round(best['minimum'],2),best['installments'],round(best['end_90'],2))
        eq_dates=sorted(set(equivalent_dates.get(eq_key,[])))
        if len(eq_dates)>1:
            explanation += (f" Plusieurs dates testées donnent le même résultat financier ; "
                            f"J+{best['decision_day']} est retenu car c'est la date la plus proche.")
    else:
        gap=round(max(0.0,best['financing']-(max_financing or 0.0)),2)
        explanation=(f"Aucun plan compatible avec vos contraintes. Le meilleur besoin de financement trouvé est "
                     f"{best['financing']:,.0f} €, pour un plafond accepté de {max_financing:,.0f} €. "
                     f"Écart : {gap:,.0f} €. Pour rendre la décision possible, augmentez le plafond de financement, "
                     f"réduisez le montant de la décision ou modifiez uniquement des hypothèses d'encaissement réellement justifiées.")
    debt_free=[c for c in candidates if c['financing']==0]
    if debt_free:
        nf=min(debt_free,key=lambda x:(x['decision_day'],x['installments'],-x['minimum']))
        no_financing={**nf,'available':True,'label':_strategy_label(nf,0),
            'explanation':f"Cette option ne nécessite aucun financement et conserve au moins {nf['minimum']:,.0f} € de trésorerie."}
    else:
        closest=max(candidates,key=lambda x:x['minimum'])
        no_financing={'available':False,'label':f"Aucune solution sans financement avant J+{deadline}",
            'explanation':(f"Avec les encaissements connus, la trajectoire la plus favorable atteint {closest['minimum']:,.0f} €. "
                           f"Il manque {max(0.0,reserve-closest['minimum']):,.0f} € pour préserver {reserve:,.0f} € de réserve."),
            'decision_day':closest['decision_day'],'installments':closest['installments'],
            'minimum':closest['minimum'],'financing':closest['financing']}
    top3=[]
    for i,c in enumerate(ranked[:3],1):
        item=dict(c); item['rank']=i; item['reason']=_plan_reason(c,reserve,max_financing,deadline); top3.append(item)
    return {'reserve':reserve,'max_financing':max_financing,'deadline':deadline,'constraints_met':constraints_met,
        'best':best,'label':label,'explanation':explanation,'alternatives':ranked[:5],
        'top3':top3,'no_financing':no_financing,
        'constraint_gap': round(max(0.0,best['financing']-(max_financing or 0.0)),2) if not constraints_met else 0.0}



def _build_constraint_resolutions(kind, amount, optimizer):
    """Build deterministic, explainable ways to close a financing constraint gap.

    The engine never invents revenue: it only changes user-controlled levers
    (decision amount / accepted financing) and exposes a mixed option.
    """
    if not optimizer or optimizer.get('constraints_met'):
        return []
    best=optimizer['best']
    cap=optimizer.get('max_financing')
    if cap is None:
        return []
    gap=round(max(0.0,best['financing']-cap),2)
    if gap <= 0:
        return []
    reserve=optimizer['reserve']
    resolutions=[]

    # 1. Keep the decision unchanged and relax only the financing ceiling.
    resolutions.append({
        'rank':1, 'kind':'financing', 'title':'Augmenter le financement disponible',
        'headline':f"Porter le plafond de financement à {best['financing']:,.0f} €",
        'effort':gap,
        'explanation':(f"Il faut {gap:,.0f} € de capacité de financement supplémentaire pour conserver "
                       f"le montant de la décision et la réserve cible de {reserve:,.0f} €."),
    })

    # 2. Keep the financing ceiling and reduce the discretionary initial amount.
    if kind in ('investment','expense','market') and amount > 0:
        reduced=max(0.0,round(amount-gap,2))
        resolutions.append({
            'rank':2, 'kind':'amount', 'title':'Réduire le montant de la décision',
            'headline':f"Ramener le décaissement initial à environ {reduced:,.0f} €",
            'effort':gap,
            'explanation':(f"Une réduction d'au moins {gap:,.0f} € ferme l'écart estimé tout en conservant "
                           f"le plafond de financement actuel de {cap:,.0f} €."),
        })

        # 3. Balanced combination: split the gap between financing and amount reduction.
        extra=round(gap/2.0,2); reduction=round(gap-extra,2)
        resolutions.append({
            'rank':3, 'kind':'mixed', 'title':'Partager l’effort',
            'headline':f"Ajouter {extra:,.0f} € de financement et réduire la décision de {reduction:,.0f} €",
            'effort':gap,
            'explanation':(f"Cette option répartit l'écart : plafond porté à {cap+extra:,.0f} € et "
                           f"décaissement ramené à environ {max(0.0,amount-reduction):,.0f} €."),
        })
    return resolutions[:3]

def _cfo_answer(question, simulation):
    q=(question or '').strip()
    if not q or not simulation or not simulation.get('optimizer'): return None
    o=simulation['optimizer']; b=o['best']; low=q.lower()
    if any(w in low for w in ('sans financement','sans emprunt','zéro dette','zero dette')):
        nf=o['no_financing']
        return nf['explanation'] if not nf['available'] else f"Oui. Plan sans financement : {nf['label']}. {nf['explanation']}"
    if any(w in low for w in ('pourquoi','why','j+','date')):
        return f"Je recommande J+{b['decision_day']} car, parmi les plans compatibles testés, il minimise d'abord le financement requis ({b['financing']:,.0f} €), puis le délai et la complexité. Point bas prévu : {b['minimum']:,.0f} €."
    if any(w in low for w in ('financement','emprunt','dette')):
        return f"Le plan recommandé nécessite {b['financing']:,.0f} € de financement pour préserver {o['reserve']:,.0f} € de réserve."
    return f"Plan recommandé : {o['label']}. Point bas {b['minimum']:,.0f} €, trésorerie J+90 {b['end_90']:,.0f} €. ProfitOS répond uniquement avec les données et scénarios actuellement connus."

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
            'max_financing': (_num(request.args.get('max_financing')) if request.args.get('max_financing') not in (None,'') else None),
            'deadline': _day(request.args.get('deadline'),90),
        }
        if submitted and cash['cash_balance'] is not None:
            sim_args={k:v for k,v in inputs.items() if k not in ('reserve','max_financing','deadline')}
            simulation=_simulate_decision(cash, **sim_args)
            simulation['optimizer']=_optimize_decision(cash, reserve=inputs['reserve'], max_financing=inputs['max_financing'], deadline=inputs['deadline'], **sim_args)
            simulation['resolutions']=_build_constraint_resolutions(kind, inputs['amount'], simulation['optimizer'])
            simulation['cfo_answer']=_cfo_answer(request.args.get('cfo_question'), simulation)
            log_activity('DECISION_SIMULATION', f"Simulation {kind} · {inputs['amount']:.2f} EUR")
        else:
            log_activity('DECISION_SIMULATOR_VIEW','Consultation du Decision Simulator')
        return render_template('decision_simulator.html',cash=cash,simulation=simulation,inputs=inputs,cfo_question=request.args.get('cfo_question',''))
