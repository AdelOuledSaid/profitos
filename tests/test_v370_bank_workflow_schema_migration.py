from pathlib import Path

RUNTIME = (Path(__file__).resolve().parents[1] / "profitos" / "runtime.py").read_text(encoding="utf-8")

def test_v370_workflow_table_is_in_runtime_schema():
    assert "CREATE TABLE IF NOT EXISTS bank_transaction_workflow(" in RUNTIME
    assert "bank_transaction_id INTEGER NOT NULL" in RUNTIME
    assert "state TEXT NOT NULL DEFAULT 'ignored'" in RUNTIME
    assert "updated_at TEXT NOT NULL" in RUNTIME
    assert "UNIQUE(entity_id,bank_transaction_id)" in RUNTIME

def test_v370_workflow_index_is_in_runtime_schema():
    assert "CREATE INDEX IF NOT EXISTS idx_bank_transaction_workflow_tx" in RUNTIME
    assert "ON bank_transaction_workflow(entity_id,bank_transaction_id)" in RUNTIME
