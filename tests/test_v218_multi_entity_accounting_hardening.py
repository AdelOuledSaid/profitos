from pathlib import Path
import sqlite3
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _text(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def test_loan_and_lease_routes_scope_object_ids_to_active_entity():
    loans = _text('profitos/routes/loans.py')
    leases = _text('profitos/routes/finance_leases.py')
    assert "WHERE id=? AND entity_id IS ?" in loans
    assert "JOIN loans l ON l.id=i.loan_id" in loans
    assert "WHERE id=? AND entity_id IS ?" in leases
    assert "JOIN finance_leases l ON l.id=p.lease_id" in leases
    assert "ownership_c.execute" in leases


def test_business_layer_rejects_cross_entity_payments_and_option():
    loans = _text('profitos/loans.py')
    leases = _text('profitos/finance_leases.py')
    assert "n'appartient pas à l'entité active" in loans
    assert leases.count("n'appartient pas à l'entité active") >= 2


def test_accounting_uses_atomic_sequence_and_source_claim_tables():
    runtime = _text('profitos/runtime.py')
    accounting = _text('profitos/accounting.py')
    assert 'CREATE TABLE IF NOT EXISTS accounting_piece_sequences' in runtime
    assert 'CREATE TABLE IF NOT EXISTS accounting_source_claims' in runtime
    assert 'ON CONFLICT(entity_key,journal_code,fiscal_year) DO UPDATE SET' in accounting
    assert 'RETURNING last_sequence' in accounting
    assert 'INSERT OR IGNORE INTO accounting_source_claims' in accounting


def test_atomic_piece_sequence_increments_per_entity_journal_year():
    import importlib.util
    spec = importlib.util.spec_from_file_location('profitos_accounting_standalone', ROOT / 'profitos/accounting.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _next_piece_number = mod._next_piece_number
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE accounting_entries(
        id INTEGER PRIMARY KEY, journal_code TEXT, piece_number TEXT, entity_id INTEGER)''')
    c.execute('''CREATE TABLE accounting_piece_sequences(
        entity_key INTEGER NOT NULL DEFAULT 0,
        journal_code TEXT NOT NULL,
        fiscal_year TEXT NOT NULL,
        last_sequence INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(entity_key,journal_code,fiscal_year))''')
    assert _next_piece_number(c, 'VE', '2026-09-28', 1) == 'VE-2026-00001'
    assert _next_piece_number(c, 'VE', '2026-09-28', 1) == 'VE-2026-00002'
    assert _next_piece_number(c, 'VE', '2026-09-28', 2) == 'VE-2026-00001'
    assert _next_piece_number(c, 'BQ', '2026-09-28', 1) == 'BQ-2026-00001'


def test_legacy_api_reads_are_entity_scoped_and_key_entity_is_validated():
    api = _text('profitos/routes/api.py')
    for endpoint in ('api_recover', 'api_save', 'api_grow', 'api_summary'):
        pos = api.index(f'def {endpoint}(')
        block = api[pos:pos + 1300]
        assert '_entity_where()' in block
    assert "SELECT id FROM entities WHERE id=?" in api
    assert 'Entité invalide' in api


def test_fresh_legacy_tables_declare_entity_id():
    runtime = _text('profitos/runtime.py')
    invoice_ddl = runtime[runtime.index('CREATE TABLE IF NOT EXISTS invoices'):]
    invoice_ddl = invoice_ddl[:invoice_ddl.index(';')]
    opp_ddl = runtime[runtime.index('CREATE TABLE IF NOT EXISTS opportunities'):]
    opp_ddl = opp_ddl[:opp_ddl.index(';')]
    assert 'entity_id INTEGER' in invoice_ddl
    assert 'entity_id INTEGER' in opp_ddl
