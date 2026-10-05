from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def test_v366_workflow_states_exist():
    assert "state='ignored'" in BANK
    assert "state='reconciled'" in BANK
    assert "state='pending'" in BANK

def test_v366_ignored_and_reconciled_do_not_get_matching_suggestions():
    # Since v379, accounted transactions intentionally remain eligible for
    # invoice matching; ignored and truly reconciled transactions do not.
    assert "reconciliation_candidates = [" in BANK
    assert "state'] in ('pending','accounted')" in BANK
    assert "for tx in reconciliation_candidates:" in BANK

def test_v366_ignored_priority_is_preserved():
    block=BANK.split("def _bank_transaction_states",1)[1].split('@app.route("/banking")',1)[0]
    assert "if ignored and ignored['state']=='ignored':" in block
    assert block.index("if ignored and ignored['state']=='ignored':") < block.index("state='reconciled'")
