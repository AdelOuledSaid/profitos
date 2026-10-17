from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
R=(ROOT/"profitos"/"reviews.py").read_text(encoding="utf-8")
T=(ROOT/"templates"/"review_detail.html").read_text(encoding="utf-8")
ast.parse(R)

def test_v396_keeps_existing_core_review_controls():
    for code in ("UNBALANCED_ENTRIES","SUSPENSE_ACCOUNTS","UNLETTERED_THIRDPARTY","MISSING_PURCHASE_DOCS"):
        assert code in R

def test_v396_annual_review_checks_depreciation():
    assert "MISSING_DEPRECIATION" in R
    assert "fixed_asset_depreciation_runs" in R

def test_v396_checks_cutoff_accounting_integrity():
    assert "INVALID_CUTOFF" in R
    assert "cutoff_entries" in R
    assert "e.source_type<>'cutoff'" in R

def test_v396_reports_period_not_closed_without_auto_closing():
    assert "PERIOD_NOT_CLOSED" in R
    block=R.split("def run_review_diagnostics",1)[1].split("def latest_review_diagnostics",1)[0]
    assert "UPDATE accounting_entity_closure" not in block
    assert "INSERT INTO accounting_entries" not in block

def test_v396_ui_explains_read_only_diagnostic():
    assert "Aucune écriture n’est créée automatiquement." in T
