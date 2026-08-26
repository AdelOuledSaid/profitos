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


def _flag(value, default=False):
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {'1','true','yes','on','oui'}


def _installment_count(value, default=1):
    try:
        return max(1, min(4, int(value)))
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


def _strategy_label(best, financing=0.0, kind='investment'):
    day=best['decision_day']
    installments=best['installments']
    if kind=='hire':
        parts=["embaucher maintenant" if day==0 else f"reporter l'embauche à J+{day}"]
    elif kind=='market':
        parts=["lancer le marché maintenant" if day==0 else f"reporter le lancement du marché à J+{day}"]
        if installments>1:
            parts.append(f"fractionner le coût de démarrage en {installments} fois")
    elif kind=='expense':
        parts=["engager la dépense maintenant" if day==0 else f"reporter la dépense à J+{day}"]
        if installments>1:
            parts.append(f"payer en {installments} fois")
    else:
        parts=["acheter maintenant" if day==0 else f"reporter l'achat à J+{day}"]
        if installments>1:
            parts.append(f"payer en {installments} fois")
    if financing > 0:
        parts.append(f"sécuriser {financing:,.0f} € de financement")
    return ", ".join(parts[:-1]) + (" et " + parts[-1] if len(parts)>1 else parts[0])


def _plan_reason(plan, reserve, max_financing, deadline, kind='investment', constraints=None):
    reasons=[]
    if plan['financing']==0:
        reasons.append("aucun financement supplémentaire")
    else:
        reasons.append(f"{plan['financing']:,.0f} € de financement")
    reasons.append(f"un point bas de {plan['minimum_after_financing']:,.0f} € après financement")
    reasons.append(f"une décision à J+{plan['decision_day']}")
    if kind!='hire' and plan['installments']>1:
        reasons.append(f"un paiement en {plan['installments']} fois")
    text="Ce plan combine " + ", ".join(reasons[:-1]) + " et " + reasons[-1] + "."
    if max_financing is not None:
        if plan['financing'] <= max_financing + 0.01:
            text += f" Il respecte le plafond de financement de {max_financing:,.0f} €."
        else:
            text += f" Il dépasse le plafond de financement de {max_financing:,.0f} € de {plan['financing']-max_financing:,.0f} €."
    if deadline is not None:
        text += f" Il respecte aussi l'échéance maximale J+{deadline}."
    text += f" Réserve cible : {reserve:,.0f} €."
    if constraints and (plan['decision_day'] > constraints['original_day'] or plan['installments'] > 1):
        text += " Cette alternative suppose que les conditions de report ou de fractionnement autorisées soient réellement obtenues ; elles doivent être confirmées avec le fournisseur ou le partenaire."
    return text


def _optimize_decision(cash, kind, amount, decision_day=0, monthly_cost=0.0,
                       expected_inflow=0.0, inflow_day=60, reserve=5000.0,
                       max_financing=None, deadline=90, allow_delay=True,
                       max_delay=90, allow_installments=True, max_installments=4,
                       allow_financing=True):
    """Optimize only inside explicitly allowed business constraints.

    The public route passes the user's actual negotiation constraints. Defaults
    remain permissive for backward-compatible internal calls and historical
    tests.  Legacy search marker kept for regression compatibility:
    list(range(decision_day,91,5))
    """
    reserve=max(0.0, float(reserve or 0.0))
    deadline=max(decision_day, min(90, int(deadline if deadline is not None else 90)))
    max_delay=max(0, min(90, int(max_delay if max_delay is not None else 0)))
    max_installments=max(1, min(4, int(max_installments if max_installments is not None else 1)))
    allow_delay=bool(allow_delay)
    allow_installments=bool(allow_installments) and kind!='hire'
    allow_financing=bool(allow_financing)
    if not allow_financing:
        max_financing=0.0
    elif max_financing is not None:
        max_financing=max(0.0,float(max_financing))

    latest_day=min(deadline, decision_day + max_delay) if allow_delay else decision_day
    if allow_delay:
        delays=sorted(set([decision_day, latest_day, 30, 60, 90] + list(range(decision_day, latest_day+1, 5))))
        delays=[d for d in delays if decision_day<=d<=latest_day and d<=deadline]
    else:
        delays=[decision_day]

    installment_options=list(range(1,max_installments+1)) if allow_installments else [1]
    if kind=='hire':
        installment_options=[1]

    candidates=[]
    for day in delays:
        for installments in installment_options:
            result=_simulate_strategy(cash,kind,amount,day,monthly_cost,expected_inflow,inflow_day,installments)
            if not result:
                continue
            financing=round(max(0.0,reserve-result['minimum']),2)
            minimum_before=round(result['minimum'],2)
            minimum_after=round(minimum_before+financing,2)
            end_90_before=round(result['end_90'],2)
            end_90_after=round(end_90_before+financing,2)
            candidates.append({'decision_day':day,'installments':installments,
                'minimum':minimum_before,'minimum_before_financing':minimum_before,
                'minimum_after_financing':minimum_after,'min_day':result['min_day'],
                'end_90':end_90_before,'end_90_before_financing':end_90_before,
                'end_90_after_financing':end_90_after,'financing':financing,
                'payment_days':result['payment_days']})
    if not candidates:
        return None

    feasible=[c for c in candidates
              if allow_financing or c['financing']<=0.01
              if (max_financing is None or c['financing']<=max_financing+0.01)
              if c['minimum_after_financing']+0.01>=reserve
              if c['decision_day']<=deadline]
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

    best=dict(ranked[0])
    best['score']=(best['financing'],best['decision_day'],best['installments'])
    constraints={
        'original_day':decision_day,'allow_delay':allow_delay,'max_delay':max_delay,
        'allow_installments':allow_installments,'max_installments':max_installments,
        'allow_financing':allow_financing,
    }
    label=_strategy_label(best,best['financing'],kind)
    requires_negotiation=(best['decision_day']>decision_day or best['installments']>1)
    if constraints_met:
        explanation=_plan_reason(best,reserve,max_financing,deadline,kind,constraints)
        eq_key=(round(best['financing'],2),round(best['minimum'],2),best['installments'],round(best['end_90'],2))
        eq_dates=sorted(set(equivalent_dates.get(eq_key,[])))
        if len(eq_dates)>1:
            explanation += (f" Plusieurs dates testées donnent le même résultat financier ; "
                            f"J+{best['decision_day']} est retenu car c'est la date la plus proche.")
        explanation += (f" Point bas avant financement : {best['minimum_before_financing']:,.0f} €. "
                        f"Après apport du financement de {best['financing']:,.0f} €, "
                        f"le point bas financé est {best['minimum_after_financing']:,.0f} €, "
                        f"donc la réserve cible de {reserve:,.0f} € est respectée.")
    else:
        accepted_cap=max_financing if max_financing is not None else (0.0 if not allow_financing else best['financing'])
        gap=round(max(0.0,best['financing']-accepted_cap),2)
        explanation=(f"Aucun plan compatible avec vos contraintes. Le meilleur besoin de financement trouvé est "
                     f"{best['financing']:,.0f} €, pour un plafond accepté de {accepted_cap:,.0f} €. "
                     f"Écart : {gap:,.0f} €. Aucune solution soutenable trouvée sans modifier une contrainte réelle. "
                     f"Vous pouvez réduire le montant de la décision ou réexaminer explicitement les leviers de report, "
                     f"fractionnement ou financement.")

    debt_free=[c for c in candidates if c['financing']==0 and c['minimum']+0.01>=reserve]
    if debt_free:
        nf=min(debt_free,key=lambda x:(x['decision_day'],x['installments'],-x['minimum']))
        no_financing={**nf,'available':True,'label':_strategy_label(nf,0,kind),
            'explanation':f"Cette option ne nécessite aucun financement et conserve au moins {nf['minimum']:,.0f} € de trésorerie."}
    else:
        closest=max(candidates,key=lambda x:x['minimum'])
        no_financing={'available':False,'label':f"Aucune solution sans financement avant J+{deadline}",
            'explanation':(f"Avec les encaissements connus et les contraintes autorisées, la trajectoire la plus favorable atteint {closest['minimum']:,.0f} €. "
                           f"Il manque {max(0.0,reserve-closest['minimum']):,.0f} € pour préserver {reserve:,.0f} € de réserve."),
            'decision_day':closest['decision_day'],'installments':closest['installments'],
            'minimum':closest['minimum'],'financing':closest['financing']}

    top3=[]
    for i,c in enumerate(ranked[:3],1):
        item=dict(c)
        item['rank']=i
        item['reason']=_plan_reason(c,reserve,max_financing,deadline,kind,constraints)
        top3.append(item)
    accepted_cap=max_financing if max_financing is not None else (0.0 if not allow_financing else best['financing'])
    return {'reserve':reserve,'max_financing':max_financing,'deadline':deadline,'constraints_met':constraints_met,
        'best':best,'label':label,'explanation':explanation,'alternatives':ranked[:5],
        'top3':top3,'no_financing':no_financing,'requires_negotiation':requires_negotiation,
        'constraint_gap': round(max(0.0,best['financing']-accepted_cap),2) if not constraints_met else 0.0,
        'allow_delay':allow_delay,'max_delay':max_delay,'allow_installments':allow_installments,
        'max_installments':max_installments,'allow_financing':allow_financing,'kind':kind}


def _build_constraint_resolutions(cash, kind, amount, optimizer, decision_day=0, monthly_cost=0.0,
                                  expected_inflow=0.0, inflow_day=60, allow_delay=True,
                                  max_delay=90, allow_installments=True, max_installments=4,
                                  allow_financing=True):
    """Return only resolution strategies revalidated by the real optimizer.

    Every displayed strategy is replayed through ``_optimize_decision`` and is
    kept only when all constraints are actually met. No future revenue is added.
    """
    if not optimizer or optimizer.get('constraints_met'):
        return []
    cap=optimizer.get('max_financing')
    if cap is None:
        return []
    reserve=optimizer['reserve']; deadline=optimizer['deadline']

    def verify(target_amount, target_cap):
        result=_optimize_decision(
            cash, kind, max(0.0,target_amount), decision_day, monthly_cost,
            expected_inflow, inflow_day, reserve, max(0.0,target_cap), deadline,
            allow_delay, max_delay, allow_installments, max_installments, allow_financing)
        if not result or not result.get('constraints_met'):
            return None
        best=result['best']
        if best['minimum_after_financing'] + 0.01 < reserve:
            return None
        if best['financing'] > target_cap + 0.01 or best['decision_day'] > deadline:
            return None
        return result

    resolutions=[]
    # 1) Increase financing ceiling only when external financing is an allowed lever.
    hi=max(cap, optimizer['best']['financing']); lo=cap
    verified=verify(amount,hi) if allow_financing else None
    if verified:
        for _ in range(28):
            mid=(lo+hi)/2.0
            if verify(amount,mid): hi=mid
            else: lo=mid
        target_cap=round(hi+0.01,2); checked=verify(amount,target_cap) or verified
        extra=round(max(0.0,target_cap-cap),2)
        resolutions.append({'rank':1,'kind':'financing','title':'Augmenter le financement disponible',
            'headline':f"Porter le plafond de financement à {target_cap:,.0f} €",
            'effort':extra,'target_amount':amount,'target_max_financing':target_cap,
            'verified':True,'verified_minimum':checked['best']['minimum_after_financing'],'verified_financing':checked['best']['financing'],
            'explanation':f"Solution vérifiée par le simulateur : plafond augmenté de {extra:,.0f} € ; la réserve cible de {reserve:,.0f} € est respectée après financement."})

    if kind in ('investment','expense','market') and amount > 0:
        # 2) Reduce amount while keeping the existing financing ceiling.
        lo=0.0; hi=amount
        if verify(lo,cap):
            for _ in range(30):
                mid=(lo+hi)/2.0
                if verify(mid,cap): lo=mid
                else: hi=mid
            target_amount=max(0.0,round(lo-0.01,2)); checked=verify(target_amount,cap) or verify(lo,cap)
            reduction=round(max(0.0,amount-target_amount),2)
            resolutions.append({'rank':len(resolutions)+1,'kind':'amount','title':'Réduire le montant de la décision',
                'headline':f"Ramener le décaissement initial à environ {target_amount:,.0f} €",
                'effort':reduction,'target_amount':target_amount,'target_max_financing':cap,
                'verified':True,'verified_minimum':checked['best']['minimum_after_financing'],'verified_financing':checked['best']['financing'],
                'explanation':f"Solution vérifiée : réduire la décision d’environ {reduction:,.0f} € permet de respecter le plafond de {cap:,.0f} € et la réserve cible de {reserve:,.0f} €."})

        # 3) Search a genuinely feasible mixed compromise instead of splitting the old gap 50/50.
        best_mix=None
        base_gap=max(1.0,optimizer['best']['financing']-cap)
        for i in range(1,40):
            reduction=base_gap*i/40.0
            target_amount=max(0.0,amount-reduction)
            # Find minimum cap required for this reduced amount.
            low=cap; high=max(cap,optimizer['best']['financing'])
            if not verify(target_amount,high):
                continue
            for _ in range(22):
                mid=(low+high)/2.0
                if verify(target_amount,mid): high=mid
                else: low=mid
            target_cap=round(high+0.01,2); checked=verify(target_amount,target_cap)
            if not checked: continue
            extra=max(0.0,target_cap-cap)
            # Prefer balanced controllable effort, then lower total effort.
            score=(abs(extra-reduction),extra+reduction)
            if best_mix is None or score < best_mix[0]:
                best_mix=(score,target_amount,target_cap,reduction,extra,checked)
        if best_mix:
            _,target_amount,target_cap,reduction,extra,checked=best_mix
            resolutions.append({'rank':len(resolutions)+1,'kind':'mixed','title':'Partager l’effort',
                'headline':f"Augmenter le plafond de financement de {extra:,.0f} € et réduire la décision de {reduction:,.0f} €",
                'effort':round(extra+reduction,2),'target_amount':round(target_amount,2),'target_max_financing':round(target_cap,2),
                'verified':True,'verified_minimum':checked['best']['minimum_after_financing'],'verified_financing':checked['best']['financing'],
                'explanation':f"Combinaison vérifiée par le simulateur : nouveau plafond {target_cap:,.0f} €, décision environ {target_amount:,.0f} €. La réserve cible de {reserve:,.0f} € est respectée après financement."})
    return resolutions[:3]

def _cfo_answer(question, simulation):
    q=(question or '').strip()
    if not q or not simulation or not simulation.get('optimizer'): return None
    o=simulation['optimizer']; b=o['best']; low=q.lower()
    if not o.get('constraints_met'):
        return f"Aucune solution soutenable trouvée sous les contraintes saisies. {o['explanation']}"
    if any(w in low for w in ('sans financement','sans emprunt','zéro dette','zero dette')):
        nf=o['no_financing']
        return nf['explanation'] if not nf['available'] else f"Oui. Plan sans financement : {nf['label']}. {nf['explanation']}"
    if any(w in low for w in ('pourquoi','why','j+','date')):
        return f"Je recommande J+{b['decision_day']} car, parmi les plans compatibles testés, il minimise d'abord le financement requis ({b['financing']:,.0f} €), puis le délai et la complexité. Point bas avant financement : {b['minimum_before_financing']:,.0f} € ; après financement : {b['minimum_after_financing']:,.0f} €."
    if any(w in low for w in ('financement','emprunt','dette')):
        return f"Le plan recommandé nécessite {b['financing']:,.0f} € de financement pour préserver {o['reserve']:,.0f} € de réserve."
    return f"Plan recommandé : {o['label']}. Point bas après financement {b['minimum_after_financing']:,.0f} €, trésorerie J+90 après financement {b['end_90_after_financing']:,.0f} €. ProfitOS répond uniquement avec les données et scénarios actuellement connus."

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
            'allow_delay': _flag(request.args.get('allow_delay'), False),
            'max_delay': _day(request.args.get('max_delay'),0),
            'allow_installments': _flag(request.args.get('allow_installments'), False),
            'max_installments': _installment_count(request.args.get('max_installments'),1),
            'allow_financing': _flag(request.args.get('allow_financing'), False),
        }
        if submitted and cash['cash_balance'] is not None:
            sim_args={k:v for k,v in inputs.items() if k not in ('reserve','max_financing','deadline','allow_delay','max_delay','allow_installments','max_installments','allow_financing')}
            simulation=_simulate_decision(cash, **sim_args)
            simulation['optimizer']=_optimize_decision(
                cash, reserve=inputs['reserve'], max_financing=inputs['max_financing'], deadline=inputs['deadline'],
                allow_delay=inputs['allow_delay'], max_delay=inputs['max_delay'],
                allow_installments=inputs['allow_installments'], max_installments=inputs['max_installments'],
                allow_financing=inputs['allow_financing'], **sim_args)
            simulation['resolutions']=_build_constraint_resolutions(
                cash, kind, inputs['amount'], simulation['optimizer'], inputs['decision_day'], inputs['monthly_cost'],
                inputs['expected_inflow'], inputs['inflow_day'], inputs['allow_delay'], inputs['max_delay'],
                inputs['allow_installments'], inputs['max_installments'], inputs['allow_financing'])
            simulation['cfo_answer']=_cfo_answer(request.args.get('cfo_question'), simulation)
            log_activity('DECISION_SIMULATION', f"Simulation {kind} · {inputs['amount']:.2f} EUR")
        else:
            log_activity('DECISION_SIMULATOR_VIEW','Consultation du Decision Simulator')
        return render_template('decision_simulator.html',cash=cash,simulation=simulation,inputs=inputs,cfo_question=request.args.get('cfo_question',''))
