"""v444 — Simulateur de décision : TVA du décaissement (payée puis récupérée le 15 du
mois suivant), recette mensuelle, rentabilité au-delà de 90 jours, coût du prêt,
test de résistance (scénario prudent), graphique avant / après, montants français."""
from datetime import date

from profitos.routes import decision_simulator as ds
from tests.test_v421_invoice_compliance import _no_boamp, app, _post, _signup  # noqa: F401


def _cash(values=None):
    values = values or [20000.0] * 91
    return {'curves': [{'mode': 'prudent', 'values': [v - 3000 for v in values], 'minimum': min(values) - 3000, 'end_90': values[-1] - 3000},
                       {'mode': 'probable', 'values': values, 'minimum': min(values), 'end_90': values[-1]}],
            'monthly_burn': 0.0, 'cash_balance': values[0]}


def test_vat_is_paid_then_recovered_on_the_15th_of_next_month():
    today = date(2026, 10, 10)
    assert ds._vat_recovery_day(0, today) == 36        # payé le 10/10, récupéré le 15/11
    assert ds._vat_recovery_day(80, today) is None     # récupération après J+90
    r = ds._simulate_decision(_cash(), 'investment', 10000, 0, vat_rate=20.0, today=today)
    assert r['values'][0] == 8000.0 and r['values'][35] == 8000.0 and r['values'][36] == 10000.0
    assert r['minimum'] == 8000.0 and r['vat_amount'] == 2000.0 and r['vat_back_day'] == 36
    hire = ds._simulate_decision(_cash(), 'hire', 3000, 0, vat_rate=20.0, today=today)
    assert hire['values'][0] == 17000.0  # pas de TVA sur une embauche


def test_monthly_revenue_starts_one_month_after_the_decision():
    r = ds._simulate_decision(_cash(), 'market', 0, 0, monthly_cost=0, monthly_revenue=3000)
    assert r['values'][30] == 20000.0 and r['values'][60] == 23000.0 and r['values'][90] == 26000.0


def test_profitability_and_loan_cost():
    assert ds.loan_payment(10000, 0, 10) == 1000.0
    assert ds.loan_payment(48000, 4.5, 48) == 1094.57
    fc = ds._financing_cost(48000, 4.5, 48)
    assert fc['interest'] == round(1094.57 * 48 - 48000, 2)
    p = ds._profitability('investment', 40000, 500, 2500, 0, 36)
    assert p['net_monthly'] == 2000.0 and p['payback'] == 20.0 and p['total'] == 32000.0 and p['positive']
    cost_only = ds._profitability('expense', 5000, 0, 0, 0, 36)
    assert cost_only['payback'] is None and 'est un coût' in cost_only['verdict']
    loss = ds._profitability('hire', 0, 4000, 3000, 0, 12)
    assert 'ne se rembourse pas' in loss['verdict'] or loss['payback'] == 0.0


def test_amounts_accept_french_format():
    assert ds._num('12 500,50') == 12500.5 and ds._num('') == 0.0 and ds._num(None, 7.0) == 7.0
    assert ds._vat('5,5') == 5.5 and ds._vat('17') == 20.0


def test_page_renders_chart_stress_and_profitability(app):
    client, org_id = _signup(app)
    _post(client, '/cash-intelligence', {'action': 'balance', 'cash_balance': '30 000'})
    url = ('/decision-simulator?simulate=1&kind=investment&amount=20+000&vat_rate=20&decision_day=0&monthly_cost=300'
           '&monthly_revenue=1+500&expected_inflow=0&inflow_day=60&reserve=5+000&deadline=90&allow_delay=0'
           '&allow_installments=0&allow_financing=0&allow_financing=1&max_financing=20000&horizon_months=36&loan_rate=4,5&loan_months=48')
    html = client.get(url).get_data(as_text=True)
    assert 'IMPACT SUR VOTRE TRÉSORERIE' in html and 'class="cash-line prudent"' in html
    assert 'Test de résistance' in html
    assert 'RENTABILITÉ SUR 36 MOIS' in html and 'La décision se rembourse en' in html
    assert 'TVA du décaissement' in html
    blank = client.get('/decision-simulator').get_data(as_text=True)
    assert '<option value="20.0" selected>20 %</option>' in blank
