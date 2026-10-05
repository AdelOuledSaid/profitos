from pathlib import Path
SRC=(Path(__file__).resolve().parents[1]/"profitos"/"routes"/"bank_sync.py").read_text(encoding="utf-8")

def _block(name, next_name):
    return SRC.split("def "+name,1)[1].split("def "+next_name,1)[0]

def test_v376_ignore_scopes_transaction_through_bank_account():
    b=_block("banking_transaction_ignore","banking_transaction_restore")
    assert "JOIN bank_accounts a ON a.id=t.account_id" in b
    assert "a.entity_id IS ?" in b
    assert "bank_transactions WHERE id=? AND entity_id" not in b

def test_v376_restore_scopes_transaction_through_bank_account():
    b=SRC.split("def banking_transaction_restore",1)[1]
    assert "JOIN bank_accounts a ON a.id=t.account_id" in b
    assert "a.entity_id IS ?" in b
    assert "bank_transactions WHERE id=? AND entity_id" not in b
