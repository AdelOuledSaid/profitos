from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RUNTIME=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")
INV=(ROOT/"profitos/routes/invoicing.py").read_text(encoding="utf-8")
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
API=(ROOT/"profitos/routes/api.py").read_text(encoding="utf-8")
WH=(ROOT/"profitos/webhooks_outbound.py").read_text(encoding="utf-8")

def test_v205_purchase_budgets_are_entity_scoped_with_legacy_migration():
    assert "PRIMARY KEY(category,entity_key)" in RUNTIME
    assert "purchase_budgets_v24" in RUNTIME
    block=INV[INV.index("def purchase_budgets"):INV.index("@app.route('/facturation/achats/recurrents")]
    assert "ekey=eid or 0" in block
    assert "WHERE entity_key=?" in block
    assert "ON CONFLICT(category,entity_key)" in block

def test_v205_sepa_export_does_not_pretend_bank_execution():
    start=INV.index("def purchase_sepa_batch")
    end=INV.find("\n    @app.route(",start+10)
    block=INV[start:end if end>0 else len(INV)]
    assert "INSERT INTO sepa_export_batches" in block
    assert "INSERT INTO sepa_export_items" in block
    assert "INSERT INTO purchase_invoice_payments" not in block
    assert "generate_purchase_partial_payment_entry" not in block
    assert "generate_purchase_payment_entry(c, p_updated)" not in block

def test_v205_bank_supplier_suggestions_use_remaining_balance_and_entity():
    block=BANK[BANK.index("def _purchase_reconciliation_suggestions"):BANK.index("def _bank_allocated_total")]
    assert "purchase_invoice_payments" in block
    assert "p.entity_id IS ?" in block
    assert "paid_total" in block
    assert "bank_purchase_reconciliations r" not in block

def test_v205_business_webhook_calls_propagate_entity():
    assert "entity_id=eid" in INV
    assert "entity_id=inv['entity_id']" in INV
    assert "entity_id=entity_id" in INV
    assert "only_subscription_id=sub_id,entity_id=eid" in API
    assert "def deliver_webhook(conn, event_type, data, only_subscription_id=None, entity_id=None)" in WH

def test_v205_no_direct_accounting_write_added_by_api():
    assert "INSERT INTO accounting_entries" not in API
    assert "UPDATE accounting_entries" not in API
