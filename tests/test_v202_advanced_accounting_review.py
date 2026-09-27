from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def txt(p): return (ROOT/p).read_text(encoding='utf-8')

def test_review_diagnostic_tables_are_persistent_and_entity_scoped():
    s=txt('profitos/runtime.py')
    assert 'CREATE TABLE IF NOT EXISTS review_diagnostic_runs' in s
    assert 'entity_id INTEGER' in s
    assert 'snapshot_max_entry_id INTEGER' in s
    assert 'CREATE TABLE IF NOT EXISTS review_diagnostic_issues' in s

def test_diagnostics_check_hard_accounting_blockers():
    s=txt('profitos/reviews.py')
    assert 'UNBALANCED_ENTRIES' in s
    assert 'SUSPENSE_ACCOUNTS' in s
    assert "account_code='467000'" in s
    assert "account_code LIKE '471%'" in s
    assert "severity,label,count" in s

def test_diagnostics_surface_lettering_and_missing_documents():
    s=txt('profitos/reviews.py')
    assert 'UNLETTERED_THIRDPARTY' in s
    assert "account_code LIKE '401%'" in s and "account_code LIKE '411%'" in s
    assert 'MISSING_PURCHASE_DOCS' in s
    assert "document_path IS NULL" in s

def test_review_completion_reruns_diagnostic_and_blocks_on_blockers():
    route=txt('profitos/routes/reviews.py')
    core=txt('profitos/reviews.py')
    assert "run_review_diagnostics(c, review_id, entity_id=eid" in route
    assert "complete_review(c, review_id, blocker_count=diag_run['blocker_count'])" in route
    assert 'anomalie(s) comptable(s) bloquante(s)' in core

def test_review_ui_shows_diagnostic_and_manual_refresh():
    s=txt('templates/review_detail.html')
    assert 'DIAGNOSTIC AUTOMATIQUE' in s
    assert 'review_diagnostic_run' in s
    assert 'diag_run.blocker_count' in s
    assert 'diag_issues' in s
