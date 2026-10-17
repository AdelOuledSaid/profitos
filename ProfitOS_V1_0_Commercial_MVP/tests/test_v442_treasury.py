"""v442 — Trésorerie pro : flux hors factures (emprunts, crédit-bail, TVA, abonnements,
flux prévus), habitudes de paiement des clients, seuil de sécurité, autonomie, cascade
« du solde à J+90 », plan mensuel, charges récurrentes repérées sur les relevés."""
import io
from datetime import date, timedelta

import pytest

from profitos.routes.cash_intelligence import compute_cash_forecast
from profitos import treasury as T
from tests.test_v421_invoice_compliance import _no_boamp, app, _post, _flashes, _signup  # noqa: F401

TODAY = date(2026, 10, 10)


def _inv(i, client, total, due, paid=0.0):
    return {'id': i, 'invoice_number': f'FA-{i}', 'client_name': client, 'total': total, 'paid_total': paid,
            'due_date': due, 'issue_date': '2026-09-01'}


def _fc(**kw):
    args = dict(cash_balance=10000, cash_as_of=TODAY.isoformat(), sales=[], purchases=[], expenses=[], today=TODAY)
    args.update(kw)
    return compute_cash_forecast(**args)


# ------------------------------------------------------------------ moteur
def test_extra_flows_move_the_curve_and_the_cascade_adds_up():
    flows = [{'date': '2026-10-20', 'amount': 1500, 'direction': 'out', 'category': 'loans', 'label': 'Prêt', 'source': 'loan'},
             {'date': '2026-11-05', 'amount': 4000, 'direction': 'in', 'category': 'planned_in', 'label': 'Subvention', 'source': 'planned'}]
    f = _fc(sales=[_inv(1, 'Alpha', 2000, '2026-10-30')], extra_flows=flows)
    prob = next(c for c in f['curves'] if c['mode'] == 'probable')
    assert prob['values'][10] == 8500.0  # prêt payé le 20/10
    assert prob['end_90'] == 10000 - 1500 + 4000 + 2000
    b = f['breakdown']
    assert round(b['start'] + sum(r['amount'] for r in b['rows']), 2) == b['end'] == prob['end_90']
    assert {r['category'] for r in b['rows']} == {'clients', 'planned_in', 'loans'}
    # Le prudent compte 55 % des encaissements prévus, décalés de 15 jours.
    prudent = next(c for c in f['curves'] if c['mode'] == 'prudent')
    assert prudent['end_90'] == round(10000 - 1500 + 0.55 * 4000 + 0.55 * 2000, 2)


def test_monthly_plan_closings_match_the_curve():
    f = _fc(sales=[_inv(1, 'Alpha', 3000, '2026-11-15')],
            expenses=[{'vendor': 'V', 'description': 'd', 'amount': 900, 'expense_date': '2026-09-01', 'category': 'c'}])
    prob = next(c for c in f['curves'] if c['mode'] == 'probable')
    months = f['plan']['months']
    assert months[0]['label'] == 'Octobre 2026' and months[0]['partial']
    assert months[-1]['closing'] == prob['end_90']
    for m in months:
        assert round(m['opening'] + sum(m['cats'].values()), 2) == m['closing']
    assert any(r['category'] == 'burn' for r in f['plan']['rows'])


def test_client_payment_habit_shifts_expected_date():
    sales = [_inv(1, 'Lent SARL', 1000, '2026-10-20')]
    f = _fc(sales=sales, client_delays={'lent sarl': {'delay': 12, 'count': 4}})
    r = f['receivables'][0]
    assert r['expected_date'] == '2026-11-01' and r['payment_habit']['delay'] == 12


def test_burn_can_be_excluded_and_threshold_alerts():
    expenses = [{'vendor': 'V', 'description': 'd', 'amount': 9000, 'expense_date': '2026-09-15', 'category': 'c'}]
    with_burn = _fc(expenses=expenses)
    without = _fc(expenses=expenses, include_burn=False)
    assert with_burn['horizons'][90] == 1000.0 and without['horizons'][90] == 10000.0
    assert with_burn['runway']['days'] == 100
    alert = _fc(expenses=expenses, safety_threshold=5000)
    assert alert['alert_level'] == 'VIGILANCE' and 'seuil de sécurité' in alert['alert']
    assert alert['threshold_day'] == 51 and alert['chart']['threshold_y'] is not None


def test_occurrences_keep_end_of_month():
    got = T.occurrences('2026-01-31', 'monthly', None, date(2026, 1, 1), date(2026, 4, 30))
    assert got == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]
    assert T.occurrences('2026-10-01', 'once', None, TODAY, TODAY + timedelta(days=90)) == []
    assert T.label_key('PRLV SEPA URSSAF IDF ECH 10/2026 REF 4455') == 'URSSAF IDF'


# ------------------------------------------------------------------ sources en base
def _cx(org_id):
    from profitos import runtime as rt
    from profitos import db as dbmod
    return dbmod.connect_tenant(org_id, rt.tenant_db(org_id))


def test_loans_leases_recurring_sales_and_vat_are_collected(app):
    from profitos.accounting import create_entry
    _, org_id = _signup(app)
    c = _cx(org_id)
    c.execute("INSERT INTO loans(entity_id,lender_name,principal_amount,annual_rate,start_date,duration_months,monthly_payment,created_at) "
              "VALUES(NULL,'Banque X',10000,3,'2026-01-05',24,430,'x')")
    lid = c.execute("SELECT id FROM loans").fetchone()['id']
    for n, d, paid in ((9, '2026-10-05', 0), (10, '2026-11-05', 0), (8, '2026-09-05', 1)):
        c.execute("INSERT INTO loan_installments(loan_id,installment_number,due_date,capital_amount,interest_amount,remaining_balance,paid) "
                  "VALUES(?,?,?,400,30,5000,?)", (lid, n, d, paid))
    c.execute("INSERT INTO finance_leases(entity_id,lessor_name,asset_description,asset_value,redevance_amount,start_date,duration_months,created_at) "
              "VALUES(NULL,'Loueur','Camion',30000,600,'2026-01-01',36,'x')")
    c.execute("INSERT INTO finance_lease_payments(lease_id,payment_number,due_date,amount) VALUES(1,11,'2026-11-01',600)")
    c.execute("INSERT INTO recurring_invoice_templates(entity_id,client_name,line_items,frequency,interval_count,due_days,next_run_date,status,created_at,updated_at) "
              "VALUES(NULL,'Abonné SAS',?, 'monthly',1,30,'2026-10-15','active','x','x')",
              ('[{"description":"Abonnement","quantity":1,"unit_price":100,"vat_rate":20}]',))
    create_entry(c, 'VE', '2026-09-10', 'Vente', [{'account_code': '411000', 'debit': 1200},
                                                 {'account_code': '706000', 'credit': 1000},
                                                 {'account_code': '445710', 'credit': 200, 'vat_rate': 20, 'vat_base': 1000}])
    create_entry(c, 'AC', '2026-09-12', 'Achat', [{'account_code': '606400', 'debit': 250},
                                                 {'account_code': '445660', 'debit': 50},
                                                 {'account_code': '401000', 'credit': 300}])
    flows = T.collect_flows(c, None, TODAY)
    c.close()
    by = {}
    for f in flows:
        by.setdefault(f['category'], []).append((f['date'], f['amount']))
    assert by['loans'] == [('2026-10-10', 430.0), ('2026-11-05', 430.0)]  # échéance du 5/10 non réglée : comptée tout de suite
    assert by['leases'] == [('2026-11-01', 600.0)]
    assert by['recurring_sales'][:2] == [('2026-11-14', 120.0), ('2026-12-15', 120.0)]
    assert by['vat'] == [('2026-10-15', 150.0)]


def test_client_payment_delays_from_paid_invoices(app):
    _, org_id = _signup(app)
    c = _cx(org_id)
    for i, (due, paid) in enumerate((('2026-06-30', '2026-07-12'), ('2026-07-31', '2026-08-10'), ('2026-08-31', '2026-09-14')), 1):
        c.execute("INSERT INTO outgoing_invoices(invoice_number,client_name,due_date,total,status) VALUES(?,?,?,100,'paid')", (f'F{i}', 'Lent SARL', due))
        c.execute("INSERT INTO outgoing_invoice_payments(invoice_id,amount,payment_date,created_at) VALUES(?,100,?,'x')", (i, paid))
    c.commit()
    delays = T.client_payment_delays(c, None)
    c.close()
    assert delays == {'lent sarl': {'delay': 12, 'count': 3}}


def test_recurring_bank_flows_suggested_then_added_and_counted(app):
    client, org_id = _signup(app)
    today = date.today()
    c = _cx(org_id)
    c.execute("INSERT INTO bank_accounts(provider,provider_account_id,name) VALUES('import','a1','Qonto')")
    for i in range(1, 4):
        d = T.add_months(today, -i).replace(day=min(T.add_months(today, -i).day, 28))
        c.execute("INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount) "
                  "VALUES('import',?,'a1',?,?,?)", (f't{i}', d.isoformat(), f'PRLV SEPA LOYER SCI DUPONT {i:02d}', -1200.0))
    c.commit()
    sugg = T.detect_recurring(c, None, today)
    c.close()
    assert len(sugg) == 1 and sugg[0]['category'] == 'loyer' and sugg[0]['amount'] == 1200.0
    s = sugg[0]
    _post(client, '/cash-intelligence', {'action': 'balance', 'cash_balance': '12 345,67'})
    _post(client, '/cash-intelligence', {'action': 'accept_suggestion', 'label': s['label'], 'category': s['category'],
                                          'amount': str(s['amount']), 'start_date': s['next_date'], 'frequency': 'monthly',
                                          'suggestion_key': s['key']})
    c = _cx(org_id)
    flow = c.execute("SELECT * FROM treasury_planned_flows").fetchone()
    bal = c.execute("SELECT cash_balance FROM financial_settings WHERE id=1").fetchone()
    c.close()
    assert flow['category'] == 'loyer' and flow['source'] == 'bank_detected' and flow['label_key'] == s['key']
    assert bal['cash_balance'] == 12345.67
    page = client.get('/cash-intelligence').get_data(as_text=True)
    assert 'Repérés sur vos relevés bancaires' not in page  # la suggestion acceptée disparaît
    assert 'PLAN DE TRÉSORERIE MENSUEL' in page and 'Loyer et charges locatives' in page
    assert 'Salaires et charges sociales' in page  # catégories proposées dans le formulaire
    r = client.get('/cash-intelligence/plan?format=xlsx')
    assert r.status_code == 200 and r.mimetype.endswith('spreadsheetml.sheet')
    csv = client.get('/cash-intelligence/plan?format=csv').get_data(as_text=True)
    assert "Solde d'ouverture" in csv and 'Loyer' not in csv.split('\n')[0]


def test_settings_and_planned_flow_validation(app):
    client, org_id = _signup(app)
    _post(client, '/cash-intelligence', {'action': 'settings', 'safety_threshold': '8 000', 'include_observed_burn': ''})
    _post(client, '/cash-intelligence', {'action': 'add_flow', 'label': 'Salaires', 'category': 'salaires', 'amount': '-5',
                                          'start_date': '2026-10-28', 'frequency': 'monthly'})
    assert any('incomplet' in m for m in _flashes(client))
    _post(client, '/cash-intelligence', {'action': 'add_flow', 'label': 'Salaires', 'category': 'salaires', 'amount': '6 500',
                                          'start_date': '2026-10-28', 'frequency': 'monthly'})
    c = _cx(org_id)
    st = T.get_settings(c, None)
    flows = T.planned_flows(c, None, TODAY, TODAY + timedelta(days=90))
    c.close()
    assert st['safety_threshold'] == 8000.0 and st['include_observed_burn'] is False
    assert [(f['date'], f['amount'], f['category']) for f in flows] == [
        ('2026-10-28', 6500.0, 'payroll'), ('2026-11-28', 6500.0, 'payroll'), ('2026-12-28', 6500.0, 'payroll')]


def test_forecast_page_net_matches_the_treasury_curve(app):
    client, org_id = _signup(app)
    _post(client, '/cash-intelligence', {'action': 'balance', 'cash_balance': '5000'})
    _post(client, '/cash-intelligence', {'action': 'add_flow', 'label': 'Subvention', 'category': 'subvention', 'amount': '2000',
                                          'start_date': (date.today() + timedelta(days=40)).isoformat(), 'frequency': 'once'})
    page = client.get('/forecast').get_data(as_text=True)
    assert '2\u00a0000\u00a0€ de flux nets sur 90 jours' in page.replace('&nbsp;', '\u00a0')
    assert '1\u00a0encaissement prévu' in page.replace('&nbsp;', '\u00a0') or '1 encaissement prévu' in page


def test_recurring_income_from_an_invoiced_client_is_not_suggested(app):
    _, org_id = _signup(app)
    today = date.today()
    c = _cx(org_id)
    c.execute("INSERT INTO bank_accounts(provider,provider_account_id,name) VALUES('import','a1','Qonto')")
    c.execute("INSERT INTO outgoing_invoices(invoice_number,client_name,total,status) VALUES('F1','Alpha Conseil',100,'paid')")
    for i in range(1, 4):
        d = T.add_months(today, -i).replace(day=10)
        c.execute("INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount) "
                  "VALUES('import',?,'a1',?,'VIR CLIENT ALPHA CONSEIL',6200)", (f'in{i}', d.isoformat()))
        c.execute("INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount) "
                  "VALUES('import',?,'a1',?,'VIR SUBVENTION REGION',500)", (f'sub{i}', d.isoformat()))
    c.commit()
    sugg = T.detect_recurring(c, None, today)
    c.close()
    assert [s['key'] for s in sugg] == ['SUBVENTION REGION'] and sugg[0]['category'] == 'subvention'


def test_prudent_scenario_tension_is_reported():
    # Probable : la facture arrive avant la sortie. Prudent : 55 % seulement, 15 j plus tard.
    sales = [_inv(1, 'Alpha', 10000, '2026-10-20')]
    flows = [{'date': '2026-10-25', 'amount': 14000, 'direction': 'out', 'category': 'planned_out', 'label': 'IS', 'source': 'planned'}]
    f = _fc(cash_balance=5000, sales=sales, extra_flows=flows)
    assert f['min_cash'] >= 0 and f['alert_level'] == 'VIGILANCE'
    assert 'négative autour du 25/10/2026' in f['alert'] and f['prudent_note']
