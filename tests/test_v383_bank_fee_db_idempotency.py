from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/'profitos/routes/bank_sync.py').read_text(encoding='utf-8')
RUNTIME=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
CORE=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')

def test_bank_fee_has_database_unique_index():
    expected="CREATE UNIQUE INDEX IF NOT EXISTS uq_accounting_entries_bank_fee_source"
    assert expected in RUNTIME
    assert expected in CORE
    assert "ON accounting_entries(entity_id, source_type, source_id)" in RUNTIME
    assert "WHERE source_type='bank_fee'" in RUNTIME

def test_bank_fee_route_catches_sqlite_and_postgres_integrity_errors():
    assert "from profitos import db as dbmod" in BANK
    assert "dbmod.psycopg2.IntegrityError" in BANK
    route=BANK.split("def banking_transaction_book_fee",1)[1].split('@app.post("/banking/transaction/<int:tx_id>/ignore")',1)[0]
    assert "except BANK_DB_INTEGRITY_ERRORS:" in route
    assert 'flash("Les frais de ce mouvement bancaire sont déjà comptabilisés.")' in route

def test_bank_fee_route_keeps_precheck_and_source_identity():
    route=BANK.split("def banking_transaction_book_fee",1)[1].split('@app.post("/banking/transaction/<int:tx_id>/ignore")',1)[0]
    assert "source_type='bank_fee' AND source_id=? LIMIT 1" in route
    assert "'bank_fee',tx_id" in route
