from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def supplier():
    return BANK.split("def _purchase_reconciliation_suggestions",1)[1].split("def _bank_allocated_total",1)[0]

def test_v363b_preserves_v361_transaction_date_fix():
    assert "tx['transaction_date']" in BANK
    assert "tx['booking_date']" not in BANK

def test_v363b_preserves_v361_supplier_score_scale():
    b=supplier()
    assert "if sim >= 30:" in b
    assert "elif sim >= 20:" in b
    assert "elif sim >= 10:" in b
    assert "if sim >= .85:" not in b

def test_v363b_supplier_partial_payment_is_suggested():
    b=supplier()
    assert "elif target < total and target > .005:" in b
    assert "score=30" in b
    assert "paiement partiel possible" in b

def test_v363b_exact_supplier_payment_keeps_priority():
    b=supplier()
    assert "if abs(total-target)<=0.01:" in b
    assert "score=60" in b
    assert "montant exact" in b

def test_v363b_overpayment_is_not_suggested():
    b=supplier()
    assert "else:" in b
    assert "continue" in b
