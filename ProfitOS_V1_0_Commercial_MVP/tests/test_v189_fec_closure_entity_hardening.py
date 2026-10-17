from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTING = (ROOT/'profitos/accounting.py').read_text(encoding='utf-8')
ROUTES = (ROOT/'profitos/routes/accounting.py').read_text(encoding='utf-8')
RUNTIME = (ROOT/'profitos/runtime.py').read_text(encoding='utf-8')


def test_closure_schema_is_entity_scoped_and_legacy_main_is_migrated():
    assert 'CREATE TABLE IF NOT EXISTS accounting_entity_closure(' in RUNTIME
    assert 'entity_key INTEGER PRIMARY KEY' in RUNTIME
    assert 'INSERT OR IGNORE INTO accounting_entity_closure' in RUNTIME
    assert 'VALUES(0,NULL,?,?,?)' in RUNTIME


def test_create_entry_checks_closure_for_its_entity_only():
    assert "SELECT closed_until FROM accounting_entity_closure WHERE entity_key=?" in ACCOUNTING
    assert "entity_key = int(entity_id) if entity_id is not None else 0" in ACCOUNTING


def test_closure_route_checks_and_locks_only_current_entity():
    assert 'WHERE e.entity_id IS ?' in ROUTES
    assert 'UPDATE accounting_entries SET is_locked=1 WHERE entity_id IS ? AND entry_date<=?' in ROUTES
    assert 'SELECT COUNT(*) n FROM accounting_entries WHERE entity_id IS ? AND is_locked=1' in ROUTES


def test_fec_account_label_join_is_entity_aware():
    assert 'AND (a.entity_id IS NULL OR a.entity_id=?)' in ACCOUNTING
    assert "(e['entity_id'], e['id'])" in ACCOUNTING
