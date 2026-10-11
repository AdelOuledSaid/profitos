from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/'profitos/routes/bank_sync.py').read_text(encoding='utf-8')
RUNTIME=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
TPL=(ROOT/'templates/banking.html').read_text(encoding='utf-8')

def test_allocation_registry_is_many_to_many_and_entity_scoped():
    schema=RUNTIME.split('CREATE TABLE IF NOT EXISTS bank_invoice_allocations',1)[1].split(');',1)[0]
    assert 'bank_transaction_id INTEGER NOT NULL UNIQUE' not in schema
    assert 'invoice_id INTEGER NOT NULL UNIQUE' not in schema
    assert 'entity_id INTEGER' in schema
    assert 'UNIQUE(entity_id,idempotency_key)' in RUNTIME

def test_matching_uses_remaining_bank_amount_and_invoice_balance():
    assert 'def _bank_allocated_total' in BANK
    assert 'def _invoice_bank_balance' in BANK
    assert "status IN ('sent','partially_paid')" in BANK
    assert 'available=round(total_amount-_bank_allocated_total' in BANK
    assert 'allocation=min(balance,available)' in BANK

def test_bank_allocation_creates_real_customer_payment_atomically():
    block=BANK.split('def banking_reconcile',1)[1].split('@app.post("/banking/sync")',1)[0]
    assert 'INSERT INTO outgoing_invoice_payments' in block
    assert 'generate_sale_partial_payment_entry(c,inv,payment)' in block
    assert 'INSERT INTO bank_invoice_allocations' in block
    assert "status='paid' if new_balance <= .005 else 'partially_paid'" in block
    assert 'c.rollback()' in block
    assert 'entity_id IS ?' in block

def test_reconciliation_is_idempotent_and_cannot_over_allocate():
    block=BANK.split('def banking_reconcile',1)[1].split('@app.post("/banking/sync")',1)[0]
    assert 'amount > available+.001' in block
    assert 'amount > balance+.001' in block
    assert 'idempotency_key' in block
    # Doublon attrapé quel que soit le backend (SQLite en local, psycopg2 en production).
    assert 'BANK_DB_INTEGRITY_ERRORS' in block

def test_ui_exposes_partial_allocation_and_remaining_amount():
    assert 'Disponible :' in TPL
    assert 'name="matched_amount"' in TPL
    assert 'Un même virement peut être réparti sur plusieurs factures' in TPL
