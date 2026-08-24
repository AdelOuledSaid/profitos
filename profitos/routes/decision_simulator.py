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
        }
        if submitted and cash['cash_balance'] is not None:
            simulation=_simulate_decision(cash, **inputs)
            log_activity('DECISION_SIMULATION', f"Simulation {kind} · {inputs['amount']:.2f} EUR")
        else:
            log_activity('DECISION_SIMULATOR_VIEW','Consultation du Decision Simulator')
        return render_template('decision_simulator.html',cash=cash,simulation=simulation,inputs=inputs)
