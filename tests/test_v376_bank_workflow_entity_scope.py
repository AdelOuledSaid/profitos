from pathlib import Path
SRC=(Path(__file__).resolve().parents[1]/"profitos"/"routes"/"bank_sync.py").read_text(encoding="utf-8")

def _block(name, next_name):
    return SRC.split("def "+name,1)[1].split("def "+next_name,1)[0]

def _assert_real_entity_scope(b):
    assert "JOIN bank_accounts a" in b
    assert "a.provider=t.provider" in b
    assert "a.provider_account_id=t.provider_account_id" in b
    assert "a.entity_id IS ?" in b
    assert "t.account_id" not in b
    assert "bank_transactions WHERE id=? AND entity_id" not in b

def test_v376_ignore_scopes_transaction_through_bank_account():
    _assert_real_entity_scope(_block("banking_transaction_ignore","banking_transaction_restore"))

def test_v376_restore_scopes_transaction_through_bank_account():
    _assert_real_entity_scope(SRC.split("def banking_transaction_restore",1)[1])
