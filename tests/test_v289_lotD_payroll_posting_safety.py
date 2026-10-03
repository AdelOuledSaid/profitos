from pathlib import Path
T=Path("profitos/routes/payroll.py").read_text(encoding="utf-8")

def test_payroll_posting_claims_pending_import_before_commit():
    block=T.split("def payroll_import_validate(import_id):",1)[1]
    assert "posted = c.execute" in block
    assert "AND status='pending'" in block
    assert "if posted.rowcount != 1:" in block
    assert "Cet import de paie n'est plus en attente de validation." in block
    assert block.index("if posted.rowcount != 1:") < block.index("c.commit()")

def test_payroll_posting_rolls_back_malformed_payload_instead_of_500():
    block=T.split("def payroll_import_validate(import_id):",1)[1]
    assert "except (AccountingError, ValueError, TypeError, json.JSONDecodeError) as exc:" in block
    assert "c.rollback()" in block
    assert "Validation impossible" in block

def test_payroll_posting_keeps_entity_scope_and_idempotent_source():
    block=T.split("def payroll_import_validate(import_id):",1)[1]
    assert "id=? AND entity_id IS ?" in block
    assert "source_type='payroll_import',source_id=row['id']" in block
    assert "entity_id=eid,commit=False" in block
