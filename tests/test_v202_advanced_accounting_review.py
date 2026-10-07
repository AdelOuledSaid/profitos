from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def txt(path):
    return (ROOT/path).read_text(encoding="utf-8")

def test_diagnostics_check_hard_accounting_blockers():
    s=txt('profitos/reviews.py')
    assert 'UNBALANCED_ENTRIES' in s
    assert 'SUSPENSE_ACCOUNTS' in s
    assert "account_code='467000'" in s
    # PostgreSQL adapter: literal SQL wildcard must be escaped as %%.
    assert "account_code LIKE '471%%'" in s

def test_diagnostics_surface_lettering_and_missing_documents():
    s=txt('profitos/reviews.py')
    assert 'UNLETTERED_THIRDPARTY' in s
    # PostgreSQL adapter: literal SQL wildcards must be escaped as %%.
    assert "account_code LIKE '401%%'" in s
    assert "account_code LIKE '411%%'" in s
    assert 'MISSING_PURCHASE_DOCS' in s
