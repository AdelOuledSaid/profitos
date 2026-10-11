"""v421 — dates réelles des écritures (emprunt, crédit-bail, notes de frais),
TVA déductible ventilée, comptes de paie, prévision de trésorerie nette des avoirs,
retard des créances calculé à la lecture."""
import io
import json
from datetime import date, timedelta

from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db, _create_invoice, _invoice, FULL_PROFILE)


def _cx(org_id):
    from profitos import runtime as rt
    from profitos import db as dbmod
    return dbmod.connect_tenant(org_id, rt.tenant_db(org_id))


def _entry_lines(c, entry_id):
    return {l['account_code']: (l['debit'], l['credit']) for l in
            c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=?', (entry_id,)).fetchall()}


def test_loan_installment_dated_at_payment_date_and_future_refused(app):
    from profitos.loans import create_loan
    client, org_id = _signup(app)
    c = _cx(org_id)
    loan_id = create_loan(c, 'Banque Test', 12000, 3.0, '2025-01-15', 12)
    inst = c.execute('SELECT * FROM loan_installments WHERE loan_id=? ORDER BY installment_number', (loan_id,)).fetchall()
    c.close()
    future = (date.today() + timedelta(days=3)).isoformat()
    _post(client, f"/comptabilite/emprunts/echeance/{inst[0]['id']}/payer", {'payment_date': future})
    assert any('futur' in m for m in _flashes(client))
    _post(client, f"/comptabilite/emprunts/echeance/{inst[0]['id']}/payer", {'payment_date': '2025-02-17'})
    _post(client, f"/comptabilite/emprunts/echeance/{inst[1]['id']}/payer")
    c = _cx(org_id)
    dates = [r['entry_date'] for r in c.execute(
        "SELECT entry_date FROM accounting_entries WHERE source_type='loan_installment' ORDER BY source_id").fetchall()]
    c.close()
    assert dates == ['2025-02-17', str(inst[1]['due_date'])[:10]]


def test_lease_payment_splits_deductible_vat_and_uses_due_date(app):
    from profitos.finance_leases import create_finance_lease, pay_lease_payment
    client, org_id = _signup(app)
    c = _cx(org_id)
    lease_id = create_finance_lease(c, 'Leaser', 'Utilitaire', 30000, 600.0, 'monthly', '2025-01-01', 12, vat_rate=20)
    pay = c.execute('SELECT * FROM finance_lease_payments WHERE lease_id=? ORDER BY payment_number', (lease_id,)).fetchone()
    entry_id = pay_lease_payment(c, pay['id'])
    e = c.execute('SELECT entry_date FROM accounting_entries WHERE id=?', (entry_id,)).fetchone()
    lines = _entry_lines(c, entry_id)
    c.close()
    assert e['entry_date'] == '2025-02-01'
    assert lines == {'612000': (500.0, 0), '445660': (100.0, 0), '512000': (0, 600.0)}


def test_expense_report_entry_splits_vat_and_dated_last_expense(app):
    from profitos.expenses import generate_expense_report_entry
    client, org_id = _signup(app)
    c = _cx(org_id)
    c.execute("INSERT INTO expense_reports(employee_email,period_label,status,created_at) VALUES('a@b.fr','Mars','approved','x')")
    rid = c.execute('SELECT MAX(id) FROM expense_reports').fetchone()[0]
    c.execute("INSERT INTO expense_report_lines(report_id,expense_date,category,amount,vat_amount) VALUES(?,?,?,?,?)",
              (rid, '2025-03-12', 'repas', 60.0, 5.45))
    c.execute("INSERT INTO expense_report_lines(report_id,expense_date,category,amount,vat_amount) VALUES(?,?,?,?,?)",
              (rid, '2025-03-20', 'kilometrique', 40.0, 3.0))
    report = c.execute('SELECT * FROM expense_reports WHERE id=?', (rid,)).fetchone()
    entry_id = generate_expense_report_entry(c, report)
    e = c.execute('SELECT entry_date FROM accounting_entries WHERE id=?', (entry_id,)).fetchone()
    lines = _entry_lines(c, entry_id)
    c.close()
    assert e['entry_date'] == '2025-03-20'
    assert lines['445660'] == (5.45, 0)          # TVA du repas seulement (pas de TVA sur IK)
    assert lines['421000'] == (0, 100.0)
    assert sum(d for d, _ in lines.values()) == 100.0


def test_payroll_accounts_normalized_and_opened(app):
    client, org_id = _signup(app)
    csv_data = ("CompteNum;EcritureLib;Debit;Credit\n6411;Salaires;3000,00;\n6451;URSSAF patronale;900,00;\n"
                "6453;Retraite;300,00;\n421;Net à payer;;2200,00\n431;URSSAF;;1300,00\n4421;PAS;;150,00\n437;Retraite;;550,00\n")
    with client.session_transaction() as s:
        s['csrf_token'] = 'f' * 64
    client.post('/comptabilite/paie', data={'csrf_token': 'f' * 64, 'provider': 'SILAE', 'period_label': '2025-03',
                                            'entry_date': '2025-03-31',
                                            'file': (io.BytesIO(csv_data.encode()), 'paie.csv')},
                content_type='multipart/form-data')
    c = _cx(org_id)
    imp = c.execute('SELECT * FROM payroll_imports').fetchone()
    c.close()
    assert imp['status'] == 'pending' and imp['total_debit'] == 4200.0
    assert [l['account_code'] for l in json.loads(imp['lines_json'])] == ['641100', '645100', '645300', '421000', '431000', '442100', '437000']
    _post(client, f"/comptabilite/paie/{imp['id']}/valider")
    c = _cx(org_id)
    entry = c.execute("SELECT id FROM accounting_entries WHERE source_type='payroll_import'").fetchone()
    lines = _entry_lines(c, entry['id'])
    c.close()
    assert lines['641100'] == (3000.0, 0) and lines['442100'] == (0, 150.0) and lines['437000'] == (0, 550.0)


def test_cash_forecast_deducts_credit_notes():
    from profitos.routes.cash_intelligence import compute_cash_forecast
    today = date(2026, 10, 1)
    sales = [{'id': 1, 'invoice_number': 'FA-1', 'client_name': 'C', 'total': 1200.0, 'paid_total': 200.0,
              'credited_total': 600.0, 'due_date': '2026-10-15', 'issue_date': '2026-09-15', 'status': 'sent'}]
    f = compute_cash_forecast(1000, '2026-10-01', sales, [], [], today=today)
    assert [round(r['amount'], 2) for r in f['receivables']] == [400.0]


def test_imported_receivable_overdue_days_computed_at_read(app):
    from profitos.runtime import refresh_invoice_aging
    client, org_id = _signup(app)
    c = _cx(org_id)
    c.execute("INSERT INTO invoices(invoice_number,customer,amount,paid_amount,due_date,status,days_overdue,score) "
              "VALUES('I-1','Client',1000,0,'2026-09-01','unpaid',1,0)")
    c.commit()
    refresh_invoice_aging(c, date(2026, 10, 1))
    row = c.execute("SELECT days_overdue,score FROM invoices WHERE invoice_number='I-1'").fetchone()
    c.close()
    assert row['days_overdue'] == 30 and row['score'] > 0
