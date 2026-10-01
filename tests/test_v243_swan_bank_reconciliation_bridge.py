from pathlib import Path

R=Path("profitos/routes/bank_sync.py").read_text(encoding="utf-8")
T=Path("templates/banking.html").read_text(encoding="utf-8")

def test_swan_sync_is_sandbox_only_and_csrf_protected():
    assert '@app.post("/banking/swan/sync")' in R
    assert "swan_environment() != 'sandbox'" in R
    assert "banking_swan_sync" in T
    assert 'name="csrf_token"' in T

def test_swan_booked_transactions_feed_existing_reconciliation_engine():
    assert "provider IN ('powens','swan')" in R
    assert "(t.get('status') or '') != 'Booked'" in R
    assert "INSERT INTO bank_transactions(provider,provider_transaction_id" in R
    assert "apply_categorization_rule(c,label,eid)" in R

def test_swan_amount_direction_is_preserved():
    assert "signed=-value" in R
    assert "'out' in str(t.get('type') or '').lower()" in R

def test_no_automatic_invoice_payment_on_sync():
    block=R.split('def banking_swan_sync():',1)[1].split('@app.get("/banking/connect")',1)[0]
    assert 'outgoing_invoice_payments' not in block
    assert 'UPDATE outgoing_invoices' not in block
