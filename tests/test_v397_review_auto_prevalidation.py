from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]

def txt(p): return (ROOT/p).read_text(encoding="utf-8")

def test_auto_prevalidation_is_read_only_and_manual_final_validation_remains():
    s=txt("profitos/reviews.py")
    assert "def automatic_review_item_statuses" in s
    block=s.split("def automatic_review_item_statuses",1)[1].split("def latest_review_diagnostics",1)[0]
    assert "UPDATE review_items" not in block
    assert "INSERT INTO review_items" not in block
    assert "'MISSING_DEPRECIATION'" in block
    assert "'INVALID_CUTOFF'" in block
    assert "'UNLETTERED_THIRDPARTY'" in block
    assert "'SUSPENSE_ACCOUNTS'" in block
    assert "'PERIOD_NOT_CLOSED'" in block
    assert "'MISSING_PURCHASE_DOCS'" in block

def test_route_passes_auto_statuses_to_review_template():
    s=txt("profitos/routes/reviews.py")
    ast.parse(s)
    assert "automatic_review_item_statuses" in s
    assert "auto_statuses=auto_statuses" in s

def test_template_keeps_manual_validation_and_displays_auto_status():
    s=txt("templates/review_detail.html")
    assert "review_item_toggle" in s
    assert "Contrôle auto OK" not in s  # label comes from backend
    assert "auto_statuses.get(i.item_order)" in s
    assert "validation finale manuelle" not in s  # detail comes from backend

def test_postgres_runtime_fix_is_preserved():
    s=txt("profitos/reviews.py")
    assert "LEFT(l.account_code,3)='471'" in s
    assert "LEFT(l.account_code,3) IN ('401','411')" in s
    assert "r'(?<!\\d)(20\\d{2})(?!\\d)'" in s
