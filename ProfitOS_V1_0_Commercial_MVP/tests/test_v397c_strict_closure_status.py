from pathlib import Path
import ast
R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")
ast.parse(R)

def test_closure_auto_status_is_strict_not_absence_of_issue():
    block=R.split("def automatic_review_item_statuses",1)[1].split("def latest_review_diagnostics",1)[0]
    assert "15: ('PERIOD_NOT_CLOSED'" not in block
    assert "SELECT closed_until FROM accounting_entity_closure" in block
    assert "str(closed_until)[:10] >= year_end" in block
    assert "'Période réellement clôturée'" in block
    assert "'Période à clôturer'" in block

def test_auto_prevalidation_still_never_checks_items():
    block=R.split("def automatic_review_item_statuses",1)[1].split("def latest_review_diagnostics",1)[0]
    assert "UPDATE review_items" not in block
    assert "INSERT INTO review_items" not in block
