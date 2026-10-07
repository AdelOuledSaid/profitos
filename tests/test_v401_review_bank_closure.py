from pathlib import Path
import ast
R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")
ast.parse(R)

def test_v401_bank_closure_is_read_only_detection():
    assert "BANK_RECONCILIATION_INCOMPLETE" in R
    assert "JOIN bank_accounts a" in R
    assert "bank_transaction_workflow" in R
    assert "COALESCE(w.state,'pending') NOT IN ('ignored','reconciled')" in R

def test_v401_review_status_does_not_mutate_bank_data():
    block=R.split("# Rapprochement bancaire à la clôture (point 0)",1)[1].split("# Provisions (point 13)",1)[0]
    assert "UPDATE " not in block
    assert "INSERT " not in block
    assert "DELETE " not in block
    assert "'state':'review'" in block
    assert "'state':'ok'" in block
    assert "validation finale manuelle" in block
