from pathlib import Path
import ast
R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")
ast.parse(R)

def test_v398_annual_vat_is_read_only_and_conservative():
    assert "VAT_RECONCILIATION_INCOMPLETE" in R
    assert "tax_declaration_preparations" in R
    assert "status='validated'" in R
    assert "missing_months" in R
    assert "invalid_validated_preparations" in R
    assert "11: ('VAT_RECONCILIATION_INCOMPLETE'" in R

def test_v398_does_not_auto_check_review_item():
    block=R.split("def automatic_review_item_statuses",1)[1].split("def latest_review_diagnostics",1)[0]
    assert "UPDATE review_items" not in block
