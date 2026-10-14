import re
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


_SOURCE_CATEGORY={'purchase_invoice':'suppliers','expense':'expenses'}


def _curve_events(receivables, scheduled_outflows=None, inflows=None, mode='probable', top_delay=None, today=None):
    """Flux datés d'un scénario : (jour 1..90, montant signé, catégorie). Source unique des
    courbes, de la cascade « du solde actuel à J+90 » et du plan de trésorerie mensuel."""
    today=today or date.today()
    rule=SCENARIO_RULES.get(mode,SCENARIO_RULES['probable'])
    events=[]
    for item in (scheduled_outflows or []):
        day=_horizon_day(item.get('day',0))
        if day is not None:
            cat=item.get('cat') or _SOURCE_CATEGORY.get(item.get('source'),'planned_out')
            events.append((day,-_safe_float(item.get('amount')),cat))
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
        events.append((day,_safe_float(r['expected_amount'])*rule['factor'],'clients'))
    # Encaissements prévus hors factures (abonnements, subvention…) : même règle de
    # prudence que les factures clients (décalage et part comptée selon le scénario).
    for item in (inflows or []):
        expected=_iso_date(item.get('date'))
        if expected is None:
            continue
        day=_horizon_day((expected-today).days+rule['shift'])
        if day is None:
            continue
        events.append((day,_safe_float(item.get('amount'))*rule['factor'],item.get('cat') or 'planned_in'))
    return events


def _simulate_curve(cash, daily_burn, receivables, scheduled_outflows=None, mode='probable', top_delay=None, today=None, inflows=None):
    """Courbe 0..90 j. Les hypothèses restent explicites et déterministes.

    top_delay : délai simulé (jours) pour la plus grosse créance (receivables[0]) ; -1 = jamais
    sur l'horizon. Les autres créances suivent la règle du scénario."""
    by_day={}
    for day,amount,_cat in _curve_events(receivables,scheduled_outflows,inflows,mode,top_delay,today):
        by_day[day]=by_day.get(day,0.0)+amount
    values=[round(cash,2)]; running=cash
    minimum=cash; min_day=0
    for day in range(1,HORIZON_DAYS+1):
        running-=daily_burn
        running+=by_day.get(day,0.0)
        running=round(running,2)
        values.append(running)
        if running<minimum:
            minimum=running; min_day=day
    return {
        'mode':mode,'values':values,'points':_curve_points(values),
        'minimum':round(minimum,2),'min_day':min_day,'end_90':round(running,2),
    }


def _breakdown(cash, curve, events, include_burn):
    """Cascade du scénario probable : total par catégorie sur 90 j. La dépense courante est
    le solde exact entre le départ, les flux et l'arrivée de la courbe (pas d'écart d'arrondi)."""
    from profitos.treasury import CATEGORIES, CATEGORY_ORDER
    totals={}
    for _day,amount,cat in events:
        totals[cat]=totals.get(cat,0.0)+amount
    burn=round(cash+sum(totals.values())-curve['end_90'],2) if include_burn else 0.0
    if abs(burn)>=0.005:
        totals['burn']=-burn
    rows=[]
    for cat in CATEGORY_ORDER:
        if abs(totals.get(cat,0.0))>=0.005:
            rows.append({'category':cat,'label':CATEGORIES[cat][1],'amount':round(totals[cat],2)})
    inflow=round(sum(r['amount'] for r in rows if r['amount']>0),2)
    outflow=round(-sum(r['amount'] for r in rows if r['amount']<0),2)
    scale=max([abs(r['amount']) for r in rows]+[abs(cash),1.0])
    for r in rows:
        r['pct']=round(abs(r['amount'])*100/scale,1)
    return {'start':round(cash,2),'rows':rows,'inflow':inflow,'outflow':outflow,'end':curve['end_90'],
            'start_pct':round(abs(cash)*100/scale,1),'end_pct':round(abs(curve['end_90'])*100/scale,1)}


def _monthly_plan(curve, events, today):
    """Plan de trésorerie par mois civil sur l'horizon (scénario probable) : solde d'ouverture,
    flux par catégorie, dépense courante (par différence) et solde de clôture = courbe."""
    from profitos.treasury import CATEGORIES, CATEGORY_ORDER
    months=[]; seg_start=1
    for day in range(1,HORIZON_DAYS+1):
        d=today+timedelta(days=day)
        last=(day==HORIZON_DAYS) or (today+timedelta(days=day+1)).month!=d.month
        if last:
            months.append((seg_start,day,d)); seg_start=day+1
    names=['janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre']
    plan=[]; used=set()
    for a,b,d in months:
        cats={}
        for day,amount,cat in events:
            if a<=day<=b:
                cats[cat]=cats.get(cat,0.0)+amount
        opening=curve['values'][a-1]; closing=curve['values'][b]
        burn=round(opening+sum(cats.values())-closing,2)
        if abs(burn)>=0.005: cats['burn']=-burn
        cats={k:round(v,2) for k,v in cats.items() if abs(v)>=0.005}
        used.update(cats)
        first=today+timedelta(days=a); lastd=today+timedelta(days=b)
        plan.append({'label':f"{names[d.month-1].capitalize()} {d.year}",'from':first.isoformat(),'to':lastd.isoformat(),
                     'partial':first.day!=1 or (lastd+timedelta(days=1)).day!=1,'opening':opening,'closing':closing,
                     'inflow':round(sum(v for v in cats.values() if v>0),2),'outflow':round(-sum(v for v in cats.values() if v<0),2),
                     'cats':cats,'minimum':min(curve['values'][a-1:b+1])})
    rows=[{'category':c,'label':CATEGORIES[c][1],'direction':CATEGORIES[c][0]} for c in CATEGORY_ORDER if c in used]
    return {'months':plan,'rows':rows}


def compute_cash_forecast(cash_balance, cash_as_of, sales, purchases, expenses, einvoice_hold=None, today=None,
                          extra_flows=None, client_delays=None, include_burn=True, safety_threshold=0.0):
    """Calcul pur (sans base de données) de la prévision 90 j.

    Une seule fonction de projection (_simulate_curve) alimente TOUS les chiffres de la page :
    KPI 30/60/90 j, point bas, alerte, courbes et What-if. Les KPI correspondent au scénario
    probable, ce qui garantit leur cohérence avec la courbe bleue.

    extra_flows   : flux hors factures (profitos.treasury : emprunts, crédit-bail, TVA, abonnements,
                    flux prévus) ; client_delays : retard moyen de paiement par client (clé norm()) ;
    include_burn  : compter ou non la dépense courante observée (à couper si les charges
                    récurrentes sont saisies en flux prévus, pour ne pas les compter deux fois) ;
    safety_threshold : seuil de sécurité de trésorerie choisi par l'entreprise."""
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
              'paid':paid if paid>.005 else 0.0,'overdue_days':max(0,(today-due).days),'in_horizon':day<=HORIZON_DAYS,'cat':'suppliers'}
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
                'description':e['description'],'category':e['category'],'source':'expense','cat':'expenses'})
    other_outflows=[]; inflows=[]
    from profitos.treasury import CATEGORIES
    for f in (extra_flows or []):
        d=_iso_date(f.get('date'))
        if not d or _safe_float(f.get('amount'))<=.005: continue
        delta=max(0,(d-today).days)
        if delta>HORIZON_DAYS: continue
        if f.get('direction')=='in':
            inflows.append({'date':max(d,today).isoformat(),'amount':round(_safe_float(f['amount']),2),'label':f.get('label'),
                            'cat':f.get('category') or 'planned_in','source':f.get('source')})
        else:
            item={'date':max(d,today).isoformat(),'day':delta,'amount':round(_safe_float(f['amount']),2),'vendor':f.get('label'),
                  'description':f.get('label'),'category':CATEGORIES.get(f.get('category'),('out','Décaissement prévu'))[1],
                  'source':f.get('source') or 'planned','cat':f.get('category') or 'planned_out','overdue':bool(f.get('overdue'))}
            scheduled_outflows.append(item); other_outflows.append(item)
    observed_90=sum(recent); daily_burn=observed_90/90.0 if recent else 0.0; monthly_burn=daily_burn*30.0
    if not include_burn: daily_burn=0.0
    planned_outflows_90=round(sum(x['amount'] for x in scheduled_outflows),2)
    receivables=[]
    held_receivables=[]
    for inv in sales:
        # Reste à encaisser = TTC − règlements − avoirs émis (un avoir réduit la créance).
        credited=_safe_float(inv['credited_total']) if 'credited_total' in inv.keys() else 0.0
        amount=max(0.0,round(_safe_float(inv['total'])-_safe_float(inv['paid_total'])-credited,2))
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
        # Habitude de paiement : un client qui règle en moyenne 12 j après l'échéance est
        # attendu 12 j après l'échéance (factures pas encore échues uniquement).
        habit=(client_delays or {}).get(norm(inv['client_name'])) if due>=today else None
        if habit and habit['delay']:
            expected=max(today,due+timedelta(days=habit['delay']))
        offset=(expected-today).days
        receivables.append({'id':inv['id'],'invoice_number':inv['invoice_number'],'customer':inv['client_name'],
            'amount':amount,'expected_amount':amount,'expected_date':expected.isoformat(),'due_date':due.isoformat(),
            'days_overdue':overdue,'in_horizon':offset<=HORIZON_DAYS,'suspicious_date':offset>SUSPICIOUS_DUE_DAYS,
            'payment_habit':habit,
            'source':(inv.get('source') if isinstance(inv,dict) else None) or 'outgoing_invoice'})
    receivables.sort(key=lambda r:r['amount'],reverse=True)
    receivables_in_horizon=[r for r in receivables if r['in_horizon']]
    receivables_beyond=sorted((r for r in receivables if not r['in_horizon']),key=lambda r:r['expected_date'])

    horizons={30:0.0,60:0.0,90:0.0}
    min_cash=None; min_day=None
    curves=[]; chart=None; breakdown=None; plan=None; runway=None
    threshold=max(0.0,_safe_float(safety_threshold))
    threshold_day=None
    if cash is not None:
        for mode in ('prudent','probable','optimiste'):
            curves.append(_simulate_curve(cash,daily_burn,receivables,scheduled_outflows=scheduled_outflows,mode=mode,today=today,inflows=inflows))
        probable=curves[1]
        for h in horizons:
            horizons[h]=probable['values'][h]
        min_cash=probable['minimum']; min_day=probable['min_day']
        events=_curve_events(receivables,scheduled_outflows,inflows,'probable',None,today)
        breakdown=_breakdown(cash,probable,events,daily_burn>0)
        plan=_monthly_plan(probable,events,today)
        if threshold>0:
            threshold_day=next((d for d,v in enumerate(probable['values']) if v<threshold),None)
        # Autonomie : au rythme net des 90 prochains jours, nombre de mois avant d'épuiser la
        # trésorerie (None si la trésorerie progresse ou est déjà négative).
        net_90=probable['end_90']-cash
        if net_90<0 and cash>0:
            runway={'days':int(cash/(-net_90/90.0)),'monthly_net':round(net_90/3,2)}
        lo,hi=_chart_scale(curves+([{'values':[threshold]}] if threshold>0 else []))
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
        threshold_y=round(18+184*(hi-threshold)/max(hi-lo,1.0),1) if threshold>0 and lo<threshold<hi else None
        chart={'lo':lo,'hi':hi,'ticks':ticks,'zero_y':zero_y,'threshold_y':threshold_y,'left':chart_left,
               'x30':round(chart_left+(920-chart_left-18)*30/90,1),'x60':round(chart_left+(920-chart_left-18)*60/90,1)}

    top=receivables[0] if receivables else None; scenarios=[]
    if cash is not None and top:
        for delay in (7,30,60):
            curve=_simulate_curve(cash,daily_burn,receivables,scheduled_outflows=scheduled_outflows,mode='probable',top_delay=delay,today=today,inflows=inflows)
            scenarios.append({'delay':delay,'minimum':curve['minimum'],'min_day':curve['min_day'],'end_90':curve['end_90'],'day30':curve['values'][30],'day60':curve['values'][60]})

    as_of=_iso_date(cash_as_of)
    balance_age_days=(today-as_of).days if as_of else None
    balance_stale=cash is not None and (balance_age_days is None or balance_age_days>STALE_BALANCE_DAYS)
    overdue_payables=[p for p in supplier_payables if p['overdue_days']>0]

    risk_day=(today+timedelta(days=min_day)).isoformat() if min_cash is not None and min_cash<0 else None
    if cash is None: alert_level='INCOMPLET'; alert='Renseignez le solde bancaire actuel pour activer la prévision.'
    elif min_cash is not None and min_cash<0: alert_level='ALERTE'; alert=f"Tension de trésorerie projetée autour du {date.fromisoformat(risk_day):%d/%m/%Y}."
    elif threshold_day is not None:
        alert_level='VIGILANCE'
        alert=(f"La trésorerie passe sous votre seuil de sécurité ({fr_number(threshold)} €) autour du "
               f"{(today+timedelta(days=threshold_day)).strftime('%d/%m/%Y')}.")
    elif horizons[30] < max(monthly_burn*.5,1000): alert_level='VIGILANCE'; alert='Marge de sécurité de trésorerie faible à 30 jours.'
    elif balance_stale:
        alert_level='VIGILANCE'
        alert=(f"Aucune tension détectée, mais le solde de départ date de {balance_age_days} jours : mettez-le à jour pour fiabiliser la prévision."
               if balance_age_days is not None else "Aucune tension détectée, mais la date du solde de départ est inconnue : mettez-le à jour.")
    else: alert_level='STABLE'; alert='Aucune tension détectée sur les données actuellement connues.'
    # Scénario probable sans tension, mais le prudent (clients en retard) en crée une : on le
    # dit, avec la date, plutôt que d'afficher « stable » sans réserve.
    prudent_note=None
    if cash is not None and alert_level in ('STABLE','VIGILANCE') and threshold_day is None and curves:
        prudent=curves[0]; limit=max(threshold,0.0)
        day=next((d for d,v in enumerate(prudent['values']) if v<(0.0 if prudent['minimum']<0 else limit)),None)
        if day is not None and (prudent['minimum']<0 or threshold>0):
            when=(today+timedelta(days=day)).strftime('%d/%m/%Y')
            prudent_note=(f"En scénario prudent (clients qui paient en retard), la trésorerie deviendrait négative autour du {when}."
                          if prudent['minimum']<0 else
                          f"En scénario prudent (clients qui paient en retard), elle passerait sous votre seuil de sécurité autour du {when}.")
            alert=(alert+' ' if alert_level=='VIGILANCE' else 'Pas de tension dans le scénario probable. ')+prudent_note
            alert_level='VIGILANCE'
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
        'top_receivable':top,'scenarios':scenarios,'inflows':inflows,'other_outflows':other_outflows,
        'breakdown':breakdown,'plan':plan,'runway':runway,'safety_threshold':threshold,'threshold_day':threshold_day,
        'burn_included':include_burn,'prudent_note':prudent_note,
        'min_cash':min_cash,'min_day':min_day,'risk_day':risk_day,'alert_level':alert_level,'alert':alert,'curves':curves,'chart':chart,
        'method_note':"Prévision calculée par entité à partir du solde disponible, des factures clients restant à encaisser (émises dans ProfitOS ou importées, datées selon l'habitude de paiement de chaque client quand elle est connue), des factures récurrentes à venir, des factures fournisseurs restant à payer, des échéances d'emprunts et de crédit-bail, de la TVA estimée d'après la comptabilité, des flux prévus que vous avez saisis et des dépenses futures enregistrées. Les paiements partiels sont déduits. Les montants à 30 / 60 / 90 jours correspondent au scénario probable. Les flux attendus après 90 jours sont exclus ; les échéances déjà dépassées sont comptées dès J+1. Les scénarios modifient le timing des encaissements (et, en prudent, la part comptée), jamais les écritures comptables."}


def build_cash_intelligence(with_extras=False):
    """Prévision 90 j fondée sur les soldes réellement ouverts de l'entité active.
    with_extras : ajoute les éléments propres à la page Trésorerie (flux prévus saisis,
    suggestions repérées sur les relevés, réalisé bancaire, solde bancaire plus récent)."""
    from profitos import treasury as T
    c=cx()
    try:
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        ef='entity_id=?' if eid else 'entity_id IS NULL'; ep=(eid,) if eid else ()
        today=date.today()
        settings=_cash_settings(c)
        tsettings=T.get_settings(c,eid)
        # Créances : vue unique (factures émises ET factures importées), cf. profitos/receivables.py.
        from profitos.receivables import open_receivables
        sales=[{'id':r['id'],'invoice_number':r['invoice_number'],'client_name':r['customer'],
                'total':r['total'],'paid_total':r['paid'],'credited_total':r['credited'],
                'due_date':(r['retention_release_date'] if r['kind']=='RETENTION' and r['retention_release_date'] else r['due_date']),
                'issue_date':r['issue_date'],'einvoice_hold':r['einvoice_hold'],
                'source':'outgoing_invoice' if r['source']=='outgoing' else 'imported_invoice'}
               for r in open_receivables(c,eid)]
        purchases=c.execute(
            "SELECT pi.*,COALESCE((SELECT SUM(p.amount) FROM purchase_invoice_payments p WHERE p.purchase_invoice_id=pi.id AND COALESCE(CAST(p.entity_id AS TEXT),'') = COALESCE(CAST(pi.entity_id AS TEXT),'')),0) paid_total "
            "FROM purchase_invoices pi WHERE pi.entity_id IS ? AND COALESCE(pi.status,'unpaid')!='paid' ORDER BY pi.due_date,pi.id",(eid,)).fetchall()
        expenses=c.execute(f"SELECT vendor,description,amount,expense_date,category FROM expenses WHERE expense_date IS NOT NULL AND {ef} ORDER BY expense_date DESC",ep).fetchall()
        extra_flows=T.collect_flows(c,eid,today)
        try:
            client_delays=T.client_payment_delays(c,eid)
        except Exception:
            c.rollback(); client_delays={}
        extras={}
        if with_extras:
            planned=T.list_planned(c,eid)
            keys={p['label_key'] for p in planned if p['label_key']}
            try:
                suggestions=T.detect_recurring(c,eid,today,keys,tsettings['dismissed'])
                realized=T.realized_by_month(c,eid,today)
                bank=T.latest_bank_balance(c,eid)
            except Exception:
                c.rollback(); suggestions=[]; realized=[]; bank=None
            extras={'planned':planned,'suggestions':suggestions,'realized':realized,'bank_balance':bank}
    finally: c.close()
    result=compute_cash_forecast(settings['cash_balance'],settings['cash_as_of'],sales,purchases,expenses,
                                 einvoice_hold=lambda inv:inv.get('einvoice_hold'),today=today,extra_flows=extra_flows,
                                 client_delays=client_delays,include_burn=tsettings['include_observed_burn'],
                                 safety_threshold=tsettings['safety_threshold'])
    result.update(extras)
    bank=extras.get('bank_balance')
    result['bank_balance_newer']=bool(bank and (not result['cash_as_of'] or bank['as_of']>str(result['cash_as_of'])[:10]
                                                 or (bank['as_of']==str(result['cash_as_of'])[:10] and abs(bank['balance']-(result['cash_balance'] or 0))>.005)))
    return result


def _plan_rows(cash):
    """Plan de trésorerie mensuel à plat (export Excel / CSV)."""
    plan=cash.get('plan') or {'months':[],'rows':[]}
    out=[{'Ligne':'Solde d\'ouverture',**{m['label']:m['opening'] for m in plan['months']}}]
    for r in plan['rows']:
        out.append({'Ligne':('+ ' if r['direction']=='in' else '− ')+r['label'],**{m['label']:m['cats'].get(r['category'],0.0) for m in plan['months']}})
    out.append({'Ligne':'Total encaissements',**{m['label']:m['inflow'] for m in plan['months']}})
    out.append({'Ligne':'Total décaissements',**{m['label']:-m['outflow'] for m in plan['months']}})
    out.append({'Ligne':'Solde de clôture',**{m['label']:m['closing'] for m in plan['months']}})
    out.append({'Ligne':'Point bas du mois',**{m['label']:m['minimum'] for m in plan['months']}})
    return out


def register(app):
    @app.route('/cash-intelligence',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @requires_feature('advanced_ai')
    def cash_intelligence():
        if request.method=='POST':
            return _handle_post()
        cash=build_cash_intelligence(with_extras=True)
        raw_delay=(request.args.get('payment_delay') or '').strip()
        custom=None
        if cash['cash_balance'] is not None and cash['top_receivable'] and raw_delay:
            mapping={'today':0,'7':7,'30':30,'60':60,'never':-1}
            if raw_delay in mapping:
                daily_burn=cash['daily_burn']
                custom=_simulate_curve(cash['cash_balance'],daily_burn,cash['receivables'],scheduled_outflows=cash.get('scheduled_outflows',[]),mode='probable',top_delay=mapping[raw_delay],inflows=cash.get('inflows'))
                custom['label']={'today':"Aujourd'hui",'7':'Sous 7 jours','30':'Sous 30 jours','60':'Sous 60 jours','never':"Pas d'encaissement sur 90 j"}[raw_delay]
                custom['choice']=raw_delay
        from profitos import treasury as T
        log_activity('CASH_INTELLIGENCE_VIEW','Consultation de Trésorerie & scénarios')
        return render_template('cash_intelligence.html',cash=cash,custom=custom,today=date.today().isoformat(),
                               planned_categories=T.PLANNED_CATEGORIES,frequencies=T.FREQUENCIES)

    @app.route('/cash-intelligence/plan')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @requires_feature('advanced_ai')
    def cash_plan_export():
        cash=build_cash_intelligence()
        if cash['cash_balance'] is None:
            flash('Renseignez le solde bancaire pour exporter le plan de trésorerie.')
            return redirect(url_for('cash_intelligence'))
        return export_response(_plan_rows(cash),f"plan-de-tresorerie-{date.today().isoformat()}")

    def _handle_post():
        from profitos import treasury as T
        from profitos.entities import set_cash_balance, current_entity_id
        eid=current_entity_id()
        action=request.form.get('action') or 'balance'
        c=cx()
        try:
            if action=='balance':
                balance=parse_amount(request.form.get('cash_balance'))
                if balance is None:
                    flash('Solde bancaire invalide.')
                else:
                    set_cash_balance(c, eid, balance, date.today().isoformat(), now())
                    log_activity('CASH_BALANCE_UPDATE','Mise à jour manuelle du solde de trésorerie')
                    flash('Solde de trésorerie mis à jour.')
            elif action=='bank_balance':
                bank=T.latest_bank_balance(c,eid)
                if not bank:
                    flash('Aucun solde bancaire disponible pour cette entité.')
                else:
                    set_cash_balance(c, eid, bank['balance'], bank['as_of'], now())
                    log_activity('CASH_BALANCE_UPDATE','Solde de trésorerie repris des comptes bancaires')
                    flash(f"Solde bancaire de {fr_number(bank['balance'],2)} € au {date.fromisoformat(bank['as_of']):%d/%m/%Y} repris dans la prévision.")
            elif action=='settings':
                raw=(request.form.get('safety_threshold') or '').strip()
                threshold=parse_amount(raw) if raw else 0.0
                if threshold is None or threshold<0:
                    flash('Seuil de sécurité invalide.')
                else:
                    T.save_settings(c,eid,now(),safety_threshold=threshold,include_observed_burn=request.form.get('include_observed_burn')=='1')
                    flash('Réglages de la prévision enregistrés.')
            elif action in ('add_flow','accept_suggestion'):
                label=(request.form.get('label') or '').strip()[:120]
                category=request.form.get('category') or ''
                amount=parse_amount(request.form.get('amount'))
                frequency=request.form.get('frequency') if request.form.get('frequency') in T.FREQUENCIES else 'monthly'
                start=_iso_date(request.form.get('start_date'))
                end=_iso_date(request.form.get('end_date')) if (request.form.get('end_date') or '').strip() else None
                if category not in T.PLANNED_CATEGORIES or not label or amount is None or amount<=0 or not start:
                    flash('Flux prévu incomplet : libellé, catégorie, montant positif et date sont obligatoires.')
                elif end and end<start:
                    flash('La date de fin doit suivre la première échéance.')
                else:
                    direction=T.PLANNED_CATEGORIES[category][0]
                    key=(request.form.get('suggestion_key') or '').strip() or T.label_key(label)
                    c.execute("""INSERT INTO treasury_planned_flows(entity_id,label,label_key,category,direction,amount,start_date,frequency,
                                 end_date,source,active,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                              (eid,label,key,category,direction,round(amount,2),start.isoformat(),frequency,
                               end.isoformat() if end else None,'bank_detected' if action=='accept_suggestion' else 'manual',
                               now(),session.get('user_email') or session.get('email')))
                    c.commit()
                    log_activity('CASH_PLANNED_FLOW_ADD',f"Flux prévu ajouté : {label}")
                    flash(f"Flux prévu « {label} » ajouté à la prévision.")
            elif action=='delete_flow':
                try: fid=int(request.form.get('flow_id') or 0)
                except ValueError: fid=0
                c.execute("UPDATE treasury_planned_flows SET active=0 WHERE id=? AND entity_id IS ?",(fid,eid))
                c.commit(); flash('Flux prévu retiré de la prévision.')
            elif action=='dismiss_suggestion':
                key=(request.form.get('suggestion_key') or '').strip()
                direction=request.form.get('direction') if request.form.get('direction') in ('in','out') else 'out'
                if key:
                    cur=T.get_settings(c,eid)['dismissed']
                    T.save_settings(c,eid,now(),dismissed=cur+[f"{direction}:{key}"])
                flash('Suggestion masquée.')
        finally:
            c.close()
        anchor=request.form.get('anchor') or ''
        return redirect(url_for('cash_intelligence')+(anchor if re.fullmatch(r'#[a-z-]{1,30}',anchor) else ''))
