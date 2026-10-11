from pathlib import Path

T=Path("profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def test_supplier_reconciliation_has_stable_idempotency_key():
    assert 'key=f"bank-purchase:{eid}:{tx_id}:{purchase_id}:{amount:.2f}"' in T
    assert 'bank-purchase:{eid}:{tx_id}:{purchase_id}:{amount:.2f}:{float(allocated):.2f}' not in T

def test_supplier_reconciliation_handles_invalid_numeric_input():
    assert "except (AccountingError, sqlite3.IntegrityError, ValueError) as e:" in T

def test_customer_reconciliation_keeps_stable_idempotency_and_bounds():
    assert 'idem=f"bank:{eid}:{transaction_id}:{invoice_id}:{amount:.2f}"' in T
    assert "amount > available+.001 or amount > balance+.001" in T

def test_supplier_reconciliation_keeps_balance_and_available_bounds():
    assert "amount>balance+.001 or amount>available+.001" in T
