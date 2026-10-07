from pathlib import Path
import ast
R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")
ast.parse(R)

def test_strict_closure_uses_review_period_not_diagnostic_snapshot():
    block=R.split("def automatic_review_item_statuses",1)[1].split("def latest_review_diagnostics",1)[0]
    assert "SELECT period_label, entity_id FROM reviews WHERE id=?" in block
    assert "review_row['period_label']" in block
    assert "review_row['entity_id']" in block
    assert "diag_run['period']" not in block
    assert "diag_run['entity_id']" not in block
    assert "SELECT closed_until FROM accounting_entity_closure" in block
