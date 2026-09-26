from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTING = (ROOT / 'profitos' / 'accounting.py').read_text(encoding='utf-8')
ROUTES = (ROOT / 'profitos' / 'routes' / 'accounting.py').read_text(encoding='utf-8')


def test_accounting_source_idempotency_is_entity_scoped():
    assert 'def _entry_already_exists(conn, source_type, source_id, entity_id=None):' in ACCOUNTING
    assert 'source_type=? AND source_id=? AND entity_id IS ?' in ACCOUNTING
    assert "_entry_already_exists(conn, 'outgoing_invoice', invoice['id'], entity_id)" in ACCOUNTING


def test_piece_number_sequence_is_entity_scoped():
    assert 'def _next_piece_number(conn, journal_code, entry_date, entity_id=None):' in ACCOUNTING
    assert 'journal_code=? AND piece_number LIKE ? AND entity_id IS ?' in ACCOUNTING
    assert '_next_piece_number(conn, journal_code, entry_date_str, entity_id)' in ACCOUNTING


def test_journal_and_lettrage_views_are_entity_scoped():
    assert 'accounting_entries WHERE entity_id IS ? GROUP BY journal_code' in ROUTES
    assert 'journal_code=? AND entity_id IS ? ORDER BY entry_date DESC' in ROUTES
    assert "l.account_code=? AND e.entity_id IS ?" in ROUTES


def test_fec_screen_count_is_entity_scoped():
    assert "COUNT(*) n FROM accounting_entries WHERE entity_id IS ?" in ROUTES
