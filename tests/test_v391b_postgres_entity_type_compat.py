from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
B = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def test_v391_accounting_validation_join_is_postgres_type_safe():
    assert "CAST(e.entity_id AS TEXT)=CAST(v.entity_id AS TEXT)" in B
    assert "e.entity_id=v.entity_id" not in B

def test_real_accounting_entry_gate_is_still_present():
    assert "bank_categorization" in B
    assert "bank_accounting_validations" in B
