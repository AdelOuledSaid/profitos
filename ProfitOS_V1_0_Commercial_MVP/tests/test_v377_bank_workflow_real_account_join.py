from pathlib import Path
SRC=(Path(__file__).resolve().parents[1]/"profitos"/"routes"/"bank_sync.py").read_text(encoding="utf-8")

def test_v377_workflow_uses_real_bank_transaction_account_keys():
    tail=SRC.split("def banking_transaction_ignore",1)[1]
    assert tail.count("a.provider=t.provider") >= 2
    assert tail.count("a.provider_account_id=t.provider_account_id") >= 2
    assert "t.account_id" not in tail
    assert tail.count("a.entity_id IS ?") >= 2
