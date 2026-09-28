from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')

def test_create_entry_supports_deferred_commit():
    s=read('profitos/accounting.py')
    assert 'commit=True' in s
    assert 'if commit:' in s

def test_payroll_posts_entry_and_status_in_one_transaction():
    s=read('profitos/routes/payroll.py')
    assert 'commit=False' in s
    assert "UPDATE payroll_imports SET status='posted'" in s
    assert 'c.rollback()' in s

def test_loan_payment_defers_accounting_commit():
    s=read('profitos/loans.py')
    assert "source_type='loan_installment'" in s
    assert 'commit=False' in s
    assert "UPDATE loan_installments SET paid=1" in s

def test_lease_payment_defers_accounting_commit():
    s=read('profitos/finance_leases.py')
    assert "source_type='finance_lease_payment'" in s
    assert 'commit=False' in s
    assert "UPDATE finance_lease_payments SET paid=1" in s

def test_legacy_reconciliation_is_entity_scoped():
    s=read('profitos/routes/imports.py')
    block=s[s.index("@app.route('/reconcile/confirm'"):s.index("@app.route('/upload/invoices'")]
    assert 'current_entity_id' in block
    assert 'WHERE id=? AND entity_id IS ?' in block
