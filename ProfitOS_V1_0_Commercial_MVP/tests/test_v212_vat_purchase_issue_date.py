from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTING = (ROOT / 'profitos' / 'routes' / 'accounting.py').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'profitos' / 'runtime.py').read_text(encoding='utf-8')


def test_purchase_invoice_schema_uses_issue_date_not_invoice_date():
    start = RUNTIME.index('CREATE TABLE IF NOT EXISTS purchase_invoices(')
    end = RUNTIME.index(');', start)
    schema = RUNTIME[start:end]
    assert 'issue_date TEXT' in schema
    assert 'invoice_date' not in schema


def test_vat_purchase_consistency_uses_real_purchase_date_column():
    start = ACCOUNTING.index('def _vat_consistency_checks')
    end = ACCOUNTING.index("@app.route('/comptabilite/tva'", start)
    vat_checks = ACCOUNTING[start:end]
    assert 'p.issue_date BETWEEN ? AND ?' in vat_checks
    assert 'p.invoice_date' not in vat_checks


def test_previous_postgresql_numeric_fix_is_preserved():
    start = ACCOUNTING.index('def _vat_consistency_checks')
    end = ACCOUNTING.index("@app.route('/comptabilite/tva'", start)
    vat_checks = ACCOUNTING[start:end]
    assert 'CAST(SUM(l.debit)-SUM(l.credit) AS NUMERIC)' in vat_checks
    assert 'HAVING ABS(diff)' not in vat_checks
