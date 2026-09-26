from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANK = (ROOT / 'profitos/routes/bank_sync.py').read_text(encoding='utf-8')
SWAN = (ROOT / 'profitos/routes/swan_baas.py').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'profitos/runtime.py').read_text(encoding='utf-8')


def test_powens_connection_is_entity_scoped():
    assert "bank_connections WHERE provider='powens' AND entity_id IS ?" in BANK
    assert 'session["powens_connect_entity_id"] = current_entity_id()' in BANK
    assert 'entity_id=excluded.entity_id' in BANK


def test_bank_reconciliations_require_same_entity():
    assert 'a.entity_id IS ?' in BANK
    assert "outgoing_invoices WHERE id=? AND entity_id IS ?" in BANK
    assert "purchase_invoices WHERE id=? AND entity_id IS ?" in BANK


def test_swan_accounts_and_cards_are_entity_scoped():
    assert "SELECT * FROM swan_accounts WHERE {ef}" in SWAN
    assert "WHERE id=? AND entity_id IS ?" in SWAN
    assert "user_can_access_entity(c, session.get('user_id'), entity_id)" in SWAN


def test_new_and_legacy_bank_schema_support_entity_id():
    cblock = RUNTIME[RUNTIME.index('CREATE TABLE IF NOT EXISTS bank_connections('):]
    cblock = cblock[:cblock.index('CREATE TABLE IF NOT EXISTS bank_accounts(')]
    ablock = RUNTIME[RUNTIME.index('CREATE TABLE IF NOT EXISTS bank_accounts('):]
    ablock = ablock[:ablock.index('CREATE TABLE IF NOT EXISTS bank_transactions(')]
    assert 'entity_id INTEGER' in cblock
    assert 'entity_id INTEGER' in ablock
    assert "('bank_connections','entity_id')" in RUNTIME
    assert "('bank_accounts','entity_id')" in RUNTIME
