from pathlib import Path

SRC=(Path(__file__).resolve().parents[1]/"profitos"/"routes"/"bank_sync.py").read_text(encoding="utf-8")

def test_v371_accounting_validation_is_used_by_workflow():
    block=SRC.split("def _bank_transaction_states",1)[1].split("@app.route(\"/banking\")",1)[0]
    assert "FROM bank_accounting_validations" in block
    assert "entity_id IS ? AND bank_transaction_id=?" in block
    assert "account_code IS NOT NULL" in block
    assert "TRIM(account_code) <> ''" in block

def test_v371_validated_accounting_marks_full_transaction_allocated():
    block=SRC.split("def _bank_transaction_states",1)[1].split("@app.route(\"/banking\")",1)[0]
    assert "if accounting_validation:" in block
    assert "allocated=total" in block
    assert "allocated >= total-.005" in block

def test_v371_ignored_state_keeps_priority():
    block=SRC.split("def _bank_transaction_states",1)[1].split("@app.route(\"/banking\")",1)[0]
    assert block.index("if ignored and ignored['state']=='ignored':") < block.index("elif total > .005 and allocated >= total-.005:")

def test_v371_preserves_customer_supplier_and_fee_allocations():
    block=SRC.split("def _bank_transaction_states",1)[1].split("@app.route(\"/banking\")",1)[0]
    assert "bank_invoice_allocations" in block
    assert "bank_purchase_allocations" in block
    assert "e.source_type='bank_fee'" in block
