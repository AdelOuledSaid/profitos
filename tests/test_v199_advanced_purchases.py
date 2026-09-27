from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
INV=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
BANK=(ROOT/'profitos/routes/bank_sync.py').read_text(encoding='utf-8')
ACC=(ROOT/'profitos/accounting.py').read_text(encoding='utf-8')
RUN=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')

def test_supplier_payment_ledger_and_bank_allocations_are_entity_scoped():
    assert 'CREATE TABLE IF NOT EXISTS purchase_invoice_payments' in RUN
    assert 'CREATE TABLE IF NOT EXISTS bank_purchase_allocations' in RUN
    assert 'UNIQUE(entity_id,idempotency_key)' in RUN

def test_supplier_partial_payment_has_dedicated_accounting_source():
    assert 'def generate_purchase_partial_payment_entry' in ACC
    assert "source_type = 'purchase_invoice_payment_v2'" in ACC
    assert "'401000','debit':amount" in ACC
    assert "'512000','credit':amount" in ACC

def test_manual_supplier_payment_uses_remaining_balance_atomically():
    b=INV.split('def purchase_mark_paid',1)[1].split("@app.route('/facturation/achats/virements'",1)[0]
    assert '_purchase_balance(c,p,eid)' in b
    assert 'INSERT INTO purchase_invoice_payments' in b
    assert 'generate_purchase_partial_payment_entry(c,p,payment)' in b
    assert 'c.rollback()' in b

def test_bank_supplier_reconciliation_supports_partial_and_multiple_allocations():
    b=BANK.split('def confirm_purchase_reconciliation',1)[1].split("@app.route('/banking/regles'",1)[0]
    assert 'bank_purchase_allocations' in b
    assert 'purchase_invoice_payments' in b
    assert 'amount>balance+.001' in b and 'amount>available+.001' in b
    assert "status='paid' if new_balance<=.005 else 'partially_paid'" in b
    assert 'entity_id IS ?' in b

def test_purchase_reporting_views_are_entity_scoped():
    assert 'query="SELECT * FROM purchase_invoices WHERE entity_id IS ?"' in INV
    assert 'SELECT * FROM purchase_invoices WHERE entity_id IS ? AND issue_date IS NOT NULL' in INV
    assert 'SELECT * FROM purchase_invoices WHERE entity_id IS ? ORDER BY due_date ASC, id DESC' in INV
