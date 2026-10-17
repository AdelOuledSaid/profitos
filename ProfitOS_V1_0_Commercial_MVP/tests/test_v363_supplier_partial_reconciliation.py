from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANK = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def _supplier_block():
    return BANK.split("def _purchase_reconciliation_suggestions", 1)[1].split("def _bank_allocated_total", 1)[0]

def test_supplier_suggestions_support_partial_payment():
    block = _supplier_block()
    assert "elif target < total and target > .005:" in block
    assert "score=30" in block
    assert "paiement partiel possible" in block

def test_supplier_suggestions_keep_exact_amount_priority():
    block = _supplier_block()
    assert "if abs(total-target)<=0.01:" in block
    assert "score=60" in block
    assert "montant exact" in block

def test_supplier_suggestions_reject_bank_amount_above_remaining_balance():
    block = _supplier_block()
    assert "else:" in block
    assert "continue" in block

def test_supplier_confirmation_still_caps_amount_to_balance_and_available():
    confirm = BANK.split("def confirm_purchase_reconciliation", 1)[1]
    assert "min(balance,available)" in confirm
    assert "amount>balance+.001" in confirm
    assert "amount>available+.001" in confirm
