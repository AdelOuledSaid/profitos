from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def test_bankcat_has_db_level_unique_guard():
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_accounting_entries_bank_categorization" in B
    assert "ON accounting_entries(entity_id,source_id)" in B
    assert "WHERE source_type='bank_categorization'" in B

def test_friendly_application_precheck_is_retained():
    assert "Cette transaction bancaire est déjà comptabilisée." in B
    assert "source_type='bank_categorization' AND source_id=?" in B

def test_integrity_collision_rolls_back_atomic_workflow():
    route=B.split("def banking_transaction_categorize(tx_id):",1)[1]
    assert "except BANK_DB_INTEGRITY_ERRORS + (ValueError,) as e:" in route
    assert "c.rollback()" in route

def test_learning_is_after_bankcat_insert_and_same_transaction():
    route=B.split("def banking_transaction_categorize(tx_id):",1)[1]
    assert route.index("INSERT INTO accounting_entries") < route.index("INSERT INTO bank_accounting_validations")
    assert route.index("INSERT INTO bank_accounting_validations") < route.index("c.commit()")
