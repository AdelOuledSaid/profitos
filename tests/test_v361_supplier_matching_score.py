from pathlib import Path

BANK = Path("profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def test_supplier_similarity_uses_score_scale():
    assert "if sim >= 30:" in BANK
    assert "elif sim >= 20:" in BANK
    assert "elif sim >= 10:" in BANK

def test_supplier_similarity_no_fractional_thresholds():
    assert "if sim >= .85:" not in BANK
    assert "elif sim >= .65:" not in BANK
    assert "elif sim >= .45:" not in BANK

def test_supplier_bank_payment_keeps_transaction_date_fix():
    assert "tx['transaction_date']" in BANK
    assert "tx['booking_date']" not in BANK
