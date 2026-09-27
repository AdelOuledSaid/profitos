from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
RUNTIME=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")

def sync_block():
    return BANK[BANK.index("def _sync_powens"):BANK.index("def _mark_powens_sync_error")]

def test_v207_sync_binds_provider_data_to_connection_entity():
    b=sync_block()
    assert "entity_id = row['entity_id']" in b
    assert "synced_account_ids = set()" in b
    assert "synced_account_ids.add(aid)" in b
    assert "account_id not in synced_account_ids" in b
    assert "apply_categorization_rule(c, label, entity_id)" in b

def test_v207_accounts_and_transactions_are_provider_idempotent():
    b=sync_block()
    assert "ON CONFLICT(provider,provider_account_id)" in b
    assert "ON CONFLICT(provider,provider_transaction_id)" in b
    assert "UNIQUE(provider,provider_account_id)" in RUNTIME
    assert "UNIQUE(provider,provider_transaction_id)" in RUNTIME

def test_v207_connection_success_update_is_entity_guarded():
    b=sync_block()
    assert "WHERE id=? AND entity_id IS ?" in b
    assert '(now, now, row["id"], entity_id)' in b

def test_v207_callback_uses_state_and_original_entity_context():
    assert 'secrets.compare_digest(expected, received)' in BANK
    assert 'session.pop("powens_connect_entity_id", None)' in BANK
    assert "user_can_access_entity(c, session.get(\"user_id\"), connect_entity_id)" in BANK

def test_v207_sync_failures_persist_reconnectable_state_without_secret():
    assert "def _mark_powens_sync_error" in BANK
    assert "status='SYNC_ERROR'" in BANK
    assert "provider secrets or raw errors" in BANK
    assert "Reconnectez la banque si le consentement a expiré." in BANK

def test_v207_tokens_are_ephemeral_not_persisted_in_bank_connection_schema():
    schema=RUNTIME[RUNTIME.index("CREATE TABLE IF NOT EXISTS bank_connections"):RUNTIME.index("CREATE TABLE IF NOT EXISTS bank_accounts")]
    assert "access_token" not in schema
    assert "refresh_token" not in schema
    assert "provider_user_id" in schema
