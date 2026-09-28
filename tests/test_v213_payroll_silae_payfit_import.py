from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def text(p): return (ROOT/p).read_text(encoding='utf-8')

def test_payroll_is_staged_before_posting():
    s=text('profitos/routes/payroll.py')
    assert "status='pending'" in s or "'pending'" in s
    assert "create_entry(c,'PA'" in s
    assert "payroll_import_validate" in s

def test_payroll_has_entity_scope_and_duplicate_hash():
    s=text('profitos/routes/payroll.py')
    assert 'current_entity_id' in s
    assert 'file_sha256' in s and 'hashlib.sha256' in s
    assert 'entity_id IS ?' in s

def test_payroll_runtime_table_and_unique_guard():
    s=text('profitos/runtime.py')
    assert 'CREATE TABLE IF NOT EXISTS payroll_imports' in s
    assert 'ux_payroll_import_entity_hash' in s
    assert 'accounting_entry_id' in s

def test_payroll_does_not_claim_to_compute_payroll_or_dsn():
    s=text('templates/payroll_imports.html')
    assert 'ne calcule ni bulletins' in s
    assert 'DSN' in s

def test_payroll_route_registered_and_nav_visible():
    assert 'payroll.register(app)' in text('profitos/__init__.py')
    assert "url_for('payroll_imports_list')" in text('templates/base.html')
