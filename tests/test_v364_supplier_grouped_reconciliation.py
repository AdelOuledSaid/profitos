from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def supplier():
    return BANK.split("def _purchase_reconciliation_suggestions",1)[1].split("def _bank_allocated_total",1)[0]

def confirm():
    return BANK.split("def confirm_purchase_reconciliation",1)[1].split("@app.route('/banking/regles'",1)[0]

def test_v364_supplier_suggestions_use_remaining_bank_amount():
    b=supplier()
    assert "FROM bank_purchase_allocations" in b
    assert "bank_transaction_id=?" in b
    assert "abs(amount)-float(allocated)" in b
    assert "if target <= .005:" in b

def test_v364_grouped_payment_can_offer_smaller_invoice():
    b=supplier()
    assert "elif total < target and total > .005:" in b
    assert "paiement groupé possible" in b
    assert "suggested_amount=total" in b

def test_v364_partial_payment_behavior_is_preserved():
    b=supplier()
    assert "elif target < total and target > .005:" in b
    assert "paiement partiel possible" in b
    assert "suggested_amount=target" in b

def test_v364_exact_payment_keeps_priority():
    b=supplier()
    assert "if abs(total-target)<=0.01:" in b
    assert "score=60" in b
    assert "montant exact" in b

def test_v364_suggestion_exposes_safe_amount_not_full_bank_debit():
    b=supplier()
    assert "'amount':suggested_amount" in b

def test_v364_confirmation_supports_reusing_transaction_across_invoices():
    b=confirm()
    assert "SUM(matched_amount)" in b
    assert "bank_purchase_allocations" in b
    assert "available=max(0.0,round(abs(float(tx['amount'] or 0))-float(allocated),2))" in b
    assert "amount>available+.001" in b
    assert "amount>balance+.001" in b

def test_v364_preserves_v361_fixes():
    assert "tx['transaction_date']" in BANK
    assert "tx['booking_date']" not in BANK
    b=supplier()
    assert "if sim >= 30:" in b
    assert "elif sim >= 20:" in b
    assert "elif sim >= 10:" in b
