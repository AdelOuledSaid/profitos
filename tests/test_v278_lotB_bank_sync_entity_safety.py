from pathlib import Path

T=Path("profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def test_powens_sync_never_reassigns_account_between_entities():
    assert "Compte bancaire déjà rattaché à une autre entité ProfitOS." in T
    assert 'SELECT entity_id FROM bank_accounts WHERE provider=? AND provider_account_id=?' in T
    assert 'existing_account["entity_id"] != entity_id' in T

def test_powens_sync_never_moves_existing_transaction_between_entities():
    assert "Transaction bancaire déjà rattachée à une autre entité ProfitOS." in T
    assert 'bt.provider_transaction_id=?' in T
    assert 'existing_tx["entity_id"] != entity_id' in T

def test_sync_preserves_existing_manual_category():
    assert "category=COALESCE(NULLIF(bank_transactions.category,''),excluded.category)" in T
